"""
VGM/VGZ and other chiptune playback via libgme + Qt audio output.
Decodes in a background thread, pushes PCM into QAudioSink. With a stage attached
(set_stage) the decoded blocks go through the player's EQ, DSP chain and Enhance first.
The decode thread makes the sink, writes to it and stops it: a QAudioSink that is used from a thread it was not made in
crashes inside Qt now and then. The volume stays at the sink (the decode thread sets it, in a ramp of at most 20 ms), so a
change is heard within a few milliseconds instead of after the 140 ms queue. At the end of a track what the stage still
holds (reverb tails, look-ahead) is written before the sink drains.
"""
from __future__ import annotations
import ctypes
import threading
import time
from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject, Qt, Signal, QTimer
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QAudioDevice, QMediaDevices

# libgme binding
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

# matches gme_info_t from gme.h (libgme 0.6.x)
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
from dsp_base import Context
# DC blocker: one-pole high-pass, about 7 Hz at 44.1 kHz, removes the DC offset some chips emit
_DC_B = np.array([1.0, -1.0], dtype=np.float64)
_DC_A = np.array([1.0, -0.999], dtype=np.float64)

_SAMPLE_RATE   = 44100
_CHANNELS      = 2
_CHUNK_FRAMES  = 1024
_CHUNK_SAMPLES = _CHUNK_FRAMES * _CHANNELS
_DEFAULT_LEN_MS = 150_000   # libgme's own default
_STALL_S = 4.0              # the sink has had no room for this long while we wait to write: its output is lost, or a device that wakes up slowly
_FADE_MS        = 4_000
_ANALYSE_S      = 30.0      # how much of a track is rendered to measure its brightness for Enhance
_VOL_TICK_S     = 0.004     # a volume change reaches the sink in steps this far apart,
_VOL_STEP       = 0.2       # none larger than this and at most five of them (16 ms), so it never steps
_ratios: dict = {}          # (path, subsong, rate) -> brightness, so a replay or a subsong switch costs nothing


def _make_format(rate: int = _SAMPLE_RATE) -> QAudioFormat:
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
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
    stalled          = Signal()                       # the sink stopped taking audio (its output is likely gone), from the decode thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self._emu:      ctypes.c_void_p | None = None
        self._dur_ms:   int   = _DEFAULT_LEN_MS
        self._playing:  bool  = False
        self._stop_flag       = threading.Event()
        self._pause_flag      = threading.Event()   # the decode thread suspends its sink and waits
        self._wake            = threading.Event()   # set by a volume change: the decode thread's naps end at once
        self._thread: threading.Thread | None = None
        self._volume: float   = 1.0
        self._sink:   QAudioSink | None = None      # made, used and stopped by the decode thread only
        self._io = None
        self._device: QAudioDevice | None = None
        # libgme isn't thread-safe: serialize every emu access, deletes included.
        # Reentrant because load() holds it across _get_track_length(), which
        # takes it too.
        self._emu_lock = threading.RLock()
        self._dc_zi = None                  # per-channel DC-blocker filter state
        self._stage = None                  # shared EQ / DSP chain / Enhance stage (a DspEngine), or None
        self._rate = _SAMPLE_RATE           # rate libgme renders at and the sink runs at, fixed at load()
        self._stage_dirty = False           # the decode thread clears the stage's memory before its next block
        self._stage_used = False            # this player has put audio through the stage since its last reset
        self._stage_failed = False          # the stage raised: play the raw blocks
        self._path:  Path | None = None     # currently loaded file (for subsong reloads)
        self._track: int = 0                # current subsong index
        self._adapt_gen = 0                 # bumps with every load, so a late measurement of an old track is dropped
        self._analysis: threading.Thread | None = None
        self._vol_at = 1.0                  # the volume the sink has been given (decode thread)
        self._vol_goal = 1.0                # the volume the steps below lead to
        self._vol_steps: list = []          # the steps of a volume change that are still to come
        self._vol_next = 0.0                # when the next one is due
        self._stalled = False               # stalled has been said for the sink in use (decode thread)

        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(100)
        self._pos_timer.timeout.connect(self._emit_position)

    # public API

    def set_stage(self, stage):
        """Route the sound through `stage`, the player's shared EQ, DSP chain and Enhance (a DspEngine).

        It needs process_external(), reset_stage(), fs and volume; process_external_flush() and
        set_external_adaptive() are used when it has them. libgme then renders at stage.fs from the next
        load(). The volume is the sink's, so a change is heard at once; the stage sees it as Context.volume
        (loudness compensation) and puts none into the sound. Without a stage the player runs
        at 44.1 kHz with a sink volume.
        """
        self._stage = stage

    def set_audio_device(self, device: QAudioDevice):
        """Move chiptune playback to another output.

        Goes through stop() and play() rather than swapping the sink in place:
        stop() already retires the decode thread before tearing the sink down,
        and getting that order wrong is what freed the emulator under a live
        render once before. Neither touches the emulator, so playback carries on
        from where it was.

        When stop() cannot retire the thread it leaves the old sink up and says
        so in the log. Starting a second sink over that would put two threads on
        one QIODevice, so in that case the new device waits for the next track.
        """
        self._device = device
        paused = not self._playing and self._thread is not None and self._thread.is_alive()
        if not self._playing and not paused:
            return               # play() builds the next sink from _device
        self.stop()              # a paused player has a thread and a sink too, still on the old output
        if self._sink is None and not paused:
            self.play()          # paused stays paused: resume() starts a thread on the new output

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, val: float):
        self._volume = max(0.0, min(1.0, val))      # the decode thread passes it on to the sink
        self._wake.set()

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
        self._stage_failed = False
        self._rate = self._stage_rate() or _SAMPLE_RATE
        emu = ctypes.c_void_p()
        err = _lib.gme_open_file(str(path).encode(), ctypes.byref(emu), self._rate)
        if err:
            return False
        with self._emu_lock:
            if self._emu:
                _lib.gme_delete(self._emu)
            self._emu = emu
            self._stage_dirty = True
            self._path = path
            self._track = track_idx
            self._dur_ms = self._get_track_length(track_idx)
            _lib.gme_set_fade(self._emu, max(0, self._dur_ms - _FADE_MS))
            err = _lib.gme_start_track(self._emu, track_idx)
            if err:
                _lib.gme_delete(self._emu)
                self._emu = None
                return False
        self._start_analysis(path, track_idx)
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
        if self._thread is not None and self._thread.is_alive():
            self.resume()                   # paused: the decode thread is still there with its sink
            return
        self._playing = True
        self._stop_flag.clear()
        self._pause_flag.clear()
        dev = self._device or QMediaDevices.defaultAudioOutput()
        self._thread = threading.Thread(target=self._decode_loop, args=(dev, self._rate), daemon=True)
        self._thread.start()
        self._pos_timer.start()
        self.state_changed.emit(True)

    def pause(self):
        if not self._playing:
            return
        self._playing = False
        self._pause_flag.set()              # the decode thread suspends its own sink
        self._wake.set()
        self._pos_timer.stop()
        self.state_changed.emit(False)

    def resume(self):
        if self._playing or not self._emu:
            return
        if self._thread is None or not self._thread.is_alive():
            # play() sets _playing, clears the flags and starts everything itself, and it
            # returns early when _playing is already set, so call it before setting it here.
            self.play()
            return
        self._playing = True
        self._pause_flag.clear()
        self._pos_timer.start()
        self.state_changed.emit(True)

    def stop(self):
        self._playing = False
        self._stop_flag.set()
        self._wake.set()
        self._pos_timer.stop()
        self._adapt_gen += 1                # a measurement under way is of a track that is gone
        an = self._analysis
        if an is not None and an.is_alive() and an is not threading.current_thread():
            an.join(timeout=2.0)            # it stops at its next piece of 1.4 ms
        if self._thread and self._thread.is_alive():
            # The decode loop checks _stop_flag every iteration and sleeps in 5 ms steps, so it
            # exits well inside this. The thread made the sink and is the only one that touches it
            # (a QAudioSink driven from a thread it was not made in crashes inside Qt), so it stops
            # the sink as it leaves. If it outlived the join it still holds the emulator and the
            # sink: leave it to finish. Audio kept playing beats memory freed under a live thread.
            self._thread.join(timeout=5.0)
        if self._thread and self._thread.is_alive():
            from applog import log
            log.warning("vgm decode thread outlived stop(); leaving sink up")
            return
        self._thread = None
        if self._stage_used:
            self._stage_used = False
            self._reset_stage()

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
        self._stage_dirty = True      # the decode thread drops the old position's filter memory

    def cleanup(self):
        self.stop()
        with self._emu_lock:
            if self._emu:
                _lib.gme_delete(self._emu)
                self._emu = None

    # internals

    def _stage_rate(self) -> int:
        """Rate of the attached stage, 0 without one."""
        try:
            return int(self._stage.fs) if self._stage is not None else 0
        except Exception:
            return 0

    def _sink_volume(self) -> float:
        return self._volume

    def _service_volume(self, sink):
        """Decode thread, every pass: give the sink the player's volume in steps of at most _VOL_STEP, _VOL_TICK_S apart, so a
        big change is a ramp of 16 ms and not a step. What the sink holds is scaled by its own volume when it is played."""
        goal = self._sink_volume()
        now = time.monotonic()
        if goal != self._vol_goal:
            cur = self._vol_at
            n = max(1, min(5, int(np.ceil(abs(goal - cur) / _VOL_STEP - 1e-9))))
            self._vol_steps = [cur + (goal - cur) * (i + 1) / n for i in range(n)]
            self._vol_steps[-1] = goal
            self._vol_goal = goal
            self._vol_next = 0.0
        if self._vol_steps and now >= self._vol_next:
            self._vol_at = self._vol_steps.pop(0)
            self._vol_next = now + _VOL_TICK_S
            sink.setVolume(self._vol_at)

    def _nap(self, s: float):
        """Sleep, but not through a volume change."""
        self._wake.wait(s)
        self._wake.clear()

    def _park(self, sink) -> bool:
        """Paused: the thread that owns the sink suspends it and waits, then resumes it. True if it did."""
        if not self._pause_flag.is_set() or self._stop_flag.is_set():
            return False
        sink.suspend()
        while self._pause_flag.is_set() and not self._stop_flag.is_set():
            self._service_volume(sink)
            self._nap(0.01)
        if not self._stop_flag.is_set():
            sink.resume()
        return True

    def _reset_stage(self):
        if self._stage is not None:
            try:
                self._stage.reset_stage()
            except Exception:
                pass

    def _through_stage(self, data: bytes, pos_ms: int) -> bytes:
        """int16 stereo bytes through the shared stage and back. Exact at flat settings:
        int16 to float32 and back by a power of two loses nothing. Any failure plays the raw block.
        The stage gets the volume only as a number for the modules that read it; the sink applies it."""
        x = np.frombuffer(data, dtype=np.int16)
        if x.size < 2:
            return data
        x = x[: x.size & ~1].reshape(-1, 2).astype(np.float32) * np.float32(1.0 / 32768.0)
        try:
            ctx = Context(self._rate, int(pos_ms * self._rate / 1000),
                          int(self._dur_ms * self._rate / 1000), float(self._volume))
            y = self._stage.process_external(x, ctx, False)
            if y.shape != x.shape or not np.isfinite(y).all():
                raise ValueError("the stage returned a bad block")
        except Exception:
            self._stage_failed = True
            from applog import log
            log.exception("chiptune stage failed, playing the raw sound")
            return data
        self._stage_used = True
        return np.clip(np.rint(y * np.float32(32768.0)), -32768, 32767).astype(np.int16).tobytes()

    def _stage_tail(self) -> bytes:
        """What the stage still holds after the last block (EQ and Enhance ringing, the DSP chain's look-ahead and tails), as
        int16 stereo bytes ending in a fade. Empty when the stage was not used or nothing audible is left."""
        flush = getattr(self._stage, "process_external_flush", None)
        if flush is None or not self._stage_used or self._stage_failed or self._stage_rate() != self._rate:
            return b""
        try:
            y = flush(Context(self._rate, -1, 0, float(self._volume)), 5.0, False)
        except Exception:
            from applog import log
            log.exception("chiptune stage flush failed, the tail is dropped")
            return b""
        if len(y) == 0:
            return b""
        return np.clip(np.rint(y * np.float32(32768.0)), -32768, 32767).astype(np.int16).tobytes()

    # Enhance brightness: the engine measures a decoded track (high_ratio of its first minute) and gives Enhance a
    # setting from it. libgme cannot be decoded ahead like a file, but it renders far faster than real time, so the
    # head of the track is rendered in a thread of its own on a second emulator and measured the same way.

    def _start_analysis(self, path: Path, track: int):
        self._adapt_gen += 1
        gen = self._adapt_gen
        set_goal = getattr(self._stage, "set_external_adaptive", None)
        if set_goal is None or self._stage_rate() != self._rate:
            return
        set_goal(None)                          # the middle setting until the measurement is in
        key = (str(path), int(track), int(self._rate))
        hit = _ratios.get(key)
        if hit is not None:
            set_goal(hit)
            return
        self._analysis = threading.Thread(target=self._analyse, args=(gen, key, int(self._dur_ms)), daemon=True,
                                          name="vgm-brightness")
        self._analysis.start()

    def _render_head(self, key: tuple, dur_ms: int, gen: int):
        """The first _ANALYSE_S seconds of the track as mono float32 at the player's rate, or None. Every libgme call
        is made under the emulator lock like the decode thread's, in pieces of about 1.4 ms so neither waits long."""
        path, track, rate = key
        emu = ctypes.c_void_p()
        with self._emu_lock:
            if _lib.gme_open_file(path.encode(), ctypes.byref(emu), rate):
                return None
        parts, got = [], 0
        try:
            with self._emu_lock:
                if _lib.gme_start_track(emu, track):
                    return None
            want = int(min(_ANALYSE_S * 1000, dur_ms) * rate / 1000)
            buf = (ctypes.c_short * 4096)()
            while got < want and gen == self._adapt_gen:
                with self._emu_lock:
                    if _lib.gme_track_ended(emu) or _lib.gme_play(emu, 4096, buf):
                        break
                parts.append(np.frombuffer(bytes(buf), dtype=np.int16).copy())
                got += 2048
        finally:
            with self._emu_lock:
                _lib.gme_delete(emu)
        if not parts or gen != self._adapt_gen:
            return None
        x = np.concatenate(parts).reshape(-1, 2)[:want].astype(np.float32)
        mono = (x[:, 0] + x[:, 1]) * np.float32(0.5 / 32768.0)
        return lfilter(_DC_B, _DC_A, mono).astype(np.float32)       # what the stage is given has the DC taken out

    def _analyse(self, gen: int, key: tuple, dur_ms: int):
        try:
            mono = self._render_head(key, dur_ms, gen)
            if mono is None:
                return
            from dsp_chain import high_ratio
            ratio = high_ratio(mono)
        except Exception:
            from applog import log
            log.exception("chiptune brightness measurement failed")
            return
        if len(_ratios) > 64:
            _ratios.clear()
        _ratios[key] = ratio
        if gen == self._adapt_gen:
            self._stage.set_external_adaptive(ratio)

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

    def _decode_loop(self, dev, rate):
        self._stalled = False
        sink = QAudioSink(dev, _make_format(rate))
        sink.setVolume(self._sink_volume())
        self._vol_at = self._vol_goal = self._sink_volume()
        self._vol_steps = []
        sink.setBufferSize(rate // 7 * _CHANNELS * 2)   # about 140 ms, against underruns
        io = sink.start()
        self._sink, self._io = sink, io
        try:
            self._decode(sink, io)
        finally:
            sink.reset()                 # stop() alone lets the queued audio play out
            sink.stop()
            self._sink = self._io = None

    def _decode(self, sink, io):
        buf = (ctypes.c_short * _CHUNK_SAMPLES)()

        while not self._stop_flag.is_set():
            self._park(sink)
            self._service_volume(sink)
            with self._emu_lock:
                if not self._emu or _lib.gme_track_ended(self._emu):
                    break
                pos_ms = _lib.gme_tell(self._emu)
                if self._dur_ms > 0 and pos_ms >= self._dur_ms:
                    break                       # hard end at the reported length, so a track cannot loop forever
                err = _lib.gme_play(self._emu, _CHUNK_SAMPLES, buf)
            if err:
                break

            data   = self._dc_correct(bytes(buf))   # strip chip DC offset
            # feed the analyzer/waveform live (ffmpeg can't decode these formats);
            # the raw chip signal, like the decoded files of the other engine
            self.waveform_chunk.emit(int(pos_ms * self._rate / 1000),
                                     int(self._dur_ms * self._rate / 1000),
                                     self._rate, data)
            if self._stage_dirty:
                self._stage_dirty = False
                self._reset_stage()
            if self._stage_rate() == self._rate and not self._stage_failed:
                data = self._through_stage(data, pos_ms)
            self._write(sink, io, data)

        if not self._stop_flag.is_set():
            self._write(sink, io, self._stage_tail())
            # Natural end: wait for the queue to empty before signalling (the sink's own state is
            # only updated by an event loop, which this thread does not have)
            drain_deadline = time.monotonic() + 2.0
            while sink.bytesFree() < sink.bufferSize() and time.monotonic() < drain_deadline:
                self._service_volume(sink)
                self._nap(0.02)
            self._playing = False
            QMetaObject.invokeMethod(self._pos_timer, "stop", Qt.QueuedConnection)    # a QTimer is stopped from its own thread
            self.state_changed.emit(False)
            self.finished.emit()

    def _write(self, sink, io, data: bytes):
        offset = 0
        full_since = 0.0
        while offset < len(data) and not self._stop_flag.is_set():
            if self._park(sink):
                full_since = 0.0
            self._service_volume(sink)
            free = sink.bytesFree()
            if free <= 0:
                # A queue that stays full is how a sink looks when its output has gone: it reports nothing, and
                # waiting for room would go on for good.
                now = time.monotonic()
                full_since = full_since or now
                if now - full_since > _STALL_S and not self._stalled:
                    from applog import log
                    self._stalled = True
                    log.warning("audio output stalled: the chiptune sink took nothing for %.1f s", now - full_since)
                    self.stalled.emit()
                self._nap(0.002 if self._vol_steps else 0.005)
                continue
            full_since = 0.0
            chunk   = data[offset: offset + min(free, len(data) - offset)]
            written = io.write(chunk)
            if written > 0:
                offset += written
            else:
                time.sleep(0.005)

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
