"""Playback and rendering of .fur modules through libfurnace_wrap (libfurnace_wrap.so, libfurnace_wrap.dll on Windows).

FurPlayer is a QObject that drives a QAudioSink from a render thread, fur_tags reads title, artist and
system for a player.py Track, and FUR_EXTS is the set of supported extensions.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import tempfile
from pathlib import Path

import numpy as np

import plat_native
import plat_paths
from native_lock import RENDER_LOCK

from PySide6.QtCore    import QObject, Signal, QTimer
from PySide6.QtMultimedia import QAudioSink, QAudioFormat, QAudioDevice

# load the shared library

_SO_SEARCH = [
    Path(__file__).parent / "libfurnace_wrap.so",
    Path(__file__).parent.parent / "libfurnace_wrap.so",   # project root (folder layout)
    Path("/usr/local/lib/libfurnace_wrap.so"),
]

_LIB_FILE = "libfurnace_wrap.dll" if sys.platform == "win32" else "libfurnace_wrap.so"
_lib: ctypes.CDLL | None = plat_native.load(_SO_SEARCH, _LIB_FILE)

FUR_EXTS = {".fur"}

# C struct

class _FurnaceInfo(ctypes.Structure):
    _fields_ = [
        ("title",         ctypes.c_char * 256),
        ("author",        ctypes.c_char * 256),
        ("system",        ctypes.c_char * 256),
        ("channel_count", ctypes.c_int),
        ("order_len",     ctypes.c_int),
        ("pattern_len",   ctypes.c_int),
        ("hz",            ctypes.c_double),
        ("duration_us",   ctypes.c_int64),
        ("subsong_count", ctypes.c_int),
    ]

# wire up signatures

def _wire():
    if _lib is None:
        return
    vp = ctypes.c_void_p
    fp = ctypes.POINTER(ctypes.c_float)

    _lib.fw_create.restype   = vp
    _lib.fw_destroy.argtypes = [vp]
    _lib.fw_destroy.restype  = None

    _lib.fw_read_meta.argtypes = [vp, ctypes.c_char_p, ctypes.POINTER(_FurnaceInfo)]
    _lib.fw_read_meta.restype  = ctypes.c_int

    _lib.fw_load.argtypes = [vp, ctypes.c_char_p, ctypes.POINTER(_FurnaceInfo)]
    _lib.fw_load.restype  = ctypes.c_int
    _lib.fw_set_sample_rate.argtypes = [vp, ctypes.c_int]
    _lib.fw_set_sample_rate.restype  = None

    _lib.fw_channel_name.argtypes = [vp, ctypes.c_int]
    _lib.fw_channel_name.restype  = ctypes.c_char_p

    _lib.fw_play.argtypes  = [vp, ctypes.c_int]
    _lib.fw_play.restype   = None
    _lib.fw_stop.argtypes  = [vp]
    _lib.fw_stop.restype   = None
    _lib.fw_is_playing.argtypes = [vp]
    _lib.fw_is_playing.restype  = ctypes.c_int

    _lib.fw_seek_order.argtypes = [vp, ctypes.c_int]
    _lib.fw_seek_order.restype  = None

    _lib.fw_current_order.argtypes = [vp]
    _lib.fw_current_order.restype  = ctypes.c_int
    _lib.fw_current_row.argtypes   = [vp]
    _lib.fw_current_row.restype    = ctypes.c_int

    _lib.fw_order_row_to_us.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    _lib.fw_order_row_to_us.restype  = ctypes.c_int64

    _lib.fw_render.argtypes = [vp, fp, ctypes.c_int]
    _lib.fw_render.restype  = ctypes.c_int

    _lib.fw_channel_vu.argtypes = [vp, ctypes.c_int]
    _lib.fw_channel_vu.restype  = ctypes.c_float
    _lib.fw_channel_visible.argtypes = [vp, ctypes.c_int]
    _lib.fw_channel_visible.restype  = ctypes.c_int
    _lib.fw_channel_scope.argtypes = [vp, ctypes.c_int, fp, ctypes.c_int]
    _lib.fw_channel_scope.restype  = ctypes.c_int

    _lib.fw_sample_rate.argtypes = [vp]
    _lib.fw_sample_rate.restype  = ctypes.c_int

    _lib.fw_select_subsong.argtypes = [vp, ctypes.c_int, ctypes.POINTER(_FurnaceInfo)]
    _lib.fw_select_subsong.restype  = ctypes.c_int

    _lib.fw_read_meta_path.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_FurnaceInfo),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    _lib.fw_read_meta_path.restype  = ctypes.c_int

    _lib.fw_render_wav_path.argtypes = [
        ctypes.c_char_p,   # path
        ctypes.c_char_p,   # out_wav
        ctypes.c_int,      # loops
        ctypes.POINTER(ctypes.c_int),  # cancel_flag (or None)
    ]
    _lib.fw_render_wav_path.restype  = ctypes.c_int

    _lib.fw_stream_load.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int64)]
    _lib.fw_stream_load.restype  = ctypes.c_int
    _lib.fw_stream_read.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_int]
    _lib.fw_stream_read.restype  = ctypes.c_int
    _lib.fw_stream_stop.argtypes = []
    _lib.fw_stream_stop.restype  = None

_wire()


# global engine lock
# The C++ g_meta_mutex serializes calls on the C++ side, but ctypes calls release
# the GIL, so Python threads can still interleave in ways that make the C++
# allocator see concurrent heap operations with the mutex held. This RLock lets
# only one thread at a time into any global-engine C function.
_global_engine_lock = threading.RLock()

# fur_tags() reads metadata by loading the module into the same singleton engine
# a live stream is pulling from, which would swap the song out from under
# playback. Tags never change for a given file, so they are cached, and while a
# stream is running they are answered from the cache without touching the engine.
_TAG_CACHE: dict = {}
_TAG_LOCK = threading.Lock()
_stream_busy = False


def _tag_key(path: Path):
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), 0, 0)


# metadata helper

_CH_STRIDE = 32   # bytes per channel name slot in the flat buffer

def fur_tags(path: Path) -> dict:
    """Return {'title', 'artist', 'album', 'system', 'channel_count',
               'duration_us', 'channel_names'} or minimal fallback.

    Uses the global-singleton fw_read_meta_path (mutex-protected) so it is
    safe to call from multiple threads without creating multiple DivEngine
    instances (which corrupts global static state in DivEngine).
    """
    if _lib is None:
        return {"title": path.stem, "artist": "", "album": ""}
    key = _tag_key(path)
    with _TAG_LOCK:
        hit = _TAG_CACHE.get(key)
    if hit is not None:
        return hit
    if _stream_busy:
        # A stream owns the engine; loading this module now would cut playback.
        return {"title": path.stem, "artist": "", "album": ""}
    info = _FurnaceInfo()
    # Pre-allocate a flat char buffer for up to 128 channel names
    max_ch = 128
    ch_buf = ctypes.create_string_buffer(max_ch * _CH_STRIDE)
    with _global_engine_lock:
        rc = _lib.fw_read_meta_path(
            str(path).encode(),
            ctypes.byref(info),
            ch_buf,
            ctypes.c_int(_CH_STRIDE),
            ctypes.c_int(max_ch),
        )
    if rc != 0:
        return {"title": path.stem, "artist": "", "album": ""}
    n = min(info.channel_count, max_ch)
    names = []
    raw = ch_buf.raw
    for i in range(n):
        slot = raw[i * _CH_STRIDE : (i + 1) * _CH_STRIDE]
        name = slot.split(b"\x00", 1)[0].decode(errors="replace")
        names.append(name or "?")
    out = {
        "title":         info.title.decode(errors="replace") or path.stem,
        "artist":        info.author.decode(errors="replace"),
        "album":         info.system.decode(errors="replace"),
        "system":        info.system.decode(errors="replace"),
        "channel_count": info.channel_count,
        "duration_us":   info.duration_us,
        "subsong_count": info.subsong_count,
        "channel_names": names,
    }
    with _TAG_LOCK:
        _TAG_CACHE[key] = out
        if len(_TAG_CACHE) > 512:
            _TAG_CACHE.pop(next(iter(_TAG_CACHE)))
    return out


# FurPlayer

_CHUNK      = 1024          # frames per render call
_LOOPS      = 1             # single pass; Player controls repeat policy
_WF_LOOPS   = 1             # waveform pre-render: single pass for speed
_RATE       = 44100         # fallback; actual rate read from engine after load
_WF_EMIT_EVERY = 4          # emit waveform_chunk every N render chunks (~90 ms), for a smooth scope


class FurPlayer(QObject):
    """Plays a single .fur file: renders on a dedicated thread, pushes the audio to a QAudioSink and emits
    position_changed(frac, ms, dur_ms) at about 10 Hz."""

    loaded           = Signal(dict)              # metadata dict from fur_tags
    finished         = Signal()                  # looping done / stopped
    position_changed = Signal(float, int, int)   # frac, pos_ms, dur_ms
    error            = Signal(str)
    waveform_chunk   = Signal(int, int, int, bytes)  # frames_so_far, total_frames, rate, pcm_bytes

    def __init__(self, audio_device: QAudioDevice | None = None, parent=None):
        super().__init__(parent)
        if _lib is None:
            raise RuntimeError(f"{_LIB_FILE} not found, so .fur files cannot be played")

        # DivEngine keeps its chip-definition table in a static array
        # (engine.h: `static DivSysDef* sysDefs[]`) but tracks registration with
        # a per-instance flag, and its constructor memsets that shared table.
        # Constructing a second DivEngine wipes the first one's registrations
        # and leaves pointers that get freed twice, which showed up as an
        # intermittent "free(): invalid pointer" during .fur playback. The wrapper
        # already keeps a global singleton engine for fw_read_meta_path and
        # fw_render_wav_path, so this instance engine would be a second one. It is
        # created lazily: .fur files render through the DSP engine and nothing uses
        # native playback, so in practice it is never created.
        self.__handle = None
        self._meta: dict      = {}
        self._duration_us: int = 0
        self._device   = audio_device

        self._render_stop   = threading.Event()
        self._render_thread: threading.Thread | None = None
        self._rendered_frames = 0          # total frames output since play()
        self._lock = threading.Lock()
        self._engine_lock = threading.RLock()
        self._stream_gen = 0      # retires superseded stream readers
        self._flush_after_seek = False

        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(100)
        self._pos_timer.timeout.connect(self._emit_position)

        self._base_vol = 0.7
        self._sink: QAudioSink | None = None   # created on render thread
        self._rate: int = _RATE                # actual engine sample rate

    # load

    def load(self, path: Path) -> bool:
        self.stop()
        # Match engine render rate to output device when possible to avoid
        # extra resampling artifacts.
        try:
            # Avoid repeated global device probing during hot playback paths.
            # On some Qt/PipeWire stacks this can crash in native code.
            target_rate = int(self._rate) if self._rate > 0 else 48000
            if self._device is not None:
                pf = self._device.preferredFormat()
                target_rate = int(pf.sampleRate())
            if target_rate <= 0:
                target_rate = 48000
            with self._engine_lock:
                _lib.fw_set_sample_rate(self._handle, int(target_rate))
        except Exception:
            pass
        info = _FurnaceInfo()
        with self._engine_lock:
            rc = _lib.fw_load(self._handle, str(path).encode(), ctypes.byref(info))
        if rc != 0:
            self.error.emit(f"Failed to load: {path}")
            return False
        with self._engine_lock:
            self._rate = _lib.fw_sample_rate(self._handle) or _RATE
        self._duration_us = max(1, info.duration_us)
        # Under the lock like every other engine call: these are pointers into
        # engine-owned memory, and a concurrent load frees them mid-iteration.
        with self._engine_lock:
            names = [
                (_lib.fw_channel_name(self._handle, i) or b"").decode(errors="replace")
                for i in range(info.channel_count)
            ]
        self._meta = {
            "title":         info.title.decode(errors="replace") or path.stem,
            "artist":        info.author.decode(errors="replace"),
            "album":         info.system.decode(errors="replace"),
            "system":        info.system.decode(errors="replace"),
            "channel_count": info.channel_count,
            "duration_us":   self._duration_us,
            "channel_names": names,
            "order_len":     info.order_len,
            "pattern_len":   info.pattern_len,
            "hz":            info.hz,
            "subsong_count": info.subsong_count,
        }
        self.loaded.emit(self._meta)
        return True

    def select_subsong(self, idx: int) -> bool:
        """Switch to a different sub-song in the loaded .fur. Returns True on success."""
        info = _FurnaceInfo()
        with self._engine_lock:
            rc = _lib.fw_select_subsong(self._handle, idx, ctypes.byref(info))
        if rc != 0:
            return False
        self._duration_us = max(1, info.duration_us)
        self._meta.update({
            "duration_us":  self._duration_us,
            "order_len":    info.order_len,
            "pattern_len":  info.pattern_len,
            "hz":           info.hz,
        })
        return True

    # playback

    def play(self, loops: int = _LOOPS):
        self.stop()
        with self._lock:
            self._rendered_frames = 0
        with self._engine_lock:
            _lib.fw_play(self._handle, loops)
        self._render_stop.clear()
        self._render_thread = threading.Thread(
            target=self._render_loop, daemon=True
        )
        self._render_thread.start()
        # a QTimer must be started from the Qt (main) thread, hence the queued invoke
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self._pos_timer, "start", Qt.QueuedConnection)

    def stop(self):
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self._pos_timer, "stop", Qt.QueuedConnection)
        self._render_stop.set()
        # Suspend the sink so dev.write() unblocks immediately
        if self._sink is not None:
            try:
                self._sink.suspend()
            except Exception:
                pass
        if self._render_thread:
            self._render_thread.join(timeout=1.0)
            self._render_thread = None
        with self._engine_lock:
            _lib.fw_stop(self._handle)
        with self._lock:
            self._rendered_frames = 0

    def is_playing(self) -> bool:
        with self._engine_lock:
            return _lib.fw_is_playing(self._handle) != 0

    # volume

    @property
    def volume(self) -> float:
        return self._base_vol

    @volume.setter
    def volume(self, val: float):
        self._base_vol = max(0.0, min(1.0, val))
        if self._sink:
            self._sink.setVolume(self._base_vol)

    # seek

    def seek(self, frac: float):
        """Seek to fractional position (0..1). Snaps to nearest order."""
        if not self._meta:
            return
        order_len = self._meta.get("order_len", 1)
        target_order = max(0, min(order_len - 1, int(frac * order_len)))
        with self._engine_lock:
            _lib.fw_seek_order(self._handle, target_order)
        # Update rendered_frames counter so position_ms stays coherent
        with self._engine_lock:
            us = _lib.fw_order_row_to_us(self._handle, target_order, 0)
        with self._lock:
            self._rendered_frames = int(us / 1_000_000 * self._rate)
            self._flush_after_seek = True

    # position

    @property
    def position_ms(self) -> int:
        with self._lock:
            return int(self._rendered_frames / self._rate * 1000)

    @property
    def duration_ms(self) -> int:
        return int(self._duration_us / 1000)

    @property
    def position_frac(self) -> float:
        dur = self._duration_us
        if dur <= 0:
            return 0.0
        pos_us = int(self._rendered_frames / self._rate * 1_000_000)
        return min(1.0, pos_us / dur)

    def _emit_position(self):
        frac   = self.position_frac
        pos_ms = self.position_ms
        dur_ms = self.duration_ms
        self.position_changed.emit(frac, pos_ms, dur_ms)

    # per-channel VU

    def channel_vu(self, ch: int) -> float:
        if not self._engine_lock.acquire(blocking=False):
            return 0.0
        try:
            return _lib.fw_channel_vu(self._handle, ch)
        finally:
            self._engine_lock.release()

    def channel_scope(self, ch: int, max_samples: int = 2048) -> list[float]:
        if max_samples <= 0:
            return []
        count = min(int(max_samples), 32768)
        FloatArr = ctypes.c_float * count
        buf = FloatArr()
        if not self._engine_lock.acquire(blocking=False):
            return []
        try:
            n = int(_lib.fw_channel_scope(self._handle, int(ch), buf, count))
        finally:
            self._engine_lock.release()
        if n <= 0:
            return []
        return [float(buf[i]) for i in range(n)]

    def channel_visible(self, ch: int) -> bool:
        try:
            if not self._engine_lock.acquire(blocking=False):
                return True
            try:
                return bool(_lib.fw_channel_visible(self._handle, int(ch)))
            finally:
                self._engine_lock.release()
        except Exception:
            return True

    # render thread

    def _render_loop(self):
        fmt = QAudioFormat()
        fmt.setSampleRate(self._rate)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)

        sink = QAudioSink(fmt) if self._device is None else QAudioSink(self._device, fmt)
        sink.setVolume(self._base_vol)
        sink.setBufferSize(self._rate // 5 * 2 * ctypes.sizeof(ctypes.c_float))
        dev = sink.start()
        self._sink = sink

        if dev is None:
            self.error.emit("QAudioSink failed to open for .fur playback")
            return

        # interleaved stereo float buffer, _CHUNK frames
        FloatArr = ctypes.c_float * (_CHUNK * 2)
        buf = FloatArr()

        total_frames = int(self._duration_us * self._rate / 1_000_000) if self._duration_us > 0 else 0
        wf_chunk_counter = 0
        wf_pcm_accum: list[bytes] = []   # accumulate raw stereo float32 chunks

        try:
            while not self._render_stop.is_set():
                with self._engine_lock:
                    playing = _lib.fw_is_playing(self._handle)
                if not playing:
                    break

                with self._engine_lock:
                    n = _lib.fw_render(self._handle, buf, _CHUNK)
                if n <= 0:
                    break

                with self._lock:
                    self._rendered_frames += n

                raw = bytes(buf)[:n * 2 * ctypes.sizeof(ctypes.c_float)]

                # Accumulate PCM for waveform-bar live preview
                wf_pcm_accum.append(raw)
                wf_chunk_counter += 1
                if wf_chunk_counter >= _WF_EMIT_EVERY:
                    wf_chunk_counter = 0
                    pcm_blob = b"".join(wf_pcm_accum)
                    wf_pcm_accum = []
                    with self._lock:
                        frames_so_far = self._rendered_frames
                    self.waveform_chunk.emit(frames_so_far, total_frames, self._rate, pcm_blob)

                if self._flush_after_seek:
                    with self._lock:
                        self._flush_after_seek = False
                    # reset() destroys the QIODevice, so it has to be opened again with start()
                    try:
                        sink.reset()
                        new_dev = sink.start()
                        if new_dev is not None:
                            dev = new_dev
                    except Exception:
                        pass
                # the sink takes only what it has room for, so write until the block is in
                written = 0
                total = len(raw)
                while written < total and not self._render_stop.is_set():
                    try:
                        chunk = dev.write(raw[written:])
                    except RuntimeError:
                        break
                    if chunk > 0:
                        written += chunk
                    else:
                        import time; time.sleep(0.001)
        finally:
            sink.stop()
            self._sink = None
            if not self._render_stop.is_set():
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(self, "finished", Qt.QueuedConnection)

    # waveform pre-render

    def stream_reader(self, path: Path, rate: int):
        """A chunk-at-a-time reader for DspEngine.load_stream().

        Rendering a module to a WAV before playing it costs a second or more for
        anything long (emulation runs 20-44x realtime). Pulling the engine
        incrementally starts playback on the first chunk, measured at about 35 ms
        to audio against seconds.

        Runs on the wrapper's singleton engine and has to stay there: DivEngine
        stores chip definitions in a static array but registers them per instance
        and memsets that array in its constructor, so a second DivEngine anywhere
        in the process corrupts the first and crashes with "free(): invalid
        pointer". Never give a stream its own handle.

        Returns (reader, duration_ms) or (None, 0) if the module won't load.
        """
        if _lib is None:
            return None, 0
        dur = ctypes.c_int64(0)
        with RENDER_LOCK:
            rc = _lib.fw_stream_load(str(path).encode(), int(rate), _LOOPS,
                                     ctypes.byref(dur))
        if rc != 0:
            return None, 0
        dur_ms = int(dur.value // 1000)
        if dur_ms <= 0:
            _lib.fw_stream_stop()
            return None, 0
        global _stream_busy
        _stream_busy = True

        CH = 8192
        buf = (ctypes.c_int16 * (CH * 2))()
        # Each reader owns the engine only until the next load supersedes it.
        gen = self._stream_gen = self._stream_gen + 1

        def _read(frames: int):
            if gen != self._stream_gen:
                return None                       # a newer track took the engine
            want = min(frames, CH)
            with RENDER_LOCK:
                if gen != self._stream_gen:
                    return None
                n = _lib.fw_stream_read(buf, want)
            if n <= 0:
                return None
            return np.frombuffer(bytes(buf), dtype=np.int16)[:n * 2].reshape(-1, 2).copy()

        def _close():
            global _stream_busy
            with RENDER_LOCK:
                if gen == self._stream_gen:       # don't stop a newer stream
                    _lib.fw_stream_stop()
                    _stream_busy = False
        _read.close = _close
        return _read, dur_ms

    def render_cached(self, path: Path, cancel: threading.Event | None = None) -> str | None:
        """Full-song render, cached on disk by (path, mtime, size) so repeat plays are instant. Shares the
        chiptune render cache with cli_render.

        `cancel` aborts a render in progress. Rendering holds an exclusive lock, so an abandoned render
        (the user clicked past it) would otherwise make every later one queue behind it, and clicking
        through a playlist of modules would wait for renders nobody wants any more.
        """
        import hashlib
        cache = plat_paths.cache_dir() / "chiprender"
        try:
            st = path.stat()
            key = f"{path}|{st.st_mtime_ns}|{st.st_size}|fur"
        except OSError:
            return None
        out = cache / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".wav")
        if out.exists() and out.stat().st_size > 1024:
            return str(out)
        wav = self.render_region_wav(path, cancel=cancel)
        if not wav:
            return None
        try:
            cache.mkdir(parents=True, exist_ok=True)
            # shutil.move, not Path.replace: the render lands in /tmp (tmpfs) and
            # the cache is on disk, and a rename across devices fails.
            import shutil
            shutil.move(wav, out)
            from native_lock import prune_cache
            prune_cache(cache)
            return str(out)
        except OSError:
            return wav      # fall back to the temp file if the cache isn't writable

    def render_region_wav(self, path: Path, cancel: threading.Event | None = None) -> str | None:
        """Render the full song (_WF_LOOPS passes) into a temp WAV on the global singleton engine
        (fw_render_wav_path), the same one fur_tags uses, so the process never has a third DivEngine.
        Returns the temp file path, or None on failure or cancel."""
        if _lib is None:
            return None
        try:
            fd, tmp = tempfile.mkstemp(suffix="_fur.wav")
            os.close(fd)

            # Shared cancel flag (int in C memory, polled by C side)
            cancel_int = ctypes.c_int(0)
            poll_stop = threading.Event()

            def _poll_cancel():
                import time
                while not poll_stop.is_set():
                    if cancel and cancel.is_set():
                        cancel_int.value = 1
                        return
                    time.sleep(0.1)

            poll_thread = None
            if cancel:
                poll_thread = threading.Thread(target=_poll_cancel, daemon=True)
                poll_thread.start()

            try:
                with RENDER_LOCK, _global_engine_lock:
                    rc = _lib.fw_render_wav_path(
                        str(path).encode(),
                        tmp.encode(),
                        ctypes.c_int(_WF_LOOPS),
                        ctypes.byref(cancel_int),
                    )
            finally:
                poll_stop.set()

            if rc != 0:
                Path(tmp).unlink(missing_ok=True)
                return None
            return tmp
        except Exception:
            import traceback
            traceback.print_exc()
            return None

    def stop_stream(self):
        """Force-retire any live streaming render.

        The furnace singleton keeps emulating while a stream is open. Anything
        that starts another native chiptune engine (SID) should call this first
        so the two never overlap.
        """
        global _stream_busy
        if _lib is None:
            return
        with RENDER_LOCK:
            self._stream_gen += 1        # retire outstanding readers
            try:
                _lib.fw_stream_stop()
            except Exception:
                pass
            _stream_busy = False

    @property
    def _handle(self):
        """The native engine, created on first actual use (see __init__)."""
        if self.__handle is None:
            self.__handle = _lib.fw_create()
        return self.__handle

    @property
    def has_engine(self) -> bool:
        return self.__handle is not None

    # cleanup

    def __del__(self):
        if not hasattr(self, "_render_stop"):
            return                      # __init__ stopped before it got this far (no libfurnace_wrap)
        self._render_stop.set()
        if self._render_thread:
            try:
                self._render_thread.join(timeout=2.0)
            except Exception:
                pass
            if self._render_thread.is_alive():
                # Never tear down under a live render thread. Clearing __handle
                # would make the _handle property fw_create() a *second*
                # DivEngine on that thread's next call, the shared-sysDefs
                # double-free described in __init__. Leaking one engine while
                # the object goes away is the cheaper outcome.
                return
            self._render_thread = None
        handle = self.__handle
        self.__handle = None
        if _lib and handle:
            try:
                with self._engine_lock:
                    _lib.fw_destroy(handle)
            except Exception:
                pass
