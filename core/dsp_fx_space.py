"""Channel, delay and room effects for the user DSP chain: crossfeed, stereo width, channel mixer, echo and reverb.

Every module works on whole blocks with numpy and scipy and keeps its state between blocks, so the output does not depend
on how the signal is cut into blocks. Recursive parts (filters) use lfilter and sosfilt with a saved state. A feedback loop
that goes through a delay of D frames is run in chunks of at most D frames: inside one chunk every sample it needs is
already known, so the chunk is plain vector arithmetic and the result is the same as a sample by sample loop. Settings are
never switched abruptly: gains glide over about 5 ms, filter coefficients glide in steps of 32 frames, and delay taps
crossfade.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter, sosfilt

from dsp_base import DspModule, Param, finite, register

RAMP_S = 0.005      # how long a setting takes to glide to its new value
GRID = 32           # frames between filter coefficient updates while gliding
TINY = 1e-20        # values below this are flushed to zero in recursive paths


def _ramp_len(fs: int) -> int:
    return max(GRID, int(round(RAMP_S * fs / GRID)) * GRID)


def _db(x: float) -> float:
    return 10.0 ** (x / 20.0)


def _col(g):
    """A per frame gain as a column so it multiplies (n, channels) arrays; plain numbers pass through."""
    return g[:, None] if isinstance(g, np.ndarray) else g


def _lowpass1(fc: float, fs: int) -> np.ndarray:
    """First order low pass with its -3 dB point at fc (the RC filter through the bilinear transform), as an sos row."""
    k = math.tan(math.pi * min(fc, 0.45 * fs) / fs)
    b = k / (1.0 + k)
    return np.array([b, b, 0.0, 1.0, (k - 1.0) / (1.0 + k), 0.0])


def _highpass2(fc: float, fs: int, q: float = 0.70710678118) -> np.ndarray:
    w0 = 2.0 * math.pi * min(fc, 0.45 * fs) / fs
    cw, alpha = math.cos(w0), math.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    return np.array([(1 + cw) / 2 / a0, -(1 + cw) / a0, (1 + cw) / 2 / a0, 1.0, -2.0 * cw / a0, (1.0 - alpha) / a0])


class _Ramp:
    """A number (or a small array) that glides linearly to a new target over n frames. take(k) gives the values for the
    next k frames: a plain float when settled, an array of shape (k, ...) while gliding."""

    def __init__(self, value, n: int):
        self.n = n
        self.cur = self.src = self.tgt = np.array(value, dtype=np.float64)
        self.pos = n

    def set(self, value, now: bool = False) -> None:
        v = np.array(value, dtype=np.float64)
        if now:
            self.cur = self.src = self.tgt = v
            self.pos = self.n
        elif not np.array_equal(v, self.tgt):
            self.src, self.tgt, self.pos = self.cur, v, 0

    def take(self, k: int):
        if self.pos >= self.n:
            return self.tgt.item() if self.tgt.ndim == 0 else self.tgt
        t = np.minimum(np.arange(self.pos + 1, self.pos + k + 1) / self.n, 1.0)
        out = self.src + (self.tgt - self.src) * t.reshape((k,) + (1,) * self.src.ndim)
        self.pos = min(self.n, self.pos + k)
        self.cur = self.tgt if self.pos >= self.n else out[-1]
        return out


_IDENT = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])


class _Cascade:
    """Biquads (rows of an sos matrix) with their state kept between blocks. A new coefficient set is reached by gliding
    in steps of GRID frames, anchored to the glide start, so the result does not depend on the block size."""

    def __init__(self, nsec: int, nch: int, fs: int):
        self.n = nsec
        self.len = _ramp_len(fs)
        self.cur = self.src = self.tgt = np.tile(_IDENT, (nsec, 1))
        self.pos = self.len
        self.zi = np.zeros((nsec, 2, nch))

    def reset(self) -> None:
        self.zi[:] = 0.0

    def set(self, sos, now: bool = False) -> None:
        sos = np.asarray(sos, dtype=np.float64).reshape(self.n, 6).copy()
        if now:
            self.cur = self.src = self.tgt = sos
            self.pos = self.len
        elif not np.array_equal(sos, self.tgt):
            self.src, self.tgt, self.pos = self.cur, sos, 0

    def _filt(self, sos, x):
        if self.n == 1:
            s = sos[0]
            y, self.zi[0] = lfilter(s[:3], s[3:], x, axis=0, zi=self.zi[0])
            return y
        y, self.zi = sosfilt(sos, x, axis=0, zi=self.zi)
        return y

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if self.pos >= self.len:
            y = self._filt(self.tgt, x)
        else:
            y = np.empty_like(x)
            i = 0
            while i < n:
                if self.pos >= self.len:
                    y[i:] = self._filt(self.tgt, x[i:])
                    break
                c = self.pos // GRID
                k = min(n - i, (c + 1) * GRID - self.pos)
                self.cur = self.src + (self.tgt - self.src) * min(1.0, (c + 1) * GRID / self.len)
                y[i:i + k] = self._filt(self.cur, x[i:i + k])
                self.pos += k
                i += k
            if self.pos >= self.len:
                self.cur = self.src = self.tgt
        self.zi[np.abs(self.zi) < TINY] = 0.0
        return y


class _Bank:
    """Delay lines side by side, one per column. read(k, d) gives the k frames written d frames ago (d a number, or one
    per column) and write(u) stores k new ones. A chunk must not be longer than the smallest d in use, and d is at most
    size."""

    def __init__(self, cols: int, size: int):
        self.C = 1 << max(4, int(size).bit_length())
        self.mask = self.C - 1
        self.cols = cols
        self.buf = np.zeros((self.C, cols))
        self.w = self.pend = 0
        self._col = np.arange(cols)

    def reset(self) -> None:
        self.buf[:] = 0.0
        self.w = self.pend = 0

    def read(self, k: int, d) -> np.ndarray:
        if isinstance(d, (int, np.integer)):
            s = (self.w - int(d)) & self.mask
            if s + k <= self.C:
                return self.buf[s:s + k].copy()
            return np.concatenate((self.buf[s:], self.buf[:s + k - self.C]))
        idx = (self.w - np.asarray(d)[None, :] + np.arange(k)[:, None]) & self.mask
        return self.buf.take(idx * self.cols + self._col)

    def write(self, u: np.ndarray) -> None:
        k = u.shape[0]
        e = self.w + k
        if e <= self.C:
            self.buf[self.w:e] = u
        else:
            m = self.C - self.w
            self.buf[self.w:] = u[:m]
            self.buf[:k - m] = u[m:]
        self.w = e & self.mask

    def flush(self, k: int) -> None:
        """Zero the tiny values among the frames written lately, now and then (keeps a decaying tail out of denormal range;
        a tail cannot sink from 1e-20 to denormal in a few dozen milliseconds)."""
        self.pend += k
        if self.pend < 4096:
            return
        k, self.pend = min(self.pend, self.C), 0
        s = (self.w - k) & self.mask
        for seg in (self.buf[s:s + k], self.buf[:max(0, s + k - self.C)]):
            seg[np.abs(seg) < TINY] = 0.0


class _TapDelay:
    """A delay of whole frames for each channel (up to maxd). A change crossfades from the old delay to the new one."""

    def __init__(self, maxd: int, nch: int, fs: int):
        self.H = max(1, int(maxd))
        self.nch = nch
        self.len = _ramp_len(fs)
        self.hist = np.zeros((self.H, nch))
        self.d = np.zeros(nch, dtype=np.int64)
        self.old = self.d.copy()
        self.pos = self.len

    def reset(self) -> None:
        self.hist[:] = 0.0

    def set(self, d, now: bool = False) -> None:
        d = np.minimum(np.asarray(d, dtype=np.int64), self.H)
        if now:
            self.d, self.old, self.pos = d, d.copy(), self.len
        elif not np.array_equal(d, self.d):
            self.old, self.d, self.pos = self.d, d, 0

    def process(self, x: np.ndarray) -> np.ndarray:
        n, H = x.shape[0], self.H
        buf = np.concatenate((self.hist, x))
        self.hist = buf[-H:].copy()
        out = np.empty_like(x)
        gliding = self.pos < self.len
        t = np.minimum((self.pos + 1 + np.arange(n)) / self.len, 1.0) if gliding else None
        for c in range(self.nch):
            new = buf[H - self.d[c]:H - self.d[c] + n, c]
            if gliding and self.old[c] != self.d[c]:
                old = buf[H - self.old[c]:H - self.old[c] + n, c]
                out[:, c] = old + (new - old) * t
            else:
                out[:, c] = new
        if gliding:
            self.pos = min(self.len, self.pos + n)
        return out


class _Fx(DspModule):
    """What every effect here shares: settings are applied before the first block, NaN and infinity in the input count as
    zero, and a block that still comes out with NaN or infinity clears the module's memory and is replaced by silence."""

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._ready = False
        self._rl = _ramp_len(self.fs)

    def process(self, x: np.ndarray, ctx) -> np.ndarray:
        if self._dirty:
            self.prepare()
        if x.shape[0] == 0:
            return x
        x = finite(x if x.dtype == np.float32 else x.astype(np.float32))
        y = self._run(x)
        if not np.isfinite(y).all():
            self.reset()
            return np.zeros_like(x)
        return y

    def _run(self, x: np.ndarray) -> np.ndarray:
        return x


def _f32(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float32)


# Crossfeed
CROSSFEED_PRESETS = {"Light": (700.0, -10.0), "Medium": (700.0, -6.0), "Strong": (650.0, -3.0)}


@register
class Crossfeed(_Fx):
    """Each ear also hears a low passed, slightly late and quieter copy of the other channel (Bauer, Linkwitz).

    With k the share of bass that crosses over and LP a first order low pass, out_L = L + k * LP(delay(R - L)), and the
    same with the channels exchanged. Only the difference between the channels is touched, so a centred signal passes
    unchanged and a hard panned one gives part of its bass to the other side. The feed level is the crossed bass relative
    to the direct bass, k = r / (1 + r) with r = 10^(feed / 20)."""
    id, name, group = "crossfeed", "Crossfeed", "Channels"
    summary = "Lets each ear hear a softened, slightly late copy of the other channel, so headphones sound less cramped."
    params = (
        Param("strength", "Strength", "choice", "Medium", choices=("Light", "Medium", "Strong", "Custom")),
        Param("cut", "Custom cut frequency", "float", 700.0, 300.0, 1500.0, 10.0, unit="Hz", scale="log"),
        Param("feed", "Custom feed level", "float", -6.0, -15.0, -1.0, 0.5, unit="dB", scale="db"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._d = max(1, int(round(0.0003 * fs)))
        self._hist = np.zeros((self._d, 2))
        self._lp = _Cascade(1, 2, fs)
        self._k = _Ramp(0.0, self._rl)

    def active_keys(self) -> tuple:
        return ("strength", "cut", "feed") if self.get("strength") == "Custom" else ("strength",)

    def _apply(self, p):
        first = not self._ready
        fc, feed = CROSSFEED_PRESETS.get(p["strength"], (p["cut"], p["feed"]))
        r = _db(feed)
        self._k.set(r / (1.0 + r), first)
        self._lp.set([_lowpass1(fc, self.fs)], first)
        self._ready = True

    def reset(self):
        self._hist[:] = 0.0
        self._lp.reset()

    def _run(self, x):
        n = x.shape[0]
        x64 = x.astype(np.float64)
        e = x64[:, ::-1] - x64
        buf = np.concatenate((self._hist, e))
        self._hist = buf[n:].copy()
        return _f32(x64 + self._lp.process(buf[:n]) * _col(self._k.take(n)))


# Stereo width
@register
class StereoWidth(_Fx):
    """Mid/side width. S = (L - R) / 2 is scaled by the width, and below the bass frequency it is removed with a fourth
    order Linkwitz-Riley high pass so the bass stays in the middle. At 100 percent with the bass switch off the output
    is the input."""
    id, name, group = "stereo_width", "Stereo width", "Channels"
    summary = "Makes the stereo image narrower or wider, and can keep the bass in the middle."
    params = (
        Param("width", "Width", "float", 130.0, 0.0, 200.0, 1.0, unit="%"),
        Param("bassmono", "Keep bass in the middle", "bool", True),
        Param("bassfreq", "Bass below", "float", 120.0, 40.0, 400.0, 1.0, unit="Hz", scale="log"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._hp = _Cascade(2, 1, fs)
        self._w = _Ramp(1.0, self._rl)
        self._m = _Ramp(0.0, self._rl)

    def _apply(self, p):
        first = not self._ready
        self._w.set(p["width"] / 100.0, first)
        self._m.set(1.0 if p["bassmono"] else 0.0, first)
        self._hp.set(np.vstack([_highpass2(p["bassfreq"], self.fs)] * 2), first)
        self._ready = True

    def reset(self):
        self._hp.reset()

    def _run(self, x):
        n = x.shape[0]
        x64 = x.astype(np.float64)
        s = (x64[:, 0] - x64[:, 1])[:, None] * 0.5
        hp = self._hp.process(s)
        m, w = _col(self._m.take(n)), _col(self._w.take(n))
        d = (w * (s + m * (hp - s)) - s)[:, 0]
        out = x64
        out[:, 0] += d
        out[:, 1] -= d
        return _f32(out)


# Channel mixer
_MIXES = {
    "Stereo": ((1.0, 0.0), (0.0, 1.0)),
    "Swap": ((0.0, 1.0), (1.0, 0.0)),
    "Mono": ((0.5, 0.5), (0.5, 0.5)),
    "Left only": ((1.0, 0.0), (1.0, 0.0)),
    "Right only": ((0.0, 1.0), (0.0, 1.0)),
}


@register
class ChannelMixer(_Fx):
    """out = diag(gain_l, gain_r) * mix * in, then a delay for each side. Balance only turns the louder side down, the
    way a balance knob does. Invert flips the polarity of that output channel."""
    id, name, group = "channel_mixer", "Channel mixer", "Channels"
    summary = "Swaps channels, mixes to mono or plays one channel on both sides, and sets balance, gain and delay."
    params = (
        Param("mode", "Mode", "choice", "Stereo", choices=tuple(_MIXES)),
        Param("invl", "Invert left", "bool", False),
        Param("invr", "Invert right", "bool", False),
        Param("balance", "Balance", "float", 0.0, -100.0, 100.0, 1.0, unit="%"),
        Param("gainl", "Left gain", "float", 0.0, -24.0, 12.0, 0.1, unit="dB", scale="db"),
        Param("gainr", "Right gain", "float", 0.0, -24.0, 12.0, 0.1, unit="dB", scale="db"),
        Param("delayl", "Left delay", "float", 0.0, 0.0, 20.0, 0.01, unit="ms"),
        Param("delayr", "Right delay", "float", 0.0, 0.0, 20.0, 0.01, unit="ms"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._mat = _Ramp(np.eye(2), self._rl)
        self._tap = _TapDelay(int(math.ceil(0.02 * fs)), 2, fs)

    def _apply(self, p):
        first = not self._ready
        bal = p["balance"] / 100.0
        gl = _db(p["gainl"]) * (-1.0 if p["invl"] else 1.0) * (1.0 - max(0.0, bal))
        gr = _db(p["gainr"]) * (-1.0 if p["invr"] else 1.0) * (1.0 - max(0.0, -bal))
        self._mat.set(np.diag([gl, gr]) @ np.array(_MIXES[p["mode"]]), first)
        self._tap.set([int(round(p["delayl"] * self.fs / 1000.0)), int(round(p["delayr"] * self.fs / 1000.0))], first)
        self._ready = True

    def reset(self):
        self._tap.reset()

    def warmup(self) -> int:
        """A channel that is delayed has no sound until its delay has filled."""
        return int(round(max(self.p["delayl"], self.p["delayr"]) * self.fs / 1000.0))

    def _run(self, x):
        n = x.shape[0]
        x64 = x.astype(np.float64)
        m = self._mat.take(n)
        if m.ndim == 2:
            y = x64 @ m.T
        else:
            y = np.empty_like(x64)
            y[:, 0] = m[:, 0, 0] * x64[:, 0] + m[:, 0, 1] * x64[:, 1]
            y[:, 1] = m[:, 1, 0] * x64[:, 0] + m[:, 1, 1] * x64[:, 1]
        return _f32(self._tap.process(y))


# Echo
@register
class Echo(_Fx):
    """A delay line with feedback. u = in + feedback * LP(v), v = u delayed by D frames, out = in + wet * v, so
    H = 1 + wet z^-D / (1 - feedback LP(z) z^-D). The loop runs in chunks of at most D frames. Ping pong feeds the mono
    sum into the left line and crosses the feedback between the sides."""
    id, name, group = "echo", "Echo", "Time"
    summary = "Repeats the sound after a delay, with feedback and an optional ping pong between the two sides."
    params = (
        Param("delay", "Delay", "float", 350.0, 1.0, 2000.0, 1.0, unit="ms", scale="log"),
        Param("feedback", "Feedback", "float", 35.0, 0.0, 95.0, 1.0, unit="%"),
        Param("highcut", "Feedback high cut", "float", 6000.0, 1000.0, 20000.0, 100.0, unit="Hz", scale="log"),
        Param("wet", "Echo level", "float", 25.0, 0.0, 100.0, 1.0, unit="%"),
        Param("pingpong", "Ping pong", "bool", False),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._dmax = int(round(2.0 * fs))
        self._bank = _Bank(2, self._dmax)
        self._lp = _Cascade(1, 2, fs)
        self._fb, self._wet, self._pp = _Ramp(0.0, self._rl), _Ramp(0.0, self._rl), _Ramp(0.0, self._rl)
        self._d = self._old = 1
        self._xpos = self._rl

    def _apply(self, p):
        first = not self._ready
        d = min(max(int(round(p["delay"] * self.fs / 1000.0)), 1), self._dmax)
        if first:
            self._d = self._old = d
        elif d != self._d:
            self._old, self._d, self._xpos = self._d, d, 0
        self._fb.set(p["feedback"] / 100.0, first)
        self._wet.set(p["wet"] / 100.0, first)
        self._pp.set(1.0 if p["pingpong"] else 0.0, first)
        self._lp.set([_lowpass1(p["highcut"], self.fs)], first)
        self._ready = True

    def reset(self):
        self._bank.reset()
        self._lp.reset()

    def tail(self) -> int:
        """Frames until the last repeat is below -90 dB: the delay times the repeats that are still above that."""
        wet, fb = self.p["wet"] / 100.0, self.p["feedback"] / 100.0
        if wet <= 3.2e-5:
            return 0
        k = 1 if fb <= 0.0 else 1 + max(0, math.ceil(math.log(3.16e-5 / wet) / math.log(fb)))
        return min(self._d * k, 10 * self.fs)

    def _run(self, x):
        n = x.shape[0]
        x64 = x.astype(np.float64)
        wet = np.empty((n, 2))
        i = 0
        while i < n:
            fading = self._xpos < self._rl and self._old != self._d
            k = min(n - i, min(self._d, self._old) if fading else self._d)
            v = self._bank.read(k, self._d)
            if fading:
                old = self._bank.read(k, self._old)
                t = np.minimum((self._xpos + 1 + np.arange(k)) / self._rl, 1.0)[:, None]
                v = old + (v - old) * t
                self._xpos += k
                if self._xpos >= self._rl:
                    self._old = self._d
            f = self._lp.process(v)
            pp, fb = self._pp.take(k), self._fb.take(k)
            inj = x64[i:i + k]
            if not (isinstance(pp, float) and pp == 0.0):
                pp = _col(pp)
                mono = inj.sum(axis=1, keepdims=True) * 0.5
                inj = inj * (1.0 - pp) + np.concatenate((mono, np.zeros_like(mono)), axis=1) * pp
                f = f * (1.0 - pp) + f[:, ::-1] * pp
            self._bank.write(inj + f * _col(fb))
            wet[i:i + k] = v
            i += k
        self._bank.flush(n)
        return _f32(x64 + wet * _col(self._wet.take(n)))


# Reverb
_COMBS = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)   # Freeverb delays at 44.1 kHz
_ALLPASS = (556, 441, 341, 225)
_SPREAD = 23                                                # right channel delays are this much longer
_AP_GAIN = 0.5


@register
class Reverb(_Fx):
    """The Schroeder and Moorer plan as in Freeverb: eight damped feedback combs in parallel for each side, then four
    allpass diffusers in series. The comb feedback is room * 0.28 + 0.7, the damping a first order low pass inside the
    loop, so every loop trip loses less than it gains and the tail always dies out. The diffusers are exact allpasses
    (-g + z^-D) / (1 - g z^-D). The mono sum of the input goes through a pre-delay into both sides."""
    id, name, group = "reverb", "Reverb", "Space"
    summary = "Adds the sound of a room around the music."
    params = (
        Param("room", "Room size", "float", 60.0, 0.0, 100.0, 1.0, unit="%"),
        Param("damp", "Damping", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
        Param("width", "Width", "float", 100.0, 0.0, 100.0, 1.0, unit="%"),
        Param("predelay", "Pre-delay", "float", 10.0, 0.0, 200.0, 1.0, unit="ms"),
        Param("wet", "Wet level", "float", 15.0, 0.0, 100.0, 1.0, unit="%"),
        Param("dry", "Dry level", "float", 100.0, 0.0, 100.0, 1.0, unit="%"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        sc = fs / 44100.0
        self._cd = np.array([max(8, int(round(d * sc))) for d in _COMBS] +
                            [max(8, int(round((d + _SPREAD) * sc))) for d in _COMBS], dtype=np.int64)
        self._apd = [np.array([max(8, int(round(d * sc))), max(8, int(round((d + _SPREAD) * sc)))], dtype=np.int64)
                     for d in _ALLPASS]
        self._combs = _Bank(16, int(self._cd.max()))
        self._aps = [_Bank(2, int(d.max())) for d in self._apd]
        self._lp = _Cascade(1, 16, fs)
        self._pre = _TapDelay(int(round(0.2 * fs)), 1, fs)
        self._fb = _Ramp(0.0, self._rl)
        self._mix = _Ramp(np.zeros(3), self._rl)      # dry, wet for the same side, wet for the other side

    def _apply(self, p):
        first = not self._ready
        self._fb.set(p["room"] / 100.0 * 0.28 + 0.7, first)
        fc = 18000.0 * (2000.0 / 18000.0) ** (p["damp"] / 100.0)
        self._lp.set(np.tile(_lowpass1(fc, self.fs), (1, 1)), first)
        w, wet = p["width"] / 100.0, p["wet"] / 100.0
        self._mix.set([p["dry"] / 100.0, wet * (1.0 + w) / 2.0, wet * (1.0 - w) / 2.0], first)
        self._pre.set([int(round(p["predelay"] * self.fs / 1000.0))], first)
        self._ready = True

    def reset(self):
        self._combs.reset()
        for b in self._aps:
            b.reset()
        self._lp.reset()
        self._pre.reset()

    def _comb_stage(self, m):
        n = m.shape[0]
        out = np.empty((n, 2))
        kmax = int(self._cd.min())
        i = 0
        while i < n:
            k = min(n - i, kmax)
            v = self._combs.read(k, self._cd)
            fb = self._fb.take(k)
            # the input gain keeps the wet level about the same for every room size
            gin = 0.35 * np.sqrt(1.0 - np.square(fb))
            u = m[i:i + k, None] * _col(gin) + self._lp.process(v) * _col(fb)
            self._combs.write(u)
            out[i:i + k] = v.reshape(k, 2, 8).sum(axis=2)
            i += k
        return out

    @staticmethod
    def _allpass(bank, d, x):
        n = x.shape[0]
        y = np.empty_like(x)
        kmax = int(d.min())
        i = 0
        while i < n:
            k = min(n - i, kmax)
            wd = bank.read(k, d)
            w = x[i:i + k] + _AP_GAIN * wd
            bank.write(w)
            y[i:i + k] = wd - _AP_GAIN * w
            i += k
        return y

    def _run(self, x):
        n = x.shape[0]
        x64 = x.astype(np.float64)
        m = self._pre.process(x64.sum(axis=1, keepdims=True) * 0.5)[:, 0]
        c = self._comb_stage(m)
        for bank, d in zip(self._aps, self._apd):
            c = self._allpass(bank, d, c)
        self._combs.flush(n)
        for bank in self._aps:
            bank.flush(n)
        mix = self._mix.take(n)
        if isinstance(mix, np.ndarray) and mix.ndim == 1:
            dry, w1, w2 = mix
        else:
            dry, w1, w2 = mix[..., 0], mix[..., 1], mix[..., 2]
        out = np.empty_like(x64)
        out[:, 0] = x64[:, 0] * dry + c[:, 0] * w1 + c[:, 1] * w2
        out[:, 1] = x64[:, 1] * dry + c[:, 1] * w1 + c[:, 0] * w2
        return _f32(out)
