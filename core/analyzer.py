from __future__ import annotations
import subprocess
import threading
import numpy as np
from pathlib import Path

BANDS = 128
_RATE = 44100

_ATTACK     = np.float32(0.55)
_DECAY      = np.float32(0.10)
_PEAK_DECAY = np.float32(0.994)
_REF_DB  = 50.0
_TRIM_DB =  6.0


def _hz_to_mel(f: float) -> float:
    return 2595.0 * np.log10(1.0 + f / 700.0)

def _mel_to_hz(m: float) -> float:
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


def _build_window_and_slices(chunk: int) -> tuple[np.ndarray, list[tuple[int,int]]]:
    win   = np.blackman(chunk).astype(np.float32)
    freqs = np.fft.rfftfreq(chunk, 1.0 / _RATE)
    mel_lo, mel_hi = _hz_to_mel(30.0), _hz_to_mel(16000.0)
    edges = [_mel_to_hz(m) for m in np.linspace(mel_lo, mel_hi, BANDS + 1)]
    slices = []
    for i in range(BANDS):
        lo = int(np.searchsorted(freqs, edges[i]))
        hi = int(np.searchsorted(freqs, edges[i + 1]))
        slices.append((lo, max(lo + 1, hi)))
    return win, slices


def _slice_arrays(slices: list[tuple[int, int]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lo = np.array([s[0] for s in slices], dtype=np.intp)
    hi = np.array([s[1] for s in slices], dtype=np.intp)
    return lo, hi, (hi - lo).astype(np.float64)


class Analyzer:
    def __init__(self, chunk: int = 4096):
        self._chunk  = chunk
        self._hop    = max(512, chunk // 4)
        self._window, self._slices = _build_window_and_slices(chunk)
        self._band_lo, self._band_hi, self._band_n = _slice_arrays(self._slices)
        self._pre    = np.float32(0.97)

        self._audio:     np.ndarray | None = None
        self._lock       = threading.Lock()
        self._gen        = 0          # one per load_track; a decode for an older one is stopped and dropped

        self._smooth     = np.zeros(BANDS, dtype=np.float32)
        self._peaks      = np.zeros(BANDS, dtype=np.float32)
        self._band_ceil  = np.full(BANDS, -_REF_DB, dtype=np.float64)

        self._last_hop   = -1
        self._pending    = np.zeros(BANDS, dtype=np.float32)
        self._computing  = False

        # Live-feed mode: for sources ffmpeg can't decode (.fur), the player
        # streams rendered output PCM in here and the scope/spectrum read the
        # most recent samples instead of indexing a decoded file.
        self._live       = False
        self._live_buf   = np.zeros(0, dtype=np.float32)
        self._live_max   = _RATE * 3
        # Engine-source mode: pull the window straight from the playback engine's
        # decoded buffer, so there is no separate decode and no lag on track change.
        self._engine_src = None

    def set_chunk(self, chunk: int):
        chunk = max(512, chunk)
        if chunk == self._chunk:
            return
        self._chunk  = chunk
        self._hop    = max(512, chunk // 4)
        self._window, self._slices = _build_window_and_slices(chunk)
        self._band_lo, self._band_hi, self._band_n = _slice_arrays(self._slices)
        self._band_ceil[:] = -_REF_DB
        self._last_hop = -1

    def set_live(self, on: bool):
        """Switch between file mode and live-PCM mode (for .fur output)."""
        with self._lock:
            self._live = bool(on)
            if on:
                self._audio = None
                self._gen += 1
            self._live_buf = np.zeros(0, dtype=np.float32)
        self._band_ceil[:] = -_REF_DB
        self._smooth[:] = 0
        self._peaks[:]  = 0
        self._last_hop  = -1

    def set_engine_source(self, fn):
        """fn(position_ms, n) -> mono window or None. When set, the scope and spectrum read from
        the engine instead of decoding the file separately."""
        self._engine_src = fn
        if fn is not None:
            with self._lock:
                self._audio = None
                self._live = False
                self._gen += 1
        self._band_ceil[:] = -_REF_DB
        self._smooth[:] = 0
        self._peaks[:]  = 0
        self._last_hop  = -1

    def feed_live(self, samples: np.ndarray):
        """Append mono float32 output samples to the live ring buffer."""
        with self._lock:
            if not self._live:
                return
            samples = np.asarray(samples, dtype=np.float32)
            self._live_buf = (samples.copy() if self._live_buf.size == 0
                              else np.concatenate([self._live_buf, samples]))
            if self._live_buf.size > self._live_max:
                self._live_buf = self._live_buf[-self._live_max:].copy()

    def load_track(self, path: Path):
        self._engine_src = None
        with self._lock:
            self._audio = None
            self._live = False
            self._live_buf = np.zeros(0, dtype=np.float32)
            self._gen += 1
            gen = self._gen
        self._band_ceil[:] = -_REF_DB
        self._smooth[:] = 0
        self._peaks[:]  = 0
        threading.Thread(target=self._decode, args=(path, gen), daemon=True).start()

    def _decode(self, path: Path, gen: int):
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-i", str(path),
                 "-f", "f32le", "-ar", str(_RATE), "-ac", "1", "-vn", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            done = threading.Event()

            def watch():                  # a newer track, or stop(): do not decode to the end for nobody
                while not done.wait(0.05):
                    if gen != self._gen:
                        proc.kill()
                        return
            threading.Thread(target=watch, daemon=True).start()
            try:
                raw = proc.communicate()[0]
            finally:
                done.set()
            data = np.frombuffer(raw, dtype=np.float32).copy()
        except Exception:
            return
        with self._lock:
            if gen == self._gen:
                self._audio = data

    def _fft_bands(self, chunk: np.ndarray) -> np.ndarray:
        """FFT one analysis chunk into normalised per-band magnitudes (0..1)."""
        chunk = chunk.copy()
        chunk[1:] -= self._pre * chunk[:-1]

        spec    = np.abs(np.fft.rfft(chunk * self._window))
        spec_db = 20.0 * np.log10(spec + 1e-10).astype(np.float64)

        # Vectorized per-band mean via cumulative sum (bands may overlap, so
        # reduceat doesn't apply): mean = (cs[hi] - cs[lo]) / (hi - lo).
        cs = np.concatenate(([0.0], np.cumsum(spec_db)))
        raw_db = (cs[self._band_hi] - cs[self._band_lo]) / self._band_n
        self._band_ceil = np.maximum(
            raw_db,
            self._band_ceil * 0.9997 + raw_db * 0.0003,
        )
        ceil = np.maximum(self._band_ceil, raw_db + 3.0)

        normed = (raw_db - (ceil - _REF_DB)) / _REF_DB
        normed -= _TRIM_DB / _REF_DB
        return np.clip(normed, 0.0, 1.0).astype(np.float32)

    def _compute_fft(self, sample: int):
        try:
            with self._lock:
                audio = self._audio
            if audio is None:
                return
            end = sample + self._chunk
            if end > len(audio):
                return
            self._pending = self._fft_bands(audio[sample:end])
        finally:
            self._computing = False

    def update(self, position_ms: int, playing: bool):
        if not playing:
            self._smooth *= np.float32(0.85)
            self._peaks  *= _PEAK_DECAY
            return

        if self._engine_src is not None:
            win = self._engine_src(position_ms, self._chunk)
            if win is not None and len(win) >= self._chunk:
                try:
                    self._pending = self._fft_bands(win[:self._chunk])
                except Exception:
                    pass
            else:
                self._smooth *= np.float32(0.85)
                self._peaks  *= _PEAK_DECAY
                return
        elif self._live:
            with self._lock:
                buf = self._live_buf
            if buf.size >= self._chunk:
                try:
                    self._pending = self._fft_bands(buf[-self._chunk:])
                except Exception:
                    pass
            else:
                self._smooth *= np.float32(0.85)
                self._peaks  *= _PEAK_DECAY
                return
        else:
            with self._lock:
                audio = self._audio
            if audio is None:
                self._smooth *= np.float32(0.85)
                self._peaks  *= _PEAK_DECAY
                return

            sample   = int(position_ms / 1000.0 * _RATE)
            hop_idx  = sample // self._hop
            if hop_idx != self._last_hop:
                self._last_hop = hop_idx
                if not self._computing:
                    self._computing = True
                    threading.Thread(
                        target=self._compute_fft, args=(sample,), daemon=True,
                    ).start()

        raw = self._pending
        gt  = raw > self._smooth
        self._smooth = np.where(
            gt,
            _ATTACK * raw + (1.0 - _ATTACK) * self._smooth,
            _DECAY  * raw + (1.0 - _DECAY)  * self._smooth,
        ).astype(np.float32)
        self._peaks = np.maximum(self._smooth, self._peaks * _PEAK_DECAY)

    def get_audio_window(self, position_ms: int, n_samples: int) -> np.ndarray | None:
        """Return a copy of n_samples, or None if unavailable. In live mode the
        most recent n_samples of streamed output are returned (position ignored)."""
        if self._engine_src is not None:
            return self._engine_src(position_ms, n_samples)
        with self._lock:
            if self._live:
                buf = self._live_buf
                if buf.size < n_samples:
                    return None
                return buf[-n_samples:].copy()
            audio = self._audio
        if audio is None:
            return None
        pos = int(position_ms / 1000.0 * _RATE)
        if pos + n_samples > len(audio):
            return None
        return audio[pos:pos + n_samples].copy()

    def get_audio_snapshot(self) -> np.ndarray | None:
        """Return the current audio array reference under the lock (safe for read-only use)."""
        with self._lock:
            return self._audio

    @property
    def bands(self) -> np.ndarray:
        return self._smooth.copy()

    @property
    def live_bands(self) -> np.ndarray:
        return self._smooth.copy()

    @property
    def peaks(self) -> np.ndarray:
        return self._peaks.copy()

    def start(self): pass

    def stop(self):
        with self._lock:
            self._audio = None
            self._gen += 1
        self._smooth[:] = 0
        self._peaks[:] = 0
