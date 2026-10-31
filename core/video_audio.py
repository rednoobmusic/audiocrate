"""The sound of video files, through the same EQ, DSP chain and Enhance as everything else.

QMediaPlayer keeps playing the picture on its own clock but is given no audio output. Its decoded sound
comes out of a QAudioBufferOutput (already converted by Qt to float stereo at the engine's rate) and is
played through our own QAudioSink after the engine's stage (DspEngine.process_external). The tap hands over
a buffer when the media clock reaches its start time, the same moment the video sink gets the frame with that
time (measured: both within 1 ms), so a buffer's arrival is a precise picture clock. The sound can only be
late by what our sink has queued; that queue is the "lag", kept near TARGET_MS and trimmed by SyncCore.

Everything audio runs on its own thread (_Feeder), never on the GUI thread. If anything fails the player's
own audio output is put back and the video goes on with the sound it had before; when the output works again
(a device change) the sound comes back to our sink.

The tap carries the sound unchanged at any playback speed, so a speed other than 1 is resampled here (the pitch
follows the speed, as it does on tape). The same resampler makes the small rate corrections of the sync.
The sound can be delayed (a positive "video sound delay") or the picture can be (a negative one).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QMetaObject, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtMultimedia import (QAudioBuffer, QAudioFormat, QAudioSink,
                                  QMediaDevices, QVideoSink)

try:
    from PySide6.QtMultimedia import QAudioBufferOutput
except ImportError:                                 # Qt before 6.8 has no sound tap: the player keeps the sound of a video
    QAudioBufferOutput = None

from dsp_base import Context

try:
    from applog import log
except Exception:                                   # a bare test run
    import logging
    log = logging.getLogger("audiocrate")

_CH = 2
_FRAME_BYTES = _CH * 4
_SINK_MS = 400                  # capacity asked of the sink; how much is kept queued is SyncCore's business
_WATCH_MS = 250                 # how often the audio thread looks at the sink and the sync
_SYNC_FAIL_S = 5.0              # sync lost for this long hands the sound back to the player
_NO_AUDIO_S = 2.0               # a playing video with sound that sends no buffer for this long is given up
_RING_S = 2.0                   # seconds of processed sound kept for the scope and the spectrum
_RETRIES = 3                    # times the sound is taken back after a lost device, per video
_RETRY_MS = 2500                # how long after a fallback the output is tried again
_DELAY_MAX = 200                # the video sound delay setting runs from minus to plus this many ms
_PIC_FRAMES = 16                # most frames the picture delay holds back; a decoder has only so many buffers to give out

TARGET_MS = 50.0                # the sound plays this long after the clock reaches it; one late wake-up cannot starve the sink
MAX_TARGET_MS = 150.0           # raised toward this when the sink keeps running dry
DEAD_MS = 12.0                  # drift inside this band is left alone
RELEASE_MS = 4.0                # a correction that has started goes on until the drift is this small
FULL_MS = 20.0                  # drift at which the rate change reaches its limit
MAX_RATE = 0.005                # the rate change is at most 0.5 percent
BIG_MS = 40.0                   # past this the sound is also skipped forward, in small steps
GAP_MS = 30.0                   # a lag this much too small (a longer delay was asked for) gets silence put in at once
STEP_MS = 10.0                  # no single skip is larger than this
UNSYNC_MS = 60.0                # drift beyond this counts toward giving up
JUMP_S = 0.03                   # a buffer this far from where the last one ended is a seek or a loop
LATE_S = 1.0                    # arriving this late is a pause or a buffering stop, not a slow thread
LEAK = 0.001                    # the clock offset may creep up by 1 ms per second
_FADE_S = 0.002                 # fade around a skip or after silence
PLAY_MIN, PLAY_MAX = 0.1, 8.0    # playback speeds the sync follows


def _skip(x: np.ndarray, d: int):
    """x without d frames somewhere in the middle, the seam crossfaded over 64 frames. Returns (block, frames removed)."""
    n = len(x)
    f = 64
    d = min(d, n - f - 1)
    if d <= 0:
        return x, 0
    p = f + (n - f - d) // 2
    w = np.linspace(0.0, 1.0, f, endpoint=False, dtype=np.float32)[:, None]
    seam = x[p - f:p] * (1.0 - w) + x[p - f + d:p + d] * w
    return np.concatenate([x[:p - f], seam, x[p + d:]]), d


def _gap(x: np.ndarray, pad: int, f: int, first: bool):
    """x with pad frames of silence in front (first, the start of a stream) or put in after its first f frames, which
    fade out as the rest fades in. Returns (block, frame the silence starts at)."""
    f = max(0, min(f, len(x) // 2))
    w = np.linspace(0.0, 1.0, f, endpoint=False, dtype=np.float32)[:, None]
    zeros = np.zeros((pad, _CH), np.float32)
    if first:
        y = x.copy()
        y[:f] *= w
        return np.concatenate([zeros, y]), 0
    tail = x[f:].copy()
    tail[:f] *= w
    return np.concatenate([x[:f] * (1.0 - w), zeros, tail]), f


_RS_HALF = 6                    # taps on each side of the point read, at speeds up to 1
_RS_PHASES = 2048               # fractions of a frame the table tells apart (a phase error of 1/4096 frame is -72 dB at 8 kHz)
_RS_BETA = 8.0                  # Kaiser window of the sinc
_RS_SETTLE = 0.002              # per frame, how fast the read point is walked back onto the frame grid
_kernels: dict = {}


def _kernel(scale: float):
    """(table, half width) of the windowed sinc for a read speed of `scale` input frames per output frame. Row i is the
    taps for a point i / _RS_PHASES past a frame; taps cover -(half - 1) .. half frames around that frame."""
    key = round(max(1.0, scale), 3)
    k = _kernels.get(key)
    if k is None:
        half = int(math.ceil(_RS_HALF * key))
        fc = 0.5 if key <= 1.0 else 0.48 / key              # below the new Nyquist when it plays faster
        taps = np.arange(-(half - 1), half + 1, dtype=np.float64)
        u = np.arange(_RS_PHASES + 1, dtype=np.float64) / _RS_PHASES
        x = taps[None, :] - u[:, None]
        h = 2 * fc * np.sinc(2 * fc * x)
        h *= np.i0(_RS_BETA * np.sqrt(np.clip(1 - (x / half) ** 2, 0, None))) / np.i0(_RS_BETA)
        if fc == 0.5:
            h[0] = 0.0
            h[0, half - 1] = 1.0                            # a point on a frame is that frame, exactly
            h[-1] = 0.0
            h[-1, half] = 1.0
        if len(_kernels) > 8:
            _kernels.clear()
        k = _kernels[key] = (h.astype(np.float32), half)
    return k


class _Resampler:
    """Reads a stream at a point that moves `step` frames per output frame, between the frames, with a windowed sinc.
    The point and the last frames are kept between calls, so the output is one continuous signal however the calls
    cut it up. The output trails the input by `delay` frames (half the sinc), the same for any step. At step 1 with
    the point on the frame grid the output is the input, delayed, bit for bit and with no arithmetic."""

    def __init__(self):
        self.set_scale(1.0)

    def set_scale(self, scale: float):
        tab, half = _kernel(scale)
        old = getattr(self, "half", None)
        self.tab, self.half = tab, half
        self.off = np.arange(2 * half)
        if old is None:
            self.reset()
        elif old != half:
            hist = np.concatenate((np.zeros((2 * half, _CH), np.float32), self.hist))[-2 * half:]
            self.hist, self.pos = hist, self.pos + (half - old)

    def reset(self):
        self.hist = np.zeros((2 * self.half, _CH), np.float32)
        self.pos = float(self.half)

    @property
    def delay(self) -> float:
        """Frames of the input that have been taken in and not yet come out."""
        return 2 * self.half - self.pos

    def process(self, x: np.ndarray, step: float = 1.0) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        h = self.half
        ext = np.concatenate((self.hist, x))
        self.hist = ext[-2 * h:]
        on_grid = self.pos == h
        if step == 1.0 and on_grid:
            return ext[h:h + n]
        limit = n + h
        m = int(math.ceil((limit - self.pos) / step)) + 3
        j = np.arange(m + 1, dtype=np.float64)
        if step == 1.0:                                     # off the grid at an even speed: walk back onto it
            d = self.pos - h
            d = -d if d < 0.5 else 1.0 - d
            walk = np.sign(d) * np.minimum(j * _RS_SETTLE, abs(d))
            p = self.pos + j + walk
        else:
            p = self.pos + j * step
        k = int(np.searchsorted(p, limit, side="left"))
        nxt = float(p[k])
        p = p[:k]
        idx = np.floor(p).astype(np.intp)
        coef = self.tab[((p - idx) * _RS_PHASES + 0.5).astype(np.intp)]
        at = (idx - (h - 1))[:, None] + self.off
        out = np.empty((k, _CH), np.float32)
        for c in range(_CH):
            out[:, c] = np.einsum("mk,mk->m", ext[:, c][at], coef)
        self.pos = nxt - n
        if abs(self.pos - h) < 1e-9:
            self.pos = float(h)
        return out


class SyncCore:
    """Keeps the sound at a steady distance behind the picture clock. Pure numpy, no Qt, no time of its own.

    The tap delivers a buffer when the media clock reaches its start time t, so (now - t) minus the
    smallest value seen is how late this one is, and the lag when it is heard is that plus what the sink
    has queued. The lag is steered to `target` (plus `extra`, the video sound delay) with a rate change of at
    most 0.5 percent made by a fractional resampler, and after a stall by skipping at most STEP_MS per buffer.
    Starting, a seek and an empty sink begin with a short run of silence, and so does a longer delay.
    A playback speed other than 1 is resampled the same way, so the sink always gets one second a second.
    """

    def __init__(self, fs: int, target_ms: float = TARGET_MS):
        self.fs = int(fs)
        self._target0 = target_ms / 1000.0
        self.target = self._target0
        self.extra = 0.0            # seconds added to the target, set by the video sound delay
        self.rate = 1.0             # playback speed of the media
        self.underruns = 0
        self.pads = 0
        self.skips = 0
        self.jumps = 0
        self.chunks = 0
        self.corrected = 0.0        # frames added (+) or removed (-) by the rate change and skips, in total
        self.max_step = 0.0         # the most frames one buffer was changed by, not counting silence at a start
        self.pad_frames = 0
        self.lag = 0.0              # seconds, as of the last buffer
        self.err = 0.0              # lag minus target
        self.last_gap = (0, 0)      # where silence was put into the last block and how much
        self.last_lead = 0.0        # frames of the input the first frame of the last block trails by
        self._rs = _Resampler()
        self._t_a = self._tau_a = 0.0
        self._ur: deque = deque(maxlen=4)
        self.reset()

    def set_rate(self, rate: float):
        """The media runs at this speed. The wall clock model carries on from the last buffer."""
        rate = max(PLAY_MIN, min(PLAY_MAX, float(rate)))
        if abs(rate - self.rate) < 1e-6:
            return
        self._t_a, self._tau_a = self._t_last, self._tau_last
        self.rate = rate
        self._rs.set_scale(rate)

    def reset(self, full: bool = False):
        """Forget the clock: a start, a resume or a seek."""
        if full:
            self.target = self._target0
            self._ur.clear()
        self._w0 = None
        self._last = 0.0
        self._next_t = None
        self._correcting = False
        self._skipping = False
        self._cushion = False
        self._bad_since = None
        self._t_last, self._tau_last = self._t_a, self._tau_a
        self._rs.reset()

    def bad_for(self, now: float) -> float:
        """How long the drift has been beyond UNSYNC_MS without a break."""
        return 0.0 if self._bad_since is None else now - self._bad_since

    def process(self, x: np.ndarray, t: float, now: float, queued: int) -> np.ndarray:
        """x (n, 2) float32 starting at media time t seconds, delivered at monotonic time `now` with
        `queued` frames waiting in the sink. Returns what to play."""
        n = len(x)
        if n == 0:
            return x
        fs = self.fs
        rate = self.rate
        self.chunks += 1
        tau = self._tau_a + (t - self._t_a) / rate         # the media time as wall time at this speed
        self._t_last, self._tau_last = t, tau
        off = now - tau
        jumped = self._next_t is not None and abs(t - self._next_t) > JUMP_S
        self._next_t = t + n / fs
        if self._w0 is None:
            self._w0, self._cushion = off, True
        elif jumped:
            self._w0 = off
            self.jumps += 1
        else:
            self._w0 = min(off, self._w0 + LEAK * max(0.0, now - self._last))
        self._last = now
        late = off - self._w0
        if late > LATE_S:
            self._w0, late = off, 0.0
        elif not self._cushion and queued == 0 and not jumped:
            self.underruns += 1
            self._cushion = True
            self._ur.append(now)
            if len(self._ur) >= 2 and now - self._ur[-2] < 10.0:
                self.target = min(MAX_TARGET_MS / 1000.0, self.target + 0.02)
        rs = self._rs
        self.last_lead = rs.delay
        lag = late + (queued + rs.delay) / fs
        target = self.target + self.extra
        err = lag - target
        self.lag, self.err = lag, err
        if abs(err) > UNSYNC_MS / 1000.0:
            if self._bad_since is None:
                self._bad_since = now
        else:
            self._bad_since = None
        self.last_gap = (0, 0)
        f = int(_FADE_S * fs)

        if self._cushion:
            self._cushion = False
            d = rs.process(x, rate)
            pad = int((target - lag) * fs)
            if pad <= 0:
                return d
            self.pads += 1
            self.pad_frames += pad
            y, at = _gap(d, pad, f, True)
            self.last_gap = (at, pad)
            return y

        if err > BIG_MS / 1000.0:
            self._skipping = True
        elif err < DEAD_MS / 1000.0:
            self._skipping = False          # once skipping, go on down to the band instead of crawling the last 30 ms
        if self._skipping:
            d = rs.process(x, rate)
            y, got = _skip(d, min(int(STEP_MS * fs / 1000), int((err - RELEASE_MS / 1000.0) * fs)))
            if got:
                self.skips += 1
                self.corrected -= got
                self.max_step = max(self.max_step, got)
            return y
        if err < -GAP_MS / 1000.0:
            self._correcting = False
            d = rs.process(x, rate)
            pad = min(int(-err * fs), int(0.4 * fs))
            self.pad_frames += pad
            y, at = _gap(d, pad, f, False)
            self.last_gap = (at, pad)
            return y
        a = abs(err)
        if a > DEAD_MS / 1000.0:
            self._correcting = True
        elif a < RELEASE_MS / 1000.0:
            self._correcting = False
        step = rate
        if self._correcting:
            step = rate * (1.0 + max(-MAX_RATE, min(MAX_RATE, err / (FULL_MS / 1000.0) * MAX_RATE)))
        y = rs.process(x, step)
        got = len(y) - n / rate
        self.corrected += got
        self.max_step = max(self.max_step, abs(got))
        return y


def block_from_buffer(buf, fs: int):
    """(block, start seconds) from a QAudioBuffer: float32 (n, 2) at fs. Raises ValueError when it cannot."""
    f = buf.format()
    if f.sampleRate() != fs:
        raise ValueError(f"the video's sound arrived at {f.sampleRate()} Hz, expected {fs}")
    sf = QAudioFormat.SampleFormat
    raw = buf.constData()
    kind = f.sampleFormat()
    if kind == sf.Float:
        a = np.frombuffer(raw, dtype=np.float32)
    elif kind == sf.Int16:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) * np.float32(1.0 / 32768.0)
    elif kind == sf.Int32:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) * np.float32(1.0 / 2147483648.0)
    elif kind == sf.UInt8:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) * np.float32(1.0 / 128.0)
    else:
        raise ValueError("the video's sound has a sample format that is not handled")
    ch = max(1, f.channelCount())
    a = a[:len(a) // ch * ch].reshape(-1, ch)
    if ch == 1:
        a = np.repeat(a, 2, axis=1)
    elif ch > 2:
        a = a[:, :2]
    st = buf.startTime()
    return np.array(a, dtype=np.float32, copy=True), (st / 1e6 if st >= 0 else -1.0)


def _make_sink(device, fmt):
    return QAudioSink(device, fmt)


class _Ring:
    """The last _RING_S seconds of processed sound as mono, filed by media time (frame numbers at the stream's rate).
    The feeder thread writes, the GUI thread reads, and a window that was overwritten while it was read is refused."""

    def __init__(self, fs: int):
        self.size = 1 << int(math.ceil(math.log2(fs * _RING_S)))
        self.buf = np.zeros(self.size, np.float32)
        self.lo = self.hi = 0               # the media frames from lo to hi are in there, in one piece
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self.lo = self.hi

    def put(self, i0: int, seg: np.ndarray):
        n = len(seg)
        if n == 0 or n > self.size:
            return
        with self._lock:
            if abs(i0 - self.hi) > 8 or self.lo == self.hi:
                self.lo = i0                # a seek, a loop or the first block: what was there is of another place
            i = i0 % self.size
            k = min(n, self.size - i)
            self.buf[i:i + k] = seg[:k]
            self.buf[:n - k] = seg[k:]
            self.hi = i0 + n

    def get(self, pos: int, n: int):
        """n frames from media frame pos, or the newest n if pos is past what has come in. None if they are not there."""
        with self._lock:
            hi, lo = self.hi, max(self.lo, self.hi - self.size)
            p = min(pos, hi - n)
            if n <= 0 or p < lo:
                return None
            i = p % self.size
            k = min(n, self.size - i)
            return np.concatenate((self.buf[i:i + k], self.buf[:n - k]))


class _Feeder(QObject):
    """Lives on the audio thread: buffers in, processed audio into the sink."""
    failed = Signal(str)
    faded = Signal()

    def __init__(self, stage, fs: int, sink_factory, clock):
        super().__init__()
        self.stage = stage
        self.fs = fs
        self.core = SyncCore(fs)
        self.ring = _Ring(fs)
        self._make_sink = sink_factory
        self._clock = clock
        self._dev = None
        self._sink = None
        self._io = None
        self._fmt = QAudioFormat()
        self._fmt.setSampleRate(fs)
        self._fmt.setChannelCount(_CH)
        self._fmt.setSampleFormat(QAudioFormat.Float)
        self._running = False
        self._dead = False
        self._fading = False
        self.level = 1.0            # the player's volume, written from the GUI thread
        self.rate = 1.0             # playback speed, written from the GUI thread
        self.extra_s = 0.0          # the video sound delay if positive, written from the GUI thread
        self.dur_s = 0.0            # written from the GUI thread
        self._lvl = 1.0
        self._pos_t = 0.0
        self.chunks = 0             # read from the GUI thread
        self.stage_ms = 0.0         # average cost of one buffer through the stage, milliseconds
        self.stage_peak_ms = 0.0
        self.own_ms = 0.0           # and everything else we do per buffer
        self.dropped = 0
        self._timer = None

    @Slot()
    def setup(self):
        self._timer = QTimer()
        self._timer.setInterval(_WATCH_MS)
        self._timer.timeout.connect(self.watchdog)
        self._timer.start()

    @Slot()
    def teardown(self):
        if self._timer is not None:
            self._timer.stop()
            self._timer = None            # freed here, in the thread that made it
        self._drop_sink(0)

    @Slot()
    def begin(self):
        self.core.reset(full=True)
        self.ring.clear()
        self.chunks = 0
        self.stage_ms = self.stage_peak_ms = self.own_ms = 0.0
        self._lvl = self.level
        self._running = True
        self._dead = False
        self._fading = False

    @Slot()
    def restart_clock(self):
        """A pause or resume: the next buffer starts the clock again."""
        self.core.reset()

    @Slot()
    def flush(self):
        """A seek or another audio track: nothing that is queued belongs to the new sound."""
        self.core.reset()
        self.ring.clear()
        if self._sink is not None:
            try:
                self._sink.reset()
                self._io = self._sink.start()
            except Exception:
                self._drop_sink(0)
        if self.stage is not None:
            self.stage.reset_stage()

    @Slot()
    def fade_out(self):
        """The next buffer is the last one and fades to silence; then the sink plays out and `faded` is sent."""
        self._fading = True

    @Slot()
    def keep_going(self):
        """The fade-out was asked for and then called off. If its buffer was already out the sink was let go, and the
        next buffer opens another."""
        self._fading = False
        self._running = True

    @Slot(int)
    def halt(self, drain_ms: int):
        self._running = False
        self._drop_sink(drain_ms)

    @Slot(object)
    def set_device(self, dev):
        self._dev = dev
        if self._sink is not None:
            self._drop_sink(0)           # the next buffer opens one on the new device and starts with silence
            self.core.reset()

    def _drop_sink(self, drain_ms: int):
        sink, self._sink, self._io = self._sink, None, None
        if sink is None:
            return

        def stop():
            try:
                sink.reset()            # stop() alone lets what is queued play out
                sink.stop()
            except Exception:
                pass
        if drain_ms > 0:
            QTimer.singleShot(int(drain_ms), stop)      # let what is queued play out, a hard cut clicks
        else:
            stop()

    def _open(self) -> bool:
        if self._sink is not None:
            return True
        dev = self._dev if self._dev is not None else QMediaDevices.defaultAudioOutput()
        sink = self._make_sink(dev, self._fmt)
        sink.setVolume(1.0)
        sink.setBufferSize(int(self.fs * _FRAME_BYTES * _SINK_MS / 1000))
        io = sink.start()
        if io is None:
            raise RuntimeError("the sound output did not start")
        self._sink, self._io = sink, io
        return True

    def queued(self) -> int:
        s = self._sink
        return 0 if s is None else max(0, (s.bufferSize() - s.bytesFree()) // _FRAME_BYTES)

    def _file(self, z: np.ndarray, t: float, n: int):
        """The processed block into the ring at its media time, without the silence that was put in and at the length
        of the media it covers."""
        c = self.core
        at, pad = c.last_gap
        if pad:
            z = np.concatenate((z[:at], z[at + pad:]))
        if len(z) == 0:
            return
        mono = (z[:, 0] + z[:, 1]) * np.float32(0.5)
        if len(mono) != n:
            mono = np.interp(np.linspace(0.0, len(mono) - 1, n), np.arange(len(mono)), mono).astype(np.float32)
        self.ring.put(int(round(t * self.fs)) - int(round(c.last_lead)), mono)

    @Slot(QAudioBuffer)
    def on_buffer(self, buf):
        if not self._running or self._dead:
            return
        if not buf.isValid() or buf.frameCount() == 0:
            return                          # the tap sends an empty buffer when the media ends
        try:
            t0 = time.perf_counter()
            x, t = block_from_buffer(buf, self.fs)
            if t < 0:
                t = self._pos_t
            self._pos_t = t + len(x) / self.fs
            self._open()
            self.core.set_rate(self.rate)
            self.core.extra = self.extra_s
            y = self.core.process(x, t, self._clock(), self.queued())
            t1 = time.perf_counter()
            if self.stage is not None:
                y = self.stage.process_external(
                    y, Context(self.fs, int(t * self.fs), int(self.dur_s * self.fs), self.stage.volume), False)
            t2 = time.perf_counter()
            self._file(y, t, len(x))
            tgt = self.level
            if abs(tgt - self._lvl) < 1e-4:
                if tgt != 1.0:
                    y = y * np.float32(tgt)
            else:
                y = y * np.linspace(self._lvl, tgt, len(y), dtype=np.float32)[:, None]
            self._lvl = tgt
            if self._fading:
                y = y * np.linspace(1.0, 0.0, len(y), dtype=np.float32)[:, None]
            data = np.ascontiguousarray(y, dtype=np.float32).tobytes()
            free = self._sink.bytesFree()
            if len(data) > free:
                self.dropped += (len(data) - free) // _FRAME_BYTES
                data = data[:free // _FRAME_BYTES * _FRAME_BYTES]
            if data:
                self._io.write(data)
            t3 = time.perf_counter()
            self.chunks += 1
            if self.chunks > 2:             # the first calls pay for imports and the sink opening
                k = 1.0 / min(self.chunks - 2, 200)
                self.stage_ms += ((t2 - t1) * 1000 - self.stage_ms) * k
                self.stage_peak_ms = max(self.stage_peak_ms, (t2 - t1) * 1000)
                self.own_ms += (((t1 - t0) + (t3 - t2)) * 1000 - self.own_ms) * k
            if self._fading:
                self._fading = False
                self._running = False
                self._drop_sink(int(self.core.lag * 1000) + 40)
                self.faded.emit()
        except Exception as e:
            self._dead = True
            log.warning("video sound: %s", e)
            self.failed.emit(str(e) or type(e).__name__)

    @Slot()
    def watchdog(self):
        if not self._running or self._dead or self._sink is None:
            return
        try:
            err = self._sink.error().name
        except Exception:
            err = "NoError"
        if err not in ("NoError", "UnderrunError"):
            self._dead = True
            self.failed.emit(f"the sound output failed ({err})")
        elif self.core.bad_for(self._clock()) >= _SYNC_FAIL_S:
            self._dead = True
            self.failed.emit("the sound could not be kept in step with the picture")


class _PictureDelay(QObject):
    """Holds the frames of the video back so the picture trails the sound: the other half of the video sound delay,
    since the sound cannot come earlier than the clock that releases it. The player draws into `sink`, and each frame
    goes on to the real video sink `delay` seconds after it arrived."""

    def __init__(self, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.sink = QVideoSink(self)
        self.target = None
        self.delay = 0.0
        self._q: deque = deque()
        self._clock = clock
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._fire)
        self.sink.videoFrameChanged.connect(self._on_frame)

    def _on_frame(self, frame):
        if not frame.isValid():
            self.release()                  # the media ended or was cleared: the picture goes at once
            if self.target is not None:
                self.target.setVideoFrame(frame)
            return
        self._q.append((self._clock() + self.delay, frame))
        while len(self._q) > _PIC_FRAMES:
            old = self._q.popleft()[1]
            if self.target is not None:
                self.target.setVideoFrame(old)
        self._arm()

    def _arm(self):
        if self._q and not self._timer.isActive():
            self._timer.start(max(0, int((self._q[0][0] - self._clock()) * 1000.0 + 0.5)))

    def _fire(self):
        now = self._clock()
        while self._q and self._q[0][0] <= now + 0.0005:
            frame = self._q.popleft()[1]
            if self.target is not None:
                self.target.setVideoFrame(frame)
        self._arm()

    def set_delay(self, seconds: float):
        """A shorter delay hands over whatever is now due; a longer one holds the next frames back."""
        self.delay = max(0.0, seconds)
        now = self._clock()
        self._q = deque((min(due, now + self.delay), f) for due, f in self._q)
        self._timer.stop()
        self._fire()

    def release(self):
        """Show the newest held frame now and drop the rest."""
        if self._q and self.target is not None:
            self.target.setVideoFrame(self._q[-1][1])
        self._q.clear()
        self._timer.stop()


def _unlock_disconnects(out):
    """The first disconnect notice of a QAudioOutput made in Python takes the interpreter lock. The audio renderer of a
    player listens to that output and is deleted in a thread of its own, which sends the notice while it holds a Qt
    lock. If the GUI thread holds the interpreter lock inside a call of the player that needs the same Qt lock, as a
    call that makes the player rebuild its renderers does, the two wait for each other. One connection made and
    removed here, on the GUI thread, makes every later notice free of the interpreter lock."""
    def probe(*_):
        pass
    try:
        out.volumeChanged.connect(probe)
        out.volumeChanged.disconnect(probe)
    except Exception:
        pass


class VideoAudio(QObject):
    """Owns the tap, the sink and the fallback for the sound of one video at a time.

    The player calls start() just before it loads a video, and stop(), pause(), resume(), seek(),
    set_level(), set_audio_device() where the matching thing happens. While active, the player's own
    QAudioOutput is detached; fell_back says why when it was put back. The switch (set_enabled), the playback
    speed, the audio track and the video sound delay are followed while the video plays."""
    fell_back = Signal(str)
    active_changed = Signal(bool)       # our sink took the sound over, or the player's own output has it again
    _w_begin = Signal()
    _w_halt = Signal(int)
    _w_flush = Signal()
    _w_clock = Signal()
    _w_dev = Signal(object)
    _w_fade = Signal()
    _w_keep = Signal()

    def __init__(self, media, out, stage, parent=None, sink_factory=None, tap_factory=None,
                 clock=time.monotonic):
        super().__init__(parent)
        self.media = media
        self.out = out
        _unlock_disconnects(out)
        self.stage = stage
        self.enabled = True
        self._sink_factory = sink_factory or _make_sink
        self._tap_factory = tap_factory
        self._clock = clock
        self._active = False
        self._noted = False
        self._video = False         # a video is loaded, whoever plays its sound
        self._resume = False        # we fell back because the output failed, and may take the sound back
        self._tries = 0
        self._releasing = False
        self.reason = ""            # why the last start fell back, "" when it did not
        self._level = 1.0
        self._rate = 1.0
        self._delay_ms = 0
        self._dev = None
        self._feeder = None
        self._thread = None
        self._tap = None
        self._t_play = None
        self._widget = None
        self._vo = None
        self._pic = None
        self._watch = QTimer(self)
        self._watch.setInterval(500)
        self._watch.timeout.connect(self._look)
        for sig, slot in (("durationChanged", self._on_duration), ("playbackRateChanged", self._on_rate),
                          ("activeTracksChanged", self.track_changed)):
            try:
                getattr(media, sig).connect(slot)
            except Exception:
                pass

    @property
    def active(self) -> bool:
        return self._active

    @property
    def fs(self) -> int:
        return int(getattr(self.stage, "fs", 44100))

    @property
    def delay_ms(self) -> int:
        return self._delay_ms

    @property
    def device_failed(self) -> bool:
        """The sound was given back because the output failed and may be taken again (retry(), a device change)."""
        return self._resume

    def _on_duration(self, ms):
        if self._feeder is not None:
            self._feeder.dur_s = max(0, int(ms)) / 1000.0

    def _on_rate(self, rate):
        self._rate = max(PLAY_MIN, min(PLAY_MAX, float(rate)))
        if self._feeder is not None:
            self._feeder.rate = self._rate

    def _ensure_worker(self):
        if self._thread is not None:
            return
        f = _Feeder(self.stage, self.fs, self._sink_factory, self._clock)
        th = QThread()
        th.setObjectName("video-audio")
        f.moveToThread(th)
        th.started.connect(f.setup)
        f.failed.connect(self._on_failed)
        f.faded.connect(self._finish_release)
        self._w_begin.connect(f.begin)
        self._w_halt.connect(f.halt)
        self._w_flush.connect(f.flush)
        self._w_clock.connect(f.restart_clock)
        self._w_dev.connect(f.set_device)
        self._w_fade.connect(f.fade_out)
        self._w_keep.connect(f.keep_going)
        th.start()
        self._feeder, self._thread = f, th
        f.level = self._level
        f.rate = self._rate
        f.extra_s = max(0, self._delay_ms) / 1000.0
        f._dev = self._dev
        try:
            f.dur_s = max(0, self.media.duration()) / 1000.0
        except Exception:
            pass

    def _ensure_tap(self):
        if self._tap is not None:
            return self._tap
        if self._tap_factory is not None:
            tap = self._tap_factory(self.fs)
        else:
            fmt = QAudioFormat()
            fmt.setSampleRate(self.fs)
            fmt.setChannelCount(_CH)
            fmt.setSampleFormat(QAudioFormat.Float)
            tap = QAudioBufferOutput(fmt)
        tap.audioBufferReceived.connect(self._feeder.on_buffer, Qt.QueuedConnection)
        self._tap = tap
        return tap

    # called by the player

    def start(self) -> bool:
        """Take over the sound of the video about to load. False: the player keeps its own audio."""
        self._video = True
        self._tries = 0
        self._resume = False
        return self._take()

    def _take(self) -> bool:
        """Attach the tap and detach the player's own output. Works at any moment of a video."""
        if self._active:
            self._end(restore=False)
        self.reason = ""
        self._releasing = False
        if not self.enabled or self.stage is None or not hasattr(self.stage, "process_external"):
            return False
        if QAudioBufferOutput is None and self._tap_factory is None:
            self.reason = "this Qt is older than 6.8, so the sound of a video does not go through the EQ and DSP chain"
            return False
        try:
            self._ensure_worker()
            tap = self._ensure_tap()
            try:
                self._on_rate(self.media.playbackRate())
            except Exception:
                pass
            middle = getattr(self.stage, "set_external_adaptive", None)
            if middle is not None:
                middle(None)                 # a video has no measured brightness: not the one of the chiptune before it
            self._w_begin.emit()
            self.media.setAudioBufferOutput(tap)
            self.media.setAudioOutput(None)
        except Exception as e:
            self._give_back(f"the sound tap could not be set up ({e})")
            return False
        self._active = True
        self._t_play = None
        self._watch.start()
        self.active_changed.emit(True)
        return True

    def stop(self, drain: bool = True):
        """The video is over or gone. The tail of what is queued plays out unless drain is False."""
        self._video = self._resume = False
        if self._pic is not None:
            self._pic.release()
        self._end(restore=True, drain=drain)

    def silence(self):
        """Drop what the sink holds now, for a change of track. The player takes a moment to stop (it blocks the
        GUI thread for a while), and our sink is not waiting for it."""
        if self._active and self._feeder is not None:
            self._w_halt.emit(0)

    def pause(self):
        if self._active:
            self._w_clock.emit()

    resume = pause

    def seek(self):
        if self._pic is not None:
            self._pic.release()
        if self._active:
            self._w_flush.emit()

    def track_changed(self):
        """Another audio track of the video was chosen: the new sound starts clean, like after a seek."""
        self.seek()

    def set_level(self, v: float):
        self._level = max(0.0, min(1.0, float(v)))
        if self._feeder is not None:
            self._feeder.level = self._level

    def set_audio_device(self, device):
        """The output changed. If we gave the sound back because the old one failed, this is the moment to try again."""
        if device is not self._dev:
            self._tries = 0                     # another output is a new chance, however many times the old one failed
        self._dev = device
        if self._feeder is not None:
            self._w_dev.emit(device)
        if self._resume and not self._active:
            self._retake()

    def set_enabled(self, on: bool):
        """The Settings switch. Applies at once, in the middle of a video."""
        on = bool(on)
        if on == self.enabled:
            return
        self.enabled = on
        if not self._video:
            return
        if on and self._releasing:
            self._releasing = False             # off and on again before our sound handed over: it just goes on
            self._w_keep.emit()
        elif on and not self._active:
            self._tries = 0
            self._take()
        elif not on:
            self._resume = False
            if self._active:
                self._release()

    def set_delay_ms(self, ms: int):
        """Video sound delay: positive plays our sound later, negative holds the picture back by that much instead."""
        ms = max(-_DELAY_MAX, min(_DELAY_MAX, int(ms)))
        self._delay_ms = ms
        if self._feeder is not None:
            self._feeder.extra_s = max(0, ms) / 1000.0
        self._route_video()

    def set_video_output(self, widget):
        """The widget the picture is drawn in. It stays the media player's output unless the picture has to be delayed."""
        self._widget = widget
        self._vo = None
        self._route_video()

    def _route_video(self):
        w = self._widget
        if w is None:
            return
        hold = max(0, -self._delay_ms) / 1000.0
        sink_of = getattr(w, "videoSink", None)
        if hold > 0.0 and callable(sink_of):
            if self._pic is None:
                self._pic = _PictureDelay(self)
            self._pic.target = sink_of()
            self._pic.set_delay(hold)
            want = self._pic.sink
        else:
            want = w.videoSink() if getattr(w, "plain", False) else w
            if self._pic is not None:
                self._pic.set_delay(0.0)
                self._pic.release()
        if want is not self._vo:
            self._vo = want
            try:
                self.media.setVideoOutput(want)
            except Exception:
                log.exception("video sound: the picture output could not be changed")

    def lag_ms(self):
        """How far behind the picture clock our sound is heard, in ms as measured (the sound card's own delay is not
        in it), or None when we are not playing the sound or nothing has come in yet."""
        f = self._feeder
        if not self._active or f is None or f.chunks == 0:
            return None
        return f.core.lag * 1000.0

    def offset_ms(self):
        """lag_ms() less the time the picture is held back: positive is sound behind the picture, negative ahead."""
        lag = self.lag_ms()
        if lag is None:
            return None
        return lag - max(0, -self._delay_ms)

    def window(self, position_ms: float, n: int):
        """n mono frames of the processed sound from media time position_ms on (or the newest, if that is later than
        what has come in), for the scope and the spectrum. None while there is none."""
        f = self._feeder
        if not self._active or f is None:
            return None
        return f.ring.get(int(round(position_ms / 1000.0 * self.fs)), int(n))

    def retry(self):
        """Take the sound back if we gave it up for a failing output and the video is still there."""
        if self._resume and not self._active:
            self._retake()

    def shutdown(self):
        self._end(restore=False, drain=False)
        if self._tap is not None:
            try:
                self._detach_tap()
            except Exception:
                pass
        self._video = self._resume = False
        if self._pic is not None:
            self._pic.release()
        th, self._thread = self._thread, None
        if th is not None:
            if self._feeder is not None:
                QMetaObject.invokeMethod(self._feeder, "teardown", Qt.BlockingQueuedConnection)
            th.quit()
            th.wait(2000)
        self._feeder = None

    def stats(self) -> dict:
        f = self._feeder
        c = f.core if f is not None else None
        return {
            "active": self._active, "fell_back": self.reason,
            "chunks": f.chunks if f else 0,
            "lag_ms": round(c.lag * 1000, 1) if c else 0.0,
            "target_ms": round((c.target + c.extra) * 1000, 1) if c else 0.0,
            "underruns": c.underruns if c else 0, "skips": c.skips if c else 0,
            "corrected_frames": int(round(c.corrected)) if c else 0, "jumps": c.jumps if c else 0,
            "stage_ms": round(f.stage_ms, 3) if f else 0.0, "own_ms": round(f.own_ms, 3) if f else 0.0,
            "rate": c.rate if c else 1.0, "delay_ms": self._delay_ms,
        }

    # internals

    def _end(self, restore: bool, drain: bool = True):
        self._watch.stop()
        self._releasing = False
        was, self._active = self._active, False
        if was:
            try:
                self._detach_tap()
                if restore:
                    self.media.setAudioOutput(self.out)
            except Exception:
                pass
            self._w_halt.emit(int((self._feeder.core.target + self._feeder.core.extra) * 1000) + 20 if drain and self._feeder else 0)
            self.active_changed.emit(False)

    def _detach_tap(self):
        """Take the tap off the player and let it go. A QAudioBufferOutput that was detached keeps a pointer to the player,
        and its destructor crashes if the player was deleted first, so it never outlives its time on the player."""
        self.media.setAudioBufferOutput(None)
        tap, self._tap = self._tap, None
        if tap is not None and self._feeder is not None:
            try:
                tap.audioBufferReceived.disconnect(self._feeder.on_buffer)
            except Exception:
                pass

    def _release(self):
        """Our sound fades out on the next buffer, then the player's own output takes over. A video that is not
        running has no next buffer, so there it happens at once."""
        try:
            playing = self.media.playbackState().name == "PlayingState"
        except Exception:
            playing = False
        if not playing or self._feeder is None:
            self._end(restore=True)
            return
        self._releasing = True
        self._w_fade.emit()
        QTimer.singleShot(150, self._finish_release)        # no buffer came: the video stopped meanwhile

    @Slot()
    def _finish_release(self):
        if self._releasing and self._active:
            self._end(restore=True, drain=True)

    def _retake(self):
        self._tries += 1
        if self._tries > _RETRIES:
            return
        log.info("video sound: taking the sound back (try %d)", self._tries)
        if self._take():
            self._resume = False

    def _give_back(self, why: str, device: bool = False):
        self.reason = why
        self._resume = bool(device and self._video)
        was, self._active = self._active, False
        self._watch.stop()
        self._releasing = False
        try:
            self._detach_tap()
            self.media.setAudioOutput(self.out)
        except Exception:
            pass
        if self._feeder is not None:
            self._w_halt.emit(0)
        if not self._noted:
            self._noted = True
            log.warning("video sound: %s; the player's own sound is used", why)
        else:
            log.info("video sound: %s; the player's own sound is used", why)
        if self._resume:
            QTimer.singleShot(_RETRY_MS, self.retry)       # the output may be the same one, working again
        if was:
            self.active_changed.emit(False)
            self.fell_back.emit(why)

    @Slot(str)
    def _on_failed(self, why: str):
        if self._active:
            self._give_back(why, device=True)

    def _look(self):
        """GUI thread, twice a second while a video is active: a playing video whose sound never arrives."""
        try:
            playing = self.media.playbackState().name == "PlayingState"
            if not playing:
                self._t_play = None
                return
            now = self._clock()
            if self._t_play is None:
                self._t_play = now
            if (self._feeder is not None and self._feeder.chunks == 0 and now - self._t_play > _NO_AUDIO_S
                    and self.media.hasAudio()):
                self._give_back("no sound came from the video")
        except Exception:
            pass
