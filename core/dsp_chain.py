"""Real-time port of the website's Web Audio enhancer (site/src/lib/audioEnhancer.ts).

Plain numpy/scipy, run per buffer inside the audio engine with the filter state carried across buffers.

  ENABLED (wet):
    EQ chain   : lowshelf 90 Hz, peak 280, peak 4.2k, peak 6k, highshelf 9.5k, highshelf 14k
    + parallel : splash exciter (8k), air exciter (12k), bass body, sub-harmonic,
                 mid saturation         (each tanh/abs shaped, summed in)
    + M/S widener (mono below 120 Hz, sides widened above)
    + soft limiter
  DISABLED: true bypass (raw audio), matching the on/off button.

The site's per-track "adaptive gain" is done by measuring the high/low spectral ratio once on load (the site
measures it live every 800 ms) and picking the same tiers.
"""
from __future__ import annotations
import numpy as np
from scipy.signal import lfilter

_FS = 44100


# biquad coefficients (Web Audio BiquadFilterNode / RBJ cookbook)

def _biquad(kind: str, f0: float, q: float, gain_db: float, fs: int = _FS):
    w0 = 2.0 * np.pi * f0 / fs
    cw, sw = np.cos(w0), np.sin(w0)
    A = 10.0 ** (gain_db / 40.0)

    if kind in ("lowshelf", "highshelf"):
        # alpha comes from Q instead of the shelf slope S: q = 1/sqrt(2) is the classic S=1
        # shelf, a higher Q makes the transition steeper.
        alpha = sw / (2.0 * q)
        sa = 2.0 * np.sqrt(A) * alpha
        if kind == "lowshelf":
            b0 =      A * ((A + 1) - (A - 1) * cw + sa)
            b1 =  2 * A * ((A - 1) - (A + 1) * cw)
            b2 =      A * ((A + 1) - (A - 1) * cw - sa)
            a0 =          (A + 1) + (A - 1) * cw + sa
            a1 =     -2 * ((A - 1) + (A + 1) * cw)
            a2 =          (A + 1) + (A - 1) * cw - sa
        else:
            b0 =      A * ((A + 1) + (A - 1) * cw + sa)
            b1 = -2 * A * ((A - 1) + (A + 1) * cw)
            b2 =      A * ((A + 1) + (A - 1) * cw - sa)
            a0 =          (A + 1) - (A - 1) * cw + sa
            a1 =      2 * ((A - 1) - (A + 1) * cw)
            a2 =          (A + 1) - (A - 1) * cw - sa
    else:
        alpha = sw / (2.0 * q)
        if kind == "peaking":
            b0 = 1 + alpha * A
            b1 = -2 * cw
            b2 = 1 - alpha * A
            a0 = 1 + alpha / A
            a1 = -2 * cw
            a2 = 1 - alpha / A
        elif kind == "lowpass":
            b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2
            a0 = 1 + alpha;    a1 = -2 * cw; a2 = 1 - alpha
        elif kind == "highpass":
            b0 = (1 + cw) / 2; b1 = -(1 + cw); b2 = (1 + cw) / 2
            a0 = 1 + alpha;    a1 = -2 * cw;   a2 = 1 - alpha
        elif kind == "notch":
            b0 = 1;            b1 = -2 * cw;   b2 = 1
            a0 = 1 + alpha;    a1 = -2 * cw;   a2 = 1 - alpha
        else:
            raise ValueError(kind)

    b = np.array([b0, b1, b2], dtype=np.float64) / a0
    a = np.array([1.0, a1 / a0, a2 / a0], dtype=np.float64)
    return b, a


class _BQ:
    """Stereo biquad whose filter state carries over from one buffer to the next."""

    def __init__(self, kind, f0, q=0.707, gain_db=0.0, fs=_FS):
        self._fs = fs
        self.b, self.a = _biquad(kind, f0, q, gain_db, fs)
        self._zi = None  # shape (2, nchan)

    def set_gain(self, gain_db, kind, f0, q=0.707):
        self.b, self.a = _biquad(kind, f0, q, gain_db, self._fs)

    def set_params(self, kind, f0, q, gain_db):
        # Recompute coefficients but keep filter state (no click on edit).
        self.b, self.a = _biquad(kind, f0, q, gain_db, self._fs)

    def process(self, x: np.ndarray) -> np.ndarray:
        # x: (N, C)
        if self._zi is None or self._zi.shape[1] != x.shape[1]:
            self._zi = np.zeros((2, x.shape[1]), dtype=np.float64)
        y, self._zi = lfilter(self.b, self.a, x, axis=0, zi=self._zi)
        return y

    def reset(self):
        self._zi = None


def _sat(x: np.ndarray, drive: float) -> np.ndarray:
    """tanh saturation curve, matching makeSatCurve (domain clamped to [-1,1])."""
    xc = np.clip(x, -1.0, 1.0)
    return np.tanh(xc * drive) / np.tanh(drive)


# adaptive gain: per-track high/low spectral ratio (mirrors MiniPlayer)

def high_ratio(mono: np.ndarray, fs: int = _FS) -> float:
    """Reproduce MiniPlayer's adaptAnalyser ratio: fftSize 512, byte-scaled
    magnitudes, lo = bins 2..10, hi = bins 46..140, ratio = hiAvg/(loAvg+1)."""
    n = 512
    if mono.size < n:
        return 0.4
    win = np.hanning(n).astype(np.float64)
    hop = n // 2
    frames = 1 + (mono.size - n) // hop
    frames = min(frames, 4000)
    # A few big FFT calls instead of up to 4000 small ones. This runs on a worker
    # thread at the start of every track (and again for the preloaded next one),
    # and a loop of tiny numpy calls takes the interpreter lock back after each,
    # which is what the GUI and the audio feed then wait behind.
    view = np.lib.stride_tricks.sliding_window_view(mono, n)[::hop][:frames]
    acc = np.zeros(n // 2 + 1)
    for i in range(0, frames, 500):
        acc += np.abs(np.fft.rfft(view[i:i + 500] * win, axis=1)).sum(axis=0) / n
    mag = acc / frames
    db = 20.0 * np.log10(mag + 1e-12)
    byt = np.clip(255.0 * (db + 100.0) / 70.0, 0.0, 255.0)  # [-100,-30] dB -> [0,255]
    lo = byt[2:11].sum() / 9.0
    hi = byt[46:141].sum() / 95.0
    return float(hi / (lo + 1.0))


class Enhancer:
    """Real-time port of createEnhancer(). process() passes the block through when disabled and runs the
    full wet graph when enabled."""

    def __init__(self, fs: int = _FS):
        self._fs = fs
        self._enabled = False

        # EQ chain: the low shelf at 90 Hz lifts bass body and not just sub, the warmth band adds
        # low-mid body, and the high shelves are eased so air does not swamp the mids and bass.
        self._subShelf  = _BQ("lowshelf", 90, gain_db=2.5, fs=fs)
        self._warmth    = _BQ("peaking", 280, 0.7, 2.0, fs)   # low-mid body
        self._presence  = _BQ("peaking", 4200, 1.1, 2.5, fs)
        self._definition = _BQ("peaking", 6000, 1.4, 2.0, fs)
        self._shelf     = _BQ("highshelf", 9500, gain_db=4.5, fs=fs)
        self._crystal   = _BQ("highshelf", 14000, gain_db=3.0, fs=fs)

        # Splash exciter (8 kHz)
        self._splashHpf = _BQ("highpass", 8000, 0.5, fs=fs)
        self._splashLp  = _BQ("lowpass", 16000, 0.5, fs=fs)
        self._splashLevel = 0.10

        # Air exciter (12 kHz)
        self._airHpf    = _BQ("highpass", 12000, 0.4, fs=fs)
        self._airShelf  = _BQ("highshelf", 13000, gain_db=4.5, fs=fs)
        self._airLevel  = 0.09

        # Bass body exciter (60-200 -> tanh -> 900 LP)
        self._bassHpf   = _BQ("highpass", 60, 0.5, fs=fs)
        self._bassLpf   = _BQ("lowpass", 200, 0.5, fs=fs)
        self._bassOutLp = _BQ("lowpass", 900, 0.5, fs=fs)
        self._bassLevel = 0.07

        # Sub-harmonic synth (40-80 -> rectify -> 80-280 BP)
        self._subHpf    = _BQ("highpass", 40, 0.5, fs=fs)
        self._subLpf    = _BQ("lowpass", 80, 0.5, fs=fs)
        self._subBpHp   = _BQ("highpass", 80, 0.8, fs=fs)
        self._subBpLp   = _BQ("lowpass", 280, 0.8, fs=fs)
        self._subHarmLevel = 0.04

        # Mid saturation (mono 300-4k -> tanh)
        self._midBpHp   = _BQ("highpass", 300, 0.6, fs=fs)
        self._midBpLp   = _BQ("lowpass", 4000, 0.6, fs=fs)
        self._midLevel  = 0.08

        # Stereo widener. It works on the side channel only; the full-range mid passes
        # untouched, so no LP/HP crossover notch eats the bass and body. High-passing
        # the side makes the lows mono and a small boost widens the highs.
        self._XO = 120
        self._W = 0.35
        self._sideHp = _BQ("highpass", self._XO, 0.707, fs=fs)


        # Wet-path headroom: the EQ chain and exciters add about 4-5 dB of energy, which
        # drove loud masters into the limiter on every block (audible as constant
        # clipping). The trim keeps the limiter a rare safety net.
        self._wet_trim = 10.0 ** (-4.5 / 20.0)

        # Safety limiter, only for peaks near 0 dBFS. Instant attack, ~80 ms release (a fast
        # release acts like a per-sample waveshaper and creates distortion).
        self._lim_thr = 10.0 ** (-1.0 / 20.0)    # ~-1 dBFS
        self._lim_ratio = 20.0
        rel = np.exp(-1.0 / (0.080 * fs))
        self._lim_b = np.array([1.0 - rel])
        self._lim_a = np.array([1.0, -rel])
        self._lim_zi = np.zeros(1)

    def set_enabled(self, on: bool):
        self._enabled = bool(on)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_adaptive(self, ratio: float):
        """Adapt to the track's brightness (tiers at 0.2/0.4/0.6). Eased highs and a
        bass-shelf floor keep mids/bass present rather than swamped by air."""
        r = ratio
        self._shelf.set_gain(
            2.0 if r > 0.6 else 3.0 if r > 0.4 else 4.5 if r > 0.2 else 6.0,
            "highshelf", 9500)
        self._splashLevel = 0.05 if r > 0.6 else 0.08 if r > 0.4 else 0.10 if r > 0.2 else 0.14
        self._airLevel    = 0.04 if r > 0.6 else 0.06 if r > 0.4 else 0.09 if r > 0.2 else 0.13
        # keep some bass body (floor 1.5 dB), never zero it out
        self._subShelf.set_gain(
            3.0 if r > 0.6 else 2.5 if r > 0.4 else 2.0 if r > 0.2 else 1.5,
            "lowshelf", 90)
        self._bassLevel    = 0.07 if r > 0.6 else 0.06 if r > 0.4 else 0.05 if r > 0.2 else 0.04
        self._subHarmLevel = 0.04 if r > 0.6 else 0.03 if r > 0.4 else 0.022 if r > 0.2 else 0.015

    def process(self, block: np.ndarray) -> np.ndarray:
        """block: (N, 2) float32 in, (N, 2) float32 out."""
        if not self._enabled:
            return block

        x = block.astype(np.float64, copy=False)

        # Wet EQ chain (series)
        e = self._subShelf.process(x)
        e = self._warmth.process(e)
        e = self._presence.process(e)
        e = self._definition.process(e)
        e = self._shelf.process(e)
        wet = self._crystal.process(e)

        # Parallel exciters (each from the dry input x)
        splash = self._splashLp.process(_sat(self._splashHpf.process(x), 4.0)) * self._splashLevel
        air    = self._airShelf.process(_sat(self._airHpf.process(x), 2.5)) * self._airLevel
        bass   = self._bassOutLp.process(
            _sat(self._bassLpf.process(self._bassHpf.process(x)), 1.6)) * self._bassLevel
        subh   = self._subBpLp.process(self._subBpHp.process(
            np.abs(np.clip(self._subLpf.process(self._subHpf.process(x)), -1.0, 1.0)))) * self._subHarmLevel

        mono = (0.5 * (x[:, 0] + x[:, 1]))[:, None]
        mono = np.repeat(mono, 2, axis=1)
        mid  = _sat(self._midBpLp.process(self._midBpHp.process(mono)), 1.0) * self._midLevel

        mix = (wet + splash + air + bass + subh + mid) * self._wet_trim

        # Stereo widener (mid untouched; side high-passed, so the bass is mono and the highs wider)
        mid  = 0.5 * (mix[:, 0] + mix[:, 1])
        side = 0.5 * (mix[:, 0] - mix[:, 1])
        side = self._sideHp.process(side[:, None])[:, 0] * (1.0 + self._W)
        out = np.stack([mid + side, mid - side], axis=1)

        # Soft limiter: instant attack, ~80 ms release (threshold and ratio set in __init__)
        peak = np.maximum(np.abs(out[:, 0]), np.abs(out[:, 1]))
        sm, self._lim_zi = lfilter(self._lim_b, self._lim_a, peak, zi=self._lim_zi)
        det = np.maximum(peak, sm)
        gain = np.ones_like(det)
        over = det > self._lim_thr
        gain[over] = (self._lim_thr + (det[over] - self._lim_thr) / self._lim_ratio) / det[over]
        out *= gain[:, None]

        return out.astype(np.float32, copy=False)

    def reset(self):
        for bq in (self._subShelf, self._warmth, self._presence, self._definition,
                   self._shelf, self._crystal, self._splashHpf, self._splashLp, self._airHpf,
                   self._airShelf, self._bassHpf, self._bassLpf, self._bassOutLp,
                   self._subHpf, self._subLpf, self._subBpHp, self._subBpLp,
                   self._midBpHp, self._midBpLp, self._sideHp):
            bq.reset()
        self._lim_zi = np.zeros(1)


# User parametric EQ

EQ_TYPES = ("peaking", "lowshelf", "highshelf", "lowpass", "highpass", "notch")
EQ_FMIN, EQ_FMAX = 20.0, 20000.0
EQ_GMIN, EQ_GMAX = -24.0, 24.0
EQ_QMIN, EQ_QMAX = 0.1, 24.0


EQ_SLOPE_TYPES = ("lowpass", "highpass", "lowshelf", "highshelf")   # types whose slope can be steepened
EQ_SLOPES = (12, 24, 36, 48)                 # dB/octave (cascaded 2nd-order stages)


class EqBand:
    __slots__ = ("type", "freq", "gain", "q", "enabled", "slope", "_fs", "_stages")

    def __init__(self, btype="peaking", freq=1000.0, gain=0.0, q=1.0,
                 enabled=True, slope=12, fs=_FS):
        self._fs = fs
        self.type = btype if btype in EQ_TYPES else "peaking"
        self.freq = float(min(EQ_FMAX, max(EQ_FMIN, freq)))
        self.gain = float(min(EQ_GMAX, max(EQ_GMIN, gain)))
        self.q = float(min(EQ_QMAX, max(EQ_QMIN, q)))
        self.enabled = bool(enabled)
        self.slope = slope if slope in EQ_SLOPES else 12
        self._stages: list[_BQ] = []
        self._build()

    def _n_stages(self) -> int:
        return max(1, self.slope // 12) if self.type in EQ_SLOPE_TYPES else 1

    def _stage_params(self):
        """(type, freq, q, gain) for each stage. A shelf is one 2nd-order section at 12 dB/oct;
        steeper, it is a cascade whose sections take the Butterworth pole Qs of the whole
        filter and an equal share of the gain, so the plateau stays at the set gain. The Q
        control scales them (0.707 is the plain Butterworth shape), as it does for one section."""
        n = self._n_stages()
        if self.type in ("lowshelf", "highshelf"):
            return [(self.type, self.freq,
                     self.q / (0.7071067811865476 * 2.0 * np.sin((2 * i + 1) * np.pi / (4 * n))),
                     self.gain / n) for i in range(n)]
        return [(self.type, self.freq, self.q, self.gain)] * n

    def _build(self):
        self._stages = [_BQ(*p, self._fs) for p in self._stage_params()]

    def refresh(self):
        # rebuild only if the stage count changed (type/slope); else keep state
        if len(self._stages) != self._n_stages():
            self._build()
        else:
            for st, p in zip(self._stages, self._stage_params()):
                st.set_params(*p)

    def process(self, x):
        for st in self._stages:
            x = st.process(x)
        return x

    def reset(self):
        for st in self._stages:
            st.reset()

    def response(self, ejw, ejw2):
        total = 0.0
        for st in self._stages:
            num = st.b[0] + st.b[1] * ejw + st.b[2] * ejw2
            den = st.a[0] + st.a[1] * ejw + st.a[2] * ejw2
            total = total + 20.0 * np.log10(np.abs(num / den) + 1e-12)
        return total

    def to_dict(self):
        return {"type": self.type, "freq": self.freq, "gain": self.gain,
                "q": self.q, "enabled": self.enabled, "slope": self.slope}

    @classmethod
    def from_dict(cls, d, fs=_FS):
        return cls(d.get("type", "peaking"), d.get("freq", 1000.0),
                   d.get("gain", 0.0), d.get("q", 1.0), d.get("enabled", True),
                   d.get("slope", 12), fs)


class ParametricEQ:
    """User-defined parametric EQ: a series of biquad bands + input/output trim.
    Edited live by the EQ panel; processed per-block by the DSP engine."""

    def __init__(self, fs: int = _FS):
        self._fs = fs
        self.bands: list[EqBand] = []
        self.enabled = False
        self.input_gain = 0.0
        self.output_gain = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        if not self.enabled or not self.bands:
            return block
        x = block
        ig = 10.0 ** (self.input_gain / 20.0)
        if ig != 1.0:
            x = x * np.float32(ig)
        active = False
        for b in self.bands:
            if b.enabled:
                x = b.process(x); active = True
        og = 10.0 ** (self.output_gain / 20.0)
        if og != 1.0:
            x = x * np.float32(og)
        return x.astype(np.float32, copy=False) if (active or ig != 1.0 or og != 1.0) else block

    def reset_state(self):
        for b in self.bands:
            b.reset()

    def sos(self):
        """What process() does as (second order sections, one linear gain for both trims), or None when it does nothing.
        For measuring a whole track through this equalizer in one pass."""
        if not self.enabled:
            return None
        rows = [[*st.b, 1.0, st.a[1], st.a[2]] for b in self.bands if b.enabled for st in b._stages]
        gain = 10.0 ** ((self.input_gain + self.output_gain) / 20.0)
        return (np.array(rows, dtype=np.float64).reshape(-1, 6), gain) if rows or gain != 1.0 else None

    def response_db(self, freqs: np.ndarray) -> np.ndarray:
        """Combined magnitude response (dB) of all enabled bands and the output trim."""
        total = np.full(len(freqs), float(self.output_gain), dtype=np.float64)
        w = 2.0 * np.pi * freqs / self._fs
        ejw = np.exp(-1j * w)
        ejw2 = ejw * ejw
        for b in self.bands:
            if b.enabled:
                total += b.response(ejw, ejw2)
        return total

    def add_band(self, freq, gain=0.0, q=1.0, btype="peaking", slope=12) -> EqBand:
        b = EqBand(btype, freq, gain, q, True, slope, self._fs)
        self.bands.append(b)
        return b

    def remove_band(self, band: EqBand):
        if band in self.bands:
            self.bands.remove(band)

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "input_gain": self.input_gain,
            "output_gain": self.output_gain,
            "bands": [b.to_dict() for b in self.bands],
        }

    def load_dict(self, d: dict):
        self.enabled = bool(d.get("enabled", False))
        self.input_gain = float(d.get("input_gain", 0.0))
        self.output_gain = float(d.get("output_gain", 0.0))
        self.bands = [EqBand.from_dict(bd, self._fs) for bd in d.get("bands", [])]
