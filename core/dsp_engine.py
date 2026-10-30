"""Real-time audio playback engine with a live, bypassable DSP chain.

Plays plain audio files in place of QMediaPlayer, so the Enhance effect is a wet/dry flag in the signal
path and toggles instantly, with no re-transcoding. ffmpeg decodes incrementally into memory; output goes
through a push-mode QAudioSink owned by a dedicated audio QThread (_AudioFeedWorker). The 10 ms feed loop
runs there, so GUI stalls (library rebuilds, stylesheet reapply, menus) cannot starve the sink into an
audible dropout. The engine (GUI thread) talks to the worker only through queued signals; shared decode
state stays behind self._lock. Video (QMediaPlayer) and chiptunes (VgmPlayer) keep their own sinks and send
their blocks through process_external() to get the same EQ, DSP chain and Enhance.
"""
from __future__ import annotations
import contextlib
import copy
import logging
import os
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal, QTimer, QThread, Qt
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QAudioDevice

from dsp_base import Context
from dsp_chain import Enhancer, high_ratio, ParametricEQ
from dsp_fx_loudness import _Ramp, measure_level, normalizer_gain
import plat_audio
import plat_proc

_FS = 44100                     # engine sample rate; set to the output device's rate at startup
_CH = 2
_ENV_BLOCK = 2048        # frames per block in envelope()
_ENV_MAX_BLOCKS = 400     # most new blocks one envelope() call takes on (about 18 s of audio, a few ms)
_FRAME_BYTES = _CH * 4          # float32 stereo
_MAX_BLOCK = 4096               # frames per feed write
_SINK_MS = 700                  # capacity asked of the sink
_FILL_MS = 250                  # audio kept queued in it normally, which also bounds how late an EQ or volume change is heard
_FILL_STRESSED_MS = 600         # and while the feed thread is being delayed (busy machine, stalls elsewhere in the app)
_FILL_KEEP_MS = 120             # and with the output kept open, where what is queued cannot be thrown away and plays out after a stop or a seek
_FEED_MIN_MS = 40               # do not wake the DSP for less room than this unless the queue is empty
_LATE_MS = 30                   # a feed tick this much later than due counts as being delayed
_STALL_S = 2.0                  # audio waiting in the sink and none taken for this long: the output is lost, whatever it says
_STALL_START_S = 4.0            # and before it has taken any since it was built, which includes a device that wakes up slowly
_FS_MIN, _FS_MAX = 44100, 192000   # clamp window for device-driven rates (high-res supported)
_NG_FADE_S = 0.030              # the normalizer gain glides this long when it is switched on or off or its setting moves
_NG_EQ_FADE_S = 0.100           # and this long when the equalizer moved the peak the gain is aimed at
_EQ_SETTLE_S = 0.25             # the equalizer must have stood still this long before a track is run through it again
_LEVEL_CACHE_MAX = 256
_OFF = "off"                    # what a gain taken while the normalizer is off is tagged with
_ADAPT_FADE_MS = 30             # a new Enhance brightness for external audio is blended in over this long
_TAIL_FLOOR = 10.0 ** (-90.0 / 20.0)   # a tail below this is silence (-90 dBFS)
_alog = logging.getLogger("audiocrate.audio")


def _detect_output_rate(default: int = 44100) -> int:
    """The default audio output's preferred sample rate, clamped to _FS_MIN.._FS_MAX.

    Following the device rate means hi-res files decode at their full rate (up to 192 kHz) and a 48 kHz
    system skips a 44.1 to 48 kHz conversion in the output stack. The DSP chain is rate-parametric (every
    biquad is designed at this fs), so nothing else needs to know the number. Falls back to ``default`` if
    the device can't be queried or reports a rate outside the window.
    """
    try:
        rate = int(QMediaDevices.defaultAudioOutput().preferredFormat().sampleRate())
        if _FS_MIN <= rate <= _FS_MAX:
            return rate
    except Exception:
        pass
    return default


_LEVEL_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()   # (path, mtime, size, rate) -> {"peak", "lufs"} as decoded
_LEVEL_CACHE_LOCK = threading.Lock()


def _level_key(path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size, _FS)


def _level_cached(key) -> dict | None:
    with _LEVEL_CACHE_LOCK:
        got = _LEVEL_CACHE.get(key)
        if got is not None:
            _LEVEL_CACHE.move_to_end(key)
        return dict(got) if got is not None else None


def _level_store(key, vals: dict) -> None:
    with _LEVEL_CACHE_LOCK:
        _LEVEL_CACHE[key] = dict(vals)
        _LEVEL_CACHE.move_to_end(key)
        while len(_LEVEL_CACHE) > _LEVEL_CACHE_MAX:
            _LEVEL_CACHE.popitem(last=False)


class _Level:
    """What is measured about one decoded track. vals[sig] is {"peak": full scale 1.0, "lufs": loudness or None}: sig None
    is the track as decoded, any other sig the same track heard through a copy of the user's equalizer (see
    DspEngine._eq_sig). The audio thread reads vals, one worker thread fills it."""
    __slots__ = ("buf", "key", "vals", "cancel", "busy", "used")

    def __init__(self, buf, key):
        self.buf, self.key = buf, key
        self.vals: dict = {}
        self.cancel = threading.Event()
        self.busy = False           # a thread is measuring it
        self.used = False           # a gain has come from it, so later measurements follow edits and wait for them to settle
        hit = _level_cached(key) if key is not None else None
        if hit is not None:
            self.vals[None] = hit


def _eq_filter(sig, fs: int):
    """(sections, gain) of a private copy of the user's equalizer built from a signature, or None for a flat one."""
    bands, ig, og = sig
    eq = ParametricEQ(fs)
    eq.enabled, eq.input_gain, eq.output_gain = True, ig, og
    for btype, freq, gain, q, slope in bands:
        eq.add_band(freq, gain, q, btype, slope)
    return eq.sos()


def _duration_ms(path: Path) -> int:
    try:
        from mutagen import File as MFile
        f = MFile(str(path))
        if f is not None and f.info and f.info.length:
            return int(f.info.length * 1000)
    except Exception:
        pass
    try:
        from ffmpeg_util import probe_duration
        return int(probe_duration(path) * 1000)
    except Exception:
        pass
    return 0


class _AudioFeedWorker(QObject):
    """Owns the QAudioSink and the 10 ms feed timer on a dedicated audio thread.

    Everything that touches the sink lives here (QAudioSink is not thread-safe), driven by queued signals
    from the engine. The DSP math runs in engine._feed_once, called from this thread; numpy and scipy release
    the GIL for the heavy ops, so the GUI thread is not blocked either way.
    """
    drained = Signal(int)   # last buffer of a finished track has played out (its generation)
    failed  = Signal()   # the sink stopped taking audio (its device is likely gone)

    def __init__(self, engine: "DspEngine"):
        super().__init__()
        self._e = engine
        self._sink: QAudioSink | None = None
        self._io = None
        self._timer: QTimer | None = None
        self.buffered_frames = 0
        self.heard = 0             # cursor minus buffered, published in one store for the engine's position
        self._last_error = "NoError"   # so a stuck sink logs once, not at 100 Hz
        self.failing = False           # the sink reported an error or stopped taking audio, until the next one is built
        self.keep = False              # keep the output open while nothing plays (set from the GUI thread)
        self._corked = False           # the sink is suspended with nothing queued in it
        self._draining = False         # nothing more is fed; the sink is corked when what is queued has played
        self._drain_until = 0.0
        self._sink_dev = None          # the output the sink was made on
        self._seen = -1                # the play head at the last tick that found it moved
        self._seen_at = 0.0            # and when
        self._ran = False              # this sink has taken audio at least once
        self._t_prev = 0.0             # when the last feed tick ran
        self._late = 0.0               # decaying peak of how late recent ticks were, in seconds
        self.ticks = [0.0] * 256       # the last feed tick intervals, for the state snapshot (diag.py)
        self._ti = 0
        self.underruns = 0
        self.sink_state = ""
        self._ur_at = 0.0
        self._dev_logged = ""

    def setup(self):
        """Runs once in the audio thread (connected to QThread.started)."""
        threading.current_thread().name = "dsp-feed"      # log lines and stack dumps name this thread
        self._timer = QTimer()
        self._timer.setInterval(10)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        from applog import log
        how = plat_audio.go_realtime()
        if how:
            log.info("audio thread: %s", how)
        else:
            log.info("audio thread: normal priority (real-time scheduling not permitted)")

    def _open(self):
        """Make the sink on the engine's output."""
        e = self._e
        self._sink = QAudioSink(e._dev, e._fmt)
        self._sink.setVolume(1.0)   # volume is applied inside the DSP block
        # Room to queue more than usual when the machine is busy; how much is
        # actually kept queued is decided in DspEngine._feed_once.
        self._sink.setBufferSize(int(e._fmt.sampleRate() * _FRAME_BYTES * _SINK_MS / 1000))
        self._io = self._sink.start()
        self._sink_dev = e._dev.id()
        self._corked = False
        self._draining = False
        desc = e._dev.description()
        _alog.log(logging.DEBUG if desc == self._dev_logged else logging.INFO,
                  "audio output %s, %d Hz, queue asked for %d ms", desc, e._fmt.sampleRate(), _SINK_MS)
        self._dev_logged = desc

    def start_sink(self):
        e = self._e
        reuse = self._sink is not None and self.keep and not self.failing and self._sink_dev == e._dev.id()
        if self._sink is not None and not reuse:
            self.close_sink()
        if self._sink is None:
            self._open()
        else:
            # The output was kept open. Audio of the track before may still be queued in it: it plays out and the new
            # track follows it, since a queue cannot be emptied without closing the sink, which is what is avoided.
            self._draining = False
            if self._corked:
                self._sink.resume()
                self._corked = False
        e._fed_all = False
        self.buffered_frames = (max(0, (self._sink.bufferSize() - self._sink.bytesFree()) // _FRAME_BYTES) if reuse else 0)
        self.heard = max(0, e._cursor - self.buffered_frames)
        self._seen = -1
        self._t_prev = 0.0
        self._late = 0.0
        self._timer.start()

    def open_idle(self):
        """Keep-open mode: make the sink now and leave it suspended, so the output is there before anything plays."""
        e = self._e
        if self._sink is not None or e._dev.isNull():
            return
        self._open()
        self._sink.suspend()
        self._corked = True
        self.buffered_frames = 0
        self._timer.stop()

    def close_sink(self):
        """Close the sink for real: the output goes from the sound server, and what is wired to it is lost."""
        if self._timer is not None:
            self._timer.stop()
        if self._sink is not None:
            try:
                self._sink.reset()      # stop() alone lets the queued quarter second play out
                self._sink.stop()
            except Exception:
                pass
        self._sink = None
        self._io = None
        self._sink_dev = None
        self._corked = False
        self._draining = False
        self.buffered_frames = 0
        self._last_error = "NoError"
        self.failing = False
        self._seen = -1
        self._ran = False

    def stop_sink(self):
        """The engine wants no sink now. A kept-open output stays, and is suspended once its queue has played out."""
        if self.keep and self._sink is not None and not self.failing:
            self._drain()
        else:
            self.close_sink()

    def _drain(self):
        if self._corked or self._sink is None:
            return
        self._draining = True
        self._drain_until = time.monotonic() + 1.5       # a sink that takes nothing is not waited for
        if not self._timer.isActive():
            self._timer.start()

    def suspend(self):
        if self._sink is None:
            return
        if self.keep and not self.failing:
            self._drain()       # the queue plays out first: a suspended sink holds on to it, and it would sound again later
        else:
            self._timer.stop()
            self._sink.suspend()
            self._corked = True

    def resume(self):
        if self._sink is not None:
            if self._draining:
                self._draining = False      # not suspended yet, the queue is still playing
            else:
                self._sink.resume()
                self._corked = False
            self._seen = -1             # the queue did not move while it was suspended
            self._timer.start()

    def reset_io(self):
        """Seek while playing: flush buffered audio, keep the sink."""
        if self._sink is None:
            self.start_sink()
            return
        if self.keep and not self.failing:
            # no flush: it would close the sink. What is queued plays out and the audio from the new place follows.
            self._draining = False
            if self._corked:
                self._sink.resume()
                self._corked = False
            self._seen = -1
            if not self._timer.isActive():
                self._timer.start()
            return
        try:
            self._sink.reset()
            self._io = self._sink.start()
            self._seen = -1
            if not self._timer.isActive():
                self._timer.start()
        except Exception:
            self.start_sink()

    def _underrun(self, now: float):
        """The sink ran dry. Counted always, logged at most every 5 s so that a bad stretch is one line, not hundreds."""
        self.underruns += 1
        if now - self._ur_at > 5.0:
            self._ur_at = now
            _alog.warning("audio underrun, %d so far; the feed ticks were up to %.0f ms late",
                          self.underruns, self._late * 1000)

    def _watch(self, now: float, heard: int, queued: int):
        """A sink whose device went away can go on taking no audio and say nothing: no error, and a queue that
        stays full reads as busy. Audio waiting while the play head stands still for _STALL_S is that."""
        if not queued or heard != self._seen:
            if queued and self._seen != -1:
                self._ran = True
            self._seen, self._seen_at = heard, now
        elif now - self._seen_at > (_STALL_S if self._ran else _STALL_START_S) and not self.failing:
            from applog import log
            self.failing = True
            log.warning("audio output stalled: nothing taken for %.1f s (device %s)",
                        now - self._seen_at, self._e._dev.description())
            self.failed.emit()

    def _tick(self):
        e = self._e
        if self._sink is None or self._io is None:
            return
        try:
            if self._draining:
                s = self._sink
                if s.bytesFree() >= s.bufferSize() - 64 * _FRAME_BYTES or time.monotonic() > self._drain_until:
                    self._draining = False
                    s.suspend()
                    self._corked = True
                    self._timer.stop()
                    self.buffered_frames = 0
                return
            if e._fed_all:
                # the last buffer of a finished track has drained: tell the engine
                if self._sink.state().name == "IdleState":
                    self._timer.stop()
                    self.buffered_frames = 0
                    self.drained.emit(e._fed_gen)
                else:
                    left = max(0, (self._sink.bufferSize() - self._sink.bytesFree()) // _FRAME_BYTES)
                    self._watch(time.monotonic(), max(0, e._cursor - left), left)
                return
            now = time.monotonic()
            if self._t_prev and now - self._t_prev > 1.0:
                self._seen = -1                     # a pause or a starved thread, not a sink that stopped
            if self._t_prev:
                if now - self._t_prev < 2.0:        # longer is a pause, not a late tick
                    self.ticks[self._ti & 255] = now - self._t_prev
                    self._ti += 1
                self._late = max(now - self._t_prev - 0.010, self._late * 0.99)
                if self._late > _LATE_MS / 1000.0:
                    e._stressed_until = now + 20.0      # keep the deeper queue for a while after
            self._t_prev = now
            if e._feed_gate:
                e._feed_once(self._sink, self._io)
            # Snapshot AFTER the write so (cursor, buffered) form a consistent pair,
            # and publish what they give as one number: a reader that combined the two
            # itself could land between a feed moving the cursor and this update, and
            # the position (and the scope) would jump a whole block ahead and back.
            self.buffered_frames = max(
                0, (self._sink.bufferSize() - self._sink.bytesFree()) // _FRAME_BYTES)
            self.heard = max(0, e._cursor - self.buffered_frames)
            self._watch(now, self.heard, self.buffered_frames)
            # A sink whose device has gone stops taking bytes and reports it
            # once, here. bytesFree() then reads 0 forever, which is exactly
            # what a full buffer looks like, so without this the feed loop
            # offers audio to something that has gone and says nothing at all.
            err = self._sink.error().name
            self.sink_state = self._sink.state().name
            if err == "UnderrunError" and self._last_error != err:
                self._underrun(now)
            if err not in ("NoError", "UnderrunError") and err != self._last_error:
                from applog import log
                log.warning("audio sink error: %s (device %s)",
                            err, e._dev.description())
                self.failing = True
                self.failed.emit()     # queued to the GUI thread
            self._last_error = err
        except Exception:
            # one bad block must not kill the feed loop (an EQ edit racing a
            # process() call, say): log it and try again next tick
            from applog import log
            log.exception("audio feed tick failed")


class DspEngine(QObject):
    position_changed = Signal(float, int, int)   # frac, pos_ms, dur_ms
    state_changed    = Signal(bool)              # playing?
    finished         = Signal()
    advanced         = Signal(int)               # gapless: now playing this queue index
    _w_advanced      = Signal(int, int)          # same, with the generation, queued from the audio thread
    device_lost      = Signal()                  # the sink stopped taking audio
    # queued control signals to _AudioFeedWorker (all sink calls happen on the audio thread)
    _w_start   = Signal()
    _w_stop    = Signal()
    _w_suspend = Signal()
    _w_resume  = Signal()
    _w_reset   = Signal()
    _w_close   = Signal()                   # close the kept-open output for real
    _w_open    = Signal()                   # open it now and leave it suspended
    t_request = 0.0                 # monotonic time a track was asked for, until its first sample is written
    start_latency_ms = None         # how long the latest track took from the request to its first sample

    def __init__(self, parent=None):
        super().__init__(parent)
        global _FS
        _FS = _detect_output_rate(_FS)     # adopt the device rate once (chain/sink/decode all follow it)
        self.fs = _FS                      # exposed for diagnostics
        self._enh = Enhancer(_FS)
        self.user_eq = ParametricEQ(_FS)   # user parametric EQ (edited by the EQ panel)
        self.dsp_chain = None              # the user DSP chain (dsp_user_chain.DspChain), set by the window
        self._spec_buf = np.zeros(0, dtype=np.float32)   # rolling post-EQ mono (for the EQ analyzer)
        self._stage_owner = None           # "engine" or "ext": whose signal the filter memory holds
        self._ext_goal = 0.4               # brightness Enhance should have for the external source (VgmPlayer measures it)
        self._ext_ratio = 0.4              # the one it has
        self._ext_warm = False             # the stage has run since its last reset
        self._enh_to = None                # the Enhancer being faded in after a brightness change, or None
        self._enh_done = 0

        # gapless: pre-decoded next track
        self._next_gen = 0
        self._next_path = None
        self._next = None       # dict(buf, total, dur_ms, lvl, trim_off, idx, ratio) when ready
        self._next_proc = None  # the ffmpeg decoding it, so a newer request can stop it

        self._buf: np.ndarray = np.zeros((_FS, _CH), dtype=np.int16)
        self._lock = threading.Lock()
        self._decoded = 0          # frames written by decoder
        self._total = 0            # expected/known total frames
        self._complete = False
        self._cursor = 0           # next frame to feed
        self._dur_ms = 0
        self._gen = 0
        self._serial = 0           # bumps whenever a different track's buffer goes in
        self._source = None        # what the buffer holds: the path (or stream label) it was loaded for
        self._env = None           # per-block energy of the decoded audio, for envelope()
        self._env_last = (None, 0.0)
        self._env_last_serial = -1
        self._proc: subprocess.Popen | None = None
        self._volume = 0.7
        self._stressed_until = 0.0   # monotonic time until which the feed queue is kept deep
        self._fed_all = False
        self._fed_gen = 0          # the generation whose last buffer was fed
        self._trim = False
        self._lvl = None            # _Level of the track being played, made once it is fully decoded
        self._lvl_key = None        # what the level cache calls the file being decoded (None: not cached)
        self._lvl_lock = threading.Lock()
        self._ng = None             # the Track normalizer's gain as a ramp; None until the song's first block
        self._ng_sig = _OFF         # the equalizer signature the gain was aimed with (_OFF while the normalizer is off)
        self._ng_gen = 0            # the engine generation (load or stop) that made it
        self._playing = False       # playback intent, which a momentary sink state does not flip
        self._loaded = False        # a track has been load()ed and not yet stop()ped
        self._xfade_frames = 0      # crossfade length (0 = off, an instant gapless swap)
        self._xf_active = False     # currently mixing tail of A with head of B
        self._xf = None             # crossfade state dict while _xf_active

        fmt = QAudioFormat()
        fmt.setSampleRate(_FS)
        fmt.setChannelCount(_CH)
        fmt.setSampleFormat(QAudioFormat.Float)
        self._fmt = fmt
        self._dev: QAudioDevice = QMediaDevices.defaultAudioOutput()

        # Sink + feed loop live on a dedicated audio thread; the engine only
        # tracks intent flags and signals the worker.
        self._sink_alive = False       # a sink exists on the audio thread
        self._sink_suspended = False   # paused via sink.suspend()
        self._feed_gate = False        # False stops writes instantly (before queued stop lands)
        self._audio_thread = QThread()
        self._audio_thread.setObjectName("dsp-feed")
        self._worker = _AudioFeedWorker(self)
        self._worker.moveToThread(self._audio_thread)
        self._audio_thread.started.connect(self._worker.setup)
        self._worker.drained.connect(self._finish)          # queued to the GUI thread
        # Re-emitted rather than exposed, so the player does not reach into the
        # worker. The engine knows its output has stopped working; it does not
        # know what else is plugged in, so it says so and leaves the choice.
        self._worker.failed.connect(self.device_lost)       # queued to the GUI thread
        self._w_start.connect(self._worker.start_sink)
        self._w_stop.connect(self._worker.stop_sink)
        self._w_close.connect(self._worker.close_sink)
        self._w_open.connect(self._worker.open_idle)
        self._w_suspend.connect(self._worker.suspend)
        self._w_resume.connect(self._worker.resume)
        self._w_reset.connect(self._worker.reset_io)
        self._w_advanced.connect(self._relay_advanced)
        self._audio_thread.start(QThread.TimeCriticalPriority)

        self._postick = QTimer(self)
        self._postick.setInterval(100)
        self._postick.timeout.connect(self._emit_pos)

    # DSP control
    def set_enabled(self, on: bool):
        self._enh.set_enabled(on)

    def set_crossfade(self, ms: int):
        """Crossfade length in ms (0 disables it, and tracks swap gaplessly and instantly)."""
        self._xfade_frames = max(0, int(ms / 1000.0 * _FS))

    def set_dsp_chain(self, chain):
        self.dsp_chain = chain

    @property
    def enhance_enabled(self) -> bool:
        return self._enh.enabled

    # load / decode
    def load(self, path: Path, trim: bool = False):
        self.stop()
        self.clear_next()
        self._loaded = True
        self._gen += 1
        gen = self._gen
        self._trim = trim
        # The ffprobe duration fallback is a slow, blocking read, so it happens in the
        # decode thread. For the buffer preallocation and an immediate seekbar, use the
        # duration the library cache already has (instant when warm; one mutagen read
        # at most when cold).
        self._dur_ms = 0
        try:
            import library_cache
            self._dur_ms = int((library_cache.get_tags(path).get("duration") or 0.0) * 1000)
        except Exception:
            pass
        total = int(self._dur_ms / 1000 * _FS) if self._dur_ms else 0
        with self._lock:
            # Stored as int16 (half the RAM of float32). The small per-block reads in
            # _feed_once, mono_window and the crossfade convert back to float, which costs
            # nothing next to holding a whole track's PCM in memory twice (main + gapless).
            self._buf = np.zeros((max(total, _FS), _CH), dtype=np.int16)
            self._serial += 1
            self._source = str(path)
            self._decoded = 0
            self._cursor = 0
            self._complete = False
            self._total = total
        threading.Thread(target=self._decode, args=(path, gen), daemon=True).start()

    def load_stream(self, reader, dur_ms: int, label: str = "stream"):
        """Play a source that is produced on demand, a chunk at a time.

        `reader(frames) -> (N, 2) int16` is pulled on a worker thread and its
        output appended to the same buffer ffmpeg decoding fills, so playback
        starts as soon as the first chunk lands instead of after the whole thing
        is produced. Used for emulated chiptunes: rendering a long SID takes
        seconds, but its first second takes milliseconds.

        `dur_ms` is the known total length (chip formats get it from a database
        or a previous scan), so the seek bar is correct immediately.
        """
        self.stop()
        self.clear_next()
        self._loaded = True
        self._gen += 1
        gen = self._gen
        self._trim = False
        self._dur_ms = int(dur_ms)
        total = int(self._dur_ms / 1000 * _FS) if self._dur_ms else 0
        with self._lock:
            self._buf = np.zeros((max(total, _FS), _CH), dtype=np.int16)
            self._serial += 1
            self._source = str(label)
            self._decoded = 0
            self._cursor = 0
            self._complete = False
            self._total = total
        threading.Thread(target=self._pump_stream, args=(reader, gen, total),
                         daemon=True).start()

    def _pump_stream(self, reader, gen: int, total: int):
        CHUNK = _FS // 2                     # half a second per pull
        try:
            while True:
                if gen != self._gen:
                    return                   # track changed under us
                want = CHUNK if total <= 0 else min(CHUNK, total - self._decoded)
                if want <= 0:
                    break
                block = reader(want)
                if block is None or len(block) == 0:
                    break
                nfr = len(block)
                with self._lock:
                    if gen != self._gen:
                        return              # changed while the reader worked: this block is the old track's
                    cur = self._buf
                    end = self._decoded + nfr
                grown = None
                if end > len(cur):
                    # Geometric, and copied outside the lock (see _decode).
                    want = max(end + _FS, (len(cur) * 3) // 2)
                    grown = np.concatenate([cur, np.zeros((want - len(cur), _CH), dtype=np.int16)])
                dest = grown if grown is not None else cur
                dest[end - nfr:end] = block          # outside the lock, as in _decode
                with self._lock:
                    if self._buf is not cur:
                        return              # a newer track took over mid-copy
                    if grown is not None:
                        self._buf = grown
                    self._decoded = end
            with self._lock:
                if gen != self._gen:
                    return
                self._buf = self._buf[:self._decoded]
                self._total = self._decoded
                self._complete = True
            if self._dur_ms == 0:
                self._dur_ms = int(self._decoded / _FS * 1000)
            try:
                from logs import get, rss_mb
                get("dsp").debug(
                    "decoded %.1f s at %d Hz, buffer %.0f MB, process %.0f MB",
                    self._decoded / _FS, _FS,
                    self._buf.nbytes / 2 ** 20, rss_mb())
            except Exception:
                pass
        except Exception:
            from applog import log
            log.exception("chip stream failed")
            with self._lock:
                self._total = self._decoded
                self._complete = True
        finally:
            close = getattr(reader, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass

    def _decode(self, path: Path, gen: int):
        try:
            # The duration probe is slow (ffprobe alone could stall for up to 10 s), so it
            # runs here and not in load(), which would block the GUI on a track change.
            if gen == self._gen:
                self._lvl_key = _level_key(path)
            if self._dur_ms == 0:
                d = _duration_ms(path)
                if gen == self._gen and d:
                    self._dur_ms = d
                    with self._lock:
                        if self._total == 0:
                            self._total = int(d / 1000 * _FS)
            if gen != self._gen:
                return
            from ffmpeg_util import ffmpeg_path
            exe = ffmpeg_path()
            if not exe:
                return
            cmd = [exe, "-i", str(path),
                   "-f", "f32le", "-ar", str(_FS), "-ac", str(_CH), "-vn", "-"]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **plat_proc.popen_kwargs(group=False),
            )
            self._proc = proc
            leftover = b""
            trim_done = not self._trim
            while True:
                if gen != self._gen:
                    proc.kill(); proc.wait(); return
                data = proc.stdout.read(65536)
                if not data:
                    break
                if leftover:
                    data = leftover + data
                nfr = len(data) // _FRAME_BYTES
                if nfr == 0:
                    leftover = data
                    continue
                chunk = np.frombuffer(data[:nfr * _FRAME_BYTES], dtype=np.float32).reshape(-1, _CH)
                leftover = data[nfr * _FRAME_BYTES:]
                chunk16 = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                with self._lock:
                    if gen != self._gen:
                        continue            # a newer track owns the buffer: the loop head ends this thread
                    cur = self._buf
                    end = self._decoded + nfr
                grown = None
                if end > len(cur):
                    # Grow geometrically. A second at a time is one concatenate per
                    # second of audio, each allocating the whole new buffer and
                    # copying the old one in, so a long track of unknown duration
                    # copied itself hundreds of times and fragmented the heap. The
                    # copy is made outside the lock: this thread is the only writer,
                    # and holding the lock for tens of milliseconds stalls every
                    # reader (the scope and spectrum run on the GUI thread).
                    want = max(end + _FS, (len(cur) * 3) // 2)
                    grown = np.concatenate([cur, np.zeros((want - len(cur), _CH), dtype=np.int16)])
                # The chunk is copied in outside the lock, and only its length is
                # published under it: nothing reads past the published length. A
                # thread that holds the lock can be made to wait for the interpreter
                # lock behind every other busy thread (several start at a song
                # change), and the GUI, which takes this lock to draw the scope,
                # froze for as long as that took.
                dest = grown if grown is not None else cur
                dest[end - nfr:end] = chunk16
                loud_at = None
                if not trim_done and end >= _FS:
                    # Trim leading silence: jump the play cursor to the first audible
                    # frame once ~1s is available (only if we haven't played past it).
                    loud = np.where(np.abs(dest[:end]).max(axis=1) > 98)[0]
                    loud_at = int(loud[0]) if len(loud) else 0
                    trim_done = True
                with self._lock:
                    if gen != self._gen or self._buf is not cur:
                        continue            # a newer track took over mid-copy
                    if grown is not None:
                        self._buf = grown
                    self._decoded = end
                    if loud_at is not None and self._cursor <= loud_at:
                        self._cursor = loud_at
            proc.wait()
            with self._lock:
                if gen != self._gen:
                    return
                self._buf = self._buf[:self._decoded]
                self._total = self._decoded
                self._complete = True
                done = self._decoded
                # Only the start is read below, so only the start is converted.
                # Converting the whole track built four full-length float32
                # arrays to answer a question about the first seconds: 176 MB on
                # a 48 kHz device and 703 MB on a 192 kHz one, in a single
                # statement while the decode buffer, the visualizers and projectM
                # were all still resident. high_ratio looks at 4000 windows of
                # 512 frames, 256 apart, so about a million frames are all it can use.
                head = self._buf[: min(_FS * 60, 1_100_000)]
            # A Track normalizer holds the song's start for this, so it comes before the rest.
            lvl = self._level()
            if lvl is not None:
                self._lvl_run(lvl, lambda: gen != self._gen)
            # Worked out after the lock is released: it is a few passes over up to
            # 2.6 M frames, and every reader on the GUI thread waits on that lock.
            mono = ((head[:, 0].astype(np.float32) + head[:, 1].astype(np.float32))
                    * np.float32(0.5 / 32768.0))
            if gen != self._gen:
                return
            if self._dur_ms == 0:
                self._dur_ms = int(done / _FS * 1000)
            try:
                self._enh.set_adaptive(high_ratio(mono))
            except Exception:
                pass
        except Exception:
            from applog import log
            log.exception("decode failed: %s", path)

    # sink lifecycle (sink itself lives on the audio thread)
    def _start_sink(self):
        self._worker.heard = self._cursor
        self._sink_alive = True
        self._sink_suspended = False
        self._feed_gate = True
        self._w_start.emit()
        self._postick.start()

    def _stop_sink(self):
        self._feed_gate = False
        self._sink_alive = False
        self._sink_suspended = False
        self._w_stop.emit()
        self._postick.stop()

    def _relay_advanced(self, idx: int, gen: int):
        # queued from the audio thread: a track change may have come in since, and
        # the index then belongs to a track nobody is playing
        if gen == self._gen:
            self.advanced.emit(idx)

    def _finish(self, gen: int = -1):
        # `drained` is queued from the audio thread, so one can still be in the
        # event queue when the user changes track. Acting on it then fires
        # finished -> next(), which both hijacks playback and invalidates the
        # render the new track is waiting on (it bumps the chip generation, so
        # the finished render is discarded as stale and the UI sits on
        # "Rendering..." forever). stop() clears _loaded, and the generation the
        # audio thread saw when it fed the last buffer tells a drain that belongs
        # to an earlier load() apart from one that belongs to this track.
        if not self._loaded or gen not in (-1, self._gen):
            return
        self._playing = False
        self._loaded = False
        self._stop_sink()
        with self._lock:
            self._cursor = 0
        self.state_changed.emit(False)
        self.finished.emit()

    @contextlib.contextmanager
    def _soft_lock(self, timeout: float = 0.003):
        """The engine lock, but giving up after a few milliseconds. The audio thread
        must not sit behind a thread that holds the lock and is itself waiting for
        the interpreter lock; skipping one 10 ms tick costs nothing, the sink has a
        quarter of a second queued."""
        got = self._lock.acquire(timeout=timeout)
        try:
            yield got
        finally:
            if got:
                self._lock.release()

    # feeding (runs on the audio thread via _AudioFeedWorker._tick)
    def _feed_once(self, sink: QAudioSink, io):
        gen = self._gen
        free = sink.bytesFree()
        queued = (sink.bufferSize() - free) // _FRAME_BYTES
        fill = (_FILL_STRESSED_MS if time.monotonic() < self._stressed_until
                else _FILL_KEEP_MS if self._worker.keep else _FILL_MS)
        room = min(free // _FRAME_BYTES, int(_FS * fill / 1000) - queued)
        # Top up in worthwhile blocks rather than a few hundred frames every tick:
        # each call is a wake-up that has to win the interpreter lock, and under
        # load fewer, larger ones are the more likely to land in time. An empty
        # queue is fed at once whatever the size.
        if room < 1 or (queued > 0 and room < int(_FS * _FEED_MIN_MS / 1000)):
            return
        want = min(room, _MAX_BLOCK)

        # Crossfade: once within xfade_frames of the end and the next track is
        # decoded, overlap the two instead of swapping instantly.
        if (not self._xf_active and self._xfade_frames > 0 and self._next is not None
                and self._complete and 0 < (self._total - self._cursor) <= self._xfade_frames):
            self._start_crossfade()
        if self._xf_active:
            self._feed_crossfade(want, io)
            return

        with self._soft_lock() as got:
            if not got:
                return          # held up by another thread; the sink has a queue to ride it out
            avail = self._decoded - self._cursor
        if avail <= 0:
            if self._complete:
                if self._gapless_swap():   # continue into the next track without a gap
                    return
                if self._feed_tail(io, want):
                    return
                self._fed_gen = gen
                self._fed_all = True
            return
        goal = self._norm_want()
        if goal is None:
            return              # a Track normalizer is on: the song's start waits for its measurement
        with self._soft_lock() as got:
            if not got:
                return
            take = min(want, avail)
            pos = self._cursor
            block = self._buf[self._cursor:self._cursor + take]
            if not len(block):
                return          # stopped between the two looks at the buffer
            block = block.astype(np.float32) * np.float32(1.0 / 32768.0)
            self._cursor += take
            self._tail = None
        # The Track normalizer's gain goes before the equalizer and the chain, so the Enhancer's limiter sees
        # the leveled signal. It is one value for the whole song unless a setting or the equalizer moved.
        g = self._norm_step(len(block), goal, gen)
        if isinstance(g, np.ndarray):
            block = block * g.astype(np.float32)[:, None]
        elif g != 1.0:
            block = block * np.float32(g)
        eqd = self.user_eq.process(block)
        # Keep the last ~2048 post-EQ mono samples for the EQ panel's analyzer.
        mono = (eqd[:, 0] + eqd[:, 1]) * np.float32(0.5)
        self._spec_buf = np.concatenate([self._spec_buf, mono])[-2048:]
        ch = self.dsp_chain
        if ch is not None and ch.active:
            eqd = ch.process(eqd, Context(_FS, pos, self._total, self._volume))
        out = self._enh.process(eqd) * self._volume
        np.clip(out, -1.0, 1.0, out=out)
        io.write(out.astype(np.float32, copy=False).tobytes())
        if self.t_request:
            self._first_sample()

    def _first_sample(self):
        ms = round((time.monotonic() - self.t_request) * 1000)
        self.t_request, self.start_latency_ms = 0.0, ms
        _alog.log(logging.INFO if ms > 400 else logging.DEBUG, "track start took %d ms from the request to the first sample", ms)

    _tail = None        # the chain's flush while the end of the queue is being written

    def _feed_tail(self, io, want: int) -> bool:
        """At the real end of the queue the chain still holds its look-ahead and whatever it rings with. Writes it, up to
        `want` frames a tick, through Enhance and the volume like the rest. True while there is more to come."""
        if self._tail is None:
            ch = self.dsp_chain
            self._tail = [ch.flush_blocks() if ch is not None and ch.active else iter(()), np.zeros((0, _CH), np.float32)]
        gen, buf = self._tail
        while len(buf) < want:
            blk = next(gen, None)
            if blk is None:
                break
            buf = np.concatenate((buf, blk))
        if not len(buf):
            return False
        out, self._tail[1] = buf[:want], buf[want:]
        out = self._enh.process(out) * self._volume
        np.clip(out, -1.0, 1.0, out=out)
        io.write(out.astype(np.float32, copy=False).tobytes())
        return True

    # Track normalizer. A song is measured whole, from the decoded buffer, and one gain from the measurement is in force
    # from its first sample: the start waits for the measurement when a normalizer is on (a few tenths of a second), and
    # nothing waits when none is. The gain moves only when the normalizer is switched or changed, or the equalizer moves
    # the peak (a copy of it is run over the song again in a thread once it has stood still), over 30 to 100 ms.
    def _norm_settings(self):
        f = getattr(self.dsp_chain, "normalizer_settings", None)
        try:
            return f() if f is not None else None
        except Exception:
            return None

    def _eq_sig(self):
        """What the user's equalizer does to a song, as a value that changes when it does; None when it does nothing."""
        eq = self.user_eq
        if not eq.enabled:
            return None
        bands = tuple((b.type, b.freq, b.gain, b.q, b.slope) for b in eq.bands if b.enabled)
        if (all(g == 0.0 and t in ("peaking", "lowshelf", "highshelf") for t, _, g, _, _ in bands)
                and not eq.input_gain and not eq.output_gain):
            return None         # a band with no gain is a filter that does nothing
        return bands, eq.input_gain, eq.output_gain

    def _level(self):
        """The _Level of the song in the engine once it is fully decoded, else None."""
        if not self._complete:
            return None
        buf, lvl = self._buf, self._lvl
        if lvl is not None and lvl.buf is buf:
            return lvl
        with self._lvl_lock:
            lvl = self._lvl
            if lvl is None or lvl.buf is not buf:
                if lvl is not None:
                    lvl.cancel.set()
                lvl = self._lvl = _Level(buf, self._lvl_key)
        return lvl

    def _lvl_todo(self, lvl):
        """(equalizer signature, loudness wanted) of the first measurement the normalizer lacks, or None."""
        st = self._norm_settings()
        loud = st is not None and st.get("mode") == "Loudness"
        eqs = self._eq_sig() if st is not None else None
        for sig in ((None,) if eqs is None else (None, eqs)):
            have = lvl.vals.get(sig)
            want = loud and (eqs is None or sig is not None)       # as decoded, the loudness only matters with no equalizer
            if have is None or (want and "lufs" not in have):
                return sig, want
        return None

    def _lvl_kick(self, lvl):
        """Starts a thread for what is missing, unless one is on it."""
        if not (lvl.busy or lvl.cancel.is_set()) and self._lvl_todo(lvl) is not None:
            threading.Thread(target=self._lvl_run, args=(lvl,), name="dsp-level", daemon=True).start()

    def _lvl_settle(self, lvl, sig) -> bool:
        """Waits until the equalizer has stood still for _EQ_SETTLE_S. False if it moved, or the song went."""
        for _ in range(int(_EQ_SETTLE_S / 0.02)):
            if lvl.cancel.wait(0.02) or self._eq_sig() != sig:
                return False
        return True

    def _lvl_run(self, lvl, ext=None):
        """Measures in this thread what the normalizer needs of lvl until nothing is missing; stops when the song is
        dropped or ext() says so. A failure leaves a neutral measurement, which gives gain 1.0, so nothing waits for it."""
        with self._lvl_lock:
            if lvl.busy:
                return
            lvl.busy = True

        def stop():
            return lvl.cancel.is_set() or (ext is not None and ext())
        try:
            while not stop():
                todo = self._lvl_todo(lvl)
                if todo is None:
                    return
                sig, loud = todo
                if sig is not None and lvl.used and not self._lvl_settle(lvl, sig):
                    continue
                f = None if sig is None else _eq_filter(sig, _FS)
                t0 = time.monotonic()
                got = measure_level(lvl.buf, _FS, f[0] if f else None, f[1] if f else 1.0, loud, stop)
                if got is None:
                    return
                dt = time.monotonic() - t0
                _alog.log(logging.INFO if dt > 2.0 else logging.DEBUG, "level analysis took %.2f s for %.0f s of audio%s",
                          dt, len(lvl.buf) / _FS, " (with the equalizer)" if f else "")
                v = dict(lvl.vals.get(sig) or {})
                v.update(got)
                lvl.vals[sig] = v
                if sig is None and lvl.key is not None:
                    _level_store(lvl.key, v)
                for old in [k for k in lvl.vals if k is not None and k != sig][:-3]:
                    lvl.vals.pop(old, None)                        # the newest few equalizer settings are enough
        except Exception:
            from applog import log
            log.exception("measuring the level of a song failed")
            for sig in (None, self._eq_sig()):
                lvl.vals.setdefault(sig, {"peak": 0.0, "lufs": None})
        finally:
            lvl.busy = False

    @staticmethod
    def _goal(lvl, st, eqs, fallback=False):
        """(gain, signature it was measured under) for song lvl under settings st, or None while a measurement it needs
        is missing. fallback accepts the song as decoded when the equalizer's measurement is not in yet."""
        loud = st.get("mode") == "Loudness"
        for sig in ((eqs, None) if fallback and eqs is not None else (eqs,)):
            v = lvl.vals.get(sig)
            if v is not None and (not loud or "lufs" in v):
                return normalizer_gain(st, v["peak"], v.get("lufs")), sig
        return None

    def _goal_for(self, lvl):
        """The goal for a song that is about to start playing: (1.0, _OFF) with no normalizer on, None when one is on
        and the song is not measured."""
        st = self._norm_settings()
        if st is None:
            return 1.0, _OFF
        goal = self._goal(lvl, st, self._eq_sig(), True) if lvl is not None else None
        if goal is not None:
            lvl.used = True
        return goal

    def _norm_want(self):
        """(gain, signature) the normalizer wants for the next block, or None to wait: the song has not begun and its
        measurement is not in. Once a gain is in force it is kept while a new measurement is awaited."""
        st = self._norm_settings()
        if st is None:
            return 1.0, _OFF
        if self._ng is not None and self._ng_gen != self._gen:
            self._ng = None                    # made by a block of a song that has been stopped since
        keep = None if self._ng is None else (self._ng.t, self._ng_sig)
        lvl = self._level()
        if lvl is None:
            return keep
        self._lvl_kick(lvl)
        goal = self._goal(lvl, st, self._eq_sig())
        if goal is None:
            return keep
        lvl.used = True
        return goal

    def _ng_set(self, g: float, sig, gen: int):
        self._ng, self._ng_sig, self._ng_gen = _Ramp(g, int(_NG_FADE_S * _FS)), sig, gen

    def _norm_step(self, n: int, goal, gen: int):
        """The gain for the next n frames: a float, or an array while it moves."""
        g, sig = goal
        r = self._ng
        if r is None:
            self._ng_set(g, sig, gen)
            r = self._ng
        elif g != r.t:
            eq_moved = sig != self._ng_sig and sig is not _OFF and self._ng_sig is not _OFF
            r.length = max(1, int((_NG_EQ_FADE_S if eq_moved else _NG_FADE_S) * _FS))
            r.to(g)
        self._ng_sig = sig
        return r.take(n)

    def _set_lvl(self, lvl):
        old, self._lvl = self._lvl, lvl
        if old is not None and old is not lvl:
            old.cancel.set()

    def _norm_reset(self):
        """A new song or a stop: forget the measurements and the gain, and let the threads measuring go."""
        lvl, self._lvl = self._lvl, None
        if lvl is not None:
            lvl.cancel.set()
        self._lvl_key = None
        self._ng, self._ng_sig = None, _OFF

    def wait_for_level(self, timeout: float = 10.0) -> bool:
        """Blocks until the normalizer has what it needs of the song in the engine, or the timeout: for tests and tools,
        the audio thread never waits. True when there is nothing to wait for."""
        for _ in range(int(timeout / 0.002)):
            st = self._norm_settings()
            if st is None:
                return True
            lvl = self._level()
            if lvl is not None:
                self._lvl_kick(lvl)
                if self._goal(lvl, st, self._eq_sig()) is not None:
                    return True
            threading.Event().wait(0.002)
        return False

    # the stage for audio that does not come from this engine (chiptunes, video)
    def process_external(self, block: np.ndarray, ctx: Context | None = None,
                         apply_volume: bool = True) -> np.ndarray:
        """Run a (n, 2) float32 block at self.fs through the same stage as the engine's own audio:
        user EQ, user DSP chain, Enhance, then the volume when apply_volume. Returns a new float32
        array clipped to -1..1. The EQ analyzer tap (output_window) follows the post-EQ signal.

        Call it from one thread at a time, only while the engine itself is stopped: Player stops
        the engine before another one starts. The Track normalizer is not applied here, since it needs the
        whole song. ctx defaults to Context(fs, -1, 0, volume), which tells modules
        that the position is unknown. The first call after the engine played resets the filter
        memory and gives Enhance its middle brightness setting, since it has no track to measure;
        set_external_adaptive() gives it the measured one.
        """
        if self._stage_owner != "ext":
            self._stage_owner = "ext"
            self.reset_stage()
            self._ext_goal = self._ext_ratio = 0.4
            self._enh.set_adaptive(0.4)
        if self._ext_goal != self._ext_ratio:
            if self._ext_warm and self._enh.enabled:
                self._enh_to = copy.deepcopy(self._enh)
                self._enh_to.set_adaptive(self._ext_goal)
                self._enh_done = 0
                self._ext_ratio = self._ext_goal
            else:
                self._enh.set_adaptive(self._ext_goal)
                self._ext_ratio = self._ext_goal
        x = np.asarray(block, dtype=np.float32)
        eqd = self.user_eq.process(x)
        mono = (eqd[:, 0] + eqd[:, 1]) * np.float32(0.5)
        self._spec_buf = np.concatenate([self._spec_buf, mono])[-2048:]
        ch = self.dsp_chain
        if ch is not None and ch.active:
            eqd = ch.process(eqd, ctx or Context(self.fs, -1, 0, self._volume))
        out = self._enhance(eqd)
        self._ext_warm = True
        if apply_volume:
            out = out * np.float32(self._volume)
        return np.clip(out, -1.0, 1.0).astype(np.float32, copy=False)

    def _enhance(self, x: np.ndarray) -> np.ndarray:
        """Enhance, with a change of brightness blended in over _ADAPT_FADE_MS: the new setting runs beside the old
        one from the same filter memory, so nothing steps."""
        b = self._enh_to
        if b is None:
            return self._enh.process(x)
        total = max(1, int(self.fs * _ADAPT_FADE_MS / 1000))
        a_out, b_out = self._enh.process(x), b.process(x)
        w = np.clip((self._enh_done + 1 + np.arange(len(x))) / total, 0.0, 1.0).astype(np.float32)[:, None]
        self._enh_done += len(x)
        if self._enh_done >= total:
            b._enabled = self._enh._enabled
            self._enh, self._enh_to = b, None
        return a_out + (b_out - a_out) * w

    def set_external_adaptive(self, ratio: float | None):
        """The brightness (high_ratio of the music) Enhance adapts to for the external source now playing, None for
        the middle setting. Any thread. It is applied at the next block, faded in when the stage is already running."""
        self._ext_goal = 0.4 if ratio is None else float(ratio)

    def process_external_flush(self, ctx: Context | None = None, max_seconds: float = 5.0,
                               apply_volume: bool = True) -> np.ndarray:
        """The sound still inside the stage when an external source ends: what the EQ is ringing with, then the DSP
        chain (its latency, look-ahead and tails, DspChain.flush) and then Enhance, as one (n, 2) float32 block ready
        to be written after the last one, ending in a short fade. Empty when the stage was not in use or nothing
        audible remains. The stage is left reset. Same thread as process_external."""
        none = np.zeros((0, 2), np.float32)
        if self._stage_owner != "ext" or not self._ext_warm:
            return none
        fs = self.fs
        ctx = ctx or Context(fs, -1, 0, self._volume)
        budget = [max(0, int(max_seconds * fs))]
        try:
            e = self._ring_out(self.user_eq.process, budget)
            ch = self.dsp_chain
            if ch is not None and ch.active:
                y = np.concatenate([ch.process(e[i:i + 8192], ctx) for i in range(0, len(e), 8192)] + [e[:0]])
                y = np.concatenate((y, ch.flush(budget[0] / fs).astype(np.float32, copy=False)))
            else:
                y = e
            if self._enh_to is not None:
                self._enh, self._enh_to = self._enh_to, None
            if self._enh.enabled:
                y = np.concatenate([self._enh.process(y[i:i + 8192]) for i in range(0, len(y), 8192)]
                                   + [self._ring_out(self._enh.process, budget)])
        finally:
            self.reset_stage()
        y = np.asarray(y, dtype=np.float32)
        if apply_volume:
            y = y * np.float32(self._volume)
        y = np.clip(y, -1.0, 1.0)
        loud = np.flatnonzero(np.abs(y).max(axis=1) > _TAIL_FLOOR) if len(y) else []
        if len(loud) == 0:
            return none
        y = y[:loud[-1] + 1].copy()
        f = min(len(y), max(1, int(fs * 0.002)))
        y[-f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)[:, None]
        return y

    @staticmethod
    def _ring_out(process, budget: list) -> np.ndarray:
        """What `process` (a filter that is holding sound) gives out when fed silence, until it has been quiet for 50 ms
        or the budget of frames (a one item list, used up here) is spent."""
        block, out, quiet = 2048, [], 0
        while budget[0] > 0 and quiet < 2400:
            n = min(block, budget[0])
            y = process(np.zeros((n, 2), np.float32))
            budget[0] -= n
            out.append(np.array(y, dtype=np.float32))
            loud = np.flatnonzero(np.abs(y).max(axis=1) > _TAIL_FLOOR)
            quiet = n - 1 - int(loud[-1]) if len(loud) else quiet + n
        return np.concatenate(out) if out else np.zeros((0, 2), np.float32)

    def reset_stage(self):
        """Forget the signal in the EQ, Enhance and the DSP chain, and empty the analyzer tap."""
        if self._enh_to is not None:         # a brightness change that was being blended in is the setting now
            self._enh_to._enabled = self._enh._enabled
            self._enh, self._enh_to = self._enh_to, None
        self._enh.reset()
        self._ext_warm = False
        self.user_eq.reset_state()
        if self.dsp_chain is not None:
            self.dsp_chain.reset()
        self._spec_buf = np.zeros(0, dtype=np.float32)

    # transport
    def play(self):
        self._playing = True
        self._stage_owner = "engine"
        if self._sink_alive and self._sink_suspended:
            self._sink_suspended = False
            self._feed_gate = True
            self._w_resume.emit()
            self._postick.start()
            self.state_changed.emit(True)
            return
        self._start_sink()
        self.state_changed.emit(True)

    def pause(self):
        self._playing = False
        if self._sink_alive:
            self._feed_gate = False
            self._sink_suspended = True
            self._w_suspend.emit()
            self._postick.stop()
            self.state_changed.emit(False)

    def stop(self):
        self._feed_gate = False   # halt audio-thread writes before anything else
        self._playing = False
        self._loaded = False
        self._gen += 1   # cancel any in-flight decode
        self._norm_reset()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        self._stop_sink()
        with self._lock:
            self._cursor = 0
            # The decoded song goes back to the system: Player.play() after a stop loads it again, and a 60 minute mix
            # held for nothing is most of the process. load() calls this first, so a song change never holds two.
            self._buf = np.zeros((1, _CH), dtype=np.int16)
            self._serial += 1
            self._decoded = self._total = 0
            self._complete = False
        self._xf_active = False
        self._xf = None
        self.reset_stage()
        self.state_changed.emit(False)

    def seek(self, frac: float):
        n = self._total or self._decoded
        if n <= 0:
            return
        target = max(0, min(int(frac * n), n - 1))
        self._xf_active = False     # cancel any in-progress crossfade
        self._xf = None
        with self._lock:
            self._cursor = target
        self._worker.heard = target
        self._enh.reset()
        self.user_eq.reset_state()
        if self.dsp_chain is not None:
            self.dsp_chain.reset()
        self._fed_all = False
        # The resume decision uses the playback intent, not the momentary sink state,
        # which can read Idle or Buffering mid-seek and froze playback.
        if self._playing:
            if not self._sink_alive:
                self._start_sink()
            else:
                # Flush buffered audio and continue from the new cursor. The worker
                # reuses the sink, which keeps rapid drag-seeks safe.
                self._sink_suspended = False
                self._feed_gate = True
                self._w_reset.emit()
                if not self._postick.isActive():
                    self._postick.start()
        else:
            self._stop_sink()   # paused or stopped: play() starts fresh from the cursor
        self._emit_pos()

    # properties
    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        return self._playing is False and self._loaded

    @property
    def source(self):
        """The path (as text) the playback buffer was loaded for, None before the first load. A reader that
        draws from the buffer, like the waveform bar, can tell that it is not holding another track's audio."""
        return self._source

    def output_window(self, n: int = 2048):
        """Most recent n post-EQ mono samples for the analyzer, or None."""
        b = self._spec_buf
        return b[-n:].copy() if b.size >= n else None

    def envelope(self, n: int = 4096):
        """RMS envelope of the decoded track so far, for the waveform seek bar.

        The engine already decodes the whole file into memory to play it, so the seek bar bins that
        buffer instead of running a second ffmpeg pass over the same audio. Returns (points,
        fraction_decoded); the caller can draw the partial shape at once and refine it as decoding runs ahead.

        This runs on the GUI thread every 120 ms while a track decodes. Binning
        the whole buffer each time cost 400 ms by the end of a long track, so the
        energy of each 2048-frame block is worked out once and kept, and a call
        takes on at most a bounded number of new blocks.
        """
        if not self._lock.acquire(blocking=False):
            # busy: show what we had a moment ago, unless a new track has come in since
            return self._env_last if self._env_last_serial == self._serial else (None, 0.0)
        try:
            have = self._decoded
            buf = self._buf
            total = self._total or have
            serial = self._serial
            complete = self._complete
        finally:
            self._lock.release()
        if have < 2 or buf is None:
            return None, (0.0 if self._loaded else -1.0)    # -1: stopped, the song was given back and nothing more will come
        st = self._env
        if st is None or st[0] != serial:
            st = self._env = [serial, np.zeros(0, dtype=np.float64)]
        ss = st[1]
        nb = min(have // _ENV_BLOCK, len(ss) + _ENV_MAX_BLOCKS)
        if nb > len(ss):
            seg = buf[len(ss) * _ENV_BLOCK: nb * _ENV_BLOCK]   # decoded frames never change
            mono = (seg[:, 0].astype(np.float32) + seg[:, 1].astype(np.float32)) * np.float32(0.5 / 32768.0)
            ss = st[1] = np.concatenate([ss, np.square(mono).reshape(-1, _ENV_BLOCK).sum(axis=1, dtype=np.float64)])
        if nb < 1:
            return None, 0.0
        # Bin to n points over the *expected* length so the shape doesn't shift
        # horizontally as more of the track arrives.
        seen = nb * _ENV_BLOCK
        span = max(seen, total)
        cols = max(1, int(n * seen / span))
        cum = np.concatenate(([0.0], np.cumsum(ss[:nb])))
        edges = np.interp(np.linspace(0, nb, cols + 1), np.arange(nb + 1), cum)
        rms = np.sqrt(np.diff(edges) / (seen / cols)).astype(np.float32)
        out = np.zeros(n, dtype=np.float32)
        out[:cols] = rms
        peak = float(np.percentile(rms, 99)) if cols > 4 else float(rms.max() or 0.0)
        if peak > 1e-10:
            out = np.clip(out / peak, 0.0, 1.0)
        done = complete and nb >= have // _ENV_BLOCK
        self._env_last = (out, 1.0 if done else (seen / span if span else 0.0))
        self._env_last_serial = serial
        return self._env_last

    def mono_window(self, position_ms: int, n: int):
        """Mono window of the decoded source at the play head (for the scope/spectrum),
        or None if that region isn't decoded yet. Reuses the playback buffer, so there
        is no second decode."""
        pos = int(position_ms / 1000.0 * _FS)
        # Called every frame from the GUI thread. Decoded frames never change, so
        # this reads without the lock (a missed frame would decay or unlock the
        # trace); the serial catches a buffer that was swapped while reading.
        serial, buf, have = self._serial, self._buf, self._decoded
        if pos < 0 or buf is None or pos + n > have or serial != self._serial:
            return None
        seg = buf[pos:pos + n]
        return ((seg[:, 0].astype(np.float32) + seg[:, 1].astype(np.float32))
                 * np.float32(0.5 / 32768.0))

    # gapless: preload the next track
    def clear_next(self):
        self._next_gen += 1
        self._next_path = None
        self._next = None
        self._kill_next_proc()

    def _kill_next_proc(self):
        proc, self._next_proc = self._next_proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def set_next(self, path: Path, idx: int, trim: bool = False):
        if path is None:
            self.clear_next()
            return
        if str(path) == self._next_path:
            if self._next is not None:
                self._next["idx"] = idx     # same track already (pre)loaded
            return
        self._next_gen += 1
        gen = self._next_gen
        self._next_path = str(path)
        self._next = None
        self._kill_next_proc()
        threading.Thread(target=self._decode_next, args=(path, idx, trim, gen),
                         daemon=True).start()

    def _decode_next(self, path: Path, idx: int, trim: bool, gen: int):
        try:
            from ffmpeg_util import ffmpeg_path
            exe = ffmpeg_path()
            if not exe:
                return
            dur_ms = _duration_ms(path)
            proc = subprocess.Popen(
                [exe, "-i", str(path), "-f", "f32le", "-ar", str(_FS), "-ac", str(_CH), "-vn", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **plat_proc.popen_kwargs(group=False))
            self._next_proc = proc
            if gen != self._next_gen:
                proc.kill()                  # superseded while it was starting
            # Read in pieces and keep int16 like the main decode. The whole song as float32, plus a scan for the first
            # loud frame, the ratio and the conversion, each with its own temporaries, came to about eight times the
            # buffer, and this runs while the current song is held as well. Space is asked for from the known length
            # and only touched as it fills.
            buf = np.zeros((int(dur_ms / 1000 * _FS * 1.02) + _FS, _CH), dtype=np.int16)
            n, off, leftover = 0, None, b""
            while True:
                data = proc.stdout.read(262144)
                if not data:
                    break
                if leftover:
                    data = leftover + data
                nfr = len(data) // _FRAME_BYTES
                if nfr == 0:
                    leftover = data
                    continue
                leftover = data[nfr * _FRAME_BYTES:]
                chunk = np.frombuffer(data, dtype=np.float32, count=nfr * _CH).reshape(-1, _CH)
                chunk16 = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                if n + nfr > len(buf):
                    buf.resize((max(n + nfr + _FS, len(buf) * 5 // 4), _CH), refcheck=False)
                buf[n:n + nfr] = chunk16
                if trim and off is None:
                    loud = np.flatnonzero(np.abs(chunk16).max(axis=1) > 98)
                    if len(loud):
                        off = n + int(loud[0])
                n += nfr
            proc.wait()
            if gen != self._next_gen or n == 0:
                return
            buf.resize((n, _CH), refcheck=False)
            dur_ms = dur_ms or int(n / _FS * 1000)
            head = buf[: _FS * 60]            # high_ratio looks at the first 4000 half-overlapping 512 frame windows only
            try:
                ratio = high_ratio((head[:, 0].astype(np.float32) + head[:, 1].astype(np.float32)) * np.float32(0.5 / 32768.0))
            except Exception:
                ratio = 0.4
            if gen != self._next_gen:
                return
            # Measured here, so the swap to this song needs no hold.
            lvl = _Level(buf, _level_key(path))
            self._lvl_run(lvl, lambda: gen != self._next_gen)
            if gen != self._next_gen:
                return
            self._next = {"buf": buf, "total": len(buf), "dur_ms": dur_ms, "lvl": lvl,
                          "trim_off": off or 0, "idx": idx, "ratio": ratio, "src": str(path)}
        except Exception:
            from applog import log
            log.exception("preload (crossfade/gapless) decode failed: %s", path)

    def _gapless_swap(self) -> bool:
        gen = self._gen
        nx = self._next
        if not nx:
            return False
        with self._lock:
            if gen != self._gen:
                return False            # a track change got in first
            self._buf = nx["buf"]
            self._serial += 1
            self._source = nx.get("src")
            self._decoded = nx["total"]
            self._total = nx["total"]
            self._complete = True
            self._cursor = nx["trim_off"]
        self._dur_ms = nx["dur_ms"]
        self._set_lvl(nx.get("lvl"))
        goal = self._goal_for(self._lvl)       # the new song's own gain from its first sample; None waits for a measurement
        self._ng = None
        if goal is not None:
            self._ng_set(goal[0], goal[1], self._gen)
        self._fed_all = False
        idx = nx["idx"]
        self._next = None
        self._next_path = None
        try:
            self._enh.set_adaptive(nx["ratio"])   # no filter reset, so no click
        except Exception:
            pass
        self._w_advanced.emit(idx, gen)
        return True

    # crossfade (overlapping tail of A with head of B)
    def _start_crossfade(self):
        nx = self._next
        if not nx:
            return
        n = min(self._xfade_frames, self._total - self._cursor, nx["total"] - nx["trim_off"])
        if n <= 0:
            return
        a_gain = self._ng.v if self._ng is not None else 1.0
        b_gain, b_sig = self._goal_for(nx.get("lvl")) or (1.0, _OFF)
        self._xf = {
            "buf": nx["buf"], "cur": nx["trim_off"], "pos": 0, "len": int(n),
            "a_gain": float(a_gain), "b_gain": float(b_gain), "b_sig": b_sig, "lvl": nx.get("lvl"),
            "idx": nx["idx"], "dur": nx["dur_ms"],
            "total": nx["total"], "ratio": nx["ratio"], "src": nx.get("src"),
        }
        self._xf_active = True
        self._next = None          # consumed, so it is not also gapless-swapped
        self._next_path = None

    def _feed_crossfade(self, want: int, io):
        gen = self._gen
        xf = self._xf
        if xf is None:
            return                      # a track change cleared it since the caller looked
        take = min(want, self._total - self._cursor,
                   xf["total"] - xf["cur"], xf["len"] - xf["pos"])
        if take <= 0:
            self._finish_crossfade()
            return
        pos = self._cursor
        with self._lock:
            if gen != self._gen:
                return
            a = self._buf[pos:pos + take].astype(np.float32) * np.float32(1.0 / 32768.0)
            self._cursor += take
        b = xf["buf"][xf["cur"]:xf["cur"] + take].astype(np.float32) * np.float32(1.0 / 32768.0)
        # equal-power ramp: A follows cos (1 to 0), B follows sin (0 to 1), so the perceived loudness stays constant
        t  = (xf["pos"] + np.arange(take, dtype=np.float32)) / xf["len"]
        ga = (np.cos(t * (np.pi / 2)) * xf["a_gain"]).astype(np.float32)
        gb = (np.sin(t * (np.pi / 2)) * xf["b_gain"]).astype(np.float32)
        mixed = a * ga[:, None] + b * gb[:, None]
        xf["cur"]    += take
        xf["pos"]    += take

        eqd = self.user_eq.process(mixed)
        mono = (eqd[:, 0] + eqd[:, 1]) * np.float32(0.5)
        self._spec_buf = np.concatenate([self._spec_buf, mono])[-2048:]
        ch = self.dsp_chain
        if ch is not None and ch.active:
            eqd = ch.process(eqd, Context(_FS, pos, self._total, self._volume, True))   # the outgoing track's position
        out = self._enh.process(eqd) * self._volume
        np.clip(out, -1.0, 1.0, out=out)
        io.write(out.astype(np.float32, copy=False).tobytes())

        if xf["pos"] >= xf["len"] or self._cursor >= self._total or xf["cur"] >= xf["total"]:
            self._finish_crossfade()

    def _finish_crossfade(self):
        gen = self._gen
        xf = self._xf
        self._xf_active = False
        self._xf = None
        if not xf:
            return
        with self._lock:
            if gen != self._gen:
                return                  # a track change got in first
            self._buf = xf["buf"]
            self._serial += 1
            self._source = xf.get("src")
            self._decoded = xf["total"]
            self._total = xf["total"]
            self._complete = True
            self._cursor = xf["cur"]
        self._dur_ms = xf["dur"]
        self._set_lvl(xf["lvl"])
        self._ng_set(xf["b_gain"], xf["b_sig"], self._gen)      # the fade applied it already
        self._fed_all = False
        try:
            self._enh.set_adaptive(xf["ratio"])
        except Exception:
            pass
        self._w_advanced.emit(xf["idx"], gen)

    @property
    def position_ms(self) -> int:
        return int(self._heard_frames() / _FS * 1000)

    @property
    def duration_ms(self) -> int:
        return self._dur_ms

    def _heard_frames(self) -> int:
        # The audio thread works this out each tick (reading the sink from here
        # would cross threads); without a sink nothing is queued.
        return self._worker.heard if self._sink_alive else self._cursor

    def _emit_pos(self):
        dur = self._dur_ms
        pos_ms = self.position_ms
        frac = pos_ms / dur if dur > 0 else 0.0
        self.position_changed.emit(min(1.0, frac), pos_ms, dur)

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, v: float):
        self._volume = max(0.0, min(1.0, v))

    def set_audio_device(self, device: QAudioDevice):
        """Move playback to another output, whatever state it is in. A suspended sink stays bound to the
        device it was built on, so any sink that exists is torn down here and play() builds the next one
        from _dev."""
        self._dev = device
        keep = self._worker.keep
        if keep:
            self._w_close.emit()     # the kept-open output on the old device closes for real, also when it is the same one again
        if not self._sink_alive:
            if keep:
                self._w_open.emit()  # and the next one is opened on this device now
            return               # nothing running; the next play() picks it up
        pos = self._cursor
        if keep:
            self._feed_gate = False
            self._sink_alive = False
            self._sink_suspended = False
            self._postick.stop()     # the worker is closing the sink already
        else:
            self._stop_sink()
        with self._lock:
            self._cursor = pos
        if self._playing:
            self._start_sink()   # worker builds the new sink from self._dev
        elif keep:
            self._w_open.emit()

    def set_keep_open(self, on: bool):
        """Leave the output open when nothing plays. Stopping, skipping, seeking and pausing then keep the stream in the
        sound server, so what is wired to it in a patchbay stays wired. What is queued in the sink when playback stops
        plays out (a short queue is used for this), because emptying the queue closes the sink."""
        on = bool(on)
        if on == self._worker.keep:
            return
        self._worker.keep = on
        if not self._sink_alive:
            (self._w_open if on else self._w_close).emit()
