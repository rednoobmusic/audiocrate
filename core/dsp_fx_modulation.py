"""Chorus, Flanger and Phaser for the user DSP chain.

Every module works on whole blocks with numpy and keeps its memory between blocks, so the output does not depend on how
the signal is cut into blocks. There is no loop over samples.

Chorus and flanger read a delay line at a position that moves with an LFO, with a 4 point Lagrange interpolator (a
fractional delay). The delay line is the last H input samples glued in front of the block and every read of a block is
one gather. The LFO phase is carried from block to block. Both have a gentle low cut (first order, bilinear) on the wet
path only, so the delayed copy does not pile onto the bass.

Flanger feedback needs the output of earlier samples. A read at delay d only needs samples older than d, so the block
is cut into pieces that are each shorter than the shortest delay inside them. Inside a piece every sample it reads is
already known, so a piece is one gather and one multiply (a few dozen pieces per 10 ms block, not one per sample).

The phaser is a chain of first order allpass sections whose coefficient follows the LFO sample by sample, closed by the
feedback loop y = A(x + g z^-1 y). Written in the signals at the section boundaries (u_i[n] = a (u_(i-1)[n] - u_i[n-1]) +
u_(i-1)[n-1], u_0[n] = x[n] + g y[n-1]) the whole block is one linear system that is lower triangular with a band of
N + 2 diagonals, and forward substitution on it is the sample by sample recursion itself. BLAS solves it in one call
(dtbsv), so the loop is exact for every feedback value and every coefficient trajectory, and the memory that crosses a
block edge is the signals themselves, which do not depend on the coefficients. The stage count is a set of tap weights on
the section outputs that glide, so a change of stages (even a fast series of them) is a blend from the current state.
"""
from __future__ import annotations

from bisect import bisect_left

import numpy as np
from scipy.linalg import blas, lapack, solve_banded
from scipy.signal import lfilter, lfilter_zi

from dsp_base import DspModule, Param, finite, register

TINY = 1e-20        # values below this are flushed to zero in recursive paths
_TWO_PI = 2.0 * np.pi
_OFF4 = np.array([-1, 0, 1, 2], dtype=np.intp)


def _f32(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float32)


def _by_blas(k, ab, b):
    return blas.dtbsv(k, ab, b, lower=1, overwrite_x=1)


def _by_lapack(k, ab, b):
    return lapack.dtbtrs(ab, b[:, None], uplo="L", overwrite_b=1)[0][:, 0]


def _by_banded(k, ab, b):
    """The general banded solver: exact, but several times slower than the two triangular ones."""
    return solve_banded((k, 0), ab, b)


def _pick_solver():
    """The lower triangular band solve of BLAS (dtbsv), or of LAPACK (dtbtrs) if that one fails, both public scipy
    functions that cost about as much as the rest of the block. Each is tried on a small system against the general
    banded solver before it is used."""
    rng = np.random.default_rng(3)
    k, n = 6, 70
    ab = rng.standard_normal((n, k + 1)).T * 0.3
    ab[0] = 1.0
    b = rng.standard_normal(n)
    want = solve_banded((k, 0), ab, b)
    for f in (_by_blas, _by_lapack):
        try:
            if np.allclose(f(k, ab, b.copy()), want, rtol=1e-9, atol=1e-9):
                return f
        except Exception:
            pass
    return _by_banded


_solve = _pick_solver()


def _wave(ph, tri):
    """LFO value in -1..1 for a phase in cycles. tri is the share of triangle in the mix (0 sine, 1 triangle)."""
    s = np.sin(ph * _TWO_PI)
    if np.any(tri > 1e-6):
        t = 4.0 * np.abs(((ph + 0.75) % 1.0) - 0.5) - 1.0
        s = s + tri * (t - s)
    return s


def _weights(pos, T):
    """Taps and Lagrange weights for read positions pos (..., 2), the last axis is the channel.

    Returns flat indexes (..., 4, 2) into a buffer that holds the left channel in [0, T) and the right in [T, 2T),
    and float32 weights of the same shape."""
    i = np.floor(pos)
    f = pos - i
    i = i.astype(np.intp)
    f1 = f + 1.0
    f2 = f - 1.0
    f3 = f - 2.0
    w = np.stack((-f * f2 * f3 / 6.0, f1 * f2 * f3 / 2.0, -f1 * f * f3 / 2.0, f1 * f * f2 / 6.0), axis=-2)
    off = _OFF4[:, None] + np.array([0, T], dtype=np.intp)[None, :]
    return i[..., None, :] + off, w.astype(np.float32)


class _Slew:
    """Two cascaded one pole smoothers on a vector of settings. The start is flat (no kink), so a jump in delay or
    centre frequency does not make a click. Exact per sample, so it does not depend on the block size."""

    def __init__(self, fs: int, tau: float = 0.015):
        c = float(np.exp(-1.0 / (tau * fs)))
        self.b = [(1.0 - c) ** 2]
        self.a = [1.0, -2.0 * c, c * c]
        self.zu = lfilter_zi(self.b, self.a)[:, None]
        self.z = None
        self.tgt = None

    def target(self, vals) -> None:
        self.tgt = np.asarray(vals, dtype=np.float64)
        if self.z is None:
            self.snap()

    def snap(self) -> None:
        if self.tgt is not None:
            self.z = self.zu * self.tgt[None, :]

    def ramp(self, n: int) -> np.ndarray:
        """(n, K) values for the next n samples."""
        tgt = self.tgt
        want = self.zu * tgt[None, :]
        full = np.broadcast_to(tgt, (n, tgt.size))
        if np.all(np.abs(self.z - want) <= 1e-9 * (1.0 + np.abs(want))):
            self.z = want
            return full
        y, self.z = lfilter(self.b, self.a, full, axis=0, zi=self.z)
        return y


def _mix_gains(m):
    """Dry and wet gain for a mix of 0..1 (array). A plain crossfade sounds about 3 dB quieter in the middle on music
    (the two paths do not add up in phase), so half of that is made up: at 50 percent broadband material drops about
    1.5 dB and nothing rises more than about 3 dB."""
    mk = ((1.0 - m) ** 2 + m ** 2) ** -0.25
    return (1.0 - m) * mk, m * mk


class _Mod(DspModule):
    """What the three modules share: the LFO, the smoothing, the sample rate and the safety net. NaN and infinity in the
    input count as zero; a block that still comes out with NaN or infinity clears the memory and is replaced by silence."""

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._build(self.fs)

    def _build(self, fs: int) -> None:
        self.fs = int(fs)
        self._slew = _Slew(self.fs)
        self._ph = 0.0
        self._inc = 0.5 / self.fs
        self._ar = np.arange(65536 + 8, dtype=np.float64)
        self._init_state()
        self._init_hp()
        self._dirty = True

    def _arange(self, n: int) -> np.ndarray:
        if n > self._ar.size:
            self._ar = np.arange(n + 8, dtype=np.float64)
        return self._ar[:n]

    def _init_state(self) -> None:
        pass

    def _init_hp(self) -> None:
        self._hpx = np.zeros(2)
        self._hpy = np.zeros(2)

    def _rate(self, p) -> None:
        self._inc = float(p["rate"]) / self.fs

    def _lfo_phase(self, n: int) -> np.ndarray:
        ph = self._ph + self._inc * self._arange(n)
        self._ph = (self._ph + self._inc * n) % 1.0
        return ph

    def reset(self) -> None:
        self._ph = 0.0
        self._slew.snap()
        self._init_state()
        self._init_hp()

    def _lowcut(self, wet, lc):
        """High pass on the wet path, first order and bilinear, so the pass band is exactly flat and the corner is the
        setting. lc is log2 of the corner in Hz for every sample. y[n] = c y[n-1] + (1 + c) / 2 (x[n] - x[n-1]) with the
        coefficient following the setting sample by sample is a bidiagonal system, solved the way the phaser's is."""
        n = wet.shape[0]
        wet = wet.astype(np.float64)
        t = np.tan((np.pi / self.fs) * np.exp2(lc))
        c = (1.0 - t) / (1.0 + t)
        ab = np.zeros((2, n, 2))                    # both channels one after the other, nothing couples them
        ab[:, :, 0] = 1.0
        ab[:, :n - 1, 1] = -c[1:]
        prev = np.empty_like(wet)
        prev[0] = self._hpx
        prev[1:] = wet[:-1]
        b = np.ascontiguousarray(((0.5 + 0.5 * c)[:, None] * (wet - prev)).T)
        b[:, 0] += c[0] * self._hpy
        y = _solve(1, ab.reshape(2 * n, 2).T, b.reshape(-1)).reshape(2, n).T
        self._hpx = wet[-1].copy()
        self._hpy = y[-1].copy()
        self._hpx[np.abs(self._hpx) < TINY] = 0.0
        self._hpy[np.abs(self._hpy) < TINY] = 0.0
        return y

    def process(self, x, ctx):
        if ctx is not None and ctx.fs and int(ctx.fs) != self.fs:
            self._build(int(ctx.fs))
        if self._dirty:
            self.prepare()
        n = x.shape[0]
        if n == 0:
            return x
        y = self._run(finite(x))
        if not np.isfinite(y).all():
            self.reset()
            return np.zeros((n, 2), np.float32)
        y[np.abs(y) < TINY] = 0.0
        return y

    def _run(self, x):
        return x


def _delay_ring(fs: int, ms: float) -> int:
    return int(np.ceil(ms * 1e-3 * fs)) + 8


# Chorus
_VPH = np.array([0.0, 0.5, 0.25, 0.75, 0.125, 0.625])      # LFO phase of each voice, nested so more voices only add
_VSC = np.array([1.0, 0.92, 1.08, 0.96, 1.04, 0.88])       # each voice's own delay, so they do not line up as one comb
_MAXV = 6
_LOWCUT = 80.0      # Hz, where the wet path starts to roll off (6 dB per octave)


@register
class Chorus(_Mod):
    id, name, group = "chorus", "Chorus", "Time"
    summary = "Thickens the sound with several slightly detuned, slowly moving copies."
    params = (
        Param("voices", "Voices", "int", 3, 1, _MAXV, 1),
        Param("rate", "Rate", "float", 0.8, 0.05, 8.0, 0.01, unit="Hz", scale="log"),
        Param("depth", "Depth", "float", 2.5, 0.0, 10.0, 0.1, unit="ms"),
        Param("delay", "Delay", "float", 15.0, 3.0, 40.0, 0.1, unit="ms"),
        Param("shape", "Wave", "choice", "Sine", choices=("Sine", "Triangle")),
        Param("spread", "Stereo spread", "float", 100.0, 0.0, 100.0, 1.0, unit="%"),
        Param("lowcut", "Low cut", "float", _LOWCUT, 20.0, 500.0, 1.0, unit="Hz", scale="log"),
        Param("mix", "Mix", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
    )

    def _init_state(self) -> None:
        self._H = _delay_ring(self.fs, 40.0 * 1.08 + 10.0)
        self._hist = np.zeros((2, self._H), dtype=np.float32)

    def warmup(self) -> int:
        return int((self.p["delay"] + self.p["depth"]) * 1e-3 * self.fs) + 5 if self.p["mix"] > 0.0 else 0

    def _apply(self, p) -> None:
        fs = self.fs
        v = int(p["voices"])
        vg = np.zeros(_MAXV)
        vg[:v] = v ** -0.75
        self._slew.target(np.concatenate((
            [p["delay"] * 1e-3 * fs, p["depth"] * 1e-3 * fs, p["mix"] / 100.0, p["spread"] / 200.0,
             1.0 if p["shape"] == "Triangle" else 0.0, np.log2(p["lowcut"])], vg)))
        self._rate(p)

    def _run(self, x):
        n = x.shape[0]
        H, T = self._H, self._H + n
        sm = self._slew.ramp(n)
        vg = sm[:, 6:]
        active = np.nonzero((vg > 1e-6).any(axis=0))[0]
        nv = int(active[-1]) + 1 if active.size else 1
        ar = self._arange(n)
        ph = self._lfo_phase(n)
        phase = np.empty((n, nv, 2))
        phase[:, :, 0] = ph[:, None] + _VPH[:nv]
        phase[:, :, 1] = phase[:, :, 0] + sm[:, 3, None]
        lfo = _wave(phase, sm[:, 4, None, None])
        d = sm[:, 0, None, None] * _VSC[None, :nv, None] + sm[:, 1, None, None] * lfo
        np.clip(d, 2.5, H - 4.0, out=d)
        pos = (H + ar)[:, None, None] - d

        flat = np.empty(2 * T, dtype=np.float32)
        buf = flat.reshape(2, T)
        buf[:, :H] = self._hist
        buf[:, H:] = x.T
        idx, w = _weights(pos, T)
        w *= vg[:, :nv, None, None].astype(np.float32)
        wet = self._lowcut(np.einsum("nvkc,nvkc->nc", flat.take(idx), w), sm[:, 5])
        self._hist[:] = buf[:, T - H:]
        dry, wg = _mix_gains(sm[:, 2, None])
        return _f32(dry * x + wg * wet)


# Flanger
_FB_CLIP = 64.0


@register
class Flanger(_Mod):
    id, name, group = "flanger", "Flanger", "Time"
    summary = "A short delay swept up and down, with feedback, for the jet sweep and hollow comb sound."
    params = (
        Param("rate", "Rate", "float", 0.3, 0.02, 10.0, 0.01, unit="Hz", scale="log"),
        Param("delay", "Delay", "float", 2.0, 0.3, 10.0, 0.01, unit="ms", scale="log"),
        Param("depth", "Depth", "float", 70.0, 0.0, 95.0, 1.0, unit="%"),
        Param("feedback", "Feedback", "float", 30.0, -95.0, 95.0, 1.0, unit="%"),
        Param("shape", "Wave", "choice", "Sine", choices=("Sine", "Triangle")),
        Param("spread", "Stereo spread", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
        Param("lowcut", "Low cut", "float", _LOWCUT, 20.0, 500.0, 1.0, unit="Hz", scale="log"),
        Param("mix", "Mix", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
    )

    def _init_state(self) -> None:
        self._H = _delay_ring(self.fs, 10.0 * 1.95)
        self._hist = np.zeros((2, self._H), dtype=np.float32)
        self._dmin = max(4.0, 1.5e-4 * self.fs)

    def warmup(self) -> int:
        return int(self.p["delay"] * (1.0 + self.p["depth"] / 100.0) * 1e-3 * self.fs) + 5 if self.p["mix"] > 0.0 else 0

    def _apply(self, p) -> None:
        self._slew.target([p["delay"] * 1e-3 * self.fs, p["depth"] / 100.0, p["feedback"] / 100.0, p["mix"] / 100.0,
                           p["spread"] / 200.0, 1.0 if p["shape"] == "Triangle" else 0.0, np.log2(p["lowcut"])])
        self._rate(p)

    def _run(self, x):
        n = x.shape[0]
        H, T = self._H, self._H + n
        sm = self._slew.ramp(n)
        ar = self._arange(n)
        ph = self._lfo_phase(n)
        phase = np.empty((n, 2))
        phase[:, 0] = ph
        phase[:, 1] = ph + sm[:, 4]
        lfo = _wave(phase, sm[:, 5, None])
        d = sm[:, 0, None] * (1.0 + sm[:, 1, None] * lfo)
        np.clip(d, self._dmin, H - 4.0, out=d)
        pos = (H + ar)[:, None] - d
        idx, w = _weights(pos, T)

        flat = np.empty(2 * T, dtype=np.float32)
        buf = flat.reshape(2, T)
        buf[:, :H] = self._hist
        buf[:, H:] = x.T
        g = sm[:, 2]
        if np.any(np.abs(g) > 1e-5):
            self._feedback(flat, buf, x, idx, w, g, H, n)
        wet = self._lowcut(np.einsum("nkc,nkc->nc", flat.take(idx), w), sm[:, 6])
        self._hist[:] = buf[:, T - H:]
        dry, wg = _mix_gains(sm[:, 3, None])
        wg = wg * np.sqrt(1.0 - np.abs(g))[:, None]
        return _f32(dry * x + wg * wet)

    @staticmethod
    def _feedback(flat, buf, x, idx, w, g, H, n):
        """Run s = x + g * read(s) piece by piece. buf[:, H:] is overwritten with s."""
        wg = w * g[:, None, None].astype(np.float32)
        T = buf.shape[1]
        # highest sample index each read touches, as a position inside its own channel row, running maximum over the block
        hi = np.maximum.accumulate(np.max(idx[:, 3, :] - np.array([0, T]), axis=1)).tolist()
        bounds = [0]
        a = 0
        while a < n:
            b = bisect_left(hi, H + a)
            a = b if b > a else a + 1
            bounds.append(a)
        for k in range(len(bounds) - 1):
            a, b = bounds[k], bounds[k + 1]
            seg = np.einsum("lkc,lkc->lc", flat.take(idx[a:b]), wg[a:b])
            seg += x[a:b]
            np.clip(seg, -_FB_CLIP, _FB_CLIP, out=seg)
            buf[:, H + a:H + b] = seg.T
        s = buf[:, H:]
        s[np.abs(s) < TINY] = 0.0


# Phaser
_MAXST = 12
_CHUNK = 1024       # frames per banded solve, keeps the matrix a few megabytes


@register
class Phaser(_Mod):
    id, name, group = "phaser", "Phaser", "Time"
    summary = "Sweeps a set of notches up and down the spectrum with a chain of allpass filters."
    params = (
        Param("stages", "Stages", "int", 6, 2, _MAXST, 1),
        Param("rate", "Rate", "float", 0.4, 0.02, 10.0, 0.01, unit="Hz", scale="log"),
        Param("depth", "Depth", "float", 60.0, 0.0, 100.0, 1.0, unit="%"),
        Param("centre", "Center frequency", "float", 700.0, 100.0, 5000.0, 1.0, unit="Hz", scale="log"),
        Param("feedback", "Feedback", "float", 30.0, -95.0, 95.0, 1.0, unit="%"),
        Param("shape", "Wave", "choice", "Sine", choices=("Sine", "Triangle")),
        Param("spread", "Stereo spread", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
        Param("mix", "Mix", "float", 50.0, 0.0, 100.0, 1.0, unit="%"),
    )

    def _build(self, fs: int) -> None:
        self._tap = _Slew(int(fs), 0.005)      # tap weights glide faster than the other settings
        super()._build(fs)

    def _init_state(self) -> None:
        self._s = np.zeros((2, _MAXST + 1))    # the signal at every section boundary, one sample ago
        self._yp = np.zeros(2)                 # the wet output one sample ago
        self._mats = {}

    def reset(self) -> None:
        super().reset()
        self._tap.snap()

    def _apply(self, p) -> None:
        tap = np.zeros(_MAXST + 1)
        tap[int(p["stages"])] = 1.0
        self._tap.target(tap)
        self._slew.target([np.log2(p["centre"]), 2.0 * p["depth"] / 100.0, p["feedback"] / 100.0, p["mix"] / 100.0,
                           p["spread"] / 200.0, 1.0 if p["shape"] == "Triangle" else 0.0])
        self._rate(p)

    def _matrix(self, C):
        """[band matrix, taps written into it] for C frames, both channels, with the entries that never change in place."""
        ent = self._mats.get(C)
        if ent is None:
            if len(self._mats) > 2:
                self._mats.clear()
            M, k = _MAXST + 1, _MAXST + 2
            ab = np.zeros((2, C, M, k + 1))     # LAPACK band storage by columns: ab[.., d] = A[column + d, column]
            ab[..., 0] = 1.0
            ab[:, :C - 1, :_MAXST, _MAXST + 2] = -1.0
            ent = self._mats[C] = [ab, ()]
        return ent

    def _chunk(self, x, a, g, tp):
        """The loop for one piece of the block, both channels in one solve. x (C, 2), a (C, 2) allpass coefficient, g (C,)
        feedback, tp (C, N + 1) tap weights. Unknowns are the section boundary signals u_0..u_N of every frame; row (n, i)
        of the system is the section equation and row (n, 0) is the loop closure."""
        C, N = x.shape[0], _MAXST
        M, k = N + 1, N + 2
        ent = self._matrix(C)
        ab = ent[0]
        ab[:, :, :N, 1] = -a.T[:, :, None]
        if C > 1:
            ab[:, :C - 1, 1:, N + 1] = a.T[:, 1:, None]
            live = np.nonzero(tp.any(axis=0))[0]
            for i in ent[1]:
                if i not in live:
                    ab[:, :C - 1, i, N + 1 - i] = 0.0
            ab[:, :C - 1, live, N + 1 - live] = -(g[1:, None] * tp[:C - 1, live])[None]
            ent[1] = tuple(live.tolist())
        b = np.zeros((2, C, M))
        b[:, :, 0] = x.T
        b[:, 0, 0] += g[0] * self._yp
        s = self._s
        b[:, 0, 1:] = -a[0][:, None] * s[:, 1:] + s[:, :N]
        u = _solve(k, ab.reshape(2 * C * M, k + 1).T, b.reshape(-1)).reshape(2, C, M)
        y = np.einsum("ti,cti->tc", tp, u)
        self._s = u[:, -1].copy()
        self._yp = y[-1].copy()
        return y

    def _run(self, x):
        n = x.shape[0]
        fs = self.fs
        sm = self._slew.ramp(n)
        tp = self._tap.ramp(n)
        ph0 = self._ph
        self._lfo_phase(n)
        phase = np.empty((n, 2))
        phase[:, 0] = ph0 + self._inc * self._arange(n)
        phase[:, 1] = phase[:, 0] + sm[:, 4]
        f = np.exp2(sm[:, 0, None] + sm[:, 1, None] * _wave(phase, sm[:, 5, None]))
        np.clip(f, 30.0, 0.45 * fs, out=f)
        t = np.tan((np.pi / fs) * f)
        a = (t - 1.0) / (t + 1.0)
        g = sm[:, 2]

        y = np.empty((n, 2))
        cuts = (np.arange(-(-n // _CHUNK) + 1) * n // (-(-n // _CHUNK))).tolist()      # equal pieces, so the matrices are reused
        for c0, c1 in zip(cuts[:-1], cuts[1:]):
            y[c0:c1] = self._chunk(x[c0:c1], a[c0:c1], g[c0:c1], tp[c0:c1])
        self._s[np.abs(self._s) < TINY] = 0.0
        self._yp[np.abs(self._yp) < TINY] = 0.0
        dry, wg = _mix_gains(sm[:, 3, None])
        wg = wg * np.sqrt(1.0 - np.abs(g))[:, None]
        return _f32(dry * x + wg * y)
