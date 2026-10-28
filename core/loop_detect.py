"""Find where a chiptune actually ends.

Formats like SID, and any tune rendered for a fixed window, carry no reliable
end marker: the player routine just keeps running, either looping forever or
falling silent. This module recovers the real length from the rendered audio.

Two independent signals, cheapest first:

  1. Trailing silence: the tune stopped playing. The end is the last audible
     sample (plus a short tail).
  2. Loop detection: the tune repeats forever. Compare a coarse loudness/
     spectral envelope against itself at every candidate period and take the
     shortest lag whose match is strong and stable; that lag is one loop.

Returns a suggested play length in seconds and how it was decided, so callers
can prefer an authoritative source (e.g. the HVSC songlength database) and fall
back to this only for tunes nothing knows about: new releases, private
exports and the user's own music.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Envelope resolution: 20 frames/sec is plenty to lock a loop and keeps the
# correlation cheap even for a 10-minute render.
_FPS = 20
_SILENCE_DB = -75.0        # absolute floor: below this is silence regardless
_REL_FLOOR_DB = 45.0       # ...or this far under the tune's own peak
_MIN_SILENCE_S = 1.5       # this much dead air / static in a row means "it stopped"
_STATIC_DB = 0.25          # envelope moving less than this is not music
_MIN_LOOP_S = 0.4          # short jingles legitimately loop in well under a second
_MATCH_THRESHOLD = 0.70    # accept weaker loops; callers weigh .confidence
_STRONG_MATCH = 0.80       # at/above this the loop is considered reliable
_COMB_H = 8                # harmonics averaged when scoring a candidate period
_JITTER = 1                # frames of slack around each harmonic
_TAIL_S = 0.35             # keep a little air after the last audible sample


@dataclass
class LoopResult:
    length_s: float          # suggested play length
    reason: str              # "silence" | "loop" | "full": how it was decided
    loop_s: float | None     # detected loop period, if any
    confidence: float        # 0..1


def _envelope(mono: np.ndarray, rate: int) -> np.ndarray:
    """Short-term RMS in dB at _FPS frames/sec, the shape we match against."""
    hop = max(1, rate // _FPS)
    n = len(mono) // hop
    if n < 2:
        return np.zeros(0, dtype=np.float32)
    frames = mono[: n * hop].reshape(n, hop).astype(np.float32)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return 20.0 * np.log10(rms + 1e-9)


def _trailing_silence(env: np.ndarray) -> int | None:
    """First frame of the final "nothing is happening" run, or None.

    Silence is the obvious case, but chip players rarely reach it: when a SID
    tune ends the player routine keeps running and the chip holds a *constant*
    tone. Measured on RoboCop 3, the envelope pins to a dead-flat -51 dB with
    a standard deviation of exactly 0.00 and never moves again. Level alone
    can't distinguish that from a quiet passage, so the real signal is that the
    envelope stops *changing* for the whole remainder of the render.

    Walking back from the end (rather than forward) matters: a tune may hold a
    steady note mid-song, but only the final drone lasts to the last frame.
    """
    if env.size == 0:
        return 0
    peak = float(env.max())
    floor = max(_SILENCE_DB, peak - _REL_FLOOR_DB)

    # Start of the constant run that reaches the end of the render.
    last = float(env[-1])
    j = env.size - 1
    while j > 0 and abs(float(env[j - 1]) - last) <= _STATIC_DB:
        j -= 1

    static_s = (env.size - j) / _FPS
    if static_s >= _MIN_SILENCE_S and (j > 0 or last <= floor):
        return j
    # Fall back to plain level-based silence.
    audible = np.flatnonzero(env > floor)
    if audible.size == 0:
        return 0
    lastaud = int(audible[-1])
    if (len(env) - lastaud) / _FPS >= _MIN_SILENCE_S:
        return lastaud + 1
    return None


def _autocorr(x: np.ndarray) -> np.ndarray:
    """Normalised autocorrelation of a mean-removed signal, via FFT."""
    n = x.size
    fsize = 1 << int(np.ceil(np.log2(2 * n)))
    f = np.fft.rfft(x, fsize)
    ac = np.fft.irfft(f * np.conjugate(f), fsize)[:n]
    # Normalise per lag by the overlap length so long lags aren't penalised
    # purely for having fewer samples in common.
    counts = np.arange(n, 0, -1, dtype=np.float64)
    ac = ac / counts
    if ac[0] > 1e-12:
        ac = ac / ac[0]
    return ac


def _detect_loop(env: np.ndarray) -> tuple[float | None, float]:
    """Loop period of the envelope, as the fundamental and not a multiple of it.

    A looping tune correlates strongly at its loop length and at every multiple
    of it. Taking the strongest peak therefore often returns 2x/3x/4x the real
    loop (measured: a 23 s loop reported as 92 s). So: find the best peak, then
    walk back to the shortest lag that scores nearly as well and divides it.
    """
    min_lag = int(_MIN_LOOP_S * _FPS)
    max_lag = env.size // 2
    if max_lag <= min_lag:
        return None, 0.0

    x = env - env.mean()
    if float(np.dot(x, x)) < 1e-9:
        return None, 0.0

    ac = _autocorr(x)[:max_lag]
    if ac.size <= min_lag:
        return None, 0.0

    # Comb score: a true loop period L correlates at L, 2L, 3L, and so on. A spurious
    # peak at 4L only correlates at 4L, 8L, ... so averaging over the harmonic series
    # picks the fundamental instead of its multiples. Each lag is scored on up to
    # _COMB_H harmonics that fit.
    n = ac.size
    best_lag, best_comb = None, 0.0
    for lag in range(min_lag, max_lag):
        hs = range(lag, n, lag)
        tot = cnt = 0.0
        for h in hs:
            # take the local max around each harmonic, since periods drift slightly
            lo, hi = max(0, h - _JITTER), min(n, h + _JITTER + 1)
            tot += float(ac[lo:hi].max()); cnt += 1.0
            if cnt >= _COMB_H:
                break
        if cnt < 2:            # need at least two periods of evidence
            continue
        comb = tot / cnt
        if comb > best_comb:
            best_comb, best_lag = comb, lag
    if best_lag is None or best_comb < _MATCH_THRESHOLD:
        return None, best_comb
    return best_lag / _FPS, best_comb


def analyze(mono: np.ndarray, rate: int, loops: int = 2,
            max_s: float | None = None) -> LoopResult:
    """Suggest a play length for a rendered tune.

    mono   : rendered audio, mono float32 (any scale)
    rate   : its sample rate
    loops  : how many times to play a looping tune before ending
    max_s  : clamp (defaults to the render length)
    """
    total_s = len(mono) / float(rate) if rate else 0.0
    cap = min(total_s, max_s) if max_s else total_s
    env = _envelope(mono, rate)
    if env.size == 0:
        return LoopResult(cap, "full", None, 0.0)

    # did it simply stop?
    sil = _trailing_silence(env)
    if sil is not None:
        end = min(cap, sil / _FPS + _TAIL_S)
        if end > 0.5:
            return LoopResult(end, "silence", None, 1.0)

    # does it loop?
    loop_s, score = _detect_loop(env)
    if loop_s:
        return LoopResult(min(cap, loop_s * max(1, loops)), "loop", loop_s, score)

    return LoopResult(cap, "full", None, score)
