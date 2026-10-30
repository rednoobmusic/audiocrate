"""Drive the machine's real RGB lighting from the music.

Two outputs, each optional:
 - the Acer Predator PHN16S-71's keyboard (four zones), lid emblem and mode LED,
   through the ENE 0CF2:5130 controller over hidraw. The protocol is the one in
   predator-sense/ene_rgb.py (report 0xA2 selects a target, 0xA4 sets it).
 - every device OpenRGB knows about, set to one colour. OpenRGB has no library to
   link against, and its command line is only another client of the same code
   that rescans the hardware on every call, so the only way to drive it at music
   speed is its SDK server. If nothing is listening and the address is this
   machine, the server is started here (`openrgb --server`) and stopped again.

Colour comes from the album-art accent, brightness from the spectrum. Device I/O
runs on worker threads so a slow write never stalls the window. While lighting is
on the lights belong to us (a dim steady glow when nothing plays); switching it
off, or quitting, puts them back as /etc/ene-rgb.conf and OpenRGB had them.
"""
from __future__ import annotations

import colorsys
import glob
import math
import os
import sys
import select
import shutil
import socket
import struct
import subprocess
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QColor

import plat_proc

_ENE_IDS = ("0018:00000CF2:00005130", "0003:00000CF2:00005130")
_ENE_STATE = "/etc/ene-rgb.conf"
_ENE_TARGETS = {"keyboard": 0x21, "emblem": 0x83, "mode": 0x65}
_ENE_MODES = {"off": 0x00, "static": 0x02, "breathing": 0x04, "neon": 0x05}

_BANDS_HZ = ((30, 150), (150, 600), (600, 2500), (2500, 12000))
_DEVICE_KEYS = ("keyboard", "emblem", "mode_led", "openrgb", "openrgb_host", "openrgb_exclude")
_FFT_N = 2048
_IDLE_LEVEL = 0.35      # brightness of the steady glow while nothing plays
_FADE_TAU = 0.3         # seconds; the colour is about settled after a second, as in the UI
_REFRESH_S = 1.0        # resend each light this often even if unchanged
_SETTLE_S = 0.008       # pause after switching the controller to another target


# Predator (ENE controller)

class _Predator:
    def __init__(self):
        self._fd = -1
        self._target = None

    @property
    def is_open(self) -> bool:
        return self._fd >= 0

    @staticmethod
    def _find() -> str:
        for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            try:
                with open(os.path.join(path, "device/uevent"), encoding="utf-8") as fh:
                    uevent = fh.read()
            except OSError:
                continue
            if any(f"HID_ID={i}" in uevent for i in _ENE_IDS):
                return "/dev/" + os.path.basename(path)
        raise OSError("ENE 0CF2:5130 controller not found")

    def open(self):
        self._fd = os.open(self._find(), os.O_RDWR)

    def close(self):
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def _send(self, buf):
        import fcntl            # Linux only: the controller is found through /sys, so no other platform gets here
        # HIDIOCSFEATURE(len)
        fcntl.ioctl(self._fd, (3 << 30) | (len(buf) << 16) | (ord("H") << 8) | 0x06,
                    bytearray(buf))

    def set(self, target: str, rgb, mode=0x02, brightness=100, zones=0x0F):
        t = _ENE_TARGETS[target]
        r, g, b = rgb
        if t != self._target:
            if self._target is not None:
                time.sleep(_SETTLE_S)      # the controller needs a moment to change target
            self._target = t
        # the target must be selected on the same open fd as the write
        self._send([0xA2, t])
        self._send([0xA4, t, mode, brightness, 0, 0x01, r, g, b, zones & 0xFF, zones >> 8])

    def restore(self):
        """Put every light back the way /etc/ene-rgb.conf has it."""
        try:
            with open(_ENE_STATE, encoding="utf-8") as fh:
                lines = [ln.split() for ln in fh if ln.strip()]
        except OSError:
            return
        for ln in lines:
            if ln[0] not in _ENE_TARGETS:
                continue
            a = ln[1:]
            try:
                mode = a[4] if len(a) > 4 else "static"
                self.set(ln[0], tuple(int(x) for x in a[:3]),
                         _ENE_MODES.get(mode, int(mode) if mode.isdigit() else 0x02),
                         int(a[3]) if len(a) > 3 else 100,
                         int(a[5], 16) if len(a) > 5 else 0x0F)
            except (ValueError, IndexError, OSError):
                pass


# OpenRGB SDK client

_ORGB_COUNT, _ORGB_DATA, _ORGB_VERSION, _ORGB_NAME = 0, 1, 40, 50
_ORGB_LISTUPDATED = 100
_ORGB_UPDATELEDS, _ORGB_CUSTOMMODE, _ORGB_UPDATEMODE = 1050, 1100, 1101
_ORGB_PROTOCOL = 4      # newest layout this client parses


class _Reader:
    """Walks a controller blob."""

    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def take(self, fmt: str):
        v = struct.unpack_from("<" + fmt, self.d, self.i)[0]
        self.i += struct.calcsize("<" + fmt)
        return v

    def string(self) -> str:
        n = self.take("H")
        s = self.d[self.i:self.i + n].split(b"\0")[0].decode("utf-8", "replace")
        self.i += n
        return s


class _OpenRGBDevice:
    def __init__(self, index: int, blob: bytes, version: int):
        r = _Reader(blob)
        r.take("I")                       # data size
        r.take("i")                       # device type
        self.name = r.string()
        if version >= 1:
            r.string()                    # vendor
        for _ in range(4):
            r.string()                    # description, version, serial, location
        self.index = index
        n_modes = r.take("H")
        self.active = r.take("i")
        self.modes: list[bytes] = []      # each mode as it goes back on the wire
        for i in range(n_modes):
            start = r.i
            r.string()                    # name
            r.take("i")                   # value
            r.take("I")                   # flags
            r.take("II")                  # speed min, max
            if version >= 3:
                r.take("II")              # brightness min, max
            r.take("II")                  # colours min, max
            r.take("I")                   # speed
            if version >= 3:
                r.take("I")               # brightness
            r.take("II")                  # direction, colour mode
            for _ in range(r.take("H")):
                r.take("I")               # one RGBA colour
            self.modes.append(struct.pack("<i", i) + r.d[start:r.i])
        for _ in range(r.take("H")):      # zones
            r.string()
            r.take("i")
            r.take("II")
            r.take("I")
            if r.take("H"):               # matrix
                h, w = r.take("I"), r.take("I")
                r.i += 4 * h * w
            if version >= 4:
                for _ in range(r.take("H")):   # segments
                    r.string()
                    r.take("iII")
        self.led_count = r.take("H")      # the LEDs are listed next; only their number matters


class _OpenRGBLink:
    """One session with an OpenRGB SDK server, on its own thread."""

    _session = threading.Lock()                  # one link at a time; the next waits for the last to clean up
    _server: subprocess.Popen | None = None      # the server we started, shared between links
    _SERVER_ARGS = ("--server",)

    def __init__(self, host: str, port: int, exclude: tuple, say):
        self._addr = (host or "127.0.0.1", port)
        self._exclude = exclude
        self._say = say
        self._sock: socket.socket | None = None
        self.version = 0
        self.devices: list[_OpenRGBDevice] = []
        self._wake = threading.Event()
        self._stop = False
        self._kill = True
        self._mail = None
        self._last = None
        self._thread = threading.Thread(target=self._run, name="openrgb-link", daemon=True)

    def start(self):
        self._thread.start()

    def submit(self, rgb):
        self._mail = rgb
        self._wake.set()

    def stop(self, kill_server: bool = True):
        self._kill = kill_server
        self._stop = True
        self._wake.set()

    def join(self, timeout: float):
        self._thread.join(timeout)

    # wire

    def _send(self, dev: int, pkt: int, payload: bytes = b""):
        self._sock.sendall(b"ORGB" + struct.pack("<III", dev, pkt, len(payload)) + payload)

    def _recv(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise OSError("OpenRGB closed the connection")
            out += chunk
        return out

    def _read(self) -> tuple[int, bytes]:
        head = self._recv(16)
        if head[:4] != b"ORGB":
            raise OSError("not an OpenRGB server")
        _, pkt, size = struct.unpack("<III", head[4:])
        return pkt, self._recv(size)

    def _expect(self, want: int) -> bytes:
        # the server also sends unprompted packets (device list updates)
        while True:
            pkt, body = self._read()
            if pkt == want:
                return body

    # session

    def _local(self) -> bool:
        return self._addr[0] in ("127.0.0.1", "localhost", "::1")

    def _spawn(self):
        exe = shutil.which("openrgb")
        if exe is None:
            raise OSError("OpenRGB is not installed")
        self._say("OpenRGB: starting its server")
        _OpenRGBLink._server = subprocess.Popen(
            [exe, *self._SERVER_ARGS, "--server-host", self._addr[0], "--server-port", str(self._addr[1])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **plat_proc.popen_kwargs(group=False))

    def _count(self) -> int:
        self._send(0, _ORGB_COUNT)
        return struct.unpack("<I", self._expect(_ORGB_COUNT))[0]

    def _enumerate(self, count: int):
        self.devices = []
        for i in range(count):
            self._send(i, _ORGB_DATA, struct.pack("<I", self.version))
            dev = _OpenRGBDevice(i, self._expect(_ORGB_DATA), self.version)
            if dev.led_count and not any(x and x in dev.name.lower() for x in self._exclude):
                self.devices.append(dev)
                self._send(i, _ORGB_CUSTOMMODE)      # direct control of the LEDs
        self._last = None

    def _connect(self):
        started = False
        deadline = time.monotonic() + 45
        while True:
            if self._stop:
                return
            try:
                self._sock = socket.create_connection(self._addr, timeout=2)
                break
            except OSError as e:
                if not self._local() or time.monotonic() > deadline:
                    raise OSError(f"no server at {self._addr[0]}:{self._addr[1]} ({e.strerror or e})")
                if not started:
                    self._spawn()
                    started = True
                time.sleep(0.5)
        self._sock.settimeout(5)
        self._send(0, _ORGB_VERSION, struct.pack("<I", _ORGB_PROTOCOL))
        self.version = min(_ORGB_PROTOCOL, struct.unpack("<I", self._expect(_ORGB_VERSION))[0])
        self._send(0, _ORGB_NAME, b"AudioCrate\0")
        count = self._count()
        if started:
            # A server we just started is still scanning. Wait for the list to stop
            # growing, but not for long when it finds nothing at all.
            quiet = 0
            give_up = min(deadline, time.monotonic() + 20)
            while not self._stop and time.monotonic() < give_up and quiet < 2:
                time.sleep(1.0)
                n = self._count()
                quiet = quiet + 1 if (n == count and n > 0) else 0
                count = n
        self._enumerate(count)
        n = len(self.devices)
        self._say(f"OpenRGB: {n} device{'s' if n != 1 else ''}" if n else "OpenRGB: no devices found")

    def _drain(self):
        while select.select([self._sock], [], [], 0)[0]:
            pkt, _ = self._read()
            if pkt == _ORGB_LISTUPDATED:
                self._enumerate(self._count())

    def _fill(self, rgb):
        if self._last is not None and max(abs(a - b) for a, b in zip(self._last, rgb)) < 4:
            return
        self._last = rgb
        r, g, b = rgb
        for dev in self.devices:
            n = dev.led_count
            self._send(dev.index, _ORGB_UPDATELEDS,
                       struct.pack("<IH", 6 + 4 * n, n) + bytes((r, g, b, 0)) * n)

    def _run(self):
        with self._session:
            try:
                self._connect()
                while not self._stop and self._sock is not None:
                    self._wake.wait(0.5)
                    self._wake.clear()
                    self._drain()
                    rgb, self._mail = self._mail, None
                    if rgb is not None and self.devices:
                        self._fill(rgb)
            except (OSError, ValueError, struct.error) as e:
                self._say(f"OpenRGB: {e}")
            finally:
                self._teardown()

    def _teardown(self):
        if self._sock is not None:
            try:
                for dev in self.devices:        # each device back in the mode it was in
                    if 0 <= dev.active < len(dev.modes):
                        mode = dev.modes[dev.active]
                        self._send(dev.index, _ORGB_UPDATEMODE, struct.pack("<I", 4 + len(mode)) + mode)
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        srv = _OpenRGBLink._server
        if srv is not None and self._kill:
            _OpenRGBLink._server = None
            srv.terminate()
            try:
                srv.wait(3)
            except subprocess.TimeoutExpired:
                srv.kill()


# the sync itself

def _rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return int(r * 255), int(g * 255), int(b * 255)


class RgbSync(QObject):
    status_changed = Signal(str)

    def __init__(self, analyzer, player, parent=None):
        super().__init__(parent)
        self._analyzer = analyzer
        self._player = player
        self._cfg = {"enabled": False, "style": "Spectrum", "keyboard": True, "emblem": True,
                     "mode_led": False, "openrgb": False, "openrgb_host": "127.0.0.1:6742",
                     "openrgb_exclude": "predator, acer", "brightness": 100, "fps": 12}
        self._accent = QColor("#8040c8")           # where the colour is heading
        self._shown = [128.0, 64.0, 200.0]         # the colour the lights have now (r, g, b)
        self._t_fade = time.monotonic()
        self._hann = np.hanning(_FFT_N).astype(np.float32)
        freqs = np.fft.rfftfreq(_FFT_N, 1.0 / 44100)
        self._bins = [(int(np.searchsorted(freqs, a)), int(np.searchsorted(freqs, b)))
                      for a, b in _BANDS_HZ]
        self._peak = np.full(4, 1e-6)
        self._lv = np.zeros(4)
        self._bass_avg = 0.0
        self._beat = 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        # Predator worker
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._frame = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._busy_ms = 0.0
        self._link: _OpenRGBLink | None = None
        self._notes: dict[str, str] = {}
        self._status = ""

    # configuration

    def configure(self, d: dict):
        old = dict(self._cfg)
        for k in self._cfg:
            if k in d:
                self._cfg[k] = d[k]
        self._cfg["fps"] = max(2, min(30, int(self._cfg["fps"])))
        self._cfg["brightness"] = max(5, min(100, int(self._cfg["brightness"])))
        if not self._cfg["enabled"]:
            self.stop()
        elif self._thread is None:
            self.start()
        elif any(old[k] != self._cfg[k] for k in _DEVICE_KEYS):
            # a different set of lights: hand the old ones back and open the new ones.
            # Style, brightness and rate are read live and need no restart, which
            # would flash every light back to its saved state.
            self.stop(restarting=True)
            self.start()
        elif old["fps"] != self._cfg["fps"]:
            self._timer.setInterval(int(1000 / self._cfg["fps"]))

    def set_accent(self, palette: dict | None, fallback=None):
        if palette:
            self._accent = QColor(palette.get("accent", "#8040c8"))
        elif fallback is not None:
            self._accent = QColor(fallback)

    def start(self):
        if self._thread is not None or not self._cfg["enabled"]:
            return
        self._stop = False
        self._notes = {}
        self._frame = None
        self._shown = [float(self._accent.red()), float(self._accent.green()), float(self._accent.blue())]
        self._t_fade = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="rgb-sync", daemon=True)
        self._thread.start()
        if self._cfg["openrgb"]:
            host, _, port = self._cfg["openrgb_host"].partition(":")
            try:
                port = int(port or 6742)
            except ValueError:
                port = 6742
            exclude = tuple(x.strip().lower() for x in self._cfg["openrgb_exclude"].split(",") if x.strip())
            self._link = _OpenRGBLink(host.strip(), port, exclude, lambda t: self._note("openrgb", t))
            self._link.start()
        self._timer.start(int(1000 / self._cfg["fps"]))
        self._tick()        # something visible straight away, not only once the music moves

    def stop(self, restarting: bool = False):
        self._timer.stop()
        link, self._link = self._link, None
        if link is not None:
            # keep the server we started if the next session wants it, and do not
            # wait here for the link to finish: it cleans up on its own thread
            link.stop(kill_server=not (restarting and self._cfg["openrgb"]))
        t, self._thread = self._thread, None
        if t is not None:
            self._stop = True
            self._wake.set()
            t.join(2.0)
        if not restarting:
            self._notes = {}
            self._emit()

    def shutdown(self):
        link = self._link
        self.stop()
        if link is not None:
            link.join(5.0)

    # audio side (GUI thread, cheap)

    def _levels(self, win) -> bool:
        if win is None or len(win) < _FFT_N:
            return False
        win = win[-_FFT_N:]
        if float(np.sqrt(np.mean(win * win))) < 2e-3:       # silence
            self._lv *= 0.8
            return True
        spec = np.abs(np.fft.rfft(win * self._hann))
        x = np.array([spec[a:b].mean() if b > a else 0.0 for a, b in self._bins])
        self._peak = np.maximum(x, self._peak * 0.996)
        lv = (x / (self._peak + 1e-9)) ** 2
        self._lv = np.where(lv > self._lv, 0.7 * lv + 0.3 * self._lv, 0.2 * lv + 0.8 * self._lv)
        bass = float(self._lv[0])
        if bass > 0.4 and bass > 1.3 * self._bass_avg:
            self._beat = 1.0
        else:
            self._beat *= 0.8
        self._bass_avg = 0.9 * self._bass_avg + 0.1 * bass
        return True

    def _fade(self) -> bool:
        """Move the colour in use toward the accent, so a new album's colour comes in
        over about a second instead of jumping, as the interface does. True while
        it is still moving."""
        now = time.monotonic()
        k = 1.0 - math.exp(-min(0.25, now - self._t_fade) / _FADE_TAU)
        self._t_fade = now
        moving = False
        for i, want in enumerate((self._accent.red(), self._accent.green(), self._accent.blue())):
            d = want - self._shown[i]
            if abs(d) > 0.5:
                self._shown[i] += d * k
                moving = True
            else:
                self._shown[i] = float(want)
        return moving

    def _tick(self):
        moving = self._fade()
        if not self._player.is_playing:
            want = int(1000 / self._cfg["fps"]) if moving else 500   # nothing else moves, so look rarely
            if self._timer.interval() != want:
                self._timer.setInterval(want)
            self._submit(self._idle())
            return
        if not self._levels(self._analyzer.get_audio_window(self._player.position_ms, _FFT_N)):
            return
        # a slow device slows the sender down instead of queueing writes behind it
        want = max(int(1000 / self._cfg["fps"]), int(self._busy_ms * 3))
        if self._timer.interval() != want:
            self._timer.setInterval(want)
        self._submit(self._build())

    def _hs(self) -> tuple[float, float]:
        h, s, _, _ = QColor(*(int(round(c)) for c in self._shown)).getHsvF()
        return max(h, 0.0), max(s, 0.65)

    def _idle(self) -> dict:
        """A dim steady glow in the album colour, for while nothing plays."""
        h, s = self._hs()
        c = _rgb(h, s, self._cfg["brightness"] / 100.0 * _IDLE_LEVEL)
        return {"zones": [c] * 4, "emblem": c, "mode": (0, 0, 0), "overall": c}

    def _build(self) -> dict:
        h, s = self._hs()
        top = self._cfg["brightness"] / 100.0
        lv, style = self._lv, self._cfg["style"]
        mean = float(lv.mean())
        if style == "Steady":
            c = _rgb(h, s, top)
            return {"zones": [c] * 4, "emblem": c, "mode": c, "overall": c}
        if style == "Pulse":
            c = _rgb(h, s, top * (0.1 + 0.9 * float(lv[0])))
            return {"zones": [c] * 4, "emblem": c, "mode": c, "overall": c}
        overall = _rgb(h, s, top * (0.15 + 0.85 * mean))
        return {"zones": [_rgb(h + 0.07 * i, s, top * (0.12 + 0.88 * float(lv[i]))) for i in range(4)],
                "emblem": overall, "mode": _rgb(h + 0.5, s, top * self._beat), "overall": overall}

    def _submit(self, frame: dict):
        with self._lock:
            self._frame = frame
        self._wake.set()
        link = self._link
        if link is not None:
            link.submit(frame["overall"])

    # status

    def _note(self, key: str, text: str):
        self._notes[key] = text
        self._emit()

    def _emit(self):
        msg = ", ".join(v for v in self._notes.values() if v)
        if msg != self._status:
            self._status = msg
            self.status_changed.emit(msg)

    # Predator worker thread

    @staticmethod
    def _other_writers() -> bool:
        """Is another program that writes to the same controller running?
        Predator Sense's lighting panel and the ene-rgb command do, and whatever
        they write lasts until we write again."""
        me, names = os.getpid(), {"sense_ui.py", "rgb_ui.py", "ene_rgb.py", "ene-rgb"}
        for path in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                if int(os.path.basename(os.path.dirname(path))) == me:
                    continue
                with open(path, "rb") as fh:
                    args = fh.read().split(b"\0")
            except (OSError, ValueError):
                continue
            if any(os.path.basename(a.decode("utf-8", "replace")) in names for a in args[:4]):
                return True
        return False

    def _run(self):
        cfg = dict(self._cfg)
        # the Predator controller is a Linux hidraw device; nothing else has one to open
        pred = _Predator() if (cfg["keyboard"] or cfg["emblem"] or cfg["mode_led"]) and sys.platform.startswith("linux") else None
        sent: dict = {}              # light -> (colour, when it was last sent)
        next_scan = 0.0
        try:
            while not self._stop:
                self._wake.wait(0.5)
                self._wake.clear()
                if pred is None:
                    continue
                now = time.monotonic()
                if now >= next_scan:
                    next_scan = now + 3.0
                    self._note("sense", "Predator Sense is open and can overwrite the lights"
                               if self._other_writers() else "")
                if not pred.is_open:
                    try:
                        pred.open()
                        sent.clear()
                        self._note("predator", "Predator lighting on")
                    except OSError as e:
                        self._note("predator", f"Predator lighting: {e}")
                        time.sleep(1.0)
                        continue
                with self._lock:
                    frame = self._frame      # kept, not consumed: it is resent every second
                if frame is None:
                    continue
                t0 = time.monotonic()
                try:
                    self._write_predator(pred, frame, sent, cfg, now)
                except OSError as e:
                    # after a suspend the controller can come back under another
                    # device node, so let go of this one and find it again
                    pred.close()
                    self._note("predator", f"Predator lighting: lost the controller ({e.strerror or e}), retrying")
                    time.sleep(1.0)
                else:
                    self._busy_ms = (time.monotonic() - t0) * 1000.0
        finally:
            if pred is not None:
                try:
                    if pred.is_open:
                        pred.restore()
                finally:
                    pred.close()

    @staticmethod
    def _write_predator(pred: _Predator, frame: dict, sent: dict, cfg: dict, now: float):
        def due(key, rgb):
            old = sent.get(key)
            if old is not None and now - old[1] < _REFRESH_S and max(abs(a - b) for a, b in zip(old[0], rgb)) < 4:
                return False
            sent[key] = (rgb, now)
            return True

        if cfg["keyboard"]:
            groups: dict = {}
            for i, rgb in enumerate(frame["zones"]):
                if due(("z", i), rgb):
                    groups[rgb] = groups.get(rgb, 0) | (1 << i)
            for rgb, mask in groups.items():
                pred.set("keyboard", rgb, zones=mask)
        if cfg["emblem"]:
            first = "emblem" not in sent
            if due("emblem", frame["emblem"]):
                pred.set("emblem", frame["emblem"])
                if first:
                    pred.set("emblem", frame["emblem"])    # the first write only switches its mode over from neon
        if cfg["mode_led"] and due("mode", frame["mode"]):
            pred.set("mode", frame["mode"])
