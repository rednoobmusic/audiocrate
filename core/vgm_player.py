"""
VGM/VGZ and other chiptune playback via libgme + Qt audio output.
Decodes in a background thread, pushes PCM into QAudioSink.
"""
from __future__ import annotations
import ctypes
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QAudioDevice, QMediaDevices

# ── libgme binding ─────────────────────────────────────────────────────────────
_lib = ctypes.CDLL("libgme.so")

_lib.gme_open_file.argtypes  = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
_lib.gme_open_file.restype   = ctypes.c_char_p

_lib.gme_track_count.argtypes = [ctypes.c_void_p]
_lib.gme_track_count.restype  = ctypes.c_int

_lib.gme_start_track.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.gme_start_track.restype  = ctypes.c_char_p

_lib.gme_play.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_short)]
_lib.gme_play.restype  = ctypes.c_char_p

_lib.gme_track_ended.argtypes = [ctypes.c_void_p]
_lib.gme_track_ended.restype  = ctypes.c_int

_lib.gme_delete.argtypes = [ctypes.c_void_p]
_lib.gme_delete.restype  = None

_lib.gme_set_fade.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.gme_set_fade.restype  = None

_lib.gme_tell.argtypes = [ctypes.c_void_p]
_lib.gme_tell.restype  = ctypes.c_int

_lib.gme_seek.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.gme_seek.restype  = ctypes.c_char_p

# Correct struct matching gme_info_t from gme.h (libgme 0.6.x)
class _GmeInfo(ctypes.Structure):
    _fields_ = [
        ("length",       ctypes.c_int),
        ("intro_length", ctypes.c_int),
        ("loop_length",  ctypes.c_int),
        ("play_length",  ctypes.c_int),
        ("fade_length",  ctypes.c_int),
        ("i5",  ctypes.c_int), ("i6",  ctypes.c_int), ("i7",  ctypes.c_int),
        ("i8",  ctypes.c_int), ("i9",  ctypes.c_int), ("i10", ctypes.c_int),
        ("i11", ctypes.c_int), ("i12", ctypes.c_int), ("i13", ctypes.c_int),
        ("i14", ctypes.c_int), ("i15", ctypes.c_int),
        # string pointers
        ("system",    ctypes.c_char_p),
        ("game",      ctypes.c_char_p),
        ("song",      ctypes.c_char_p),
        ("author",    ctypes.c_char_p),
        ("copyright", ctypes.c_char_p),
        ("comment",   ctypes.c_char_p),
        ("dumper",    ctypes.c_char_p),
        # reserved string pointers s7-s15
        ("s7",  ctypes.c_char_p), ("s8",  ctypes.c_char_p),
        ("s9",  ctypes.c_char_p), ("s10", ctypes.c_char_p),
        ("s11", ctypes.c_char_p), ("s12", ctypes.c_char_p),
        ("s13", ctypes.c_char_p), ("s14", ctypes.c_char_p),
        ("s15", ctypes.c_char_p),
    ]

_lib.gme_track_info.argtypes = [ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.POINTER(_GmeInfo)),
                                  ctypes.c_int]
_lib.gme_track_info.restype  = ctypes.c_char_p
_lib.gme_free_info.argtypes  = [ctypes.POINTER(_GmeInfo)]
_lib.gme_free_info.restype   = None

import numpy as np
from scipy.signal import lfilter
# DC blocker (one-pole high-pass ~7 Hz @ 44.1k) — removes the DC offset some chips emit.
_DC_B = np.array([1.0, -1.0], dtype=np.float64)
_DC_A = np.array([1.0, -0.999], dtype=np.float64)

_SAMPLE_RATE   = 44100
_CHANNELS      = 2
_CHUNK_FRAMES  = 1024
_CHUNK_SAMPLES = _CHUNK_FRAMES * _CHANNELS
_DEFAULT_LEN_MS = 150_000   # libgme's own default
_FADE_MS        = 4_000


def _make_format() -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(_SAMPLE_RATE)
    fmt.setChannelCount(_CHANNELS)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


def gme_tags(path: Path) -> dict:
    """Chiptune metadata via libgme (song/author/system/game/copyright/comment + track count)."""
    emu = ctypes.c_void_p()
    if _lib.gme_open_file(str(path).encode(), ctypes.byref(emu), _SAMPLE_RATE):
        return {}
    out = {"track_count": _lib.gme_track_count(emu)}
    try:
        info_ptr = ctypes.POINTER(_GmeInfo)()
        if not _lib.gme_track_info(emu, ctypes.byref(info_ptr), 0) and info_ptr:
            i = info_ptr.contents
            d = lambda p: (p.decode(errors="replace").strip() if p else "")
            out.update(song=d(i.song), author=d(i.author), system=d(i.system),
                       game=d(i.game), copyright=d(i.copyright), comment=d(i.comment),
                       dumper=d(i.dumper), length_ms=int(i.play_length))
            _lib.gme_free_info(info_ptr)
    finally:
        _lib.gme_delete(emu)
    return out


class VgmPlayer(QObject):
    state_changed    = Signal(bool)
    position_changed = Signal(float, int, int)
    finished         = Signal()
    waveform_chunk   = Signal(int, int, int, bytes)   # frames_so_far, total_frames, rate, int16 PCM

    def __init__(self, parent=None):
        super().__init__(parent)
        self._emu:      ctypes.c_void_p | None = None
        self._dur_ms:   int   = _DEFAULT_LEN_MS
        self._playing:  bool  = False
        self._stop_flag       = threading.Event()
        self._thread: threading.Thread | None = None
        self._volume: float   = 1.0
        self._sink:   QAudioSink | None = None
        self._device: QAudioDevice | None = None
        # libgme isn't thread-safe: serialize every emu access, deletes included.
        # Reentrant because load() holds it across _get_track_length(), which
        # takes it too.
        self._emu_lock = threading.RLock()
        self._dc_zi = None                  # per-channel DC-blocker filter state
        self._path:  Path | None = None     # currently loaded file (for subsong reloads)
        self._track: int = 0                # current subsong index

        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(100)
        self._pos_timer.timeout.connect(self._emit_position)

    # ── public API ──────────────────────────────────────────────────────────────

    def set_audio_device(self, device: QAudioDevice):
        self._device = device

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, val: float):
        self._volume = max(0.0, min(1.0, val))
        if self._sink:
            self._sink.setVolume(self._volume)

    @property
    def position_ms(self) -> int:
        with self._emu_lock:
            return _lib.gme_tell(self._emu) if self._emu else 0

    @property
    def duration_ms(self) -> int:
        return self._dur_ms

    @property
    def is_playing(self) -> bool:
        return self._playing

    def load(self, path: Path, track_idx: int = 0) -> bool:
        self.stop()
        self._dc_zi = None
        emu = ctypes.c_void_p()
        err = _lib.gme_open_file(str(path).encode(), ctypes.byref(emu), _SAMPLE_RATE)
        if err:
            return False
        with self._emu_lock:
            if self._emu:
                _lib.gme_delete(self._emu)
            self._emu = emu
            self._path = path
            self._track = track_idx
            self._dur_ms = self._get_track_length(track_idx)
            _lib.gme_set_fade(self._emu, max(0, self._dur_ms - _FADE_MS))
            err = _lib.gme_start_track(self._emu, track_idx)
            if err:
                _lib.gme_delete(self._emu)
                self._emu = None
                return False
        return True

    @property
    def track_count(self) -> int:
        """Number of subsongs in the loaded file (1 for single-tune formats)."""
        with self._emu_lock:
            return int(_lib.gme_track_count(self._emu)) if self._emu else 0

    @property
    def current_track(self) -> int:
        return self._track

    def play(self):
        if not self._emu or self._playing:
            return
        self._playing = True
        self._stop_flag.clear()
        dev = self._device or QMediaDevices.defaultAudioOutput()
        self._sink = QAudioSink(dev, _make_format())
        self._sink.setVolume(self._volume)
        self._sink.setBufferSize(_SAMPLE_RATE // 7 * _CHANNELS * 2)   # ~140ms — avoid underruns
        self._io = self._sink.start()
        self._thread = threading.Thread(target=self._decode_loop, daemon=True)
        self._thread.start()
        self._pos_timer.start()
        self.state_changed.emit(True)

    def pause(self):
        if not self._playing:
            return
        self._playing = False
        self._stop_flag.set()
        if self._sink:
            self._sink.suspend()
        self._pos_timer.stop()
        self.state_changed.emit(False)

    def resume(self):
        if self._playing or not self._emu:
            return
        if not self._sink:
            # play() sets _playing, clears the flag and starts everything itself.
            # It early-returns on _playing, so this has to happen before we set it.
            self.play()
            return
        # pause() only sets _stop_flag, it never joins. Retire the old decode
        # thread before clearing the flag, or it survives the clear and two
        # threads end up writing to one QIODevice.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._playing = True
        self._stop_flag.clear()
        self._sink.resume()
        self._thread = threading.Thread(target=self._decode_loop, daemon=True)
        self._thread.start()
        self._pos_timer.start()
        self.state_changed.emit(True)

    def stop(self):
        self._playing = False
        self._stop_flag.set()
        self._pos_timer.stop()
        if self._thread and self._thread.is_alive():
            # The decode loop checks _stop_flag every iteration and sleeps in 5 ms
            # steps, so it exits well inside this. The old 0.5 s could expire while
            # the loop still held the sink, and the teardown below then ran under it.
            self._thread.join(timeout=5.0)
        # QAudioSink.stop() destroys the QIODevice start() handed out. Doing that
        # while the decode thread is still in _io.write() is a write to freed
        # memory. If the thread somehow outlived the join, leave the sink alone —
        # audio keeps playing until it exits, which beats a segfault.
        if self._thread and self._thread.is_alive():
            from applog import log
            log.warning("vgm decode thread outlived stop(); leaving sink up")
            return
        self._thread = None
        if self._sink:
            self._sink.stop()
            self._sink = None
        self._io = None

    def toggle(self):
        if self._playing:
            self.pause()
        else:
            self.resume()

    def seek(self, frac: float):
        if not self._emu:
            return
        target_ms = int(frac * self._dur_ms)
        with self._emu_lock:
            if self._emu:
                _lib.gme_seek(self._emu, target_ms)

    def cleanup(self):
        self.stop()
        with self._emu_lock:
            if self._emu:
                _lib.gme_delete(self._emu)
                self._emu = None

    # ── internals ───────────────────────────────────────────────────────────────

    def _get_track_length(self, idx: int) -> int:
        info_ptr = ctypes.POINTER(_GmeInfo)()
        with self._emu_lock:
            if not self._emu:
                return _DEFAULT_LEN_MS
            err = _lib.gme_track_info(self._emu, ctypes.byref(info_ptr), idx)
        if not err and info_ptr:
            length = info_ptr.contents.play_length
            _lib.gme_free_info(info_ptr)
            if length > 0:
                return length
        return _DEFAULT_LEN_MS

    def _decode_loop(self):
        import time
        buf = (ctypes.c_short * _CHUNK_SAMPLES)()

        while not self._stop_flag.is_set():
            with self._emu_lock:
                if not self._emu or _lib.gme_track_ended(self._emu):
                    break
                pos_ms = _lib.gme_tell(self._emu)
                if self._dur_ms > 0 and pos_ms >= self._dur_ms:
                    break                       # hard end at reported length — no infinite loop
                err = _lib.gme_play(self._emu, _CHUNK_SAMPLES, buf)
            if err:
                break

            data   = self._dc_correct(bytes(buf))   # strip chip DC offset
            # feed the analyzer/waveform live (ffmpeg can't decode these formats)
            self.waveform_chunk.emit(int(pos_ms * _SAMPLE_RATE / 1000),
                                     int(self._dur_ms * _SAMPLE_RATE / 1000),
                                     _SAMPLE_RATE, data)
            offset = 0
            while offset < len(data) and not self._stop_flag.is_set():
                # Read both once per pass: stop() can null them between the guard
                # and the write, and `self._io.write` would then raise on None.
                sink, io = self._sink, self._io
                if sink is None or io is None:
                    break
                free = sink.bytesFree()
                if free <= 0:
                    time.sleep(0.005)
                    continue
                chunk   = data[offset: offset + min(free, len(data) - offset)]
                written = io.write(chunk)
                if written > 0:
                    offset += written
                else:
                    time.sleep(0.005)

        if not self._stop_flag.is_set():
            # Natural end — wait for sink to drain before signalling
            if self._sink:
                import time as _t
                drain_deadline = _t.monotonic() + 2.0
                while (self._sink.state().name != "IdleState"
                       and _t.monotonic() < drain_deadline):
                    _t.sleep(0.02)
            self._playing = False
            self._pos_timer.stop()
            self.state_changed.emit(False)
            self.finished.emit()

    def _dc_correct(self, data: bytes) -> bytes:
        """Strip per-channel DC offset (one-pole high-pass, state carried between
        chunks). Some chips (AY, etc.) emit a constant offset that pins the waveform
        off-centre and ruins the oscilloscope; this re-centres it."""
        x = np.frombuffer(data, dtype=np.int16)
        if x.size < 2:
            return data
        x = x.astype(np.float64).reshape(-1, 2)
        if self._dc_zi is None:
            self._dc_zi = [np.zeros(1), np.zeros(1)]
        out = np.empty_like(x)
        for ch in range(2):
            out[:, ch], self._dc_zi[ch] = lfilter(_DC_B, _DC_A, x[:, ch], zi=self._dc_zi[ch])
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()

    def _emit_position(self):
        if not self._emu:
            return
        with self._emu_lock:
            pos = _lib.gme_tell(self._emu) if self._emu else 0
        frac = pos / self._dur_ms if self._dur_ms > 0 else 0.0
        self.position_changed.emit(frac, pos, self._dur_ms)
