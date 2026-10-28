"""Level and dynamics modules for the user DSP chain: Amplify, Compressor, Leveler and Fade. Each is a DspModule (see dsp_base.py) working on float32 (n, 2) blocks.

How the recursive parts stay fast: the envelope followers of the Compressor and the Leveler are written as
y[n] = max(x[n], R*y[n-1] + (1-R)*x[n]). That looks sample by sample, but the maximum commutes with the linear step, so
y[n] is the largest of the one pole filtered "restarts" at every earlier sample, and each of those is a closed
form in R**n. `_Hold` evaluates it with a cumulative sum and a cumulative maximum, in chunks short enough that R**-n
stays small. It matches the plain loop to rounding error, for any block size. Everything else is a one pole lfilter.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter

from dsp_base import Context, DspModule, Param, register

_DB = math.log(10.0) / 20.0     # natural log units per dB of amplitude


def _in(x):
    """x as float32 with no NaN or infinity (one bad sample would poison every recursive state for good)."""
    if x.dtype != np.float32:
        x = x.astype(np.float32)
    if not np.isfinite(x.sum()):
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x


def _flush(v: float) -> float:
    """State values far below anything audible go to zero so they never turn into slow denormals."""
    return 0.0 if abs(v) < 1e-9 else float(v)


def _lp(a: float, x: np.ndarray, y0: float) -> np.ndarray:
    """One pole low pass y[n] = a*y[n-1] + (1-a)*x[n], started from the output y0 of the sample before x[0]."""
    y, _ = lfilter(np.array([1.0 - a]), np.array([1.0, -a]), x, zi=np.array([a * y0]))
    return y


def _tau_coef(seconds: float, fs: int) -> float:
    return math.exp(-1.0 / max(seconds * fs, 1e-3))


class _Ramp:
    """A value that glides to its new target over a few milliseconds. Exact for any block size."""

    def __init__(self, v: float = 0.0):
        self.cur = self.tgt = float(v)
        self.step = 0.0
        self.left = 0

    def set(self, v: float, fs: int, ms: float = 10.0, instant: bool = False) -> None:
        v = float(v)
        if instant:
            self.jump(v)
        elif v != self.tgt:
            self.tgt = v
            self.left = max(1, int(fs * ms / 1000.0))
            self.step = (v - self.cur) / self.left

    def jump(self, v: float) -> None:
        self.cur = self.tgt = float(v)
        self.left = 0

    def get(self, n: int):
        """A float while settled, else an array of n values."""
        if not self.left:
            return self.cur
        m = min(n, self.left)
        a = self.cur + self.step * np.arange(1, m + 1)
        self.left -= m
        self.cur = self.tgt if not self.left else float(a[-1])
        if m < n:
            a = np.concatenate((a, np.full(n - m, self.tgt)))
        return a


class _Hold:
    """y[n] = max(x[n], R*y[n-1] + (1-R)*x[n]) for a whole block at once (see the module text)."""
    LIMIT = 9.2         # ln(1e4): the largest R**-n inside one chunk, far from overflow and plenty for rounding to stay near 1e-12

    def __init__(self):
        self.R = None

    def set(self, R: float) -> None:
        if R == self.R:
            return
        self.R = R
        lam = -math.log(R)
        self.m = int(max(32, min(self.LIMIT / lam, 8192)))
        j = np.arange(self.m + 1)
        self.w = np.exp(lam * j)
        self.rp = np.exp(-lam * j)

    def run(self, x: np.ndarray, y0: float):
        """Returns (y, last y). y0 is the output at the sample before x[0]."""
        n = len(x)
        out = np.empty(n)
        a = 1.0 - self.R
        i, y = 0, y0
        while i < n:
            k = min(self.m, n - i)
            t = self.w[1:k + 1] * x[i:i + k]
            u = np.cumsum(t)
            c = np.empty(k + 1)
            c[0] = y
            c[1:] = t - a * u
            np.maximum.accumulate(c, out=c)
            out[i:i + k] = self.rp[1:k + 1] * (a * u + c[1:])
            y = out[i + k - 1]
            i += k
        return out, float(y)


class _Delay:
    """A short audio delay (the look-ahead) that can change its length without a click."""
    XF_MS = 5.0

    def __init__(self, fs: int, max_ms: float):
        self.mx = int(math.ceil(fs * max_ms / 1000.0))
        self.hist = np.zeros((self.mx, 2), np.float32)
        self.d = self.old = self.xf = 0
        self.xn = max(1, int(fs * self.XF_MS / 1000.0))

    def set(self, d: int, instant: bool = False) -> None:
        d = max(0, min(int(d), self.mx))
        if instant:
            self.d = self.old = d
            self.xf = 0
        elif d != self.d:
            if not self.xf:
                self.old = self.d
                self.xf = self.xn
            self.d = d

    def clear(self) -> None:
        self.hist[:] = 0.0
        self.old = self.d
        self.xf = 0

    def run(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        buf = np.concatenate((self.hist, x))
        self.hist = buf[n:].copy()
        out = buf[self.mx - self.d:self.mx - self.d + n]
        if self.xf:
            m = min(n, self.xf)
            w = ((self.xn - self.xf + 1) + np.arange(m, dtype=np.float32)) / np.float32(self.xn)
            ko = self.mx - self.old
            out = out.copy()
            out[:m] = out[:m] * w[:, None] + buf[ko:ko + m] * (1.0 - w)[:, None]
            self.xf -= m
        return out


# Amplify

_SOFT_KNEE = 0.7        # below this the soft clipper leaves the signal alone (-3.1 dB)


def _soft_clip(y: np.ndarray) -> np.ndarray:
    """Linear up to 0.7, then a tanh that meets it with the same slope and never passes 1.0."""
    a = np.abs(y)
    if not a.max() > _SOFT_KNEE:
        return y
    r = np.float32(1.0 - _SOFT_KNEE)
    z = np.tanh((a - np.float32(_SOFT_KNEE)) / r) * r + np.float32(_SOFT_KNEE)
    return np.where(a > _SOFT_KNEE, np.copysign(z, y), y).astype(np.float32)


@register
class Amplify(DspModule):
    id, name, group = "amplify", "Amplify", "Level"
    summary = "Raises or lowers the volume by a fixed amount. The soft clipper rounds off peaks that would pass full scale."
    params = (
        Param("gain", "Gain", "float", 0.0, -40.0, 20.0, 0.1, unit="dB", scale="db"),
        Param("softclip", "Soft clip", "bool", False),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._g = _Ramp(1.0)
        self._sc = _Ramp(0.0)
        self._ready = False

    def reset(self) -> None:
        self._g.jump(self._g.tgt)
        self._sc.jump(self._sc.tgt)

    def _apply(self, p: dict) -> None:
        first, self._ready = not self._ready, True
        self._g.set(10.0 ** (p["gain"] / 20.0), self.fs, instant=first)
        self._sc.set(1.0 if p["softclip"] else 0.0, self.fs, instant=first)

    def process(self, x, ctx):
        self.prepare()
        n = len(x)
        if not n:
            return x
        x = _in(x)
        g = self._g.get(n)
        if isinstance(g, float):
            y = x if g == 1.0 else x * np.float32(g)
        else:
            y = x * g.astype(np.float32)[:, None]
        w = self._sc.get(n)
        if isinstance(w, float):
            return _soft_clip(y) if w == 1.0 else y
        s = _soft_clip(y)
        return y + (s - y) * w.astype(np.float32)[:, None]


# Compressor

@register
class Compressor(DspModule):
    """Feed forward compressor after Giannoulis, Massberg and Reiss (JAES 2012): level in dB, static curve with a quadratic
    knee, release then attack smoothing of the gain reduction in dB (the "decoupled" detector). The two channels share one
    gain (the louder of the two sets the level), so the stereo image does not shift. Attack and release are time
    constants: the reduction covers 63 percent of a step in one attack time, and falls to 37 percent in one release time."""
    id, name, group = "compressor", "Compressor", "Dynamics"
    summary = "Turns loud parts down so the level stays more even. Both channels are reduced together."
    params = (
        Param("threshold", "Threshold", "float", -18.0, -60.0, 0.0, 0.5, unit="dB", scale="db"),
        Param("ratio", "Ratio", "float", 3.0, 1.0, 20.0, 0.1, unit=":1"),
        Param("knee", "Knee", "float", 6.0, 0.0, 24.0, 0.5, unit="dB"),
        Param("attack", "Attack", "float", 10.0, 0.1, 200.0, 0.1, unit="ms", scale="log"),
        Param("release", "Release", "float", 150.0, 5.0, 2000.0, 1.0, unit="ms", scale="log"),
        Param("makeup", "Makeup gain", "float", 0.0, 0.0, 24.0, 0.1, unit="dB", scale="db"),
        Param("lookahead", "Look-ahead", "float", 0.0, 0.0, 10.0, 0.1, unit="ms"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._hold = _Hold()
        self._delay = _Delay(self.fs, 10.0)
        self._mk = _Ramp(0.0)
        self._h = self._y = 0.0
        self._ready = False

    def reset(self) -> None:
        self._h = self._y = 0.0
        self._delay.clear()
        self._mk.jump(self._mk.tgt)

    def latency(self) -> int:
        return self._delay.d

    def _apply(self, p: dict) -> None:
        first, self._ready = not self._ready, True
        self._T = p["threshold"]
        self._k = 1.0 - 1.0 / p["ratio"]
        self._W = max(p["knee"], 0.01)
        self._A = _tau_coef(p["attack"] * 1e-3, self.fs)
        self._hold.set(_tau_coef(p["release"] * 1e-3, self.fs))
        self._mk.set(p["makeup"], self.fs, instant=first)
        self._delay.set(round(p["lookahead"] * 1e-3 * self.fs), instant=first)

    def reduction(self, level_db: np.ndarray) -> np.ndarray:
        """The static curve: gain reduction in dB for input levels in dB (textbook soft knee)."""
        d = level_db - self._T
        t = np.clip(d + self._W / 2.0, 0.0, self._W)
        return self._k * (t * t / (2.0 * self._W) + np.maximum(d - self._W / 2.0, 0.0))

    def process(self, x, ctx):
        self.prepare()
        n = len(x)
        if not n:
            return x
        x = _in(x)
        peak = np.maximum(np.abs(x[:, 0]), np.abs(x[:, 1]))
        gr = self.reduction(20.0 * np.log10(np.maximum(peak, 1e-6), dtype=np.float64))
        if self._h == 0.0 and self._y == 0.0 and not gr.any():
            y = 0.0
        else:
            y1, self._h = self._hold.run(gr, self._h)
            y = _lp(self._A, y1, self._y)
            self._h = _flush(self._h)
            self._y = _flush(y[-1])
        a = self._delay.run(x)
        g = np.exp((self._mk.get(n) - y) * _DB)
        if isinstance(g, float):
            return a * np.float32(g)
        return a * g.astype(np.float32)[:, None]


# Leveler

@register
class Leveler(DspModule):
    """A slow, look-ahead volume leveler in the spirit of VLevel. It measures the short term level (mean square over about
    0.4 s), works out the gain that moves the level `strength` of the way to the target, and lets that gain fall quickly
    (about 0.1 s) but come back up slowly (the release), which is what keeps it from pumping. A second path watches the
    peaks, ten milliseconds ahead of the audio, and never lets a boost push the recent peak past about -0.5 dBFS (a peak
    already above that keeps unity gain, it is not turned down). Quiet passages below -60 dB are left alone so a noise
    floor is not boosted. The audio is delayed by 10 ms for the look-ahead."""
    id, name, group = "leveler", "Leveler", "Dynamics"
    summary = "Slowly evens out loud and quiet passages. Quiet parts come up, loud parts go down, without sudden jumps."
    params = (
        Param("strength", "Strength", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
        Param("target", "Target level", "float", -18.0, -40.0, -6.0, 0.5, unit="dB", scale="db"),
        Param("boost", "Maximum boost", "float", 3.0, 0.0, 24.0, 0.5, unit="dB"),
        Param("release", "Release", "float", 2.0, 0.2, 10.0, 0.1, unit="s", scale="log"),
    )
    LOOKAHEAD = 0.010       # seconds
    DETECT = 0.2            # time constant of the mean square, seconds (about a 0.4 s window)
    ATTACK = 0.1            # how fast the gain may fall, seconds
    CUT = 12.0              # the most it will turn anything down, dB
    GATE = -60.0            # mean square level below which nothing is boosted, dB
    CEIL = -0.5             # peaks that a boost may not push past, dBFS
    TOP = 24.0              # the gain is smoothed as a reduction from this many dB above unity

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._hold = _Hold()
        self._delay = _Delay(self.fs, self.LOOKAHEAD * 1000.0)
        self._delay.set(self._delay.mx, instant=True)
        self._Ad = _tau_coef(self.DETECT, self.fs)
        self._Aa = _tau_coef(self.ATTACK, self.fs)
        self._Ag = _tau_coef(self.LOOKAHEAD / 8.0, self.fs)
        self.reset()

    def reset(self) -> None:
        self._ms = 0.0
        self._h = self._y = self.TOP        # gain 0 dB
        self._ph = 0.0                      # peak follower
        self._yg = 30.0                     # smoothed headroom
        self._delay.clear()

    def latency(self) -> int:
        return self._delay.d

    def _apply(self, p: dict) -> None:
        self._s = p["strength"] / 100.0
        self._T = p["target"]
        self._boost = p["boost"]
        self._hold.set(_tau_coef(p["release"], self.fs))

    def process(self, x, ctx):
        self.prepare()
        n = len(x)
        if not n:
            return x
        x = _in(x)
        e = np.einsum("ij,ij->i", x, x, dtype=np.float64) * 0.5
        ms = _lp(self._Ad, e, self._ms)
        self._ms = float(ms[-1]) if ms[-1] > 1e-30 else 0.0
        level = 10.0 * np.log10(ms + 1e-12)
        want = np.clip(self._s * (self._T - level), -self.CUT, self._boost)
        want[level < self.GATE] = 0.0
        y1, self._h = self._hold.run(self.TOP - want, self._h)
        y = _lp(self._Aa, y1, self._y)
        self._y = float(y[-1])
        slow = self.TOP - y
        peak = np.maximum(np.abs(x[:, 0]), np.abs(x[:, 1])).astype(np.float64)
        env, self._ph = self._hold.run(peak, self._ph)
        self._ph = self._ph if self._ph > 1e-30 else 0.0
        room = np.minimum(np.maximum(self.CEIL - 20.0 * np.log10(env + 1e-9), 0.0), 30.0)
        guard = _lp(self._Ag, room, self._yg)
        self._yg = float(guard[-1])
        g = np.exp(np.minimum(slow, guard) * _DB).astype(np.float32)
        return self._delay.run(x) * g[:, None]


# Fade

def _shape(t: np.ndarray, kind: str) -> np.ndarray:
    """Gain of a fade in at progress t in [0, 1]; a fade out uses it with t running down."""
    if kind == "Sine":
        return np.sin(0.5 * math.pi * t)
    if kind == "Cosine":
        return 0.5 * (1.0 - np.cos(math.pi * t))
    if kind == "Logarithmic":
        return (10.0 ** (-3.0 * (1.0 - t)) - 1e-3) / (1.0 - 1e-3)
    return t


@register
class Fade(DspModule):
    """Fades the start and the end of a track by position (Context.pos and Context.total). With an unknown position (-1)
    or length it does nothing. After a module that looks ahead the chain gives it the position of the audio it receives,
    which is earlier by that delay, and negative while the start of the track is still in the delay. While the engine
    crossfades two tracks (Context.xfade) no fade is applied, the crossfade already shapes that edge. After a seek, a
    setting change or the start or end of a crossfade the gain glides to its new value over 3 ms."""
    id, name, group = "fade", "Fade in and out", "Level"
    summary = "Fades each track in at the start and out before the end."
    params = (
        Param("fade_in", "Fade in", "float", 2.0, 0.0, 30.0, 0.1, unit="s"),
        Param("fade_out", "Fade out", "float", 4.0, 0.0, 30.0, 0.1, unit="s"),
        Param("shape", "Shape", "choice", "Cosine", choices=("Linear", "Sine", "Cosine", "Logarithmic")),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._rn = max(1, int(self.fs * 0.003))
        self.reset()

    def reset(self) -> None:
        self._next = None       # where the next block should start if playing goes on
        self._xf = False        # the last block was inside a crossfade
        self._last = None       # last gain given
        self._left = 0
        self._from = 1.0

    def _apply(self, p: dict) -> None:
        self._nin = int(round(p["fade_in"] * self.fs))
        self._nout = int(round(p["fade_out"] * self.fs))
        self._changed = True

    def gain(self, pos: int, n: int, total: int) -> np.ndarray:
        """The fade gain of frames pos ... pos+n-1 of a track of `total` frames."""
        idx = np.arange(pos, pos + n, dtype=np.float64)
        g = np.ones(n)
        shape = self.p["shape"]
        if self._nin:
            g = _shape(np.clip(idx / self._nin, 0.0, 1.0), shape)
        if self._nout:
            g = g * _shape(np.clip((total - idx) / self._nout, 0.0, 1.0), shape)
        return g

    def process(self, x, ctx):
        self.prepare()
        n = len(x)
        if not n:
            return x
        x = _in(x)
        pos, total = int(ctx.pos), int(ctx.total)
        xf = bool(getattr(ctx, "xfade", False))
        if total <= 0 or pos == -1 or not (self._nin or self._nout):
            self._next = self._last = None
            self._left = 0
            self._changed = self._xf = False
            return x
        jump = self._changed or xf != self._xf or (self._next is not None and pos != self._next)
        self._changed = False
        self._xf = xf
        self._next = None if xf else pos + n
        quiet = xf or (pos >= self._nin and pos + n <= total - self._nout)
        if quiet and not self._left and not (jump and self._last not in (None, 1.0)):
            self._last = 1.0
            return x
        g = np.ones(n) if xf else self.gain(pos, n, total)
        if jump and self._last is not None and abs(g[0] - self._last) > 1e-3:
            self._from, self._left = self._last, self._rn
        if self._left:
            m = min(n, self._left)
            w = (self._rn - self._left + 1 + np.arange(m)) / self._rn
            g[:m] = self._from * (1.0 - w) + g[:m] * w
            self._left -= m
        self._last = float(g[-1])
        return x * g.astype(np.float32)[:, None]
