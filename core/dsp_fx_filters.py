"""Filter modules for the user DSP chain: graphic equalizer, tone control and convolver.

The equalizer and the tone control are cascades of biquad sections run with sosfilt in float64. A section whose gain
is zero is left out, and a settings change fades from the old filters to the new ones over a few milliseconds. The
equalizer solves the section gains with Newton's method so that the response passes through the slider values at the
band centres.

The convolver runs the first 512 taps of the impulse response as a plain FIR on the newest samples, so the output has
no delay. The rest is cut into blocks that double in size with their distance from the start, up to 16384 samples, and
each block is multiplied in the frequency domain against a delay line of input spectra (partitioned overlap-save).
Short blocks early and long blocks late keep both the delay and the cost low for responses of any length. The file is
read and transformed on a helper thread, the audio thread only receives the finished result.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import struct
import tempfile
import threading
import time
import weakref

import numpy as np
from scipy import fft as _fft
from scipy.signal import resample, resample_poly, sosfilt, sosfreqz

import plat_fs
import plat_proc
from dsp_base import Context, DspModule, Param, register

_IDENT = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
_SQ_HALF = 0.7071067811865476


# biquad design (RBJ cookbook)
def _row(b, a):
    return np.array([b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]])


def _peaking(f0: float, gain_db: float, octaves: float, fs: int) -> np.ndarray:
    """Peaking section with a bandwidth in octaves (the warped form, so the width holds near Nyquist)."""
    w0 = 2.0 * np.pi * f0 / fs
    A = 10.0 ** (gain_db / 40.0)
    sw, cw = np.sin(w0), np.cos(w0)
    alpha = sw * np.sinh(np.log(2.0) / 2.0 * octaves * w0 / sw)
    return _row((1 + alpha * A, -2 * cw, 1 - alpha * A), (1 + alpha / A, -2 * cw, 1 - alpha / A))


def _shelf(kind: str, f0: float, gain_db: float, fs: int, q: float = _SQ_HALF) -> np.ndarray:
    w0 = 2.0 * np.pi * f0 / fs
    cw, sw = np.cos(w0), np.sin(w0)
    A = 10.0 ** (gain_db / 40.0)
    sa = 2.0 * np.sqrt(A) * sw / (2.0 * q)
    if kind == "lowshelf":
        b = (A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sa))
        a = ((A + 1) + (A - 1) * cw + sa, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa)
    else:
        b = (A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sa))
        a = ((A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa)
    return _row(b, a)


def _peaking_db(f0, gain_db, octaves: float, fs: int, fe, slope: bool = False):
    """Response in dB of one peaking section per row (arrays f0 and gain_db), at the frequencies fe. With slope=True
    also the derivative of every response with respect to the gain of its own section, for Newton's method."""
    w0 = 2.0 * np.pi * np.asarray(f0, float) / fs
    A = 10.0 ** (np.asarray(gain_db, float) / 40.0)
    sw, cw = np.sin(w0), np.cos(w0)
    alpha = sw * np.sinh(np.log(2.0) / 2.0 * octaves * w0 / sw)
    z = np.exp(-1j * 2.0 * np.pi * np.asarray(fe, float) / fs)[None, :]
    c = lambda v: v[:, None]
    base = 1.0 - c(2 * cw) * z + z * z
    u = 1.0 - z * z
    num = base + c(alpha * A) * u
    den = base + c(alpha / A) * u
    db = 20.0 * np.log10(np.abs(num / den))
    if not slope:
        return db
    return db, 0.5 * c(A * alpha) * np.real(u * (1.0 / num + 1.0 / (c(A * A) * den)))


class _Smooth:
    """A gain that moves to its target over a fixed number of frames along a raised cosine, so it has no corners
    for the ear to hear."""

    def __init__(self, value: float, frames: int):
        self.v = self.t = self.v0 = float(value)
        self.frames = max(1, int(frames))
        self.k = self.frames

    @property
    def rem(self) -> int:
        return self.frames - self.k

    def to(self, target: float) -> None:
        target = float(target)
        if target != self.t:
            self.v0, self.t, self.k = self.v, target, 0

    def snap(self) -> None:
        self.v, self.k = self.t, self.frames

    def ramp(self, n: int):
        """A float when the gain is steady for the whole block, else an (n, 1) float32 array."""
        if self.k >= self.frames:
            self.v = self.t
            return self.v
        m = min(n, self.frames - self.k)
        s = 0.5 - 0.5 * np.cos(np.pi * np.arange(self.k + 1, self.k + m + 1) / self.frames)
        r = np.full((n, 1), self.t, np.float32)
        r[:m, 0] = self.v0 + (self.t - self.v0) * s
        self.k += m
        self.v = self.t if self.k >= self.frames else float(self.v0 + (self.t - self.v0) * s[-1])
        return r


class _SosBank:
    """A cascade of biquads on both channels. A new set of coefficients fades in over `fade` frames while the
    old set fades out, each with its own memory, so moving a slider does not click."""

    def __init__(self, fs: int):
        self.fade = max(32, int(fs * 0.005))
        self.sos = np.zeros((0, 6))
        self.act = np.zeros(0, bool)
        self.zi = np.zeros((0, 2, 2))
        self.prev = None
        self.pos = 0
        self.fresh = True                                 # nothing heard yet, so a first setting needs no fade

    def set(self, sos: np.ndarray, act: np.ndarray) -> None:
        if np.array_equal(sos, self.sos) and np.array_equal(act, self.act):
            return
        if self.fresh:
            self.sos, self.act, self.zi, self.prev = sos, act, np.zeros((len(sos), 2, 2)), None
            return
        if len(sos) == len(self.sos):
            keep = (act & self.act)[:, None, None]       # a section that was a pass-through starts empty
            zi = np.where(keep, self.zi, 0.0)
        else:
            zi = np.zeros((len(sos), 2, 2))
        self.prev = (self.sos, self.act, self.zi)
        self.pos = 0
        self.sos, self.act, self.zi = sos, act, zi

    @staticmethod
    def _run(sos, act, zi, x):
        if not act.any():
            return x
        y, z = sosfilt(sos[act], x, axis=0, zi=zi[act])
        z[np.abs(z) < 1e-20] = 0.0                      # no denormals after silence
        zi[act] = z
        return y

    def process(self, x: np.ndarray) -> np.ndarray:
        self.fresh = False
        if self.prev is None and not self.act.any():
            return x
        if not np.isfinite(x).all():
            x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        n = len(x)
        parts = []
        done = 0
        if self.prev is not None:
            m = min(n, self.fade - self.pos)
            xa = x[:m]
            yo = self._run(*self.prev, xa)
            yn = self._run(self.sos, self.act, self.zi, xa)
            t = (0.5 - 0.5 * np.cos(np.pi * np.arange(self.pos, self.pos + m) / self.fade))[:, None]
            parts.append(yo + (yn - yo) * t)
            self.pos += m
            done = m
            if self.pos >= self.fade:
                self.prev = None
        if done < n:
            parts.append(self._run(self.sos, self.act, self.zi, x[done:]))
        y = np.asarray(parts[0] if len(parts) == 1 else np.concatenate(parts), np.float32)
        if not np.isfinite(y).all():                      # clear the memory and give silence rather than noise
            self.reset()
            return np.zeros_like(x)
        return y

    def reset(self) -> None:
        self.zi[:] = 0.0
        self.prev = None
        self.pos = 0
        self.fresh = True


# graphic equalizer
_ISO10 = (31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
_ISO31 = (20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000,
          2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000)
_LAYOUTS = {"10 band": ("g10", _ISO10, 1.0), "31 band": ("g31", _ISO31, 1.0 / 3.0)}

_MIN_DB = 1e-4            # a section below this does nothing the response could show (the sliders move in half dB steps)
_MAX_SECTION_DB = 40.0    # the worst slider pattern needs about 22 dB in one section, this leaves room

# gain in dB at a frequency, straight lines between the points on a log frequency axis
_PRESET_CURVES = {
    "Flat": ((20, 0.0), (20000, 0.0)),
    "Bass boost": ((20, 5.0), (40, 6.0), (80, 6.0), (160, 4.0), (320, 2.0), (640, 0.5), (1000, 0.0), (20000, 0.0)),
    "Vocal": ((20, -6.0), (80, -4.0), (160, -1.0), (315, 0.0), (630, 0.0), (1250, 1.5), (2500, 3.5), (4000, 3.5),
              (6300, 2.0), (10000, 1.0), (20000, 0.0)),
    "Loudness": ((20, 6.0), (50, 6.0), (100, 4.5), (200, 2.0), (400, 0.0), (2500, 0.0), (4000, 1.0), (6300, 2.5),
                 (10000, 4.0), (16000, 5.0), (20000, 5.0)),
}


def _gkey(prefix: str, f: float) -> str:
    return "%s_%s" % (prefix, ("%g" % f).replace(".", "_"))


def _hz_label(f: float) -> str:
    return ("%g kHz" % (f / 1000.0)) if f >= 1000 else ("%g Hz" % f)


def _preset_gain(name: str, f: float) -> float:
    pts = _PRESET_CURVES[name]
    g = float(np.interp(math.log10(f), [math.log10(p[0]) for p in pts], [p[1] for p in pts]))
    return round(g * 2.0) / 2.0


def _band_params(prefix: str, freqs, layout: str) -> tuple:
    return tuple(Param(_gkey(prefix, f), _hz_label(f), "float", 0.0, -12.0, 12.0, 0.5, unit="dB", scale="db",
                       hint=layout) for f in freqs)


@register
class GraphicEQ(DspModule):
    """Ten octave bands or thirty one third octave bands, each a peaking section, in series. The bands of the layout
    that is not selected keep their values but do nothing; active_keys() lists the settings that matter now."""
    id = "graphic_eq"
    name = "Graphic equalizer"
    group = "Tone"
    summary = "Raises or lowers fixed bands from bass to treble, in 10 or 31 steps."
    params = (
        Param("bands", "Bands", "choice", "10 band", choices=("10 band", "31 band")),
        Param("preset", "Preset", "choice", "Flat", choices=("Custom", "Flat", "Bass boost", "Vocal", "Loudness")),
        Param("preamp", "Preamp", "float", 0.0, -12.0, 12.0, 0.1, unit="dB", scale="db"),
    ) + _band_params("g10", _ISO10, "10 band") + _band_params("g31", _ISO31, "31 band")

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._bank = _SosBank(fs)
        self._pre = _Smooth(1.0, int(fs * 0.005))
        self._geo = {}

    # settings (GUI thread)
    def active_keys(self) -> tuple:
        prefix, freqs, _ = _LAYOUTS[self.get("bands")]
        return ("bands", "preset", "preamp") + tuple(_gkey(prefix, f) for f in freqs)

    def _preset_values(self, name: str) -> dict:
        out = {}
        for prefix, freqs, _ in _LAYOUTS.values():
            for f in freqs:
                out[_gkey(prefix, f)] = _preset_gain(name, f)
        return out

    def set(self, key: str, value) -> None:
        spec = self._by_key.get(key)
        if spec is None:
            return
        if key == "preset" and value not in spec.choices:
            return                                        # a mistyped name must not wipe the bands
        v = spec.clamp(value)
        change = {key: v}
        if key == "preset" and v != "Custom":
            change.update(self._preset_values(v))
        elif key[0] == "g" and self.get("preset") != "Custom":
            if abs(v - self._preset_values(self.get("preset"))[key]) > 1e-9:
                change["preset"] = "Custom"
        self._pending.update(change)                      # one dict update, so the audio thread sees all or none
        self._dirty = True

    # audio thread
    def _geometry(self, bands: str):
        """Band centres, which of them the sample rate allows, and the inverse of the matrix that says how much
        each section (1 dB) moves the response at every band centre."""
        g = self._geo.get(bands)
        if g is None:
            prefix, freqs, octaves = _LAYOUTS[bands]
            f = np.array(freqs, float)
            ok = f < 0.46 * self.fs
            fo = f[ok]
            m = _peaking_db(fo, np.full(len(fo), 6.0), octaves, self.fs, fo).T / 6.0
            g = self._geo[bands] = (prefix, f, ok, octaves, np.linalg.inv(m))
        return g

    def section_gains(self, s: dict | None = None) -> np.ndarray:
        """The gain of every section in dB. It is not the slider value: the neighbours of a raised band add to it,
        so the sections are solved until the response at each band centre equals its slider. Newton's method from the
        linear solution gets there in three or four steps for any pattern of sliders."""
        s = s or self.settings()
        prefix, f, ok, octaves, minv = self._geometry(s["bands"])
        target = np.array([s[_gkey(prefix, fq)] for fq in f[ok]])
        out = np.zeros(len(f))
        if not np.any(np.abs(target) >= _MIN_DB):
            return out
        fo = f[ok]
        g = minv @ target
        best, best_err = g, np.inf
        for _ in range(12):
            db, slope = _peaking_db(fo, g, octaves, self.fs, fo, True)
            err = target - db.sum(axis=0)
            e = float(np.abs(err).max())
            if e < best_err:
                best, best_err = g, e
            else:
                break                                     # no longer improving, keep the best so far
            if e < 1e-6:
                break
            try:
                g = g + np.linalg.solve(slope.T, err)
            except np.linalg.LinAlgError:
                g = g + minv @ err
        out[ok] = np.clip(best, -_MAX_SECTION_DB, _MAX_SECTION_DB)
        return out

    def _design(self, s: dict):
        prefix, freqs, octaves = _LAYOUTS[s["bands"]]
        gains = self.section_gains(s)
        sos = np.tile(_IDENT, (len(freqs), 1))
        act = np.abs(gains) >= _MIN_DB
        for i in np.nonzero(act)[0]:
            sos[i] = _peaking(freqs[i], gains[i], octaves, self.fs)
        return sos, act

    def _apply(self, p: dict) -> None:
        self._bank.set(*self._design(p))
        self._pre.to(10.0 ** (p["preamp"] / 20.0))
        if self._bank.fresh:
            self._pre.snap()

    def process(self, x: np.ndarray, ctx: Context) -> np.ndarray:
        if self._dirty:
            self.prepare()
        y = self._bank.process(x)
        g = self._pre.ramp(len(y))
        if isinstance(g, float):
            return y if g == 1.0 else y * np.float32(g)
        return y * g

    def reset(self) -> None:
        self._bank.reset()
        self._pre.snap()

    def response_db(self, freqs) -> np.ndarray:
        """The magnitude response of the settings as they stand, for the panel to draw."""
        s = self.settings()
        sos, act = self._design(s)
        f = np.asarray(freqs, float)
        h = np.ones(len(f), complex)
        if act.any():
            h = sosfreqz(sos[act], worN=f, fs=self.fs)[1]
        return 20.0 * np.log10(np.maximum(np.abs(h), 1e-9)) + s["preamp"]


# tone control
@register
class ToneControl(DspModule):
    id = "tone"
    name = "Tone control"
    group = "Tone"
    summary = "Bass and treble controls, each a shelf with its own corner frequency."
    params = (
        Param("bass_freq", "Bass frequency", "float", 120.0, 40.0, 400.0, 1.0, unit="Hz", scale="log"),
        Param("bass_gain", "Bass", "float", 2.0, -12.0, 12.0, 0.1, unit="dB", scale="db"),
        Param("treble_freq", "Treble frequency", "float", 6000.0, 2000.0, 16000.0, 10.0, unit="Hz", scale="log"),
        Param("treble_gain", "Treble", "float", 2.0, -12.0, 12.0, 0.1, unit="dB", scale="db"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self._bank = _SosBank(fs)

    def _design(self, s: dict):
        sos = np.tile(_IDENT, (2, 1))
        act = np.zeros(2, bool)
        if abs(s["bass_gain"]) >= _MIN_DB:
            sos[0], act[0] = _shelf("lowshelf", s["bass_freq"], s["bass_gain"], self.fs), True
        if abs(s["treble_gain"]) >= _MIN_DB and s["treble_freq"] < 0.46 * self.fs:
            sos[1], act[1] = _shelf("highshelf", s["treble_freq"], s["treble_gain"], self.fs), True
        return sos, act

    def _apply(self, p: dict) -> None:
        self._bank.set(*self._design(p))

    def process(self, x: np.ndarray, ctx: Context) -> np.ndarray:
        if self._dirty:
            self.prepare()
        return self._bank.process(x)

    def reset(self) -> None:
        self._bank.reset()

    def response_db(self, freqs) -> np.ndarray:
        sos, act = self._design(self.settings())
        f = np.asarray(freqs, float)
        if not act.any():
            return np.zeros(len(f))
        h = sosfreqz(sos[act], worN=f, fs=self.fs)[1]
        return 20.0 * np.log10(np.maximum(np.abs(h), 1e-9))


# convolver
IR_MAX_SECONDS = 120.0
_NO_FILE = "No impulse response chosen. The sound passes through unchanged."
_HEAD = 512                                               # taps run as a plain FIR, so the output has no delay
_BLOCK_MAX = 16384                                        # the longest block, its FFT is twice as long
_CHUNK = 65536                                            # most frames one engine pass takes


class IrError(Exception):
    """A problem with the impulse response file, worded for the person who chose it."""


_free_ram = plat_fs.free_ram


def _cap_seconds(rate: int, ch: int, fs: int, ceiling: float) -> float:
    """Seconds of an impulse response to take: the ceiling, or less when 40 percent of the free memory would not hold
    the file as it is read, resampled and turned into spectra (a rough count of bytes per second of response)."""
    per_second = ch * rate * 24.0 + fs * (min(ch, 4) * 12.0 + 16.0)
    return max(1.0, min(ceiling, 0.4 * _free_ram() / per_second))


def _read_wav(path: str, limit):
    """(float32 frames x channels, rate, frames in file, seconds taken) for PCM and float WAV files, plain or
    extensible. limit(rate, channels) says how many seconds to take; only that much plus one frame is read. Raises
    ValueError for anything it does not know, so that the caller can hand the file to ffmpeg, and IrError for a file
    that is cut short."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(12)
        if head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise ValueError("not a wav file")
        fmt = None
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                raise ValueError("no data chunk")
            cid, n = hdr[:4], int.from_bytes(hdr[4:], "little")
            pos = fh.tell()
            if cid == b"fmt " and n >= 16:
                f = fh.read(min(n, 40))
                tag, ch, rate, _, align, bits = struct.unpack("<HHIIHH", f[:16])
                if tag == 0xFFFE and len(f) >= 26:
                    tag = struct.unpack("<H", f[24:26])[0]
                fmt = (tag, ch, rate, align, bits)
            elif cid == b"data":
                break
            fh.seek(pos + n + (n & 1))
        if fmt is None:
            raise ValueError("no format chunk")
        tag, ch, rate, align, bits = fmt
        if ch < 1 or rate < 1 or align != ch * bits // 8 or (tag, bits) not in (
                (1, 8), (1, 16), (1, 24), (1, 32), (3, 32), (3, 64)):
            raise ValueError("format")
        if n in (0, 0xFFFFFFFF):
            n = size - pos                                # written by something that streams
        elif pos + n > size:
            raise IrError("The file is damaged or cut short.")
        total = n // align
        seconds = limit(rate, ch)
        keep = min(total, int(seconds * rate) + 1)
        if bits == 24:
            raw = np.frombuffer(fh.read(keep * align), np.uint8).reshape(-1, 3).astype(np.int32)
            v = (raw[:, 0] << 8) | (raw[:, 1] << 16) | (raw[:, 2] << 24)
            d = v.astype(np.float32) / 2147483648.0
        else:
            dt = {(1, 8): "u1", (1, 16): "<i2", (1, 32): "<i4", (3, 32): "<f4", (3, 64): "<f8"}[(tag, bits)]
            d = np.fromfile(fh, dt, keep * ch)
            if bits == 8:
                d = (d.astype(np.float32) - 128.0) / 128.0
            elif tag == 1:
                d = d.astype(np.float32) / float(2 ** (bits - 1))
            else:
                d = d.astype(np.float32)
    if d.size != keep * ch:
        raise IrError("The file is damaged or cut short.")
    return d.reshape(-1, ch), int(rate), total, seconds


def _read_soundfile(path: str, limit):
    import soundfile                                      # only when it is installed
    with soundfile.SoundFile(path) as f:
        rate = f.samplerate
        seconds = limit(rate, f.channels)
        d = f.read(frames=int(seconds * rate) + 1, dtype="float32", always_2d=True)
        return d, int(rate), max(int(f.frames), len(d)), seconds


def _ffmpeg() -> str | None:
    try:
        from ffmpeg_util import ffmpeg_path
        return ffmpeg_path() or None
    except Exception:
        return shutil.which("ffmpeg")


def _probe_ffmpeg(exe: str, path: str):
    """(rate, channels) from the stream line ffmpeg prints, or the largest case when it cannot be read."""
    try:
        r = subprocess.run([exe, "-hide_banner", "-nostdin", "-i", path], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15,
                           **plat_proc.popen_kwargs(group=False))
        m = re.search(r"Audio: [^,\n]*, (\d+) Hz, ([^,\n(]*)", r.stderr.decode("utf-8", "replace"))
        if m:
            lay = m.group(2).strip()
            ch = {"mono": 1, "stereo": 2, "quad": 4}.get(lay)
            if ch is None:
                k = re.match(r"(\d+) channels", lay) or re.match(r"(\d+)\.(\d+)", lay)
                ch = sum(int(g) for g in k.groups()) if k else 6
            return int(m.group(1)), ch
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 192000, 6


def _read_ffmpeg(path: str, limit):
    exe = _ffmpeg()
    if not exe:
        raise IrError("This file needs ffmpeg to be read and ffmpeg was not found.")
    seconds = limit(*_probe_ffmpeg(exe, path))
    tmp = tempfile.mkdtemp(prefix="acdsp_")
    try:
        out = os.path.join(tmp, "ir.wav")
        cmd = [exe, "-v", "error", "-nostdin", "-i", path, "-vn", "-t", "%.3f" % (seconds + 1.0),
               "-c:a", "pcm_f32le", "-f", "wav", out]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120,
                           **plat_proc.popen_kwargs(group=False))
        if r.returncode != 0 or not os.path.isfile(out):
            raise IrError("That file could not be read as audio.")
        return _read_wav(out, limit)
    except subprocess.TimeoutExpired:
        raise IrError("Reading that file took too long.") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _resample(d: np.ndarray, rate: int, fs: int) -> np.ndarray:
    """Convert to the engine rate. The values are scaled by rate/fs so the filter keeps its frequency response."""
    g = math.gcd(rate, fs)
    up, down = fs // g, rate // g
    if max(up, down) <= 640:
        y = resample_poly(d, up, down, axis=0)
    else:
        y = resample(d, max(1, int(round(d.shape[0] * fs / rate))), axis=0)
    return y * (rate / fs)


def load_impulse_response(path: str, fs: int, seconds: float | None = None):
    """Read an impulse response and bring it to the engine rate. Slow, so never on the audio thread.

    Takes up to `seconds` of the file (IR_MAX_SECONDS when None), fewer when the free memory would not hold more.
    Returns (ir, info): ir is float32 with 1 (mono), 2 (left and right) or 4 columns (true stereo, in the order
    left to left, left to right, right to left, right to right); info has seconds, layout, rate, resampled, cut (the
    file was longer than what was taken), limit (the seconds that could be taken), reader (wav, soundfile or
    ffmpeg) and channels (in the file; only the first 2 are used unless there are exactly 4). Raises IrError with a plain message for a missing, broken, empty or silent file."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise IrError("The file was not found.")
    if os.path.getsize(path) == 0:
        raise IrError("The file is empty.")
    ceiling = IR_MAX_SECONDS if seconds is None else float(seconds)
    limit = lambda rate, ch: _cap_seconds(rate, ch, fs, ceiling)
    d = None
    for name, reader in (("wav", _read_wav), ("soundfile", _read_soundfile), ("ffmpeg", _read_ffmpeg)):
        try:
            d, rate, total, cap = reader(path, limit)
            break
        except (IrError, MemoryError):
            raise
        except Exception:
            continue
    if d is None:
        raise IrError("That file could not be read as audio.")
    if d.shape[0] < 1 or rate < 1000:
        raise IrError("That file has no audio in it.")
    keep = int(cap * rate)
    cut = total > keep
    d = d[:keep].astype(np.float64)
    np.nan_to_num(d, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    if np.abs(d).max() < 1e-9:
        raise IrError("The impulse response is silent, so the sound passes through unchanged.")
    nch = d.shape[1]
    d = d[:, :4] if nch == 4 else d[:, :2]                # four columns are true stereo, other layouts give left and right
    resampled = rate != fs
    if resampled:
        d = _resample(d, rate, fs)
    if cut:                                               # no click where the cut file stops
        m = min(d.shape[0] // 2, int(0.02 * fs))
        if m > 1:
            d[-m:] *= (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, m)))[:, None]
    peak = float(np.abs(d).max())
    if peak < 1e-9:
        raise IrError("The impulse response is silent, so the sound passes through unchanged.")
    idx = np.nonzero(np.abs(d).max(axis=1) > peak * 1e-7)[0]   # drop a silent tail, it costs time and adds nothing
    d = d[:int(idx[-1]) + 1]
    layout = {1: "mono", 2: "stereo", 4: "true stereo"}[d.shape[1]]
    return d.astype(np.float32), {"seconds": d.shape[0] / fs, "layout": layout, "rate": rate, "resampled": resampled,
                                  "cut": cut, "limit": cap, "reader": name, "channels": nch}


def _norm_gain(ir: np.ndarray, fs: int) -> float:
    """The gain that keeps the level of music about the same: the response is averaged with weight 1/f (music has
    about equal energy per octave), then limited so no frequency is boosted by more than 12 dB. A response longer than
    the FFT is folded back first, which keeps the spectrum samples exact."""
    n, ch = ir.shape
    size = min(1 << 23, 1 << max(12, int(math.ceil(math.log2(max(2, 4 * n))))))
    f = np.arange(size // 2 + 1) * fs / size
    w = np.where((f >= 20.0) & (f <= min(20000.0, fs / 2.0)), 1.0 / np.maximum(f, 1.0), 0.0)
    if w.sum() <= 0:
        return 1.0

    def power(c):
        col = ir[:, c].astype(np.float64)
        if n > size:
            col = np.pad(col, (0, -n % size)).reshape(-1, size).sum(axis=0)
        return np.abs(_fft.rfft(col, size)) ** 2

    mean_sq = peak_sq = 0.0
    for group in {1: ((0,),), 2: ((0,), (1,)), 4: ((0, 2), (1, 3))}[ch]:
        p2 = sum(power(c) for c in group)
        mean_sq = max(mean_sq, float(p2 @ w) / float(w.sum()))
        peak_sq = max(peak_sq, float(p2.max()))
    if mean_sq <= 1e-18 or peak_sq <= 0:
        return 1.0
    return min(1.0 / math.sqrt(mean_sq), 4.0 / math.sqrt(peak_sq))


def _plan(n: int) -> list:
    """The blocks that come after the head, as [offset, block size, count]. A block of size B at offset o only
    needs input that has already arrived when o >= B, so the sizes grow as fast as the offset allows, up to
    _BLOCK_MAX, and equal sizes in a row form one run."""
    runs, o = [], _HEAD
    while o < n:
        b = min(_BLOCK_MAX, 1 << (o.bit_length() - 1))
        if b > n - o and o < 2 * _BLOCK_MAX:
            b = max(_HEAD, 1 << (n - o - 1).bit_length())     # the last block need not be larger than what is left
        if runs and runs[-1][1] == b:
            runs[-1][2] += 1
        else:
            runs.append([o, b, 1])
        o += b
    return runs


class _Run:
    """`count` blocks of `size` taps from offset `start`, as spectra. h[count - 1 - k] holds block k, so that the
    delay line of input spectra pairs with it in slices that are contiguous."""
    __slots__ = ("start", "size", "count", "delta", "bins", "h", "per_slice", "slices", "chunk", "phase")

    def __init__(self, ir: np.ndarray, start: int, size: int, count: int, phase: int):
        n, ch = ir.shape
        self.start, self.size, self.count, self.bins = start, size, count, size + 1
        self.phase = phase                                # the first block starts this far into its length
        self.delta = start - size                         # input that the first block sees this much older
        h = np.empty((count, ch, self.bins), np.complex64)
        step = max(1, 65536 // size)
        for a in range(0, count, step):                   # in slices, so the audio thread gets the interpreter now and then
            e = min(count, a + step)
            blk = np.zeros(((e - a) * size, ch), np.float32)
            src = ir[start + a * size:start + e * size]
            blk[:len(src)] = src
            h[count - e:count - a] = _fft.rfft(blk.reshape(e - a, size, ch).transpose(0, 2, 1), 2 * size, axis=2)[::-1]
            time.sleep(0.0005)
        self.h = h.reshape(count, 2, 2, self.bins) if ch == 4 else h
        self.per_slice = max(1, 65536 // size)            # older blocks summed per step while the next block is awaited
        self.slices = -(-(count - 1) // self.per_slice)
        self.chunk = max(1, min(count, 131072 // self.bins))


class _ConvShared:
    """What every engine of one impulse response has in common and never changes: the head taps, the spectra of the
    later blocks and the loudness gain. Built on the loader thread."""

    def __init__(self, ir: np.ndarray, fs: int):
        n, self.ch = ir.shape
        self.length = n                                   # frames of the response
        self.norm = _norm_gain(ir, fs)
        taps = []
        for c in range(self.ch):
            h = np.ascontiguousarray(ir[:_HEAD, c], np.float32)
            nz = np.nonzero(h)[0]
            taps.append(h[:int(nz[-1]) + 1] if len(nz) else h[:1])
        self.taps = taps
        # (input channel, output channel, impulse response column)
        self.paths = {1: ((0, 0, 0), (1, 1, 0)), 2: ((0, 0, 0), (1, 1, 1)),
                      4: ((0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3))}[self.ch]
        # runs start at different points of their block, so their boundaries do not all fall in the same call
        self.runs = [_Run(ir, o, b, k, int(b * ((i * 0.6180339887) % 1.0))) for i, (o, b, k) in enumerate(_plan(n))]
        self.history = max([_HEAD] + [2 * r.size + r.delta for r in self.runs])   # input frames the engine must keep


class _ConvEngine:
    """Zero delay partitioned convolution of a stereo stream with one impulse response. Process() belongs to the
    audio thread. Exact for any block size: the block grid follows the stream, not the block boundaries.

    Every run is a delay line `ring` of input spectra, one per block of the run. The sum over the older blocks for
    the next output block is accumulated in small steps while the stream advances (`pre`), so that a boundary only
    adds the newest block, and no single call to process() does the whole sum of a long response."""

    def __init__(self, sh: _ConvShared):
        self.sh = sh
        self.norm = sh.norm
        runs = sh.runs
        self.buf = np.zeros((2, sh.history + _CHUNK), np.float32)    # input, newest at wp
        self.wp = sh.history
        self.ring = [np.zeros((r.count, 2, r.bins), np.complex64) for r in runs]
        self.pre = [np.zeros((2, r.bins), np.complex64) for r in runs]
        self.out = [np.zeros((2, 2 * r.size), np.float32) for r in runs]    # the valid half is the next `size` output frames
        shape = (lambda r: (r.chunk, 2, 2, r.bins)) if sh.ch == 4 else (lambda r: (r.chunk, 2, r.bins))
        self.tmp = [np.empty(shape(r), np.complex64) for r in runs]
        self.c = [r.phase for r in runs]                  # frames since the last boundary of each run
        self.w = [r.count - 1 for r in runs]              # slot of the newest spectrum
        self.done = [r.phase * r.slices // r.size for r in runs]   # steps of the older-block sum done for the coming boundary
        self.used = False
        self.frames = 0                                   # frames taken since the start or the last reset
        self.head_fft = {}

    def clone(self) -> "_ConvEngine":
        """Another engine on the same response that continues from this one's input history and delay lines."""
        e = _ConvEngine(self.sh)
        if self.used:
            np.copyto(e.buf, self.buf)
            for a, b in ((self.ring, e.ring), (self.pre, e.pre), (self.out, e.out)):
                for x, y in zip(a, b):
                    np.copyto(y, x)
            e.wp, e.c, e.w, e.done, e.used, e.frames = self.wp, list(self.c), list(self.w), list(self.done), True, self.frames
        return e

    def reset(self) -> None:
        if not self.used:
            return
        self.buf[:] = 0
        self.wp = self.sh.history
        for a in (self.ring, self.pre, self.out):
            for x in a:
                x[:] = 0
        for i, r in enumerate(self.sh.runs):
            self.c[i], self.w[i], self.done[i] = r.phase, r.count - 1, r.phase * r.slices // r.size
        self.used = False
        self.frames = 0

    # the head
    def _head(self, wp: int, m: int) -> np.ndarray:
        """The first taps on the newest m frames: (2, m) float32."""
        sh, taps, ch = self.sh, self.sh.taps, self.sh.ch
        n = max(len(h) for h in taps)
        if m * n <= 40000:
            y = np.zeros((2, m), np.float32)
            for i, o, c in sh.paths:
                h = taps[c]
                y[o] += np.convolve(self.buf[i, wp - len(h) + 1:wp + m], h, "valid")
            return y
        size = _fft.next_fast_len(m + n - 1)
        hs = self.head_fft.get(size)
        if hs is None:
            if len(self.head_fft) > 12:
                self.head_fft.clear()
            hs = self.head_fft[size] = np.stack([_fft.rfft(h, size) for h in taps])
        spec = _fft.rfft(self.buf[:, wp - n + 1:wp + m], size, axis=1)
        if ch == 4:
            spec = spec[0] * hs[0:2] + spec[1] * hs[2:4]
        else:
            spec = spec * hs
        return _fft.irfft(spec, size, axis=1)[:, n - 1:n - 1 + m]

    # the blocks
    def _mac(self, i: int, acc: np.ndarray, p0: int, p1: int) -> None:
        """acc += the sum over blocks p0 to p1-1 of (input spectrum that meets the block) * (block spectrum), where
        block 0 meets the spectrum that goes into the next slot of the delay line."""
        r = self.sh.runs[i]
        n, nxt = r.count, (self.w[i] + 1) % r.count
        ring, tmp, four = self.ring[i], self.tmp[i], self.sh.ch == 4
        for lo, hi, wrap in ((p0, min(p1, nxt + 1), 0), (max(p0, nxt + 1), p1, n)):
            if hi <= lo:
                continue
            s0 = nxt - hi + 1 + wrap
            rv, hv = ring[s0:s0 + hi - lo], r.h[n - hi:n - lo]
            for k in range(0, hi - lo, r.chunk):
                e = min(hi - lo, k + r.chunk)
                t = tmp[:e - k]
                if four:
                    np.multiply(rv[k:e, :, None, :], hv[k:e], out=t)
                    acc += t.sum(axis=(0, 1))
                else:
                    np.multiply(rv[k:e], hv[k:e], out=t)
                    acc += t.sum(axis=0)

    def _steps(self, i: int, c: int) -> None:
        """Do the steps of the older-block sum that are due after c frames of the current output block."""
        r = self.sh.runs[i]
        want = c * r.slices // r.size
        d = self.done[i]
        while d < want:
            p0 = 1 + d * r.per_slice
            self._mac(i, self.pre[i], p0, min(r.count, p0 + r.per_slice))
            d += 1
        self.done[i] = d

    def _boundary(self, i: int, end: int) -> None:
        """The newest `size` frames are in: file the spectrum of the window that ends `delta` frames back, add block 0
        to the older blocks and make the next `size` output frames."""
        r = self.sh.runs[i]
        a = end - r.delta - 2 * r.size
        spec = _fft.rfft(self.buf[:, a:a + 2 * r.size], axis=1)
        w = self.w[i] = (self.w[i] + 1) % r.count
        self.ring[i][w] = spec
        acc, h0 = self.pre[i], r.h[r.count - 1]
        if self.sh.ch == 4:
            acc += (spec[:, None, :] * h0).sum(axis=0)
        else:
            acc += spec * h0
        self.out[i] = _fft.irfft(acc, 2 * r.size, axis=1)
        acc[:] = 0
        self.done[i] = 0

    def _run(self, i: int, y: np.ndarray, wp: int, m: int) -> None:
        r = self.sh.runs[i]
        c, pos, out = self.c[i], 0, self.out[i]
        while pos < m:
            k = min(r.size - c, m - pos)
            y[:, pos:pos + k] += out[:, r.size + c:r.size + c + k]
            c += k
            pos += k
            if r.slices:
                self._steps(i, c)
            if c == r.size:
                self._boundary(i, wp + pos)
                out = self.out[i]
                c = 0
        self.c[i] = c

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros((0, 2), np.float32)
        if len(x) > _CHUNK:
            return np.concatenate([self.process(x[a:a + _CHUNK]) for a in range(0, len(x), _CHUNK)])
        m, keep = len(x), self.sh.history
        if self.wp + m > self.buf.shape[1]:
            self.buf[:, :keep] = self.buf[:, self.wp - keep:self.wp]
            self.wp = keep
        wp = self.wp
        self.buf[:, wp:wp + m] = x.T
        self.used = True
        self.frames += m
        y = self._head(wp, m)
        for i in range(len(self.sh.runs)):
            self._run(i, y, wp, m)
        self.wp = wp + m
        return np.ascontiguousarray(y.T, np.float32)


class _Job:
    """One impulse response being read and transformed. A convolver and its clones share the job, so the file is
    read once however many copies wait for it."""

    def __init__(self, path: str, fs: int):
        self.path, self.fs = path, fs
        self.shared = None
        self.msg = ""
        self.done = False
        self._subs = []
        self._lk = threading.Lock()

    def attach(self, conv) -> None:
        with self._lk:
            if not self.done:
                self._subs.append(weakref.ref(conv))
                return
        conv._arrived(self)

    def run(self) -> None:
        shared = None
        try:
            ir, info = load_impulse_response(self.path, self.fs)
            shared = _ConvShared(ir, self.fs)
            msg = "Loaded %s: %.1f seconds, %s" % (os.path.basename(self.path), info["seconds"], info["layout"])
            if info["resampled"]:
                msg += ", converted from %d Hz" % info["rate"]
            msg += "."
            if info["channels"] not in (1, 2, 4):
                msg += " Only the first 2 of %d channels are used." % info["channels"]
            if info["cut"]:
                msg += (" Only the first %d seconds are used, the file is longer." % info["limit"]
                        if info["limit"] >= IR_MAX_SECONDS else
                        " Only the first %d seconds are used, there is not enough free memory for more." % info["limit"])
        except IrError as e:
            msg = str(e)
        except MemoryError:
            msg = "There is not enough free memory for that file."
        except Exception:
            msg = "That file could not be used as an impulse response."
        with self._lk:
            self.shared, self.msg, self.done = shared, msg, True
            subs, self._subs = self._subs, []
        for ref in subs:
            conv = ref()
            if conv is not None:
                conv._arrived(self)


@register
class Convolver(DspModule):
    """Convolution with an impulse response file: room and hall reverbs, speaker and headphone corrections.

    The panel can read `status` at any time for a plain sentence about the file. Until a file has loaded, and
    whenever it cannot be used, the sound passes through unchanged."""
    id = "convolver"
    name = "Convolver"
    group = "Space"
    summary = "Runs the sound through an impulse response file, for room, reverb and speaker effects."
    params = (
        Param("file", "Impulse response", "file", "", hint="WAV or FLAC"),
        Param("normalize", "Match loudness", "bool", True),
        Param("gain", "Output gain", "float", 0.0, -24.0, 12.0, 0.1, unit="dB", scale="db"),
        Param("mix", "Mix", "float", 100.0, 0.0, 100.0, 1.0, unit="%"),
    )

    def __init__(self, fs: int = 44100):
        super().__init__(fs)
        self.status = _NO_FILE
        self.loading = False
        self._lk = threading.RLock()                      # taken by the audio thread per block and by clone()
        self._job = None
        self._path = ""
        self._ok = False
        self._ready = None
        self._eng = None
        self._old = None
        self._xf = 0
        self._fade = max(64, int(fs * 0.02))
        self._dry = _Smooth(1.0, self._fade)
        self._wet = _Smooth(0.0, self._fade)

    # GUI thread
    def set(self, key: str, value) -> None:
        super().set(key, value)
        if key == "file":
            self._request(self._by_key["file"].clamp(value))

    def _request(self, path: str) -> None:
        if path == self._path and (self.loading or self._ok):
            return
        self._path = path
        self._ok = False
        if not path:
            self._job = None
            self.loading = False
            self.status = _NO_FILE
            self._ready = (None, None)
            self._dirty = True
            return
        job = self._job = _Job(path, self.fs)
        self.loading = True
        self.status = "Loading %s." % os.path.basename(path)
        job.attach(self)
        threading.Thread(target=job.run, daemon=True, name="dsp-convolver-load").start()

    def _arrived(self, job: _Job) -> None:
        """The job has finished (loader thread, or the caller's thread when it was finished already)."""
        if job is not self._job:
            return                                        # another file was asked for meanwhile
        eng, msg = None, job.msg
        if job.shared is not None:
            try:
                eng = _ConvEngine(job.shared)
            except MemoryError:
                msg = "There is not enough free memory for that file."
        self.status = msg
        self._ok = eng is not None
        self._ready = (job, eng)
        self._dirty = True
        self.loading = False                              # last: whoever sees it clear finds the result waiting

    def tail(self) -> int:
        eng = self._eng
        return eng.sh.length if eng is not None and self.p["mix"] > 0.0 else 0

    def clone(self) -> "Convolver":
        """A convolver with the same settings and the same memory of the sound, so that it carries on where this one is.
        The loaded response is shared, not read again; if the file is still loading, the copy gets the result when
        the one read finishes. Safe to call while the audio thread runs: it waits for the block in hand."""
        c = type(self)(self.fs)
        with self._lk:
            c.enabled = self.enabled
            c.p, c._pending, c._dirty = dict(self.p), dict(self._pending), self._dirty
            c.status, c._path, c._ok, c._job, c._xf = self.status, self._path, self._ok, self._job, self._xf
            c._dry.__dict__.update(self._dry.__dict__)
            c._wet.__dict__.update(self._wet.__dict__)
            loading = self.loading
            c._eng = self._eng.clone() if self._eng is not None else None
            c._old = self._old.clone() if self._old is not None else None
            if self._ready is not None:
                job, eng = self._ready
                c._ready = (job, eng.clone() if eng is not None else None)
            c.loading = loading
        if loading and c._job is not None:
            c._job.attach(c)
        return c

    # audio thread
    def _apply(self, p: dict) -> None:
        with self._lk:
            r, self._ready = self._ready, None
            if r is not None and r[0] is self._job and (r[1] is not self._eng):
                self._old, self._eng, self._xf = self._eng, r[1], 0
            wet = (p["mix"] / 100.0) * 10.0 ** (p["gain"] / 20.0)
            if self._eng is not None and p["normalize"]:
                wet *= self._eng.norm
            have = self._eng is not None or self._old is not None
            self._wet.to(wet if have else 0.0)
            self._dry.to(1.0 - p["mix"] / 100.0 if self._eng is not None else 1.0)

    def process(self, x: np.ndarray, ctx: Context) -> np.ndarray:
        with self._lk:
            if self._dirty:
                self.prepare()
            if not np.isfinite(x).all():
                x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
            eng, old, n = self._eng, self._old, len(x)
            if eng is None and old is None and self._dry.rem == 0 and self._dry.v == 1.0:
                return x
            wet = eng.process(x) if eng is not None else np.zeros((n, 2), np.float32)
            if old is not None:
                m = min(n, self._fade - self._xf)
                t = (0.5 - 0.5 * np.cos(np.pi * np.arange(self._xf, self._xf + m) / self._fade)).astype(np.float32)[:, None]
                wo = old.process(x[:m])
                wet[:m] = wet[:m] * t + wo * (1.0 - t)
                self._xf += m
                if self._xf >= self._fade:
                    self._old = None
            dg, wg = self._dry.ramp(n), self._wet.ramp(n)
            y = x * (np.float32(dg) if isinstance(dg, float) else dg)
            y += wet * (np.float32(wg) if isinstance(wg, float) else wg)
            if not np.isfinite(y).all():                  # clear the memory and give silence rather than noise
                self.reset()
                return np.zeros_like(x)
            return y

    def reset(self) -> None:
        with self._lk:
            if self._eng is not None:
                self._eng.reset()
            self._old = None
            self._dry.snap()
            self._wet.snap()
