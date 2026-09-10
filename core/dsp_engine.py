"""Real-time audio playback engine with a live, bypassable DSP chain.

Replaces QMediaPlayer for plain audio files so the Enhance effect can be toggled
instantly (a wet/dry flag in the signal path) instead of re-transcoding. Decode is
done incrementally by ffmpeg into memory; output goes through a push-mode
QAudioSink owned by a dedicated audio QThread (_AudioFeedWorker) — the 10 ms feed
loop runs there, so GUI stalls (library rebuilds, stylesheet reapply, menus) can
no longer starve the sink into an audible dropout. The engine (GUI thread) talks
to the worker only through queued signals; shared decode state stays behind
self._lock. Video stays on QMediaPlayer; chiptunes stay on VgmPlayer.
"""
from __future__ import annotations
import subprocess
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal, QTimer, QThread, Qt
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices, QAudioDevice

from dsp_chain import Enhancer, high_ratio, ParametricEQ

_FS = 44100                     # engine sample rate; set to the output device's rate at startup
_CH = 2
_FRAME_BYTES = _CH * 4          # float32 stereo
_MAX_BLOCK = 4096               # frames per feed write
_FS_MIN, _FS_MAX = 44100, 192000   # clamp window for device-driven rates (high-res supported)


def _detect_output_rate(default: int = 44100) -> int:
    """The default audio output's preferred sample rate, clamped to a sane window.

    Following the device rate means hi-res files decode to their full rate (up to
    192 kHz) and, on a 48 kHz system, we skip the pointless 44.1→48 reconversion in
    the output stack. The DSP chain is rate-parametric (every biquad is designed at
    this fs) so nothing else needs to know the number. Falls back to ``default`` if
    the device can't be queried or reports an out-of-range rate.
    """
    try:
        rate = int(QMediaDevices.defaultAudioOutput().preferredFormat().sampleRate())
        if _FS_MIN <= rate <= _FS_MAX:
            return rate
    except Exception:
        pass
    return default


def _replaygain_db(path: Path):
    """Return a ReplayGain/R128 track-gain in dB if the file has the tag, else None."""
    try:
        from mutagen import File as MFile
        f = MFile(str(path))
        if f is None or not getattr(f, "tags", None):
            return None
        tags = f.tags
        # ReplayGain 2 (mp3/flac/etc.): "replaygain_track_gain" = e.g. "-6.34 dB"
        for key in ("replaygain_track_gain", "REPLAYGAIN_TRACK_GAIN"):
            try:
                v = tags[key]
                v = v[0] if isinstance(v, list) else v
                return float(str(v).lower().replace("db", "").strip())
            except Exception:
                pass
        # R128 (opus/ogg): "R128_TRACK_GAIN" in Q7.8 fixed point relative to -23 LUFS
        for key in ("R128_TRACK_GAIN", "r128_track_gain"):
            try:
                v = tags[key]
                v = v[0] if isinstance(v, list) else v
                return float(int(v)) / 256.0
            except Exception:
                pass
    except Exception:
        pass
    return None


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
    """Owns the QAudioSink + 10 ms feed timer on a dedicated audio thread.

    Everything that touches the sink lives here (QAudioSink is not thread-safe),
    driven by queued signals from the engine. The DSP math itself runs in
    engine._feed_once, called from this thread — numpy/scipy release the GIL for
    the heavy ops, so the GUI thread isn't blocked either way.
    """
    drained = Signal()   # last buffer of a finished track has played out

    def __init__(self, engine: "DspEngine"):
        super().__init__()
        self._e = engine
        self._sink: QAudioSink | None = None
        self._io = None
        self._timer: QTimer | None = None
        self.buffered_frames = 0   # read by the engine for position (plain int → GIL-safe)

    def setup(self):
        """Runs once in the audio thread (connected to QThread.started)."""
        self._timer = QTimer()
        self._timer.setInterval(10)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    def start_sink(self):
        self.stop_sink()
        e = self._e
        self._sink = QAudioSink(e._dev, e._fmt)
        self._sink.setVolume(1.0)   # volume is applied inside the DSP block
        self._io = self._sink.start()
        e._fed_all = False
        self.buffered_frames = 0
        self._timer.start()

    def stop_sink(self):
        if self._timer is not None:
            self._timer.stop()
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
        self._sink = None
        self._io = None
        self.buffered_frames = 0

    def suspend(self):
        if self._sink is not None:
            self._timer.stop()
            self._sink.suspend()

    def resume(self):
        if self._sink is not None:
            self._sink.resume()
            self._timer.start()

    def reset_io(self):
        """Seek while playing: flush buffered audio, keep the sink."""
        if self._sink is None:
            self.start_sink()
            return
        try:
            self._sink.reset()
            self._io = self._sink.start()
            if not self._timer.isActive():
                self._timer.start()
        except Exception:
            self.start_sink()

    def _tick(self):
        e = self._e
        if self._sink is None or self._io is None:
            return
        try:
            if e._fed_all:
                # drained the last buffer of a finished track → tell the engine
                if self._sink.state().name == "IdleState":
                    self._timer.stop()
                    self.buffered_frames = 0
                    self.drained.emit()
                return
            if e._feed_gate:
                e._feed_once(self._sink, self._io)
            # Snapshot AFTER the write so (cursor, buffered) form a consistent
            # pair — sampling buffered before the write made position_ms swing
            # backwards ~20 ms every other tick (visibly jittered the scope).
            self.buffered_frames = max(
                0, (self._sink.bufferSize() - self._sink.bytesFree()) // _FRAME_BYTES)
        except Exception:
            # never let one bad block kill the feed loop (e.g. an EQ edit racing
            # a process() call) — log and try again next tick
            from applog import log
            log.exception("audio feed tick failed")


class DspEngine(QObject):
    position_changed = Signal(float, int, int)   # frac, pos_ms, dur_ms
    state_changed    = Signal(bool)              # playing?
    finished         = Signal()
    advanced         = Signal(int)               # gapless: now playing this queue index
    # queued control → _AudioFeedWorker (all sink calls happen on the audio thread)
    _w_start   = Signal()
    _w_stop    = Signal()
    _w_suspend = Signal()
    _w_resume  = Signal()
    _w_reset   = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        global _FS
        _FS = _detect_output_rate(_FS)     # adopt the device rate once (chain/sink/decode all follow it)
        self.fs = _FS                      # exposed for diagnostics
        self._enh = Enhancer(_FS)
        self.user_eq = ParametricEQ(_FS)   # user parametric EQ (edited by the EQ panel)
        self._spec_buf = np.zeros(0, dtype=np.float32)   # rolling post-EQ mono (for the EQ analyzer)

        # gapless: pre-decoded next track
        self._next_gen = 0
        self._next_path = None
        self._next = None       # dict(buf, total, dur_ms, norm_gain, rg, trim_off, idx, ratio) when ready

        self._buf: np.ndarray = np.zeros((_FS, _CH), dtype=np.int16)
        self._lock = threading.Lock()
        self._decoded = 0          # frames written by decoder
        self._total = 0            # expected/known total frames
        self._complete = False
        self._cursor = 0           # next frame to feed
        self._dur_ms = 0
        self._gen = 0
        self._proc: subprocess.Popen | None = None
        self._volume = 0.7
        self._fed_all = False
        self._trim = False
        self._normalize = False
        self._norm_gain = 1.0       # target peak-based gain (preserves dynamics)
        self._norm_gain_cur = None  # smoothed value actually applied; None → seed on first use
        self._rg_db = None          # ReplayGain track gain (dB) if tagged, else None
        self._playing = False       # playback intent (robust against momentary sink states)
        self._loaded = False        # a track has been load()ed and not yet stop()ped
        self._xfade_frames = 0      # crossfade length (0 = off → instant gapless swap)
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
        self._worker.drained.connect(self._finish)          # queued → GUI thread
        self._w_start.connect(self._worker.start_sink)
        self._w_stop.connect(self._worker.stop_sink)
        self._w_suspend.connect(self._worker.suspend)
        self._w_resume.connect(self._worker.resume)
        self._w_reset.connect(self._worker.reset_io)
        self._audio_thread.start(QThread.TimeCriticalPriority)

        self._postick = QTimer(self)
        self._postick.setInterval(100)
        self._postick.timeout.connect(self._emit_pos)

    # DSP control
    def set_enabled(self, on: bool):
        self._enh.set_enabled(on)

    def set_crossfade(self, ms: int):
        """Crossfade length in ms (0 disables → instant gapless swap)."""
        self._xfade_frames = max(0, int(ms / 1000.0 * _FS))

    @property
    def enhance_enabled(self) -> bool:
        return self._enh.enabled

    # load / decode
    def load(self, path: Path, normalize: bool = False, trim: bool = False):
        self.stop()
        self.clear_next()
        self._loaded = True
        self._gen += 1
        gen = self._gen
        self._trim = trim
        self._normalize = normalize
        self._norm_gain = 1.0
        self._norm_gain_cur = None
        # ReplayGain (mutagen) and the ffprobe duration fallback are slow, blocking
        # reads — they now happen in the decode thread. For the buffer preallocation
        # and an immediate seekbar, use the duration the library cache already has
        # (instant when warm; one mutagen read at most when cold).
        self._rg_db = None
        self._dur_ms = 0
        try:
            import library_cache
            self._dur_ms = int((library_cache.get_tags(path).get("duration") or 0.0) * 1000)
        except Exception:
            pass
        total = int(self._dur_ms / 1000 * _FS) if self._dur_ms else 0
        with self._lock:
            # Stored as int16 (half the RAM of float32) — converted back to float on
            # the small per-block reads in _feed_once/mono_window/crossfade, which cost
            # nothing next to holding a whole track's PCM in memory twice (main + gapless).
            self._buf = np.zeros((max(total, _FS), _CH), dtype=np.int16)
            self._decoded = 0
            self._cursor = 0
            self._complete = False
            self._total = total
        threading.Thread(target=self._decode, args=(path, gen, normalize), daemon=True).start()

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
        self._normalize = False
        self._norm_gain = 1.0
        self._norm_gain_cur = None
        self._rg_db = None
        self._dur_ms = int(dur_ms)
        total = int(self._dur_ms / 1000 * _FS) if self._dur_ms else 0
        with self._lock:
            self._buf = np.zeros((max(total, _FS), _CH), dtype=np.int16)
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
                    end = self._decoded + nfr
                    if end > len(self._buf):
                        grow = np.zeros((end - len(self._buf) + _FS, _CH), dtype=np.int16)
                        self._buf = np.concatenate([self._buf, grow])
                    self._buf[self._decoded:end] = block
                    self._decoded = end
            if gen != self._gen:
                return
            with self._lock:
                self._buf = self._buf[:self._decoded]
                self._total = self._decoded
                self._complete = True
            if self._dur_ms == 0:
                self._dur_ms = int(self._decoded / _FS * 1000)
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

    def _decode(self, path: Path, gen: int, normalize: bool = False):
        try:
            # Slow tag probes, moved here from load() so track changes never
            # block the GUI (ffprobe alone could stall it for up to 10 s).
            if normalize:
                rg = _replaygain_db(path)
                if gen == self._gen:
                    self._rg_db = rg
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
            # No loudness filter here: dynaudnorm would lift quiet passages.
            # Normalize is applied as a single peak-based gain after decode so
            # within-song dynamics are preserved.
            cmd = [exe, "-i", str(path),
                   "-f", "f32le", "-ar", str(_FS), "-ac", str(_CH), "-vn", "-"]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self._proc = proc
            leftover = b""
            trim_done = not self._trim
            running_peak = 0.0
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
                # Running peak → normalize gain converges within the first moment
                # of playback (decode races well ahead of the play head).
                if normalize:
                    cpk = float(np.abs(chunk).max())
                    if cpk > running_peak:
                        running_peak = cpk
                        if self._rg_db is not None:
                            # honour the ReplayGain/R128 tag, but cap so it can't clip
                            g = min(10.0 ** (self._rg_db / 20.0), 0.99 / running_peak)
                        else:
                            g = 0.5 / running_peak          # peak → ~-6 dBFS
                        self._norm_gain = float(min(2.5, max(0.25, g)))
                chunk16 = np.clip(chunk * 32767.0, -32768, 32767).astype(np.int16)
                with self._lock:
                    end = self._decoded + nfr
                    if end > len(self._buf):
                        grow = np.zeros((end - len(self._buf) + _FS, _CH), dtype=np.int16)
                        self._buf = np.concatenate([self._buf, grow])
                    self._buf[self._decoded:end] = chunk16
                    self._decoded = end
                    # Trim leading silence: jump the play cursor to first audible
                    # frame once ~1s is available (only if we haven't played past it).
                    if not trim_done and self._decoded >= _FS:
                        loud = np.where(np.abs(self._buf[:self._decoded]).max(axis=1) > 98)[0]
                        off = int(loud[0]) if len(loud) else 0
                        if self._cursor <= off:
                            self._cursor = off
                        trim_done = True
            proc.wait()
            if gen != self._gen:
                return
            with self._lock:
                self._buf = self._buf[:self._decoded]
                self._total = self._decoded
                self._complete = True
                mono = (self._buf[:, 0].astype(np.float32) + self._buf[:, 1].astype(np.float32)) * np.float32(0.5 / 32768.0)
            if self._dur_ms == 0:
                self._dur_ms = int(self._decoded / _FS * 1000)
            # Final peak is already folded into _norm_gain via running_peak above.
            try:
                self._enh.set_adaptive(high_ratio(mono[: _FS * 60]))
            except Exception:
                pass
        except Exception:
            from applog import log
            log.exception("decode failed: %s", path)

    # sink lifecycle (sink itself lives on the audio thread)
    def _start_sink(self):
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

    def _finish(self):
        # `drained` is queued from the audio thread, so one can still be in the
        # event queue when the user changes track. Acting on it then fires
        # finished -> next(), which both hijacks playback and invalidates the
        # render the new track is waiting on (it bumps the chip generation, so
        # the finished render is discarded as stale and the UI sits on
        # "Rendering..." forever). stop()/load() clear _loaded, so a drain that
        # arrives after either of those is stale by definition.
        if not self._loaded:
            return
        self._playing = False
        self._loaded = False
        self._stop_sink()
        with self._lock:
            self._cursor = 0
        self.state_changed.emit(False)
        self.finished.emit()

    # feeding (runs on the audio thread via _AudioFeedWorker._tick)
    def _feed_once(self, sink: QAudioSink, io):
        free = sink.bytesFree()
        if free < _FRAME_BYTES:
            return
        want = min(free // _FRAME_BYTES, _MAX_BLOCK)

        # Crossfade: once within xfade_frames of the end and the next track is
        # decoded, overlap the two instead of swapping instantly.
        if (not self._xf_active and self._xfade_frames > 0 and self._next is not None
                and self._complete and 0 < (self._total - self._cursor) <= self._xfade_frames):
            self._start_crossfade()
        if self._xf_active:
            self._feed_crossfade(want, io)
            return

        with self._lock:
            avail = self._decoded - self._cursor
        if avail <= 0:
            if self._complete:
                if self._gapless_swap():   # seamlessly continue into the next track
                    return
                self._fed_all = True
            return
        with self._lock:
            take = min(want, avail)
            block = self._buf[self._cursor:self._cursor + take]
            block = block.astype(np.float32) * np.float32(1.0 / 32768.0)
            self._cursor += take
        # Smooth normalize gain across the block, applied BEFORE the DSP chain so
        # the Enhancer's safety limiter sees the boosted signal (applying it after
        # the limiter clipped normalized+enhanced tracks — the crossfade path
        # already gains pre-chain, this matches it). Seed so playback never starts
        # boosted (≤ unity); drop quickly to catch loud parts but rise slowly so
        # quiet intros don't swell or briefly blast.
        if self._norm_gain_cur is None:
            self._norm_gain_cur = min(self._norm_gain, 1.0)
        target = self._norm_gain
        start = self._norm_gain_cur
        if abs(target - start) < 1e-4:
            if target != 1.0:
                block = block * np.float32(target)
            self._norm_gain_cur = target
        else:
            a = 0.25 if target < start else 0.02
            self._norm_gain_cur = start + (target - start) * a
            ramp = np.linspace(start, self._norm_gain_cur, len(block), dtype=np.float32)
            block = block * ramp[:, None]
        eqd = self.user_eq.process(block)
        # Keep the last ~2048 post-EQ mono samples for the EQ panel's analyzer.
        mono = (eqd[:, 0] + eqd[:, 1]) * np.float32(0.5)
        self._spec_buf = np.concatenate([self._spec_buf, mono])[-2048:]
        out = self._enh.process(eqd) * self._volume
        np.clip(out, -1.0, 1.0, out=out)
        io.write(out.astype(np.float32, copy=False).tobytes())

    # transport
    def play(self):
        self._playing = True
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
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        self._stop_sink()
        with self._lock:
            self._cursor = 0
        self._xf_active = False
        self._xf = None
        self._enh.reset()
        self.user_eq.reset_state()
        self._spec_buf = np.zeros(0, dtype=np.float32)
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
        self._enh.reset()
        self.user_eq.reset_state()
        self._fed_all = False
        # Resume decision uses the playback *intent*, not the momentary sink state
        # (which can read Idle/Buffering mid-seek → the old freeze bug).
        if self._playing:
            if not self._sink_alive:
                self._start_sink()
            else:
                # Flush buffered audio and continue from the new cursor — the
                # worker reuses the sink (robust under rapid drag-seeks).
                self._sink_suspended = False
                self._feed_gate = True
                self._w_reset.emit()
                if not self._postick.isActive():
                    self._postick.start()
        else:
            self._stop_sink()   # paused/stopped → play() starts fresh from cursor
        self._emit_pos()

    # properties
    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        return self._playing is False and self._loaded

    def output_window(self, n: int = 2048):
        """Most recent n post-EQ mono samples for the analyzer, or None."""
        b = self._spec_buf
        return b[-n:].copy() if b.size >= n else None

    def envelope(self, n: int = 4096):
        """RMS envelope of the decoded track so far, for the waveform seek bar.

        The engine already decodes the whole file into memory to play it, so the
        seek bar can bin that buffer instead of running a second ffmpeg pass over
        the same audio. Returns (points, fraction_decoded) — the caller can draw
        the partial shape immediately and refine it as decoding runs ahead.
        """
        with self._lock:
            have = self._decoded
            buf = self._buf
            total = self._total or have
        if have < 2 or buf is None:
            return None, 0.0
        seg = buf[:have]
        # Bin to n points over the *expected* length so the shape doesn't shift
        # horizontally as more of the track arrives.
        span = max(have, total)
        cols = max(1, int(n * have / span))
        per = max(1, have // cols)
        usable = cols * per
        mono = (seg[:usable, 0].astype(np.float32) +
                seg[:usable, 1].astype(np.float32)) * np.float32(0.5 / 32768.0)
        rms = np.sqrt(np.mean(mono.reshape(cols, per) ** 2, axis=1))
        out = np.zeros(n, dtype=np.float32)
        out[:cols] = rms
        peak = float(np.percentile(rms, 99)) if cols > 4 else float(rms.max() or 0.0)
        if peak > 1e-10:
            out = np.clip(out / peak, 0.0, 1.0)
        return out, (have / span if span else 0.0)

    def mono_window(self, position_ms: int, n: int):
        """Mono window of the decoded source at the play head (for the scope/spectrum),
        or None if that region isn't decoded yet. Reuses the playback buffer — no
        second decode."""
        pos = int(position_ms / 1000.0 * _FS)
        with self._lock:
            if pos < 0 or self._buf is None or pos + n > self._decoded:
                return None
            seg = self._buf[pos:pos + n]
            return ((seg[:, 0].astype(np.float32) + seg[:, 1].astype(np.float32))
                     * np.float32(0.5 / 32768.0))

    # gapless: preload the next track
    def clear_next(self):
        self._next_gen += 1
        self._next_path = None
        self._next = None

    def set_next(self, path: Path, idx: int, normalize: bool = False, trim: bool = False):
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
        threading.Thread(target=self._decode_next, args=(path, idx, normalize, trim, gen),
                         daemon=True).start()

    def _decode_next(self, path: Path, idx: int, normalize: bool, trim: bool, gen: int):
        try:
            from ffmpeg_util import ffmpeg_path
            exe = ffmpeg_path()
            if not exe:
                return
            proc = subprocess.Popen(
                [exe, "-i", str(path), "-f", "f32le", "-ar", str(_FS), "-ac", str(_CH), "-vn", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            raw = proc.stdout.read()
            proc.wait()
            if gen != self._next_gen:
                return
            arr = np.frombuffer(raw, dtype=np.float32)
            if arr.size % _CH:
                arr = arr[:arr.size - (arr.size % _CH)]
            fbuf = arr.reshape(-1, _CH)
            if fbuf.size == 0:
                return
            dur_ms = _duration_ms(path) or int(len(fbuf) / _FS * 1000)
            ng, rg = 1.0, None
            if normalize:
                rg = _replaygain_db(path)
                peak = float(np.abs(fbuf).max())
                if peak > 1e-6:
                    g = min(10.0 ** (rg / 20.0), 0.99 / peak) if rg is not None else 0.5 / peak
                    ng = float(min(2.5, max(0.25, g)))
            off = 0
            if trim:
                loud = np.where(np.abs(fbuf).max(axis=1) > 0.003)[0]
                off = int(loud[0]) if len(loud) else 0
            try:
                ratio = high_ratio((fbuf[:, 0] + fbuf[:, 1]) * 0.5)
            except Exception:
                ratio = 0.4
            if gen != self._next_gen:
                return
            # int16, matching the main buffer — this is the second full-track buffer
            # held alongside the live one during gapless preload, so halving it counts.
            buf = np.clip(fbuf * 32767.0, -32768, 32767).astype(np.int16)
            self._next = {"buf": buf, "total": len(buf), "dur_ms": dur_ms, "norm_gain": ng,
                          "rg": rg, "trim_off": off, "idx": idx, "ratio": ratio}
        except Exception:
            from applog import log
            log.exception("preload (crossfade/gapless) decode failed: %s", path)

    def _gapless_swap(self) -> bool:
        nx = self._next
        if not nx:
            return False
        with self._lock:
            self._buf = nx["buf"]
            self._decoded = nx["total"]
            self._total = nx["total"]
            self._complete = True
            self._cursor = nx["trim_off"]
        self._dur_ms = nx["dur_ms"]
        self._norm_gain = nx["norm_gain"]
        self._norm_gain_cur = None
        self._rg_db = nx["rg"]
        self._fed_all = False
        idx = nx["idx"]
        self._next = None
        self._next_path = None
        try:
            self._enh.set_adaptive(nx["ratio"])   # no filter reset → no click
        except Exception:
            pass
        self.advanced.emit(idx)
        return True

    # crossfade (overlapping tail of A with head of B)
    def _start_crossfade(self):
        nx = self._next
        if not nx:
            return
        n = min(self._xfade_frames, self._total - self._cursor, nx["total"] - nx["trim_off"])
        if n <= 0:
            return
        a_gain = self._norm_gain_cur if self._norm_gain_cur is not None else min(self._norm_gain, 1.0)
        self._xf = {
            "buf": nx["buf"], "cur": nx["trim_off"], "pos": 0, "len": int(n),
            "a_gain": float(a_gain), "b_gain": float(nx["norm_gain"]),
            "idx": nx["idx"], "dur": nx["dur_ms"], "rg": nx["rg"],
            "total": nx["total"], "ratio": nx["ratio"],
        }
        self._xf_active = True
        self._next = None          # consumed — don't also gapless-swap it
        self._next_path = None

    def _feed_crossfade(self, want: int, io):
        xf = self._xf
        take = min(want, self._total - self._cursor,
                   xf["total"] - xf["cur"], xf["len"] - xf["pos"])
        if take <= 0:
            self._finish_crossfade()
            return
        with self._lock:
            a = self._buf[self._cursor:self._cursor + take].astype(np.float32) * np.float32(1.0 / 32768.0)
        b = xf["buf"][xf["cur"]:xf["cur"] + take].astype(np.float32) * np.float32(1.0 / 32768.0)
        # equal-power ramp: A cos→0, B sin→1 (constant perceived loudness)
        t  = (xf["pos"] + np.arange(take, dtype=np.float32)) / xf["len"]
        ga = (np.cos(t * (np.pi / 2)) * xf["a_gain"]).astype(np.float32)
        gb = (np.sin(t * (np.pi / 2)) * xf["b_gain"]).astype(np.float32)
        mixed = a * ga[:, None] + b * gb[:, None]
        self._cursor += take
        xf["cur"]    += take
        xf["pos"]    += take

        eqd = self.user_eq.process(mixed)
        mono = (eqd[:, 0] + eqd[:, 1]) * np.float32(0.5)
        self._spec_buf = np.concatenate([self._spec_buf, mono])[-2048:]
        out = self._enh.process(eqd) * self._volume
        np.clip(out, -1.0, 1.0, out=out)
        io.write(out.astype(np.float32, copy=False).tobytes())

        if xf["pos"] >= xf["len"] or self._cursor >= self._total or xf["cur"] >= xf["total"]:
            self._finish_crossfade()

    def _finish_crossfade(self):
        xf = self._xf
        self._xf_active = False
        self._xf = None
        if not xf:
            return
        with self._lock:
            self._buf = xf["buf"]
            self._decoded = xf["total"]
            self._total = xf["total"]
            self._complete = True
            self._cursor = xf["cur"]
        self._dur_ms = xf["dur"]
        self._norm_gain = xf["b_gain"]
        self._norm_gain_cur = xf["b_gain"]   # already applied during the fade → no swell
        self._rg_db = xf["rg"]
        self._fed_all = False
        try:
            self._enh.set_adaptive(xf["ratio"])
        except Exception:
            pass
        self.advanced.emit(xf["idx"])

    @property
    def position_ms(self) -> int:
        return int(self._heard_frames() / _FS * 1000)

    @property
    def duration_ms(self) -> int:
        return self._dur_ms

    def _heard_frames(self) -> int:
        # buffered_frames is maintained by the audio thread each tick — reading
        # the sink itself from here would cross threads.
        buffered = self._worker.buffered_frames if self._sink_alive else 0
        return max(0, self._cursor - buffered)

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
        self._dev = device
        if self._sink_alive and self.is_playing:
            pos = self._cursor
            self._stop_sink()
            with self._lock:
                self._cursor = pos
            self._start_sink()   # worker builds the new sink from self._dev
