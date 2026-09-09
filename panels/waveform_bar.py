from __future__ import annotations

import subprocess
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None

# waveform cache
_RATE = 22050
_N_POINTS = 4096  # internal resolution; downsampled to pixel width at draw time
_MAX_CACHE = 60

_cache: OrderedDict[str, np.ndarray] = OrderedDict()
_lock = threading.Lock()

_DEFAULT_ACCENT = "#8040c8"   # matches theme.py accent


def _compute_waveform(path: Path, n: int) -> np.ndarray:
    from ffmpeg_util import ffmpeg_path
    proc = subprocess.run(
        [
            ffmpeg_path(),
            "-i",
            str(path),
            "-f",
            "f32le",
            "-ar",
            str(_RATE),
            "-ac",
            "1",
            "-vn",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    data = np.frombuffer(proc.stdout, dtype=np.float32)
    if len(data) == 0:
        return np.zeros(n, dtype=np.float32)

    total = len(data)
    chunk_size = max(1, total // n)
    trimmed = data[: chunk_size * n]
    chunks = trimmed.reshape(n, chunk_size)
    rms = np.sqrt(np.mean(chunks**2, axis=1)).astype(np.float32)

    hi = float(np.percentile(rms, 99))
    if hi > 1e-10:
        rms = np.clip(rms / hi, 0.0, 1.0)
    return rms


def _compute_waveform_gme(path: Path, n: int, track_idx: int = 0) -> np.ndarray:
    """Full waveform for a libgme chiptune (ffmpeg can't decode these) — render the
    whole tune to its play-length and bin to n RMS points. `track_idx` selects the
    subsong for multi-tune formats (NSF/SAP/GBS/KSS/AY…)."""
    import ctypes
    from vgm_player import _lib as gme, _GmeInfo
    emu = ctypes.c_void_p()
    if gme.gme_open_file(str(path).encode(), ctypes.byref(emu), _RATE):
        return np.zeros(n, dtype=np.float32)
    try:
        # length of THIS subsong (not just track 0)
        dur_ms = 150_000
        info_ptr = ctypes.POINTER(_GmeInfo)()
        if not gme.gme_track_info(emu, ctypes.byref(info_ptr), track_idx) and info_ptr:
            pl = info_ptr.contents.play_length
            gme.gme_free_info(info_ptr)
            if pl > 0:
                dur_ms = pl
        dur_ms = min(dur_ms, 600_000)
        if gme.gme_start_track(emu, track_idx):
            return np.zeros(n, dtype=np.float32)
        chunk = 8192                          # interleaved stereo samples per call
        buf = (ctypes.c_short * chunk)()
        target_frames = int(dur_ms / 1000 * _RATE)
        parts, got = [], 0
        while got < target_frames and not gme.gme_track_ended(emu):
            if gme.gme_play(emu, chunk, buf):
                break
            parts.append(np.frombuffer(bytes(buf), dtype=np.int16))
            got += chunk // 2
        if not parts:
            return np.zeros(n, dtype=np.float32)
        data = np.concatenate(parts).astype(np.float32)
        if data.size % 2:
            data = data[:-1]
        mono = data.reshape(-1, 2).mean(axis=1)
        cs = max(1, len(mono) // n)
        rms = np.sqrt(np.mean(mono[: cs * n].reshape(n, cs) ** 2, axis=1)).astype(np.float32)
        hi = float(np.percentile(rms, 99))
        if hi > 1e-10:
            rms = np.clip(rms / hi, 0.0, 1.0)
        return rms
    finally:
        gme.gme_delete(emu)


# widget


class WaveformBar(QWidget):
    seeked = Signal(float)
    _loaded = Signal(object)
    def __init__(self, parent=None):
        super().__init__(parent)
        self._no_ffmpeg = False
        self._waveform: np.ndarray | None = None
        self._px_wave: np.ndarray | None = None
        self._px_width = 0
        self._position = 0.0
        self._dragging = False
        self._img: QImage | None = None
        self._accent: QColor | None = None
        self._default_accent = QColor(_DEFAULT_ACCENT)  # app accent (user-customisable)
        self._system_accent = False   # follow OS highlight when on system theme
        self._fur_mode = False        # progressive fill from streamed .fur output
        self._fur_rms: np.ndarray | None = None
        self._fur_peak = 1e-6
        self._fur_filled = 0
        # Cached pre-rendered bar images (played/unplayed colour) — position
        # updates then only blit, instead of rebuilding an H×W×4 buffer per frame.
        self._img_base: QImage | None = None
        self._img_played: QImage | None = None
        self._img_key = None
        # Coalesce .fur streamed-chunk repaints to ~15 Hz (one per audio chunk
        # re-resampled the whole bar).
        self._fur_flush = QTimer(self)
        self._fur_flush.setInterval(66)
        self._fur_flush.setSingleShot(True)
        self._fur_flush.timeout.connect(self._flush_fur)
        # Engine-fed mode: poll the playback buffer while it decodes.
        self._engine = None
        self._engine_key = ""
        self._engine_path = None
        self._engine_tries = 0
        self._engine_poll = QTimer(self)
        self._engine_poll.setInterval(120)
        self._engine_poll.timeout.connect(self._poll_engine)
        self._loaded.connect(self._on_loaded)
        self.setMinimumHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self.setContentsMargins(0, 0, 0, 0)

    def load(self, path: Path, engine=None, key: str | None = None):
        """Show the waveform for `path`.

        If `engine` is given, the shape is read from the playback engine's own
        decode buffer — the audio is being decoded to play it anyway, so there
        is no reason to run a second ffmpeg pass over the same file. The bar
        fills in as the decode runs ahead of the play head, then settles. Falls
        back to decoding the file directly when there is no engine buffer (video
        via QMediaPlayer, or a track that isn't loaded yet).
        """
        self._fur_mode = False
        self._waveform = None
        self._px_wave = None
        self.update()
        # `key` distinguishes shapes that share a path — a multi-subsong SID or
        # chiptune has one file but a different waveform per subsong, and keying
        # on the path alone made every subsong show subsong 1's shape.
        key = key or str(path)
        with _lock:
            if key in _cache:
                _cache.move_to_end(key)
                self._loaded.emit(_cache[key])
                return
        if engine is not None:
            self._engine = engine
            self._engine_key = key
            self._engine_path = path
            self._engine_tries = 0
            self._engine_poll.start()
            self._poll_engine()            # draw whatever is already decoded
            return
        threading.Thread(target=self._bg_load, args=(path, key), daemon=True).start()

    def _poll_engine(self) -> bool:
        """Pull the current envelope from the engine. True once it's complete."""
        eng = getattr(self, "_engine", None)
        if eng is None:
            self._engine_poll.stop()
            return False
        try:
            pts, frac = eng.envelope(_N_POINTS)
        except Exception:
            pts, frac = None, 0.0
        if pts is None:
            # Nothing decoded yet. Give the engine a moment, then decode the file
            # ourselves — this track may not be going through the engine at all.
            self._engine_tries += 1
            if self._engine_tries > 12:     # ~1.5s
                self._engine_poll.stop()
                self._engine = None
                p, k = self._engine_path, self._engine_key
                threading.Thread(target=self._bg_load, args=(p, k), daemon=True).start()
            return False
        self._waveform = pts
        self._px_wave = None
        self.update()
        if frac >= 0.999:
            self._engine_poll.stop()
            with _lock:                    # complete shape is worth caching
                _cache[self._engine_key] = pts
                _cache.move_to_end(self._engine_key)
                while len(_cache) > _MAX_CACHE:
                    _cache.popitem(last=False)
            self._engine = None
            return True
        return False

    def set_accent(self, palette: dict | None):
        if palette:
            self._accent = QColor(palette.get("accent", palette.get("accent_l", "#ffffff")))
        else:
            self._accent = None
        self.update()

    def set_system_accent(self, on: bool):
        """When on (system theme), the no-art fallback follows the OS highlight;
        otherwise it uses the app's own accent colour rather than Fusion's default."""
        self._system_accent = bool(on)
        self.update()

    def set_default_accent(self, color):
        """The app-theme no-art colour (user-customisable purple replacement)."""
        self._default_accent = QColor(color)
        self.update()

    def _idle_accent(self) -> QColor:
        if self._system_accent:
            c = self.palette().highlight().color()
            if c.isValid():
                return c
        return QColor(self._default_accent)

    def set_position(self, frac: float):
        self._position = max(0.0, min(1.0, frac))
        self.update()

    # .fur progressive waveform (streamed from FurPlayer output)

    def fur_begin(self):
        """Start a fresh progressive waveform for a .fur track."""
        self._engine_poll.stop(); self._engine = None
        self._no_ffmpeg = False
        self._fur_mode = True
        self._fur_rms = np.zeros(_N_POINTS, dtype=np.float32)
        self._fur_peak = 1e-6
        self._fur_filled = 0
        self._waveform = np.zeros(_N_POINTS, dtype=np.float32)
        self._px_wave = None
        self.update()

    def feed_fur(self, frames_so_far: int, total_frames: int, rate: int, mono: np.ndarray):
        """Fold a chunk of rendered output into the waveform as it plays."""
        if not self._fur_mode or self._fur_rms is None or mono is None or len(mono) == 0:
            return
        n = _N_POINTS
        tf = total_frames if total_frames > 0 else max(frames_so_far, rate or _RATE)
        end_bin = min(n, int(min(1.0, frames_so_far / tf) * n)) if tf > 0 else 0
        if end_bin <= self._fur_filled:
            end_bin = min(n, self._fur_filled + 1)
        rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
        self._fur_rms[self._fur_filled:end_bin] = rms
        self._fur_filled = end_bin
        self._fur_peak = max(self._fur_peak, rms)
        if not self._fur_flush.isActive():
            self._fur_flush.start()

    def _flush_fur(self):
        """Apply accumulated .fur chunks to the drawn waveform (throttled)."""
        if not self._fur_mode or self._fur_rms is None:
            return
        self._waveform = np.clip(self._fur_rms / self._fur_peak, 0.0, 1.0).astype(np.float32)
        self._px_wave = None
        self.update()

    def _bg_load(self, path: Path, key: str):
        from ffmpeg_util import ffmpeg_available
        if not ffmpeg_available():
            self._no_ffmpeg = True
            if _qt_valid(self):
                self._loaded.emit(np.zeros(_N_POINTS, dtype=np.float32))
            return
        self._no_ffmpeg = False
        try:
            data = _compute_waveform(path, _N_POINTS)
        except Exception:
            data = np.zeros(_N_POINTS, dtype=np.float32)
        with _lock:
            _cache[key] = data
            _cache.move_to_end(key)
            while len(_cache) > _MAX_CACHE:
                _cache.popitem(last=False)
        if _qt_valid(self):
            self._loaded.emit(data)

    def load_gme(self, path: Path, track_idx: int = 0):
        """Full pre-rendered waveform for a libgme chiptune (vgm/nsf/spc/ay/...).
        `track_idx` selects the subsong for multi-tune formats."""
        self._engine_poll.stop()
        self._engine = None
        self._fur_mode = False
        self._no_ffmpeg = False
        self._waveform = None
        self._px_wave = None
        self.update()
        key = "gme:%d:%s" % (track_idx, path)
        with _lock:
            if key in _cache:
                _cache.move_to_end(key)
                self._loaded.emit(_cache[key])
                return
        threading.Thread(target=self._bg_load_gme, args=(path, key, track_idx), daemon=True).start()

    def _bg_load_gme(self, path: Path, key: str, track_idx: int = 0):
        try:
            data = _compute_waveform_gme(path, _N_POINTS, track_idx)
        except Exception:
            data = np.zeros(_N_POINTS, dtype=np.float32)
        with _lock:
            _cache[key] = data
            _cache.move_to_end(key)
            while len(_cache) > _MAX_CACHE:
                _cache.popitem(last=False)
        if _qt_valid(self):
            self._loaded.emit(data)

    def _on_loaded(self, data: np.ndarray):
        self._waveform = data
        self._px_wave = None
        self.update()

    def _resample(self, W: int) -> np.ndarray:
        n = len(self._waveform)
        if n == 0 or W <= 0:
            return np.zeros(max(1, W), dtype=np.float32)
        lo = np.linspace(0, n, W + 1)[:-1].astype(np.intp)
        # per-pixel max over its sample range in one vectorized call
        return np.maximum.reduceat(self._waveform, lo).astype(np.float32)

    def _rebuild_images(self, W: int, H: int, bg: QColor, hi_c: QColor, dim_c: QColor):
        """Pre-render the whole bar twice (played/unplayed colours). paintEvent
        then just blits the two halves — no per-frame numpy allocation."""
        mid = H * 0.5
        amps = (self._px_wave * mid * 0.92).astype(np.float32)
        y_top = np.maximum(0, (mid - amps).astype(int))
        y_bot = np.minimum(H, (mid + amps).astype(int) + 1)
        y_idx = np.arange(H)[:, np.newaxis]
        bar_mask = (y_idx >= y_top) & (y_idx < y_bot)

        def _render(c: QColor, alpha: int) -> QImage:
            buf = np.empty((H, W, 4), dtype=np.uint8)
            buf[:, :, 0] = bg.red()
            buf[:, :, 1] = bg.green()
            buf[:, :, 2] = bg.blue()
            buf[:, :, 3] = 255
            buf[bar_mask] = [c.red(), c.green(), c.blue(), alpha]
            # .copy() so the QImage owns its pixels after buf goes away
            return QImage(buf.data, W, H, W * 4, QImage.Format_RGBA8888).copy()

        self._img_played = _render(hi_c, 220)
        self._img_base = _render(dim_c, 180)
        self._img_key = (W, H, bg.rgba(), hi_c.rgba(), dim_c.rgba())

    def paintEvent(self, _):
        rect = self.rect()
        W, H = rect.width(), rect.height()
        if W <= 0 or H <= 0:
            return

        p = QPainter(self)
        pal = self.palette()
        bg = pal.base().color()

        if self._waveform is None:
            p.fillRect(rect, bg)
            p.end()
            return

        if self._no_ffmpeg:
            p.fillRect(rect, bg)
            p.setPen(pal.mid().color())
            p.drawText(rect, Qt.AlignCenter, "ffmpeg not found — waveform unavailable")
            p.end()
            return

        hi_c = self._accent if self._accent else self._idle_accent()
        dim_c = hi_c.darker(280)
        key = (W, H, bg.rgba(), hi_c.rgba(), dim_c.rgba())

        if self._px_wave is None or self._px_width != W or self._img_key != key:
            if self._px_wave is None or self._px_width != W:
                self._px_wave = self._resample(W)
                self._px_width = W
            self._rebuild_images(W, H, bg, hi_c, dim_c)

        pos_px = int(self._position * (W - 1)) if W > 1 else 0
        split = pos_px + 1
        if split > 0:
            src = QRect(0, 0, split, H)
            p.drawImage(src, self._img_played, src)
        if split < W:
            src = QRect(split, 0, W - split, H)
            p.drawImage(src, self._img_base, src)
        p.fillRect(pos_px, 0, 1, H, pal.text().color())
        p.end()

    def _frac(self, x) -> float:
        w = max(1, self.width() - 1)
        return max(0.0, min(1.0, float(x) / w))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dragging = True
            f = self._frac(e.position().x())
            self.set_position(f)
            self.seeked.emit(f)

    def mouseMoveEvent(self, e):
        if self._dragging:
            f = self._frac(e.position().x())
            self.set_position(f)
            self.seeked.emit(f)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            f = self._frac(e.position().x())
            self.set_position(f)
            self.seeked.emit(f)
        self._dragging = False

    def resizeEvent(self, e):
        self._px_wave = None
        super().resizeEvent(e)
