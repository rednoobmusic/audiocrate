"""Loudness modules for the user DSP chain: Loudness compensation, EBU R128 normalizer, Track normalizer and Dither.

Loudness compensation follows the ISO 226:2003 equal loudness contours. The shape the ear needs at a quiet listening
level, relative to the shape at the reference level, is fitted with one low shelf and one high shelf (a two section
biquad cascade) every time the level changes, and the filter switches to the new coefficients with a 5 ms crossfade.

The normalizer measures loudness after ITU-R BS.1770-4 (K weighting, mean square over 400 ms blocks every 100 ms, gating
at -70 LUFS and 10 LU below the running mean). Everything that depends on the signal happens on a fixed 100 ms grid
counted in samples, so the result does not depend on how the audio is cut into blocks: gain changes ramp in a straight
line across each 100 ms hop.

The Track normalizer is a control surface: the engine measures each song once, whole, and applies one gain from its
first sample on (measure_level and normalizer_gain below do the measuring and the arithmetic), so the module itself
passes the sound through.

Dither adds triangular noise of one step of the target bit depth in front of the sound card's own rounding.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np
from scipy.signal import sosfilt

from dsp_base import DspModule, Param, register


def _in(x):
    """x as float32 with no NaN or infinity (one bad sample would poison every filter memory for good)."""
    if x.dtype != np.float32:
        x = x.astype(np.float32)
    if not np.isfinite(x.sum()):
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x


def _flush(zi: np.ndarray) -> np.ndarray:
    """Filter memory far below anything audible goes to zero so it never decays into slow denormals."""
    zi[np.abs(zi) < 1e-20] = 0.0
    return zi


def _run(sos, zi, x64):
    """x64 (n, 2) float64 through the cascade sos with memory zi. sos None means no filter."""
    if sos is None:
        return x64, None
    y, zf = sosfilt(sos, x64, axis=0, zi=zi.copy())     # scipy may work on zi in place
    return y, _flush(zf)


class _Ramp:
    """A value that glides to a new target in `length` samples, in a straight line, whatever the block size."""

    def __init__(self, value: float, length: int):
        self.v = self.t = float(value)
        self.length = max(1, int(length))
        self.step = 0.0
        self.left = 0

    def to(self, target: float) -> None:
        target = float(target)
        if target != self.t:
            self.t = target
            self.left = self.length
            self.step = (target - self.v) / self.length

    def jump(self, value: float) -> None:
        self.v = self.t = float(value)
        self.left = 0

    def take(self, n: int):
        """The next n values: a float when the ramp is idle, else an array."""
        if self.left == 0:
            return self.v
        m = min(n, self.left)
        out = np.full(n, self.t)
        out[:m] = self.v + self.step * np.arange(1, m + 1)
        self.left -= m
        self.v = self.t if self.left == 0 else float(out[m - 1])
        return out


# Loudness compensation
# ISO 226:2003 table 1: frequency, exponent of loudness perception, threshold of hearing, magnitude of the linear transfer
_ISO_F = np.array([20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600,
                   2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500], dtype=float)
_ISO_A = np.array([.532, .506, .480, .455, .432, .409, .387, .367, .349, .330, .315, .301, .288, .276, .267, .259, .253,
                   .250, .246, .244, .243, .243, .243, .242, .242, .245, .254, .271, .301])
_ISO_LU = np.array([-31.6, -27.2, -23.0, -19.1, -15.9, -13.0, -10.3, -8.1, -6.2, -4.5, -3.1, -2.0, -1.1, -.4, 0, .3, .5,
                    0, -2.7, -4.1, -1.0, 1.7, 2.5, 1.2, -2.1, -7.1, -11.2, -10.7, -3.1])
_ISO_TF = np.array([78.5, 68.7, 59.5, 51.1, 44.0, 37.5, 31.5, 26.5, 22.1, 17.9, 14.4, 11.4, 8.6, 6.2, 4.4, 3.0, 2.2, 2.4,
                    3.5, 1.7, -1.3, -4.2, -6.0, -5.4, -1.5, 6.0, 12.6, 13.9, 12.3])
_LOGF = np.log(_ISO_F)


def iso226_spl(phon, f):
    """Sound pressure level in dB that a pure tone of frequency f needs to sound as loud as `phon` (20 to 90) phon.
    The table ends at 12.5 kHz, so higher frequencies use the 12.5 kHz values. Equation 1 of the standard."""
    lf = np.log(np.minimum(np.maximum(np.asarray(f, float), 20.0), 12500.0))
    af, lu, tf = (np.interp(lf, _LOGF, t) for t in (_ISO_A, _ISO_LU, _ISO_TF))
    a = 4.47e-3 * (10.0 ** (0.025 * phon) - 1.15) + (0.4 * 10.0 ** ((tf + lu) / 10.0 - 9.0)) ** af
    return 10.0 / af * np.log10(a) - lu + 94.0


def shelf_sos(kind: str, f0: float, q: float, gain_db: float, fs: int) -> np.ndarray:
    """One second order shelf (RBJ audio EQ cookbook) as a row of an sos array. kind is "low" or "high"."""
    big_a = 10.0 ** (gain_db / 40.0)
    w = 2.0 * math.pi * f0 / fs
    c, s = math.cos(w), 2.0 * math.sqrt(big_a) * math.sin(w) / (2.0 * q)
    if kind == "low":
        b = (big_a * ((big_a + 1) - (big_a - 1) * c + s), 2 * big_a * ((big_a - 1) - (big_a + 1) * c),
             big_a * ((big_a + 1) - (big_a - 1) * c - s))
        a = ((big_a + 1) + (big_a - 1) * c + s, -2 * ((big_a - 1) + (big_a + 1) * c), (big_a + 1) + (big_a - 1) * c - s)
    else:
        b = (big_a * ((big_a + 1) + (big_a - 1) * c + s), -2 * big_a * ((big_a - 1) + (big_a + 1) * c),
             big_a * ((big_a + 1) + (big_a - 1) * c - s))
        a = ((big_a + 1) - (big_a - 1) * c + s, 2 * ((big_a - 1) - (big_a + 1) * c), (big_a + 1) - (big_a - 1) * c - s)
    return np.array([b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]])


def _sos_db(sos: np.ndarray, f: np.ndarray, fs: int) -> np.ndarray:
    z = np.exp(-2j * math.pi * np.asarray(f, float) / fs)
    h = np.ones(len(z), complex)
    for b0, b1, b2, _, a1, a2 in np.atleast_2d(sos):
        h *= (b0 + b1 * z + b2 * z * z) / (1.0 + a1 * z + a2 * z * z)
    return 20.0 * np.log10(np.abs(h))


_LO_F, _LO_Q = 150.0, 0.5       # where the bass shelf turns, and how gently
_HI_F, _HI_Q = 8000.0, 0.7
_GRID = np.geomspace(30.0, 16000.0, 56)    # frequencies the fit looks at
_FIT_W = 1.0 + 8.0 * np.exp(-np.log2(_GRID / 1000.0) ** 2 / 0.5)    # 1 kHz must stay where it is
_UNIT = 10.0


@register
class LoudnessCompensation(DspModule):
    id, name, group = "loudness", "Loudness compensation", "Tone"
    summary = "Lifts bass and treble when you listen quietly, because the ear hears them less at low volume."
    params = (
        Param("source", "Follow", "choice", "Player volume", choices=("Player volume", "Fixed level")),
        Param("reference", "Reference level", "float", 80.0, 60.0, 90.0, 1.0, unit="phon"),
        Param("level", "Listening level", "float", 60.0, 20.0, 90.0, 1.0, unit="phon"),
        Param("strength", "Strength", "float", 100.0, 0.0, 150.0, 1.0, unit="%"),
        Param("max_lift", "Maximum lift", "float", 4.0, 0.0, 20.0, 0.5, unit="dB"),
    )
    # Reference level is the loudness at full player volume. With Follow set to Player volume, each 6 dB the player
    # volume goes down moves the listening level down by 6 phon. Listening level is only used with Fixed level.

    def active_keys(self) -> tuple:
        fixed = self.get("source") == "Fixed level"
        return ("source", "reference") + (("level",) if fixed else ()) + ("strength", "max_lift")

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self.status = ""
        sl = _sos_db(shelf_sos("low", _LO_F, _LO_Q, _UNIT, fs), _GRID, fs) / _UNIT
        sh = _sos_db(shelf_sos("high", _HI_F, _HI_Q, _UNIT, fs), _GRID, fs) / _UNIT
        self._solve = np.linalg.pinv(np.stack([sl, sh], 1) * _FIT_W[:, None])
        self._xf = max(1, int(fs * 0.005))
        self._ref_curve = np.zeros(len(_GRID))
        self._cache: dict = {}
        self._cur_key = None
        self._sos = None
        self._zi = None
        self.reset()

    def _apply(self, p):
        r = p["reference"]
        self._ref_curve = iso226_spl(r, _GRID) - r
        self._cache = {}

    def reset(self) -> None:
        self._cur_key = None
        self._sos = None
        self._zi = np.zeros((2, 2, 2))

    def level_for(self, volume: float) -> float:
        """The listening level in phon the module compensates for, at this player volume."""
        p = self.p
        if p["source"] == "Fixed level":
            lv = p["level"]
        else:
            lv = p["reference"] + 20.0 * math.log10(min(1.0, max(volume, 1e-3)))
        return min(90.0, max(20.0, lv))

    def lifts(self, volume: float) -> tuple:
        """Gain in dB of the bass shelf and the treble shelf at this player volume."""
        lv = round(self.level_for(volume), 2)
        got = self._cache.get(lv)
        if got is None:
            p = self.p
            want = (iso226_spl(lv, _GRID) - lv - self._ref_curve) * (p["strength"] / 100.0)
            g = np.clip(self._solve @ (want * _FIT_W), -p["max_lift"], p["max_lift"])
            got = (float(g[0]), float(g[1]))
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[lv] = got
        return got

    def _design(self, lo: float, hi: float):
        if abs(lo) < 0.005 and abs(hi) < 0.005:
            return None
        sos = np.array([shelf_sos("low", _LO_F, _LO_Q, lo, self.fs), shelf_sos("high", _HI_F, _HI_Q, hi, self.fs)])
        # the contour is measured against 1 kHz, so keep 1 kHz at 0 dB exactly
        sos[0, :3] *= 10.0 ** (-float(_sos_db(sos, [1000.0], self.fs)[0]) / 20.0)
        return sos

    def response_db(self, f, volume: float = 1.0) -> np.ndarray:
        """Frequency response in dB at the volume given, from the coefficients in use. Used by the panel and tests."""
        sos = self._design(*self.lifts(volume))
        return np.zeros(len(np.atleast_1d(f))) if sos is None else _sos_db(sos, np.atleast_1d(f), self.fs)

    def process(self, x, ctx):
        self.prepare()
        if not len(x):
            return x
        x = _in(x)
        lo, hi = self.lifts(ctx.volume)
        self.status = ("The player volume is at its maximum, so nothing is lifted. Turn the volume down or follow a fixed level."
                       if self.p["source"] == "Player volume" and ctx.volume >= 0.995 else "")
        key = (round(lo, 3), round(hi, 3))
        if key == self._cur_key:
            if self._sos is None:
                return x
            y, self._zi = _run(self._sos, self._zi, x.astype(np.float64))
            return y.astype(np.float32)
        new = self._design(lo, hi)
        x64 = x.astype(np.float64)
        n = len(x64)
        if self._cur_key is None:                      # first block since reset: no old sound to fade from
            y, zf = _run(new, np.zeros((2, 2, 2)), x64)
        else:
            m = min(n, self._xf)
            old = _run(self._sos, self._zi, x64[:m])[0]
            y, zf = _run(new, self._zi if new is not None and self._zi is not None else np.zeros((2, 2, 2)), x64)
            w = (np.arange(1, m + 1) / m)[:, None]
            y = y.copy()
            y[:m] = old * (1.0 - w) + y[:m] * w
        self._cur_key, self._sos = key, new
        self._zi = zf if zf is not None else np.zeros((2, 2, 2))
        return y.astype(np.float32)


# EBU R128 normalizer
def k_weight_sos(fs: int) -> np.ndarray:
    """The two BS.1770-4 filters (high shelf, then the revised low frequency B curve high pass) as two sos rows.
    At 48 kHz this gives the coefficients printed in the standard; other rates use the same analog prototype."""
    f0, g, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / fs)
    vh = 10.0 ** (g / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    pre = [(vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0, 1.0,
           2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / fs)
    a0 = 1.0 + k / q + k * k
    rlb = [1.0, -2.0, 1.0, 1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    return np.array([pre, rlb])


_BINS = 800          # gating histogram: 0.1 LU steps from -70 to +10 LUFS
_OFFSET = -0.691


def _lufs(power: float) -> float:
    return _OFFSET + 10.0 * math.log10(power) if power > 0.0 else -math.inf


class _Meter:
    """BS.1770-4 loudness on a fixed 100 ms grid. scan() filters a block and reports the hops it completed, commit()
    files one finished hop. The two are separate so the normalizer can act on each hop with the state as it was then."""

    def __init__(self, fs: int):
        self.fs = int(fs)
        self.hop = max(1, int(round(0.1 * fs)))
        self.sos = k_weight_sos(fs)
        self.reset()

    def reset(self) -> None:
        self.zi = np.zeros((2, 2, 2))
        self.acc = 0.0              # K weighted power sum in the hop being filled (both channels)
        self.pk = 0.0               # largest input sample in that hop
        self.ph = 0                 # samples into that hop
        self.hops = deque(maxlen=30)    # mean power of the last 3 s of hops, newest last
        self.peaks = deque(maxlen=30)
        self.count = 0              # hops finished since the reset
        self.cnt = np.zeros(_BINS, np.int64)    # histogram of 400 ms gating blocks that passed the -70 LUFS gate
        self.sums = np.zeros(_BINS)             # their power, summed per bin

    def scan(self, x: np.ndarray):
        """x (n, 2) float32. Returns (phase of the hop at the start of x, [(end, power, peak) for every hop finished])."""
        n = len(x)
        y, self.zi = sosfilt(self.sos, x.astype(np.float64), axis=0, zi=self.zi.copy())
        _flush(self.zi)
        ph0 = self.ph
        p = np.einsum("ij,ij->i", y, y)
        peak = np.abs(x).max(axis=1)
        cuts = list(range(self.hop - ph0, n + 1, self.hop))
        edges = [0] + cuts + ([n] if not cuts or cuts[-1] < n else [])
        cs = np.concatenate(([0.0], np.cumsum(p)))
        events = []
        acc, pk = self.acc, self.pk
        for i in range(len(edges) - 1):
            s, e = edges[i], edges[i + 1]
            if e > s:
                acc += cs[e] - cs[s]
                pk = max(pk, float(peak[s:e].max()))
            if i < len(cuts):
                events.append((e, acc / self.hop, pk))
                acc, pk = 0.0, 0.0
        self.acc, self.pk, self.ph = acc, pk, (ph0 + n) % self.hop
        return ph0, events

    def commit(self, power: float, peak: float) -> None:
        self.hops.append(power)
        self.peaks.append(peak)
        self.count += 1
        if self.count >= 4:
            blk = sum(list(self.hops)[-4:]) / 4.0
            if _lufs(blk) >= -70.0:
                i = min(_BINS - 1, int((_lufs(blk) + 70.0) * 10.0))
                self.cnt[i] += 1
                self.sums[i] += blk

    def _window(self, hops: int):
        if self.count < 4:
            return None
        h = list(self.hops)[-hops:]
        v = _lufs(sum(h) / len(h))
        return v if v >= -70.0 else None

    def momentary(self):
        return self._window(4)

    def short_term(self):
        return self._window(30)

    def integrated(self):
        """Gated loudness of everything since the reset: absolute gate at -70 LUFS, relative gate 10 LU below."""
        c, s = self.cnt.sum(), self.sums.sum()
        if c == 0:
            return None
        gate = _lufs(s / c) - 10.0
        lo = max(0, int(math.ceil((gate + 70.0) * 10.0 - 1e-9)))
        c, s = self.cnt[lo:].sum(), self.sums[lo:].sum()
        return _lufs(s / c) if c else None

    def recent_peak(self) -> float:
        return max(self.peaks) if self.peaks else 0.0


def integrated_lufs(x: np.ndarray, fs: int):
    """Integrated loudness in LUFS of a whole (n, 2) or (n,) signal, or None when it is silent. For tests and tools."""
    x = np.asarray(x, np.float32)
    x = np.stack([x, x], 1) if x.ndim == 1 else x
    m = _Meter(fs)
    for _, power, peak in m.scan(x)[1]:
        m.commit(power, peak)
    return m.integrated()


@register
class R128Normalizer(DspModule):
    id, name, group = "r128", "EBU R128 normalizer", "Level"
    summary = ("Measures loudness in LUFS and moves the volume slowly towards a target level. It measures as the song plays, "
               "so it needs a moment to settle. Track normalizer sets the level from the first sample.")
    params = (
        Param("target", "Target loudness", "float", -18.0, -23.0, -8.0, 0.5, unit="LUFS"),
        Param("window", "Measured over", "choice", "Track so far",
              choices=("Momentary (0.4 s)", "Short term (3 s)", "Track so far")),
        Param("smooth", "Smoothing", "float", 5.0, 0.5, 30.0, 0.1, unit="s", scale="log"),
        Param("max_boost", "Maximum boost", "float", 3.0, 0.0, 24.0, 0.5, unit="dB"),
        Param("protect", "Avoid clipping", "bool", True),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self.meter = _Meter(fs)
        self.reset()

    def reset(self) -> None:
        self.meter.reset()
        self._ga = self._gb = 1.0       # amplitude gain at the start and the end of the hop being played
        self._gdb = 0.0                 # the smoothed gain in dB (the value the hop ramp is heading for)
        self._valid = 0                 # hops since the first usable measurement
        self.loudness = None            # last measurement of the chosen window, LUFS
        self.gain_db = 0.0              # gain in force at the end of the last block, dB

    def _measure(self):
        m, w = self.meter, self.p["window"]
        return m.momentary() if w.startswith("Mom") else m.short_term() if w.startswith("Short") else m.integrated()

    def _hop_done(self, power: float, peak: float) -> None:
        p, m = self.p, self.meter
        m.commit(power, peak)
        loud = self._measure()
        self._ga = self._gb
        if loud is None:                 # silence or too little audio: hold the gain where it is
            return
        self.loudness = loud
        want = min(p["max_boost"], p["target"] - loud)
        if p["protect"]:
            pk = m.recent_peak()
            want = min(want, max(0.0, -20.0 * math.log10(pk)) if pk > 1e-6 else want)
        self._valid += 1
        tau = min(p["smooth"], max(0.3, 0.05 * self._valid))      # settles fast at the start, then slowly
        self._gdb += (want - self._gdb) * (1.0 - math.exp(-0.1 / tau))
        self._gb = 10.0 ** (self._gdb / 20.0)

    def process(self, x, ctx):
        self.prepare()
        if not len(x):
            return x
        x = _in(x)
        n, hop = len(x), self.meter.hop
        ph0, events = self.meter.scan(x)
        out = np.empty_like(x)
        pos, start = ph0, 0
        for end, power, peak in events + [(n, None, None)]:
            if end > start:
                if self._ga == self._gb:
                    g = np.float32(self._ga)
                else:
                    g = (self._ga + (self._gb - self._ga) * ((pos + np.arange(end - start)) / hop)).astype(np.float32)[:, None]
                out[start:end] = x[start:end] * g
            pos += end - start
            start = end
            if power is not None:
                pos = 0
                self._hop_done(power, peak)
        self.gain_db = 20.0 * math.log10(self._ga + (self._gb - self._ga) * (pos / hop))
        return out


# Track normalizer
def _gated_lufs(block_power):
    """Integrated loudness from the mean power of each 400 ms block, gated as BS.1770-4 says: blocks under -70 LUFS
    go, then blocks more than 10 LU under the mean of the rest. None when nothing is left. Exact, where the live meter
    above counts blocks in 0.1 LU bins."""
    p = np.asarray(block_power, np.float64)
    with np.errstate(divide="ignore"):
        keep = p[_OFFSET + 10.0 * np.log10(p) >= -70.0] if len(p) else p
        if not len(keep):
            return None
        keep = keep[_OFFSET + 10.0 * np.log10(keep) >= _lufs(float(keep.mean())) - 10.0]
    return _lufs(float(keep.mean())) if len(keep) else None


def measure_level(x, fs, sos=None, gain=1.0, lufs=False, cancel=None):
    """The sample peak (full scale is 1.0) and, when asked, the integrated loudness of a whole track, an (n, 2) int16
    or float32 array, as it is heard through the filter `sos` (second order sections) and the linear `gain`: that is
    how the user's equalizer moves both numbers. It works in pieces of about a second and asks cancel() between them;
    None comes back when that said yes. Otherwise {"peak": float} and, with lufs, "lufs" (a float, or None when the
    track is silent or shorter than one 400 ms block). Uses the K weighting and the 100 ms grid of the live meter."""
    n, ints, filt = len(x), x.dtype == np.int16, sos is not None and len(sos) > 0
    step = max(4096, int(fs))
    zi = np.zeros((len(sos), 2, 2)) if filt else None
    meter = _Meter(fs) if lufs else None
    powers, peak = [], 0.0
    for i in range(0, n, step):
        if cancel is not None and cancel():
            return None
        c = x[i:i + step]
        if ints and meter is None and not filt and gain == 1.0:
            peak = max(peak, max(int(c.max()), -int(c.min())) / 32768.0)
            continue
        f = c.astype(np.float32) * np.float32(1.0 / 32768.0) if ints else c.astype(np.float32, copy=False)
        if filt or gain != 1.0:
            y = f.astype(np.float64)
            if filt:
                y, zi = sosfilt(sos, y, axis=0, zi=zi)
            f = (y * gain).astype(np.float32)
        peak = max(peak, float(np.abs(f).max()))
        if meter is not None:
            powers.extend(power for _, power, _ in meter.scan(f)[1])
    out = {"peak": peak}
    if lufs:
        p = np.asarray(powers)
        out["lufs"] = _gated_lufs((p[3:] + p[2:-1] + p[1:-2] + p[:-3]) / 4.0 if len(p) >= 4 else [])
    return out


_NORMALIZER_DEFAULTS = {"mode": "Peak", "peak_target": 0.0, "loudness_target": -14.0, "direction": "Up and down",
                        "max_boost": 24.0, "protect": True}


def normalizer_gain(settings: dict, peak: float, lufs=None) -> float:
    """The linear gain the Track normalizer gives a song whose sample peak is `peak` (full scale 1.0) and whose
    integrated loudness is `lufs` (None when it could not be measured). 1.0 for a silent song, and in Loudness mode for
    one with no loudness. The gain is rounded to the float32 that multiplies the samples, and one step down when that
    product would land just above the level aimed at, so a peak aimed at 0 dBFS never reads 1.0000001."""
    s = {**_NORMALIZER_DEFAULTS, **settings}
    if not peak > 0.0:
        return 1.0
    peak_db = 20.0 * math.log10(peak)
    if s["mode"] == "Loudness":
        if lufs is None:
            return 1.0
        db = s["loudness_target"] - lufs
        aim = None
        if s["protect"] and db > -peak_db:
            db, aim = -peak_db, 1.0              # the loudest sample ends at 0 dBFS, not above
    else:
        db = s["peak_target"] - peak_db
        aim = 10.0 ** (s["peak_target"] / 20.0)
    if s["direction"] == "Only down":
        db = min(db, 0.0)
    elif s["direction"] == "Only up":
        db = max(db, 0.0)
    db = min(db, s["max_boost"])
    g = np.float32(10.0 ** (db / 20.0))
    p32, a32 = np.float32(peak), np.float32(aim if aim is not None else np.inf)
    for _ in range(3):
        if p32 * g > a32 and p32 * g < a32 * np.float32(1.000001):
            g = np.nextafter(g, np.float32(0.0))
    return float(g)


@register
class TrackNormalizer(DspModule):
    id, name, group = "normalizer", "Track normalizer", "Level"
    summary = ("Sets the level of each song before the other effects, from the song's measured peak or loudness, "
               "from the first sample on.")
    status = "It works on songs the engine plays. Chiptunes played by libgme and videos are not leveled."
    params = (
        Param("mode", "Measure", "choice", "Peak", choices=("Peak", "Loudness"),
              hint="Peak puts the loudest sample of the song at the target. Loudness puts the song's integrated loudness "
                   "(ITU-R BS.1770-4) at the target."),
        Param("peak_target", "Peak target", "float", 0.0, -24.0, 0.0, 0.1, unit="dBFS"),
        Param("loudness_target", "Loudness target", "float", -14.0, -30.0, -6.0, 0.5, unit="LUFS"),
        Param("direction", "Direction", "choice", "Up and down", choices=("Up and down", "Only down", "Only up"),
              hint="Only down never turns a song up, only up never turns one down."),
        Param("max_boost", "Maximum boost", "float", 24.0, 0.0, 30.0, 0.5, unit="dB",
              hint="Stops silence and very quiet songs from being turned up into noise."),
        Param("protect", "Keep peaks under 0 dBFS", "bool", True,
              hint="In Loudness mode a song is not turned up so far that its loudest sample passes 0 dBFS."),
    )

    def active_keys(self) -> tuple:
        """The settings that matter in the mode chosen; the panel hides the others."""
        if self.get("mode") == "Loudness":
            return ("mode", "loudness_target", "direction", "max_boost", "protect")
        return ("mode", "peak_target", "direction", "max_boost")

    def process(self, x, ctx):
        self.prepare()
        return x


# Dither
_SEED = 20261004
_SHAPE = 0.5        # shaped dither is r[k] - 0.5 * r[k-1]: less noise in the bass, more above 10 kHz, still TPDF


@register
class Dither(DspModule):
    id, name, group = "dither", "Dither", "Utility"
    summary = ("Adds very quiet noise of one 16 or 24 bit step so the sound card's rounding turns into faint hiss "
               "instead of distortion on quiet passages.")
    params = (
        Param("depth", "Bit depth", "choice", "16 bit", choices=("16 bit", "24 bit")),
        Param("noise", "Noise", "choice", "TPDF", choices=("TPDF", "Shaped")),
        Param("amount", "Amount", "float", 100.0, 0.0, 200.0, 1.0, unit="%"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        ramp = int(fs * 0.005)
        self._amp = _Ramp(0.0, ramp)
        self._shape = _Ramp(0.0, ramp)
        self._first = True
        self.enabled = False            # off until the user switches it on
        self.reset()

    def _apply(self, p):
        amp = p["amount"] / 100.0 * 2.0 ** (-15 if p["depth"] == "16 bit" else -23)
        shape = _SHAPE if p["noise"] == "Shaped" else 0.0
        if self._first:
            self._amp.jump(amp)
            self._shape.jump(shape)
            self._first = False
        else:
            self._amp.to(amp)
            self._shape.to(shape)

    def reset(self) -> None:
        self._rng = np.random.Generator(np.random.PCG64(_SEED))
        self._prev = np.zeros(2, np.float32)

    def process(self, x, ctx):
        self.prepare()
        if not len(x):
            return x
        x = _in(x)
        n = len(x)
        a, c = self._amp.take(n), self._shape.take(n)
        if not isinstance(a, np.ndarray) and a == 0.0:
            return x
        # four numbers per frame in a fixed order, so any cutting of the stream draws the same noise
        r = self._rng.random((n, 4), dtype=np.float32)
        t = np.stack([r[:, 0] + r[:, 1], r[:, 2] + r[:, 3]], 1) - np.float32(1.0)
        if isinstance(c, np.ndarray) or c != 0.0:
            before = np.concatenate([self._prev[None, :], t[:-1]])
            cc = c[:, None] if isinstance(c, np.ndarray) else c
            nz = t - np.asarray(cc, np.float32) * before
        else:
            nz = t
        self._prev = t[-1].copy()
        aa = a[:, None] if isinstance(a, np.ndarray) else a
        return x + nz * np.asarray(aa, np.float32)
