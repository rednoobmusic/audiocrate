#!/usr/bin/env python3
"""
Mic Codec Lab — real-time mic MP3/Opus mangler (Maim-inspired).

Pipeline: mic --(ffmpeg/PipeWire)--> tone (lo/hi cut) + rate-snap + drive + bit-crush
--> MP3 or Opus encode --> [optional bitstream corruption / Opus packet loss]
--> decode + makeup gain --> virtual PipeWire source "Mic Codec Lab (processed)".

NOTE vs Maim: Maim patches the LAME/Opus *source* to bend the encoder internals
(MDCT bands, packet structure). We drive stock ffmpeg (a black box), so we mirror
Maim's control layout with what's externally achievable: drive/makeup/cuts/bitrate
+ MP3 bitstream error + real Opus packet loss.

Run:   python mic_codec_lab.py
Deps:  PySide6, ffmpeg (libmp3lame/libopus), pactl + pipewire-pulse
"""
from __future__ import annotations

import atexit
import math
import os
import shutil
import signal
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, QRectF, Signal
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QComboBox, QPushButton, QLabel, QProgressBar, QStackedWidget,
)

SELF = os.path.abspath(__file__)
SINK_NAME   = "mic_codec_lab"
SOURCE_NAME = "mic_codec_lab_src"
SOURCE_DESC = "Mic_Codec_Lab_(processed)"
MON_SINK_NAME = "mic_codec_lab_monitor"
MON_SINK_DESC = "Mic_Codec_Lab_Monitor"

GENERIC_BR = ["6k", "8k", "12k", "16k", "24k", "32k", "48k", "64k", "96k", "128k"]
MP3_RATES  = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]
OPUS_RATES = [8000, 12000, 16000, 24000, 48000]
CODECS = {
    "None (rate / bitcrush)":   dict(enc=None,         fmt=None, br=False, brs=GENERIC_BR, rates=None),
    "MP3":                      dict(enc="libmp3lame", fmt="mp3", br=True,  brs=GENERIC_BR, rates=MP3_RATES),
    "Opus (gritty @ low kbps)": dict(enc="libopus",    fmt="ogg", br=True,  brs=GENERIC_BR, rates=OPUS_RATES),
}


# --------------------------------------------------------------------------
def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def list_input_sources() -> list[str]:
    out = _run(["pactl", "list", "short", "sources"])
    return [p[1] for line in out.splitlines()
            if len(p := line.split("\t")) >= 2 and not p[1].endswith(".monitor")]


def default_source() -> str:
    return _run(["pactl", "get-default-source"])


def list_sinks() -> list[str]:
    out = _run(["pactl", "list", "short", "sinks"])
    return [l.split("\t")[1] for l in out.splitlines() if len(l.split("\t")) >= 2]


def cleanup_stale_modules():
    subprocess.run(["pkill", "-9", "-f", f"ffmpeg.*{SINK_NAME}"], capture_output=True)


def run_corruptor(prob: float):
    """MP3 ERROR: flip random bits in ~prob fraction of bytes; skip first 2 KB (headers)."""
    import random
    skip, seen = 2048, 0
    try:
        while True:
            b = os.read(0, 8192)
            if not b:
                break
            if prob > 0 and seen >= skip:
                ba = bytearray(b)
                for _ in range(max(1, int(len(ba) * prob))):
                    ba[random.randrange(len(ba))] ^= 1 << random.randrange(8)
                b = bytes(ba)
            seen += len(b)
            os.write(1, b)
    except (BrokenPipeError, OSError):
        pass


def run_packetloss(rate: float, jitter: float):
    """OPUS PACKET LOSS: randomly drop ~packet-sized chunks (Opus PLC then warbles)."""
    import random
    skip, seen = 2048, 0
    try:
        while True:
            b = os.read(0, 800)
            if not b:
                break
            seen += len(b)
            drop = False
            if rate > 0 and seen >= skip:
                pr = rate * (1.0 + (random.random() - 0.5) * 2.0 * jitter)
                drop = random.random() < pr
            if not drop:
                os.write(1, b)
    except (BrokenPipeError, OSError):
        pass


# --------------------------------------------------------------------------
class Knob(QWidget):
    """Draggable rotary knob (drag up/down or scroll). Numeric or label-list."""
    valueChanged = Signal(int)

    def __init__(self, name, lo=0, hi=100, value=0, suffix="", step=1, labels=None, parent=None):
        super().__init__(parent)
        self._name = name
        self._labels = labels
        self._lo, self._hi = (0, len(labels) - 1) if labels is not None else (lo, hi)
        self._suffix, self._step = suffix, step
        self._val = max(self._lo, min(self._hi, value))
        self._drag = None
        self.setFixedSize(76, 90)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip(name)

    def value(self):
        return self._val

    def text_value(self):
        return self._labels[self._val] if self._labels is not None else f"{self._val}{self._suffix}"

    def setValue(self, v):
        v = max(self._lo, min(self._hi, int(round(v))))
        if v != self._val:
            self._val = v
            self.update()
            self.valueChanged.emit(v)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.position().y()

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        dy = self._drag - e.position().y()
        self.setValue(self._val + dy * (self._hi - self._lo) / 160.0)
        self._drag = e.position().y()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def wheelEvent(self, e):
        self.setValue(self._val + (self._step if e.angleDelta().y() > 0 else -self._step))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        fg, accent = pal.text().color(), pal.highlight().color()
        w, cx, cy, r = self.width(), self.width() / 2, 42, 22
        f = p.font(); f.setPointSizeF(7.5); p.setFont(f)
        p.setPen(fg)
        p.drawText(0, 0, w, 13, Qt.AlignCenter, self._name)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        start, sweep = 225.0, -270.0
        frac = (self._val - self._lo) / (self._hi - self._lo) if self._hi > self._lo else 0.0
        pen = QPen(QColor(fg.red(), fg.green(), fg.blue(), 55)); pen.setWidthF(3)
        p.setPen(pen); p.drawArc(rect, int(start * 16), int(sweep * 16))
        pen.setColor(accent); p.setPen(pen)
        p.drawArc(rect, int(start * 16), int(sweep * frac * 16))
        ang = math.radians(start + sweep * frac)
        p.drawLine(int(cx), int(cy), int(cx + (r - 3) * math.cos(ang)), int(cy - (r - 3) * math.sin(ang)))
        p.setPen(fg)
        p.drawText(0, self.height() - 14, w, 13, Qt.AlignCenter, self.text_value())
        p.end()


# --------------------------------------------------------------------------
class Engine:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.sink_mod = self.src_mod = self.mon_mod = None

    def create_virtual_source(self):
        self.destroy_virtual_source()
        cleanup_stale_modules()
        if SINK_NAME in _run(["pactl", "list", "short", "sinks"]):
            self.sink_mod = self.src_mod = None
            return
        self.sink_mod = _run(["pactl", "load-module", "module-null-sink",
                              f"sink_name={SINK_NAME}", "rate=48000", "channels=1", "channel_map=mono",
                              "sink_properties=device.description=MicCodecLab_internal"]) or None
        self.src_mod = _run(["pactl", "load-module", "module-remap-source",
                             f"master={SINK_NAME}.monitor", f"source_name={SOURCE_NAME}", "channels=1",
                             f"source_properties=device.description={SOURCE_DESC}"]) or None

    def destroy_virtual_source(self):
        for mod in (self.src_mod, self.sink_mod):
            if mod and mod.isdigit():
                _run(["pactl", "unload-module", mod])
        self.src_mod = self.sink_mod = None

    def create_monitor_sink(self):
        if not self.mon_mod:
            self.mon_mod = _run(["pactl", "load-module", "module-null-sink",
                                 f"sink_name={MON_SINK_NAME}",
                                 f"sink_properties=device.description={MON_SINK_DESC}"]) or None

    def destroy_monitor_sink(self):
        if self.mon_mod and self.mon_mod.isdigit():
            _run(["pactl", "unload-module", self.mon_mod])
        self.mon_mod = None

    def build_cmd(self, mic, monitor_dev, p) -> str:
        c = CODECS[p["codec"]]
        eff_rate = min(c["rates"], key=lambda r: abs(r - p["rate"])) if c.get("rates") else p["rate"]
        pre = []
        if p["locut"] > 10:
            pre.append(f"highpass=f={p['locut']}")
        if p["hicut"] < 20000:
            pre.append(f"lowpass=f={p['hicut']}")
        pre.append(f"aresample={eff_rate}")
        if p["drive"]:
            pre.append(f"volume={p['drive']}dB")
        if p["bits"] < 16:
            pre.append(f"acrusher=bits={p['bits']}:mode=lin:mix=1")
        common = "ffmpeg -hide_banner -loglevel error -nostats"
        lowlat = "-fflags +nobuffer -flags +low_delay -max_delay 0"

        def pulse_out(sink, name):
            return f'-ac 1 -ar 48000 -f pulse -device "{sink}" "{name}"'

        outs = pulse_out(SINK_NAME, "MicCodecLab")
        if monitor_dev:
            outs += " " + pulse_out(monitor_dev, "MicCodecLabMon")

        if c["enc"] is None:                       # no codec: one ffmpeg, makeup folded into chain
            if p["makeup"]:
                pre.append(f"volume={p['makeup']}dB")
            return f'{common} {lowlat} -f pulse -i "{mic}" -ac 1 -af "{",".join(pre)}" {outs}'

        af = ",".join(pre)
        post = f"-af volume={p['makeup']}dB" if p["makeup"] else ""
        br = f"-b:a {p['bitrate']}" if (c["br"] and p.get("bitrate")) else ""
        extra = "-application lowdelay -frame_duration 20" if c["enc"] == "libopus" else ""
        enc = (f'{common} {lowlat} -f pulse -i "{mic}" -ac 1 -af "{af}" -ar {eff_rate} '
               f'-c:a {c["enc"]} {br} {extra} -flush_packets 1 -f {c["fmt"]} pipe:1')
        dec = f'{common} {lowlat} -probesize 32 -analyzeduration 0 -f {c["fmt"]} -i pipe:0 -ac 1 {post} {outs}'

        mid = ""
        if p["codec"] == "MP3" and p.get("error"):
            mid = f'python3 "{SELF}" --corrupt {p["error"] / 100.0 * 0.003:.6f} | '
        elif c["enc"] == "libopus" and p.get("loss"):
            mid = f'python3 "{SELF}" --loss {p["loss"] / 100.0:.4f} {p.get("jitter", 0) / 100.0:.4f} | '
        return f"{enc} | {mid}{dec}"

    def start(self, *args) -> str:
        self.stop_proc()
        cmd = self.build_cmd(*args)
        env = dict(os.environ, PIPEWIRE_LATENCY="512/48000")
        self.proc = subprocess.Popen(["bash", "-o", "pipefail", "-c", cmd],
                                     stderr=subprocess.PIPE, start_new_session=True, env=env)
        return cmd

    def stop_proc(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass
        self.proc = None

    def shutdown(self):
        self.stop_proc()
        self.destroy_virtual_source()
        self.destroy_monitor_sink()


class Meter:
    def __init__(self):
        self.level = 0.0
        self.stream = None
        try:
            import numpy as np
            import sounddevice as sd
            self._np, self._sd, self.ok = np, sd, True
        except Exception:
            self.ok = False

    def start(self):
        if not self.ok:
            return
        np = self._np

        def cb(indata, frames, t, status):
            self.level = float(np.sqrt(np.mean(indata.astype("float32") ** 2)))
        try:
            self.stream = self._sd.InputStream(channels=1, samplerate=48000, blocksize=1024,
                                               dtype="float32", callback=cb)
            self.stream.start()
        except Exception:
            self.stream = None

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.level = 0.0


# --------------------------------------------------------------------------
class MicCodecLab(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mic Codec Lab")
        self.engine = Engine()
        self.meter = Meter()
        self._restart_timer = QTimer(self); self._restart_timer.setSingleShot(True)
        self._restart_timer.setInterval(400); self._restart_timer.timeout.connect(self._apply_if_running)
        self._poll = QTimer(self); self._poll.setInterval(120); self._poll.timeout.connect(self._tick)
        self._build_ui()
        atexit.register(self.engine.shutdown)
        self._poll.start()

    def _krow(self, *knobs):
        row = QHBoxLayout()
        for k in knobs:
            row.addWidget(k)
            k.valueChanged.connect(lambda *_: self._restart_timer.start())
        row.addStretch()
        w = QWidget(); w.setLayout(row)
        return w

    def _build_ui(self):
        root = QVBoxLayout(self)

        io = QGroupBox("Input / Output")
        form = QFormLayout(io)
        self.cmb_in = QComboBox()
        srcs = list_input_sources()
        self.cmb_in.addItems(srcs or ["(no sources found)"])
        dflt = default_source()
        for i, s in enumerate(srcs):
            if s == dflt or "AKG" in s or "C44" in s:
                self.cmb_in.setCurrentIndex(i); break
        refresh = QPushButton("⟳"); refresh.setFixedWidth(30); refresh.clicked.connect(self._refresh_inputs)
        rin = QHBoxLayout(); rin.addWidget(self.cmb_in, 1); rin.addWidget(refresh)
        wrap = QWidget(); wrap.setLayout(rin); form.addRow("Mic:", wrap)
        self.cmb_mon = QComboBox()
        self.cmb_mon.addItem("Off (virtual source only)", ("", False))
        self.cmb_mon.addItem("Virtual device — Mic Codec Lab Monitor", (MON_SINK_NAME, True))
        for s in list_sinks():
            self.cmb_mon.addItem(s, (s, False))
        self.cmb_mon.setCurrentIndex(0)
        form.addRow("Monitor →", self.cmb_mon)
        root.addWidget(io)

        man = QGroupBox("Tone & Drive")
        mv = QVBoxLayout(man)
        self.k_rate   = Knob("RATE",   2000, 48000, 8000, suffix=" Hz", step=500)
        self.k_bits   = Knob("BITS",   1, 16, 16, step=1)
        self.k_locut  = Knob("LO CUT", 10, 4000, 10, suffix=" Hz", step=50)
        self.k_hicut  = Knob("HI CUT", 500, 20000, 20000, suffix=" Hz", step=200)
        self.k_drive  = Knob("DRIVE",  -36, 36, 0, suffix=" dB", step=1)
        self.k_makeup = Knob("MAKEUP", -36, 36, 0, suffix=" dB", step=1)
        mv.addWidget(self._krow(self.k_rate, self.k_bits, self.k_locut))
        mv.addWidget(self._krow(self.k_hicut, self.k_drive, self.k_makeup))
        root.addWidget(man)

        cod = QGroupBox("Codec")
        cv = QVBoxLayout(cod)
        self.cmb_codec = QComboBox(); self.cmb_codec.addItems(CODECS.keys())
        cv.addWidget(self.cmb_codec)
        self.stack = QStackedWidget()
        self.pages = {}
        for name, c in CODECS.items():
            page = QWidget(); pl = QHBoxLayout(page); pl.setContentsMargins(0, 4, 0, 0); pl.setSpacing(12)
            knobs = {}
            if c["br"]:
                knobs["bitrate"] = Knob("BITRATE", labels=c["brs"], value=max(0, len(c["brs"]) // 3))
            if name == "MP3":
                knobs["error"] = Knob("ERROR", 0, 100, 0, suffix="%", step=5)
            if c["enc"] == "libopus":
                knobs["loss"] = Knob("LOSS", 0, 100, 0, suffix="%", step=5)
                knobs["jitter"] = Knob("JITTER", 0, 100, 0, suffix="%", step=5)
            for k in knobs.values():
                pl.addWidget(k)
                k.valueChanged.connect(lambda *_: self._restart_timer.start())
            tags = []
            if c["enc"] is None:
                tags.append("raw rate / bit-crush")
            if c.get("rates"):
                tags.append("RATE→" + "/".join(f"{r/1000:g}" for r in c["rates"]) + "k")
            if tags:
                lab = QLabel("\n".join(tags)); lab.setStyleSheet("color: palette(mid);"); lab.setWordWrap(True)
                pl.addWidget(lab)
            pl.addStretch()
            self.pages[name] = knobs
            self.stack.addWidget(page)
        cv.addWidget(self.stack)
        root.addWidget(cod)

        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        root.addWidget(QLabel("Input level:")); root.addWidget(self.bar)
        self.btn = QPushButton("▶  Start"); self.btn.setCheckable(True)
        self.btn.toggled.connect(self._toggle); root.addWidget(self.btn)
        self.status = QLabel("Stopped. Pick a mic and hit Start."); self.status.setWordWrap(True)
        root.addWidget(self.status)

        for cmb in (self.cmb_in, self.cmb_mon):
            cmb.currentIndexChanged.connect(lambda *_: self._restart_timer.start())
        self.cmb_codec.currentIndexChanged.connect(self._codec_changed)
        self._codec_changed()
        self.setMinimumWidth(430)

    def _refresh_inputs(self):
        cur = self.cmb_in.currentText()
        self.cmb_in.blockSignals(True)
        self.cmb_in.clear(); self.cmb_in.addItems(list_input_sources() or ["(none)"])
        idx = self.cmb_in.findText(cur)
        if idx >= 0:
            self.cmb_in.setCurrentIndex(idx)
        self.cmb_in.blockSignals(False)

    def _codec_changed(self):
        self.stack.setCurrentIndex(self.cmb_codec.currentIndex())
        self._restart_timer.start()

    def _prep_monitor(self):
        _, is_virtual = self.cmb_mon.currentData()
        (self.engine.create_monitor_sink if is_virtual else self.engine.destroy_monitor_sink)()

    def _args(self):
        pg = self.pages[self.cmb_codec.currentText()]
        gv = lambda key: pg[key].value() if key in pg else 0
        p = dict(codec=self.cmb_codec.currentText(),
                 rate=self.k_rate.value(), bits=self.k_bits.value(),
                 locut=self.k_locut.value(), hicut=self.k_hicut.value(),
                 drive=self.k_drive.value(), makeup=self.k_makeup.value(),
                 bitrate=pg["bitrate"].text_value() if "bitrate" in pg else "",
                 error=gv("error"), loss=gv("loss"), jitter=gv("jitter"))
        return (self.cmb_in.currentText(), self.cmb_mon.currentData()[0], p)

    def _toggle(self, on):
        self._start() if on else self._stop()

    def _start(self):
        if self.cmb_in.currentText().startswith("("):
            self.status.setText("No input source available."); self.btn.setChecked(False); return
        self.engine.create_virtual_source()
        self._prep_monitor()
        self.engine.start(*self._args())
        self.meter.start()
        self.btn.setText("■  Stop")
        msg = f'Running.  Pick "{SOURCE_DESC.replace("_", " ")}" as your mic in any app.'
        if self.cmb_mon.currentData()[1]:
            msg += f'\nMonitor → "{MON_SINK_DESC.replace("_", " ")}".'
        self.status.setText(msg)

    def _stop(self):
        self.meter.stop(); self.engine.shutdown()
        self.btn.setText("▶  Start"); self.bar.setValue(0); self.status.setText("Stopped.")

    def _apply_if_running(self):
        if self.btn.isChecked():
            self._prep_monitor()
            self.engine.start(*self._args())

    def _tick(self):
        if not self.btn.isChecked():
            return
        self.bar.setValue(int(min(1.0, self.meter.level * 4.0) * 100))
        p = self.engine.proc
        if p is not None and p.poll() is not None:
            err = ""
            try:
                err = (p.stderr.read() or b"").decode(errors="ignore")[-200:]
            except Exception:
                pass
            self.status.setText(f"⚠ Pipeline stopped — ease off ERROR/LOSS or change codec.\n{err}")
            self.btn.setChecked(False)

    def closeEvent(self, e):
        self._stop()
        super().closeEvent(e)


def main():
    if "--corrupt" in sys.argv:
        run_corruptor(float(sys.argv[sys.argv.index("--corrupt") + 1]))
        return
    if "--loss" in sys.argv:
        i = sys.argv.index("--loss")
        run_packetloss(float(sys.argv[i + 1]), float(sys.argv[i + 2]))
        return
    if shutil.which("ffmpeg") is None or shutil.which("pactl") is None:
        print("Needs ffmpeg and pactl (pipewire-pulse) on PATH.", file=sys.stderr)
        sys.exit(1)
    app = QApplication(sys.argv)
    w = MicCodecLab()
    if "--selftest" in sys.argv:
        print("selftest: window constructed OK")
        return
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
