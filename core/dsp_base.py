"""The contract of the user DSP chain: what a module is, how its settings are described, and where modules are found.

A module turns blocks of stereo audio into blocks of stereo audio. It lives on the audio thread; the window changes
its settings from the GUI thread. That is the whole threading rule: set() may be called at any moment from the GUI
thread and never blocks, process() runs on the audio thread and picks the new settings up at the start of its next block.

    @register
    class Amplify(DspModule):
        id, name, group = "amplify", "Amplify", "Level"
        summary = "Raises or lowers the volume."
        params = (Param("gain", "Gain", "float", 0.0, -40.0, 20.0, 0.1, unit="dB"),)

        def _apply(self, p):                      # p is a plain dict of the settings now in force
            self._g = 10.0 ** (p["gain"] / 20.0)

        def process(self, x, ctx):
            return x * np.float32(self._g)
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np

KINDS = ("float", "int", "bool", "choice", "file")
GROUPS = ("Level", "Dynamics", "Tone", "Space", "Time", "Channels", "Utility")


@dataclass(frozen=True)
class Param:
    """One setting. The panel builds its control from this, so everything it needs is here.

    kind     float or int (a slider between lo and hi), bool (a checkbox), choice (a list, `choices` holds the labels and
             the value is the label), file (a path, "" for none; `hint` names the kinds of file).
    scale    how a slider moves across lo..hi: lin, log (frequencies, times) or db (the value is already in dB, linear).
    default  the value a fresh module starts with and Reset returns to.
    """
    key: str
    label: str
    kind: str
    default: object
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    unit: str = ""
    choices: tuple = ()
    scale: str = "lin"
    hint: str = ""

    def clamp(self, value):
        """The value forced into what this setting allows. Never raises: a bad value becomes the default."""
        try:
            if self.kind == "bool":
                return bool(value)
            if self.kind == "choice":
                return value if value in self.choices else self.default
            if self.kind == "file":
                return str(value or "")
            v = float(value)
            if v != v or v in (float("inf"), float("-inf")):
                return self.default
            if self.lo is not None:
                v = max(self.lo, v)
            if self.hi is not None:
                v = min(self.hi, v)
            if self.kind == "int":
                return int(round(v))
            return float(v)
        except (TypeError, ValueError):
            return self.default


@dataclass
class Context:
    """What a module may need to know about where the block is in the music.

    fs      sample rate of the block (the output device's rate; do not assume 44100)
    pos     frame number of the block's first frame inside the track, -1 when not known
    total   frames in the track, 0 when not known (still decoding, or a stream)
    volume  the player's volume 0..1 (loudness compensation reads it)
    xfade   True while the engine is crossfading two tracks (a fade module leaves the track edges alone then)
    """
    fs: int = 44100
    pos: int = -1
    total: int = 0
    volume: float = 1.0
    xfade: bool = False


class DspModule:
    """Subclass, give it an id, a name, a group and params, implement process(), and decorate it with @register."""

    id: str = ""            # short, lower case, never changes once released (it is saved in presets)
    name: str = ""          # what the panel shows
    group: str = "Utility"  # one of GROUPS
    summary: str = ""       # one plain sentence for the Add menu tooltip
    params: tuple = ()      # tuple of Param

    def __init__(self, fs: int = 44100):
        self.fs = int(fs)
        self._by_key = {p.key: p for p in self.params}
        self.p = {p.key: p.default for p in self.params}        # settings in force on the audio thread
        self._pending: dict = {}
        self._dirty = True                                       # the first block builds the coefficients
        self.enabled = True

    # settings
    def set(self, key: str, value) -> None:
        """Change one setting. GUI thread, any time, never blocks. Unknown keys are ignored."""
        spec = self._by_key.get(key)
        if spec is None:
            return
        self._pending[key] = spec.clamp(value)
        self._dirty = True

    def get(self, key: str):
        return self._pending.get(key, self.p.get(key))

    def settings(self) -> dict:
        return {k: self.get(k) for k in self._by_key}

    def reset_settings(self) -> None:
        for p in self.params:
            self.set(p.key, p.default)

    def to_dict(self) -> dict:
        return {"id": self.id, "enabled": bool(self.enabled), "settings": self.settings()}

    def load_settings(self, d: dict) -> None:
        for k, v in (d or {}).items():
            self.set(k, v)

    # audio thread
    def prepare(self) -> None:
        """Called at the start of every block by the chain: applies pending settings. Cheap when nothing changed."""
        if self._dirty:
            self._dirty = False
            pend, self._pending = self._pending, {}
            self.p.update(pend)
            self._apply(self.p)

    def _apply(self, p: dict) -> None:
        """Rebuild coefficients from the settings in force. Runs on the audio thread, once per change, so keep it short
        (no file reads here: do those in set() or a helper thread and hand the result over)."""

    def process(self, x: np.ndarray, ctx: Context) -> np.ndarray:
        """x is (n, 2) float32. Return an (n, 2) float32 array of the same length. Do not keep a reference to x after
        returning and do not change it in place unless you return that same array. Must never raise, never return
        NaN or infinity, and must be exact for any n from 1 to 65536 (the chain feeds 10 to 50 ms blocks, a seek or a
        short track can give odd sizes)."""
        return x

    def reset(self) -> None:
        """Forget the signal (filter memory, delay lines, envelopes) but keep the settings. Called on a seek, a track
        change and when the chain is switched on."""

    def latency(self) -> int:
        """Frames by which the output trails the input (look-ahead). 0 for most. The chain adds these up, hands the
        sum to anyone who asks (DspChain.latency) and keeps a fade in from fading a hole in while the delay fills."""
        return 0

    def warmup(self) -> int:
        """Frames after a cold start (reset) before the output has nothing missing: the delay the module has to fill before
        its first real output, which is its latency for a look-ahead and a channel delay for a mixer. A fade in waits this
        long so it does not fade a hole in. Defaults to latency()."""
        return self.latency()

    def tail(self) -> int:
        """Frames of sound the module can still send after its input has gone silent, for modules whose output can
        pause in between (an echo's repeats). Only DspChain.flush asks: it stops at a gap of silence unless the tail
        says more is coming. 0 for modules whose output dies away without gaps."""
        return 0

    def clone(self) -> "DspModule":
        """An independent module with the same settings and the same internal state, so it goes on exactly where this
        one is. The chain uses it to run the old and the new arrangement side by side while one replaces the other.
        The default copies every attribute. A module that owns a thread, a lock or an open file overrides it and
        shares what cannot be copied. May run on the audio thread, between two blocks: no file reads, nothing slow."""
        return copy.deepcopy(self)


REGISTRY: dict[str, type[DspModule]] = {}


def register(cls):
    """Class decorator: makes the module available to the chain and the Add menu."""
    if not cls.id or cls.id in REGISTRY:
        raise ValueError("a DSP module needs a unique id: %r" % (cls.id,))
    REGISTRY[cls.id] = cls
    return cls


def create(module_id: str, fs: int = 44100) -> DspModule | None:
    cls = REGISTRY.get(module_id)
    return cls(fs) if cls is not None else None


def finite(x: np.ndarray) -> np.ndarray:
    """x with every NaN and infinity replaced by 0. Modules use it on the way out when they have feedback."""
    if not np.isfinite(x).all():
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x
