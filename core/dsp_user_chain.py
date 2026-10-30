"""The user's DSP chain: an ordered list of dsp_base modules that sits between the user EQ and the Enhancer.

The window (GUI thread) edits it through the index based methods below. The audio thread calls process() once per
block and never waits for the GUI: the module list is a tuple that is swapped whole, a module's settings go through
DspModule.set(), and everything the audio thread must do after an edit (resetting state, fading a module in or out,
crossing over to a new arrangement) it does itself at the start of the next block. Writers share one lock; process()
never takes it, except for a try once when the output rate changes.

Settings and presets are dicts:
    {"version": 1, "master": True, "limiter": True,
     "modules": [{"id": "amplify", "enabled": True, "settings": {"gain": 3.0}}, ...]}
"master" and "limiter" are left alone when a dict has no such key (presets do not carry them).

Editing while playing does not cut the sound. Switching a module on or off fades it over RAMP_S. Adding, removing or
moving modules and loading a chain or a preset change the arrangement: the audio thread keeps a copy of the old
arrangement running (each module continues from the state it had, DspModule.clone) beside the new one and crosses over
from the old output to the new output with weights that add up to 1, so the sound never goes above the louder of the
two and a moved reverb keeps its tail. The crossover takes XFADE_S, or TAIL_S when a module that was sounding is gone
from the new arrangement, so what it was ringing with dies away instead of being cut. Only what comes after the first
changed module runs twice, and only during the crossover.

Latency: the safety limiter looks 1.5 ms ahead and a module can look further (DspModule.latency: the Leveler 10 ms,
the Compressor up to 10 ms). While the chain is active the signal comes out latency() frames later than it went in; with
the chain off or empty nothing is delayed. A module that has just come in has nothing to say until its delay has filled
(DspModule.warmup: its latency, or the longest channel delay for the channel mixer), so every fade in (a module, the
chain, a new arrangement) waits that many frames first instead of fading a hole in. Such a module also starts with
its input faded in over RAMP_S, and a module that carries on in a new arrangement has its input changed over from what it
used to get while the new output is held back. A delay with feedback stores the sound it is given, so a step at its input
would come round again with every trip and tick through the music long after the edit.
Switching the chain on or off, or the limiter, crosses over between the delayed and the undelayed signal over RAMP_S.
The engine does not delay anything to compensate; whoever needs the time reads latency(). flush() gives back what the
delay lines still hold when the audio ends, together with what the modules are ringing with.

A new stream (reset(): a seek, a stop, a track change) starts the chain at once, without the fade in, and with silence
in front of the limiter's delay line: nothing sounded before it, so there is nothing to cross over from.

A saved module id this version does not know is kept in the file (not shown, not processed) and written back on save.
"""
from __future__ import annotations

import importlib
import itertools
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal
from scipy.ndimage import maximum_filter1d

import plat_fs
import plat_paths
from dsp_base import REGISTRY, Context, create

VERSION = 1
MAX_MODULES = 32
MAX_FILE = 1 << 20              # a settings or preset file larger than this is not ours
RAMP_S = 0.010                  # master and per module on/off fade
XFADE_S = 0.030                 # the old arrangement crosses over to the new one in this time
TAIL_S = 0.100                  # in this time when a sounding module is gone from the new one, so its ringing fades out
LOOK_S = 0.0015                 # the safety limiter's look-ahead, part of the chain's delay
QUIET_DB = -90.0                # flush: below this the output counts as silent,
QUIET_S = 0.050                 # when it stays so for this long,
FLUSH_BLOCK = 2048              # and it is produced in blocks of this many frames
FLUSH_FADE_S = 0.005            # the end of a tail that was cut off fades out over this
GUARD_LOAD = 0.85               # share of a block's duration the chain may take
GUARD_SECONDS = 2.0             # for this long before the guard bypasses the chain
_PLUGINS = ("dsp_fx_dynamics", "dsp_fx_space", "dsp_fx_filters", "dsp_fx_loudness", "dsp_fx_modulation")
_loaded = False


def _log():
    from applog import log
    return log


def load_modules() -> None:
    """Imports the files that register modules, once. A missing or broken one is logged and skipped."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in _PLUGINS:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                _log().warning("DSP modules in %s need %s, which is missing", name, e.name)
        except Exception:
            _log().exception("DSP modules in %s failed to load", name)


def config_dir() -> Path:
    return plat_paths.config_dir()


def _plain(o):
    return o.item() if hasattr(o, "item") else str(o)


def _write_json(path: Path, data) -> None:
    """Atomic: a reader sees the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=_plain)
            f.flush()
            os.fsync(f.fileno())
        plat_fs.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    if path.stat().st_size > MAX_FILE:
        raise ValueError("file is too large")
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_name(name) -> str:
    """A preset name that is safe as a file name. Empty when nothing usable is left."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip().strip(".").strip()
    return s[:60].strip()


def _as_bool(v, default=True) -> bool:
    if isinstance(v, bool):
        return v
    return bool(v) if isinstance(v, (int, float)) else default


def _ramp(a: float, b: float, n: int, ramp_n: int):
    """Gain moving from a toward b by 1/ramp_n per sample: ((n, 1) float32 array, where it ended)."""
    k = np.arange(1, n + 1, dtype=np.float32) * np.float32(1.0 / ramp_n)
    w = (a + np.copysign(np.minimum(k, abs(b - a)), b - a)).astype(np.float32)
    end = float(w[-1])
    return w[:, None], (b if abs(end - b) < 1e-6 else end)


def _ramp_after(a: float, b: float, n: int, ramp_n: int, lead: int):
    """_ramp that holds at a for the first `lead` frames. A module with look-ahead has nothing to say until its delay
    has filled, so a fade in starts then. Returns (gain, where it ended, frames of lead still to wait)."""
    k = min(lead, n)
    if k == n:
        return np.full((n, 1), a, np.float32), a, lead - n
    w, end = _ramp(a, b, n - k, ramp_n)
    if k:
        w = np.concatenate((np.full((k, 1), a, np.float32), w))
    return w, end, 0


class _Limiter:
    """Look-ahead peak limiter for the end of the chain. The signal is delayed by LOOK_S; the gain starts falling that
    long before a peak reaches the output, as a smooth ramp, and comes back with an exponential release of 100 ms.
    The gain applied to a sample never exceeds CEIL / |sample|, so nothing leaves above CEIL. Below the ceiling the
    output is the input, delayed, bit for bit. Costs a copy, a max and a min when nothing is over the ceiling."""
    CEIL = float(np.float32(1.0))       # 0 dBFS, exactly representable, so a product that rounds to it can never pass it

    def __init__(self):
        self.reset()

    def reset(self, silent: bool = False):
        """silent: the next block starts a stream, so the delay line holds silence. Otherwise it holds the first frame,
        which keeps a limiter that comes in while the music plays from stepping."""
        self.hist = None        # the last L input frames: the delay line, None until the first block
        self.tail = None        # the last L - 1 release smoothed reductions of the blocks before
        self.dr = 0.0           # the reduction still being released, 0..1
        self.silent = silent
        self.red = 0.0          # the deepest reduction in the last block, 0..1

    @staticmethod
    def delay(fs: int) -> int:
        return max(2, int(round(LOOK_S * fs)))

    def process(self, y: np.ndarray, fs: int) -> np.ndarray:
        n = len(y)
        if n == 0:
            return y
        if n > 65536:                   # rel ** n would underflow
            return np.concatenate([self.process(y[i:i + 65536], fs) for i in range(0, n, 65536)])
        w = self.delay(fs)
        if self.hist is None or len(self.hist) != w:
            if self.silent and self.hist is None:
                self.hist = np.zeros((w, 2), np.float32)
                self.dr = 0.0
            else:
                self.hist = np.repeat(y[:1], w, axis=0)     # holding the first frame: no step where the real signal starts
                a0 = float(np.abs(y[:1]).max()) if n else 0.0
                self.dr = 1.0 - self.CEIL / max(a0, self.CEIL)
            self.silent = False
            self.tail = np.full(w - 1, self.dr)
        ext = np.concatenate((self.hist, y))                # (w + n, 2): n delayed frames and w frames of look-ahead
        self.hist = ext[n:].copy()
        peak = max(float(ext.max()), -float(ext.min()))
        if not math.isfinite(peak):
            ext = np.clip(np.nan_to_num(ext, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
            peak = float(np.abs(ext).max())
        if peak <= self.CEIL and self.dr < 1e-4:
            self.dr, self.tail, self.red = 0.0, None, 0.0
            return ext[:n]
        rel = math.exp(-1.0 / (0.1 * fs))
        a = np.abs(ext).max(axis=1).astype(np.float64)
        d = 1.0 - self.CEIL / np.maximum(a, self.CEIL)
        dm = maximum_filter1d(d, size=w, origin=-(w // 2), mode="nearest")[:n]    # the largest of the next w values
        rp = rel ** np.arange(n)
        dr = rp * np.maximum.accumulate(dm / rp)                                  # dr[k] = max(dm[k], rel * dr[k - 1])
        if self.dr > 0.0:
            dr = np.maximum(dr, self.dr * rel * rp)
        tail = self.tail if self.tail is not None and len(self.tail) == w - 1 else np.zeros(w - 1)
        edr = np.concatenate((tail, dr))
        cs = np.concatenate(([0.0], np.cumsum(edr)))
        g = 1.0 - (cs[w:] - cs[:-w]) / w                    # w sample boxcar over the reduction: the attack ramp
        self.tail = edr[-(w - 1):].copy()
        self.dr = float(dr[-1])
        self.red = float(1.0 - g.min())
        out = ext[:n].astype(np.float64) * g[:, None]
        np.clip(out, -self.CEIL, self.CEIL, out=out)
        return out.astype(np.float32)


class _Slot:
    """One module in the audio thread's list. mix is the module's own fade (0 off, 1 on), owned by the audio thread.
    cur and prev are (milliseconds in total, calls, longest call) for this and the previous second of audio.
    warm is True from the moment the module came in until it has faded out again; lead is how many frames its fade in
    still waits for the module's own look-ahead delay to fill. prime is how many frames of a fade in of the module's own
    input are left: a module with a delay that starts cold must not see the sound begin with a step, or the step comes
    round again with every trip through its delay line. ghost marks a slot of the arrangement being crossed over from:
    the window does not know it, so its failures are not reported."""
    __slots__ = ("uid", "mod", "mix", "failed", "cur", "prev", "ep", "ghost", "lead", "warm", "prime")

    def __init__(self, uid, mod):
        self.uid, self.mod, self.mix, self.failed = uid, mod, 0.0, None
        self.cur = self.prev = (0.0, 0, 0.0)
        self.ep = 0
        self.ghost, self.lead, self.warm, self.prime = False, 0, False, 0


class _Ghost:
    """The old arrangement while the sound crosses over from it to the new one. Everything before slot k is shared (it
    runs once, in the new arrangement); slots holds the old modules from k on. u is the weight of the new arrangement
    (0 to 1), ramp the number of frames it takes to get there and lead how many frames it still waits at 0 for the
    delays in the new arrangement to fill. morph holds the uids of modules with a delay that carry on in the new
    arrangement: while the new output is held back, their input changes over from what they used to get (what the old
    copy gets) to what they get now, so the sound they store has no step in it. mp is how far that has come (0 to 1)."""
    __slots__ = ("slots", "k", "u", "lead", "ramp", "morph", "mp")

    def __init__(self, slots, k, lead, ramp, morph):
        self.slots, self.k, self.u, self.lead, self.ramp = slots, k, 0.0, lead, ramp
        self.morph, self.mp = morph, 0.0 if morph else 1.0


def _describe(s):
    return (s.mod.id, bool(s.mod.enabled), s.mod.settings())


def _retuned(s) -> bool:
    """True when the window has given the module settings or a switch that the audio thread has not taken over yet."""
    m = s.mod
    return m.settings() != m.p or bool(m.enabled) != s.warm


def _on(s) -> bool:
    """True when the slot's module is switched on and working."""
    return bool(s.mod.enabled) and s.failed is None


def _sounds(s) -> bool:
    """True when the slot changes the sound now or is still fading out."""
    return _on(s) or s.mix > 0.0


def _alive(slots) -> bool:
    """True when something in the list is sounding or about to."""
    for s in slots:
        if _sounds(s):
            return True
    return False


def _latency(mod, fresh: bool = False) -> int:
    """The look-ahead delay a module reports, in frames. fresh (audio thread only) applies the settings it is waiting
    for first, since a setting can change the delay."""
    try:
        if fresh:
            mod.prepare()
        return max(0, min(int(mod.latency()), 1 << 16))
    except Exception:
        return 0


def _heard_latency(slots, fresh: bool = False) -> int:
    """Frames by which the modules that are switched on delay the signal: they work in series, so the delays add up."""
    return sum(_latency(s.mod, fresh) for s in slots if _on(s))


def _warmup(mod) -> int:
    """Frames a module that starts cold has no real output for (audio thread only: it takes the settings it is waiting
    for first). A fade in holds off that long."""
    try:
        mod.prepare()
        return max(0, min(int(mod.warmup()), 1 << 16))
    except Exception:
        return 0


class DspChain(QObject):
    changed = Signal()                  # anything that is saved changed (any edit, from any thread)
    structure_changed = Signal()        # the list itself was replaced or reordered: rebuild the view of it
    overloaded = Signal(float)          # the guard bypassed the chain; load in percent when it did
    recovered = Signal()                # reenable() cleared the guard
    module_failed = Signal(int, str, str)   # uid, module name, what went wrong (once per failure)

    def __init__(self, fs: int = 44100, parent=None, path=None, preset_dir=None):
        super().__init__(parent)
        load_modules()
        self._fs = int(fs)
        self._slots: tuple = ()             # what the window sees: the modules in order
        self._cur: tuple = ()               # what the audio thread runs: _slots as of the start of its last block
        self._unknown: tuple = ()           # (position, entry) of saved modules this version does not know
        self._wlock = threading.Lock()
        self._uids = itertools.count(1)
        self._master = True
        self._limiter_on = True
        self._path = Path(path) if path else None
        self._preset_dir = Path(preset_dir) if preset_dir else None
        self.last_error = ""
        self._unsaved = False
        # audio thread state
        self._lim = _Limiter()
        self._lm = 0.0                      # how much of the limiter is in the output (it fades over RAMP_S)
        self._ghost = None                  # the old arrangement while the sound crosses over from it
        self._on = False                    # the chain has been running since it came in
        self._mix = 0.0
        self._lead = 0                      # frames the chain's own fade in still waits for the delays to fill
        self._tripped = False
        self._over = 0.0
        self._reset_gen = 0
        self._seen_reset = 0
        self._clock = time.perf_counter
        self._broke = False
        self._blocks = 0
        self._block_ms = self._last_t = 0.0
        self._cur_acc = self._prev_acc = (0.0, 0.0, 0, 0.0)     # (seconds spent, seconds of audio, blocks, longest block in ms)
        self._epoch = 0
        self._last = (-1, 0, 1.0)           # position after the last block, track length, player volume
        self._flushing = False

    # the list
    def count(self) -> int:
        return len(self._slots)

    def module(self, i: int):
        """The live module instance at i (id, name, summary, params, get). Read only, and do not keep it: a change
        of the output rate replaces the instances. None when i is out of range."""
        s = self._slot(i)
        return s.mod if s else None

    def entries(self) -> list:
        """One dict per module in order: uid, id, name, group, enabled, failed ("" when fine)."""
        return [{"uid": s.uid, "id": s.mod.id, "name": s.mod.name, "group": s.mod.group,
                 "enabled": bool(s.mod.enabled), "failed": s.failed or ""} for s in self._slots]

    def uid(self, i: int) -> int:
        s = self._slot(i)
        return s.uid if s else 0

    def index_of(self, uid: int) -> int:
        for i, s in enumerate(self._slots):
            if s.uid == uid:
                return i
        return -1

    def add(self, module_id: str, index=None, settings=None, enabled=None) -> int:
        """Adds a module (at the end unless index is given; enabled None keeps the module's own default, which is on
        for most and off for dither). Returns its index, -1 for an unknown id or a full chain."""
        s = self._make(module_id, settings, enabled)
        if s is None:
            return -1
        with self._wlock:
            live = list(self._slots)
            if len(live) >= MAX_MODULES:
                return -1
            i = len(live) if index is None else max(0, min(int(index), len(live)))
            live.insert(i, s)
            self._slots = tuple(live)
        self._edited(True)
        return i

    def remove(self, i: int) -> bool:
        with self._wlock:
            live = list(self._slots)
            if not (isinstance(i, int) and 0 <= i < len(live)):
                return False
            live.pop(i)
            self._slots = tuple(live)
        self._edited(True)
        return True

    def move(self, i: int, j: int) -> bool:
        """Moves the module at i so that it ends up at index j. The module itself carries on from where it was, and a
        copy of it keeps playing from the old place while the sound crosses over."""
        with self._wlock:
            live = list(self._slots)
            if not (isinstance(i, int) and 0 <= i < len(live)):
                return False
            s = live.pop(i)
            live.insert(max(0, min(int(j), len(live))), s)
            self._slots = tuple(live)
        self._edited(True)
        return True

    def duplicate(self, i: int) -> int:
        """A copy with the same settings right after i. Returns its index or -1."""
        s = self._slot(i)
        if s is None:
            return -1
        return self.add(s.mod.id, i + 1, s.mod.settings(), s.mod.enabled)

    def clear(self) -> None:
        with self._wlock:
            self._replace([], ())
        self._edited(True)

    def enabled(self, i: int) -> bool:
        s = self._slot(i)
        return bool(s and s.mod.enabled)

    def set_enabled(self, i: int, on: bool) -> None:
        s = self._slot(i)
        if s is not None:
            s.mod.enabled = bool(on)
            if on:
                s.failed = None             # a bypassed module gets another try when the user switches it on
            self._edited(False)

    def failed(self, i: int) -> str:
        """Why the module at i is being bypassed, "" when it is not."""
        s = self._slot(i)
        return (s.failed or "") if s else ""

    def set(self, i: int, key: str, value) -> None:
        s = self._slot(i)
        if s is not None:
            s.mod.set(key, value)
            self._edited(False)

    def get(self, i: int, key: str):
        s = self._slot(i)
        return s.mod.get(key) if s else None

    def settings(self, i: int) -> dict:
        s = self._slot(i)
        return s.mod.settings() if s else {}

    def reset_module(self, i: int) -> None:
        """Every setting of the module at i back to its default."""
        s = self._slot(i)
        if s is not None:
            s.mod.reset_settings()
            s.failed = None
            self._edited(False)

    def _slot(self, i):
        slots = self._slots
        return slots[i] if isinstance(i, int) and 0 <= i < len(slots) else None

    def _make(self, module_id, settings=None, enabled=None):
        try:
            m = create(module_id, self._fs) if isinstance(module_id, str) else None
            if m is None:
                return None
            if isinstance(settings, dict):
                m.load_settings(settings)
            if enabled is not None:
                m.enabled = _as_bool(enabled)
            return _Slot(next(self._uids), m)
        except Exception:
            _log().exception("DSP module %r could not be created", module_id)
            return None

    def _replace(self, new: list, unknown) -> None:
        """Under the lock. The modules in new take over from the current ones, in order. A current module with the same
        id, switch and settings as the next one wanted stays where it is, with its memory and its uid. Failing that a
        module of the same kind takes the new settings and goes on from its state: a preset that only turns a reverb
        up does not start a new reverb. The others are new modules. The audio thread crosses over to the result at the
        start of its next block."""
        old, pos, use = self._slots, 0, []
        for n in new:
            d, hit, kin = _describe(n), None, None
            for j in range(pos, len(old)):
                o = old[j]
                if o.failed is None:
                    if _describe(o) == d:
                        hit = j
                        break
                    if kin is None and o.mod.id == n.mod.id:
                        kin = j
            j = hit if hit is not None else kin
            if j is None:
                use.append(n)
                continue
            o, pos = old[j], j + 1
            if hit is None:
                o.mod.load_settings(n.mod.settings())
                o.mod.enabled = bool(n.mod.enabled)
            use.append(o)
        self._slots = tuple(use)
        self._unknown = tuple(unknown)

    def _edited(self, structure: bool) -> None:
        self._unsaved = True
        if structure:
            self.structure_changed.emit()
        self.changed.emit()

    # master, limiter, guard
    @property
    def master(self) -> bool:
        return self._master

    def set_master(self, on: bool) -> None:
        self._master = bool(on)
        self._edited(False)

    @property
    def limiter(self) -> bool:
        return self._limiter_on

    def set_limiter(self, on: bool) -> None:
        self._limiter_on = bool(on)
        self._edited(False)

    @property
    def tripped(self) -> bool:
        """True while the overload guard has the chain bypassed."""
        return self._tripped

    def reenable(self) -> None:
        if self._tripped:
            self._over = 0.0
            self._tripped = False
            self.recovered.emit()

    @property
    def active(self) -> bool:
        """False when process() would hand the block back untouched: the engine skips the call then (master off,
        guard tripped, or no module switched on or still fading out)."""
        if self._mix > 0.0 or self._ghost is not None:
            return True
        if self._master and not self._tripped:
            for s in self._slots:
                if _sounds(s):
                    return True
        return False

    def normalizer_settings(self):
        """The settings of the first Track normalizer that is switched on while the chain is sounding (master on, guard
        not tripped), else None. The engine reads it to level each song; the module itself passes the sound through."""
        if self._master and not self._tripped:
            for s in self._slots:
                if s.mod.id == "normalizer" and s.mod.enabled and s.failed is None:
                    return s.mod.settings()
        return None

    def latency(self) -> int:
        """Frames by which the output trails the input right now: the look-ahead of the modules that are switched on
        (they work in series, so the delays add up) and the limiter's, while the chain is sounding. 0 otherwise. The
        engine does not compensate for it; a caller that must line something up with the sound subtracts it."""
        if self._mix <= 0.0:
            return 0
        return _heard_latency(self._cur) + (_Limiter.delay(self._fs) if self._lm > 0.0 else 0)

    @property
    def volume(self) -> float:
        """The player volume of the last block the chain processed (Context.volume), 1.0 before the first."""
        return self._last[2]

    def reset(self) -> None:
        """Forget the signal in every module (seek, stop, track change). The audio thread does it at the start of its
        next block, so any thread may call this."""
        self._reset_gen += 1

    def set_fs(self, fs: int) -> None:
        """Rebuilds every module for a new sample rate from its stored settings. process() does the same by itself
        when the Context has another rate than the chain; this is for a caller that knows earlier."""
        with self._wlock:
            if int(fs) != self._fs:
                self._rebuild(int(fs))

    def _try_rebuild(self, fs: int) -> bool:
        if not self._wlock.acquire(blocking=False):
            return False                # an edit is under way: this one block goes through untouched
        try:
            if fs != self._fs:
                self._rebuild(fs)
        finally:
            self._wlock.release()
        return True

    def _rebuild(self, fs: int) -> None:
        self._fs = fs
        slots = []
        for s in self._slots:
            try:
                m = create(s.mod.id, fs)
                m.load_settings(s.mod.settings())
                m.enabled = s.mod.enabled
                n = _Slot(s.uid, m)
            except Exception:
                _log().exception("DSP module %s could not be rebuilt for %d Hz", s.mod.id, fs)
                n = _Slot(s.uid, s.mod)
                n.failed = "could not be rebuilt for the new sample rate"
            slots.append(n)
        self._slots = self._cur = tuple(slots)         # nothing of the old rate is left to cross over from
        self._ghost = None
        self._lim.reset()

    # audio thread
    def process(self, x: np.ndarray, ctx: Context) -> np.ndarray:
        """(n, 2) float32 in, (n, 2) float32 out. Never raises: if anything inside fails the block goes through."""
        try:
            return self._run(x, ctx)
        except Exception:
            if not self._broke:
                self._broke = True
                _log().exception("DSP chain failed, passing audio through")
            return x

    def _run(self, x, ctx):
        n = len(x)
        if n == 0 or x.ndim != 2:
            return x
        fresh = self._seen_reset != self._reset_gen
        self._seen_reset = self._reset_gen
        if ctx is None:
            ctx = Context(self._fs)
        fs = ctx.fs
        if fs != self._fs and not self._try_rebuild(fs):
            return x
        self._adopt()
        cur, mix = self._cur, self._mix
        want = 1.0 if (self._master and not self._tripped and (self._ghost is not None or _alive(cur))) else 0.0
        if fresh:
            # a new stream starts at full effect, the RAMP_S fade in is for a chain that comes in while music plays
            self._ghost = None
            mix = self._mix = want
        if want == 0.0 and mix == 0.0:
            self._on = False
            self._ghost = None
            return x
        clock = self._clock
        t0 = clock()
        self._last = (ctx.pos + n if ctx.pos >= 0 else -1, ctx.total, ctx.volume)
        lw = 1.0 if self._limiter_on else 0.0
        if fresh or not self._on:
            self._on = True
            self._lim.reset(silent=fresh)
            self._lm = lw
            self._lead = 0
            ramp_n = max(1, int(RAMP_S * fs))
            lat = 0
            for s in cur:
                if s.failed is None:
                    self._reset_module(s)
                s.mix = 1.0 if _on(s) else 0.0
                s.warm, s.lead, s.prime = s.mix > 0.0, 0, 0
                if s.warm and not fresh:
                    w = _warmup(s.mod)
                    lat += w
                    if w:
                        s.prime = ramp_n
            if not fresh:
                self._lead = (lat + ramp_n if lat else 0) + (_Limiter.delay(fs) if lat and lw > 0.0 else 0)
        ramp_n = max(1, int(RAMP_S * fs))
        dry = x.copy() if mix != want else x     # a module may change its input in place, the fade needs the original
        gh = self._ghost
        k = gh.k if gh is not None else len(cur)
        y, ran, up = x, False, 0
        for s in cur[:k]:
            y, r = self._step(s, y, self._at(ctx, up), n, fs, ramp_n)
            ran = ran or r
            if r and _on(s):
                up += _latency(s.mod)
        ins = wm = None
        if gh is not None:
            yo, upo, ins = y.copy(), up, {}
            for s in gh.slots:
                if s.uid in gh.morph:
                    ins[s.uid] = yo.copy()          # what the old copy of this module is given
                yo, r = self._step(s, yo, self._at(ctx, upo), n, fs, ramp_n)
                if r and _on(s):
                    upo += _latency(s.mod)
            if ins:
                wm, gh.mp = _ramp(gh.mp, 1.0, n, ramp_n)
                if gh.mp >= 1.0:
                    gh.morph = frozenset()
        for s in cur[k:]:
            ref = ins.get(s.uid) if ins else None
            y, r = self._step(s, y, self._at(ctx, up), n, fs, ramp_n, ref, wm)
            ran = ran or r
            if r and _on(s):
                up += _latency(s.mod)
        if gh is not None:
            w, gh.u, gh.lead = _ramp_after(gh.u, 1.0, n, gh.ramp, gh.lead)
            y = yo + (y - yo) * w                # the old output fades out as the new one fades in, weights add up to 1
            ran = True
            if gh.u >= 1.0:
                self._ghost = None
        if not ran and mix == 0.0:
            return x                             # nothing to blend and nothing running
        lm = self._lm
        if lw > 0.0 or lm > 0.0:
            if lm == 0.0:
                self._lim.reset()                # the limiter is coming in: it starts on this block
            yl = self._lim.process(y, fs)
            if lm != lw:
                w, self._lm = _ramp(lm, lw, n, ramp_n)
                yl = y + (yl - y) * w
            y = yl
        if mix != want:
            w, self._mix, self._lead = _ramp_after(mix, want, n, ramp_n, self._lead if want > mix else 0)
            y = dry + (y - dry) * w
        if not self._flushing:
            now = clock()
            self._account(now, now - t0, n, fs)
        return y

    @staticmethod
    def _at(ctx, up: int):
        """The context for a module that comes after modules that look ahead by `up` frames in all: what it is given
        left the track that long ago, so the position it sees is earlier by that much (-1 stays for unknown)."""
        if up == 0 or ctx.pos < 0:
            return ctx
        pos = ctx.pos - up
        return Context(ctx.fs, -2 if pos == -1 else pos, ctx.total, ctx.volume, ctx.xfade)

    def _step(self, s, y, ctx, n, fs, ramp_n, ref=None, m=None):
        """One module on one block, with its own fade in or out. Returns (the block, whether the module ran). ref and
        m, for a module that carries on in a new arrangement, change its input over from ref (what it used to get) to y."""
        goal = 1.0 if _on(s) else 0.0
        sm = s.mix
        if goal == 0.0 and sm == 0.0:
            s.warm, s.lead, s.prime = False, 0, 0
            return y, False
        if not s.warm:                           # coming in after being off: its memory is old
            self._reset_module(s)
            w = _warmup(s.mod)
            s.warm, s.lead, s.prime = True, w + (ramp_n if w else 0), (ramp_n if w else 0)
        ts = self._clock()
        xin = y.copy() if sm != goal else y
        yin = y
        if s.prime:                              # the module starts cold: its input fades in, so the start is not a step
            fed = ramp_n - s.prime
            k = min(s.prime, n)
            r = np.ones((n, 1), np.float32)
            r[:k, 0] = (fed + 1 + np.arange(k, dtype=np.float32)) / np.float32(ramp_n)
            s.prime -= k
            yin = y * r
        elif ref is not None:
            yin = ref + (y - ref) * m
        out = self._call(s, yin, ctx)
        if out is None:
            return y, False
        if sm != goal:
            w, s.mix, s.lead = _ramp_after(sm, goal, n, ramp_n, s.lead if goal > sm else 0)
            out = xin + (out - xin) * w
        if not s.ghost:
            self._tick(s, (self._clock() - ts) * 1000.0)
        return out, True

    def _adopt(self) -> None:
        """Audio thread, at the start of a block: takes over the arrangement the window has made. When that changes the
        sound, the old arrangement stays on as a ghost and the output crosses over to the new one. A second change
        that comes while a crossover is under way waits for it: the window only ever shows the latest, so the sound
        gets there in one more step and no more than two arrangements ever run at once."""
        tgt, cur = self._slots, self._cur
        if tgt is cur or self._ghost is not None:
            return
        k = 0
        for a, b in zip(cur, tgt):
            if a is not b or _retuned(b):
                break
            k += 1
        old, new = cur[k:], tgt[k:]
        self._cur = tgt
        had = {id(s) for s in old}
        tuned = {id(s) for s in new if id(s) in had and _retuned(s)}
        if self._mix <= 0.0 or not (any(_sounds(s) for s in new if id(s) in tuned) or
                                    [s for s in old if _sounds(s)] != [s for s in new if _sounds(s)]):
            return                               # nothing is playing, or the change cannot be heard
        keep = {id(s) for s in new}
        ghost = []
        for s in old:
            if id(s) in keep:
                g = self._copy_slot(s)
                if g is not None:
                    if id(s) in tuned:           # the copy plays the module as it sounded before the new settings
                        g.mod.load_settings(dict(g.mod.p))
                        g.mod.enabled = s.warm
                    ghost.append(g)
            else:
                s.ghost = True                   # nobody else holds it any more, so it can run on as it is
                ghost.append(s)
        ramp_n, morph = max(1, int(RAMP_S * self._fs)), set()
        for s in new:
            if id(s) not in had:                 # new to the chain: cold, and the crossover is its fade in
                s.mix = 1.0 if _on(s) else 0.0
                s.warm, s.lead = s.mix > 0.0, 0
                s.prime = ramp_n if s.warm and _warmup(s.mod) > 0 else 0
            elif s.warm and _on(s) and _warmup(s.mod) > 0:
                morph.add(s.uid)
        # the new output has nothing missing once the delays of its modules have filled and the fade in or change over
        # of their input is through, a kept one with the audio it held before the change gone from it: the crossover
        # starts then
        delays = [_warmup(s.mod) for s in new if _on(s)]
        lead = sum(delays) + (ramp_n if any(delays) else 0)
        gone = any(id(s) not in keep and _sounds(s) for s in old)      # a module that is gone may be ringing
        self._ghost = _Ghost(tuple(ghost), k, lead, max(1, int((TAIL_S if gone else XFADE_S) * self._fs)), frozenset(morph))

    def _copy_slot(self, s):
        """A private copy of a running slot that goes on where it is (between two blocks nothing else touches it).
        The window may change a setting while it is copied, which can make the copy fail: then it is tried again, and
        failing that the copy starts cold from the module's settings."""
        m = None
        for _ in range(3):
            try:
                m = s.mod.clone()
                break
            except RuntimeError:
                continue
            except Exception:
                _log().exception("DSP module %s could not be copied", s.mod.id)
                break
        if m is None:
            try:
                m = create(s.mod.id, self._fs)
                m.load_settings(s.mod.settings())
                m.enabled = s.mod.enabled
            except Exception:
                return None
        g = _Slot(s.uid, m)
        g.mix, g.failed, g.warm, g.lead, g.prime, g.ghost = s.mix, s.failed, s.warm, s.lead, s.prime, True
        return g

    def _call(self, s, y, ctx):
        try:
            m = s.mod
            m.prepare()
            out = m.process(y, ctx)
            if type(out) is not np.ndarray or out.shape != y.shape:
                raise ValueError("returned an array of the wrong kind or shape")
            if out.dtype != np.float32:
                out = out.astype(np.float32)
            if not math.isfinite(out.sum()):
                raise ValueError("returned NaN or infinity")
            return out
        except Exception as e:
            self._fail(s, f"{type(e).__name__}: {e}"[:200])
            return None

    def _reset_module(self, s):
        try:
            s.mod.reset()
        except Exception as e:
            self._fail(s, f"{type(e).__name__}: {e}"[:200])

    def _fail(self, s, why):
        if s.failed is not None:
            return
        s.failed, s.mix = why, 0.0
        _log().warning("DSP module %s is bypassed: %s", s.mod.id, why)
        if s.ghost:
            return                               # the window does not know this one: nothing to report
        try:
            self.module_failed.emit(s.uid, s.mod.name, why)
        except RuntimeError:
            pass

    def _tick(self, s, ms):
        if s.ep != self._epoch:
            s.prev = s.cur if s.ep == self._epoch - 1 else (0.0, 0, 0.0)
            s.cur, s.ep = (0.0, 0, 0.0), self._epoch
        t, n, pk = s.cur
        s.cur = (t + ms, n + 1, ms if ms > pk else pk)

    def _account(self, now, dt, n, fs):
        dur = n / fs
        ms = dt * 1000.0
        t, d, k, pk = self._cur_acc
        self._cur_acc = (t + dt, d + dur, k + 1, ms if ms > pk else pk)
        self._blocks += 1
        self._block_ms = dur * 1000.0
        if self._cur_acc[1] >= 1.0:
            self._prev_acc, self._cur_acc = self._cur_acc, (0.0, 0.0, 0, 0.0)
            self._epoch += 1
        self._last_t = now
        # blocks over the limit add up, a block under it pays back double, so a stall now and then does not count
        if dt > GUARD_LOAD * dur:
            self._over += dur
        else:
            self._over = max(0.0, self._over - 2.0 * dur)
        if self._over >= GUARD_SECONDS:
            self._over = 0.0
            self._tripped = True
            pct = self._load()
            _log().warning("DSP chain bypassed: it used %.0f percent of the time per block", pct)
            try:
                self.overloaded.emit(pct)
            except RuntimeError:
                pass

    def _load(self) -> float:
        a, b = self._cur_acc, self._prev_acc
        dur = a[1] + b[1]
        return (a[0] + b[0]) / dur * 100.0 if dur > 0.0 else 0.0

    # the end of the audio
    def flush(self, max_seconds: float = 5.0) -> np.ndarray:
        """Audio thread only, when the audio has ended and no other track follows. Feeds silence through the chain and
        returns everything that was still inside, (n, 2) float32: the delay of the modules and of the limiter, and
        what the modules are ringing with. Stops when the output has stayed below QUIET_DB for QUIET_S (an echo says
        through DspModule.tail() that more is coming), or after max_seconds, and then the end fades out. Empty when the
        chain is not sounding or nothing audible remains. The chain is reset afterwards."""
        blocks = list(self.flush_blocks(max_seconds))
        return np.concatenate(blocks) if blocks else np.zeros((0, 2), np.float32)

    def flush_blocks(self, max_seconds: float = 5.0, block: int = FLUSH_BLOCK):
        """flush() as a generator that gives the tail in blocks, so the audio thread can write them one feed tick at a
        time instead of working out seconds of sound at once. Closing it early is fine; the chain is reset either way."""
        if not (self._mix > 0.0 and self._master and not self._tripped):
            return
        fs, gen = self._fs, self._reset_gen
        pos, total, vol = self._last
        thr = 10.0 ** (QUIET_DB / 20.0)
        need, win = int(QUIET_S * fs), max(16, int(0.0025 * fs))
        limit, fade = int(max_seconds * fs), max(1, int(FLUSH_FADE_S * fs))
        hint = self._tail_frames()
        made = quiet = 0
        rel, pend, lend = [], [], 0                  # blocks of the tail, silent blocks after them, end of sound in rel[-1]
        silence = np.zeros((block, 2), np.float32)
        try:
            while made < limit and gen == self._reset_gen:
                n = min(block, limit - made)
                y = self._silent_block(silence[:n].copy(), Context(fs, pos + made if pos >= 0 else -1, total, vol, False))
                if y is None:
                    break
                made += n
                e = np.square(y.astype(np.float64)).max(axis=1)
                starts = np.arange(0, n, win)
                loud = np.nonzero(np.add.reduceat(e, starts) > thr * thr * np.diff(np.append(starts, n)))[0]
                if len(loud):
                    ws = int(starts[loud[-1]])
                    lend = ws + int(np.nonzero(np.abs(y[ws:ws + win]).max(axis=1) > thr)[0][-1]) + 1
                    rel.extend(pend)
                    pend = []
                    rel.append(y)
                    quiet = n - lend
                    while len(rel) > 1:
                        yield rel.pop(0)
                else:
                    pend.append(y)
                    quiet += n
                if made >= hint and quiet >= need:
                    break
            if rel and gen == self._reset_gen:
                last = rel[-1][:lend].copy()
                if quiet < need:                     # cut off while it still sounded: the end fades out
                    f = min(fade, len(last))
                    last[len(last) - f:] *= np.cos(np.linspace(0.0, np.pi / 2, f, dtype=np.float32))[:, None] ** 2
                yield last
        finally:
            if gen == self._reset_gen:               # not when a seek or a new track has already started over
                self.reset()

    def _silent_block(self, x, ctx):
        """One block of the flush: the chain on silence, without the load accounting of real time. None on a failure."""
        self._flushing = True
        try:
            return self._run(x, ctx)
        except Exception:
            _log().exception("DSP chain failed while flushing")
            return None
        finally:
            self._flushing = False

    def _tail_frames(self) -> int:
        t = 0
        for s in self._cur:
            if _on(s):
                try:
                    t += max(0, int(s.mod.tail()))
                except Exception:
                    pass
        return t

    def stats(self) -> dict:
        """What the chain cost over the last one to two seconds of audio. load is a percentage of the blocks'
        duration; avg_ms and peak_ms are milliseconds per block, for the chain and for each module. All zero
        while nothing is being processed. latency is in frames. limiter_db is how far the safety limiter pulled the level
        down in the latest block, limiting says whether it did at all."""
        idle = self._blocks == 0 or self._clock() - self._last_t > 1.5
        mods = []
        for i, s in enumerate(self._slots):
            quiet = idle or not s.mod.enabled
            if s.ep != self._epoch:         # no call since the window moved on
                cur, prev = (0.0, 0, 0.0), (s.cur if s.ep == self._epoch - 1 else (0.0, 0, 0.0))
            else:
                cur, prev = s.cur, s.prev
            n = cur[1] + prev[1]
            mods.append({"index": i, "uid": s.uid, "id": s.mod.id, "name": s.mod.name,
                         "enabled": bool(s.mod.enabled), "failed": s.failed or "",
                         "avg_ms": 0.0 if quiet or not n else (cur[0] + prev[0]) / n,
                         "peak_ms": 0.0 if quiet else max(cur[2], prev[2])})
        a, b = self._cur_acc, self._prev_acc
        n = a[2] + b[2]
        gh = self._ghost
        red = self._lim.red if not idle and self._limiter_on and self._lm > 0.0 else 0.0
        return {"load": 0.0 if idle else self._load(),
                "avg_ms": 0.0 if idle or not n else (a[0] + b[0]) * 1000.0 / n,
                "peak_ms": 0.0 if idle else max(a[3], b[3]),
                "block_ms": self._block_ms, "blocks": self._blocks,
                "ghosts": len(gh.slots) if gh is not None else 0, "latency": self.latency(),
                "limiting": red > 1e-3, "limiter_db": -20.0 * math.log10(max(1e-6, 1.0 - red)) if red > 1e-3 else 0.0,
                "active": self.active, "tripped": self._tripped, "master": self._master, "modules": mods}

    # settings and presets
    def to_dict(self) -> dict:
        mods = [s.mod.to_dict() for s in self._slots]
        for pos, item in self._unknown:         # modules of a newer build go back where they were
            mods.insert(min(pos, len(mods)), item)
        return {"version": VERSION, "master": self._master, "limiter": self._limiter_on, "modules": mods}

    def load_dict(self, d) -> list:
        """Replaces the chain with what d describes and returns notes (plain sentences) about what was skipped.
        A d that is not a chain changes nothing."""
        if not isinstance(d, dict):
            return ["This is not a DSP chain."]
        notes = []
        ver = d.get("version")
        if isinstance(ver, int) and ver > VERSION:
            notes.append("This was saved by a newer version. Only what this version knows is used.")
        items = d.get("modules")
        if not isinstance(items, list):
            items = []
        new, unknown = [], []
        for item in items:
            if len(new) >= MAX_MODULES:
                notes.append("Only the first %d modules were used." % MAX_MODULES)
                break
            mid = item.get("id") if isinstance(item, dict) else None
            s = self._make(mid, item.get("settings"), item.get("enabled", True)) if mid is not None else None
            if s is not None:
                new.append(s)
            elif isinstance(mid, str) and mid not in REGISTRY and len(unknown) < MAX_MODULES:
                unknown.append((len(new) + len(unknown), item))
                notes.append("Skipped a module this version does not have: %s. It stays in the saved file." % mid)
            else:
                notes.append("Skipped a module this version does not have: %s." % (mid if mid is not None else "unnamed"))
        with self._wlock:
            self._replace(new, unknown)
            if "master" in d:
                self._master = _as_bool(d["master"], self._master)
            if "limiter" in d:
                self._limiter_on = _as_bool(d["limiter"], self._limiter_on)
        self._edited(True)
        return notes

    @property
    def path(self) -> Path:
        return self._path or config_dir() / "dsp.json"

    def save(self) -> bool:
        try:
            _write_json(self.path, self.to_dict())
        except Exception as e:
            self.last_error = "DSP settings could not be saved: %s" % e
            _log().warning(self.last_error)
            return False
        self._unsaved = False
        return True

    def load(self) -> list:
        """Reads the settings file. Missing means a fresh start. A file that cannot be read is set aside as
        dsp.json.bad and the chain starts empty. Never raises. Returns notes."""
        p = self.path
        if not p.exists():
            return []
        try:
            d = _read_json(p)
            if not isinstance(d, dict):
                raise ValueError("not a DSP chain")
        except Exception as e:
            self.last_error = "The DSP settings file could not be read (%s). Starting with an empty chain." % e
            _log().warning(self.last_error)
            try:
                plat_fs.replace(p, p.with_name(p.name + ".bad"))
            except OSError:
                pass
            return [self.last_error]
        notes = self.load_dict(d)
        self._unsaved = False
        return notes

    def shutdown(self) -> None:
        """Writes the settings if anything changed since they were last written or read."""
        if self._unsaved:
            self.save()

    def preset_dir(self) -> Path:
        return self._preset_dir or config_dir() / "dsp_presets"

    def preset_names(self) -> list:
        try:
            return sorted((p.stem for p in self.preset_dir().glob("*.json")), key=str.casefold)
        except OSError:
            return []

    def save_preset(self, name: str) -> bool:
        """Stores the modules and their settings under name (replacing one of that name)."""
        name = _clean_name(name)
        if not name:
            self.last_error = "A preset needs a name."
            return False
        data = self.to_dict()
        del data["master"], data["limiter"]
        try:
            _write_json(self.preset_dir() / (name + ".json"), data)
        except Exception as e:
            self.last_error = "The preset could not be saved: %s" % e
            _log().warning(self.last_error)
            return False
        return True

    def load_preset(self, name: str):
        """Replaces the modules with the preset's. Returns notes, or None (chain untouched) when it cannot be read."""
        try:
            d = _read_json(self.preset_dir() / (_clean_name(name) + ".json"))
            if not isinstance(d, dict):
                raise ValueError("not a DSP preset")
        except Exception as e:
            self.last_error = "The preset could not be read: %s" % e
            return None
        return self.load_dict(d)

    def delete_preset(self, name: str) -> bool:
        try:
            (self.preset_dir() / (_clean_name(name) + ".json")).unlink()
            return True
        except OSError as e:
            self.last_error = "The preset could not be deleted: %s" % e
            return False
