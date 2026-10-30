"""A waterfall spectrogram: frequency across, time flowing away from the newest row.

Every row is one FFT of the audio at that moment, mapped to a colour. New rows enter
at one edge and the history scrolls away, so you see how the spectrum moves over the
last few seconds (a sweep is a diagonal, a drum is a vertical streak, a held note is a
line). Everything about it can be set from the right click menu: the FFT, the window,
the frequency axis, the level range, the smoothing, the colour map, the direction.

The audio is read from the engine's decoded buffer by time, so rows stay true to the
clock even when a frame is late, and a seek or a new track leaves a marker line.

The history is kept as levels on a fixed grid of log spaced columns, not as the picture.
The picture is derived from it for whatever axis, range and levels are set, so zooming,
changing the axis, the level range or the direction reaches what is already on screen.
"""
from __future__ import annotations

import math
import time
from types import SimpleNamespace

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontInfo, QFontMetrics, QImage, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMenu, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)
from scipy.signal import windows as _windows

import power
from analyzer import Analyzer, _RATE
from panel_oscilloscope import _FpsSpin
from player import Player

_DEFAULT_ACCENT = "#8040c8"
FFT_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768)
WINDOWS = ("Hann", "Hamming", "Blackman", "Blackman-Harris", "Nuttall", "Flat top",
           "Kaiser", "Gaussian", "Rectangular")
SCALES = ("Linear", "Log", "Mel", "Bark", "ERB")
SMOOTHS = ("Off", "1/12 octave", "1/6 octave", "1/3 octave", "1/2 octave", "1 octave")
_SMOOTH_OCT = {"Off": 0.0, "1/12 octave": 1 / 12, "1/6 octave": 1 / 6,
               "1/3 octave": 1 / 3, "1/2 octave": 0.5, "1 octave": 1.0}
DIRECTIONS = ("Down", "Up", "Left", "Right")      # where the history goes
TILTS = (0.0, 3.0, 4.5, 6.0)                      # dB per octave added, so pink noise reads flat
_MAX_ROWS_PER_FRAME = 12

# The history: _STORE_ROWS rows of _M columns, log spaced from _M_LO to _M_HI Hz, kept as
# (dB + 200) * 100 in 16 bits (silence is 0, so an untouched row costs no memory). The
# marker a seek or a new track leaves is a row far above any ceiling.
_M = 4096
_M_LO, _M_HI = 10.0, 24000.0
_M_OCT = math.log2(_M_HI / _M_LO)
_M_EDGES = np.geomspace(_M_LO, _M_HI, _M + 1)
_M_CENTRES = np.sqrt(_M_EDGES[:-1] * _M_EDGES[1:])
_STORE_ROWS = 2048
_MARK_U16 = 40000
_SILENT_DB = -200.0

DEFAULTS = {
    "fft": 4096, "window": "Hann", "kaiser_beta": 8.0, "pad": 1, "rate": 60,
    "scale": "Log", "fmin": 30, "fmax": 20000,
    "floor": -90.0, "ceil": -10.0, "gamma": 1.0, "bright": 0, "tilt": 3.0,
    "auto": False, "auto_release": 6.0,
    "t_avg": 40, "f_smooth": "Off",
    "cmap": "Magma", "follow": False, "reverse": False,
    "custom": "#000000, #2a0a5e, #c0392b, #f5b041, #ffffff",
    "direction": "Down", "labels": True, "legend": True, "grid": True,
    "strip_on": True, "strip": 18, "peak": True,
    "fps": 60, "vsync": False,
}

# which part of the pipeline a changed setting reaches
_ANALYSIS_KEYS = {"fft", "window", "kaiser_beta", "pad", "rate", "f_smooth"}
_LAYOUT_KEYS = {"direction", "labels", "legend", "strip_on", "strip"}
_VIEW_KEYS = {"scale", "fmin", "fmax", "tilt", "floor", "ceil", "gamma", "bright", "auto"}
_COLOUR_KEYS = {"cmap", "follow", "reverse", "custom"}
_TIMER_KEYS = {"fps", "vsync"}

# colour maps as anchor colours, sampled to 256 steps (so no plotting library is needed)
_MAPS = {
    "Viridis": "#440154 #482878 #3e4989 #31688e #26828e #1f9e89 #35b779 #6ece58 #b5de2b #fde725",
    "Magma": "#000004 #180f3d #451077 #721f81 #9f2f7f #cd4071 #f1605d #fd9567 #fec98d #fcfdbf",
    "Inferno": "#000004 #1b0c41 #4a0c6b #781c6d #a52c60 #cf4446 #ed6925 #fb9b06 #f7d13d #fcffa4",
    "Plasma": "#0d0887 #46039f #7201a8 #9c179e #bd3786 #d8576b #ed7953 #fb9f3a #fdca26 #f0f921",
    "Cividis": "#00204d #00336f #39486b #575c6e #707173 #8a8779 #a69d75 #c4b56c #e4cf5b #ffea46",
    "Turbo": "#30123b #4662d7 #36aaf9 #1ae4b6 #72fe5e #c7ef34 #faba39 #f66b19 #ca2a04 #7a0403",
    "Grayscale": "#000000 #ffffff",
    "Inverted gray": "#ffffff #000000",
    "Fire": "#000000 #3b0000 #a00000 #ff4500 #ffa500 #ffff66 #ffffff",
    "Ice": "#000000 #001a4d #0050b3 #1e90ff #00e5ff #bffcff #ffffff",
    "SDR": "#00000f #00007a #0000ff #00b4ff #00ff80 #ffff00 #ff7a00 #ff0000 #ffffff",
    "Rainbow": "#1a0033 #3300cc #0066ff #00ccff #00ff99 #99ff00 #ffcc00 #ff6600 #ff0033",
}
CMAPS = tuple(_MAPS) + ("Album art", "Accent", "Custom")


def _stops_lut(colors: list[QColor]) -> np.ndarray:
    """256 packed 0xAARRGGBB entries blended through the anchor colours."""
    if len(colors) == 1:
        colors = colors * 2
    pos = np.linspace(0.0, 1.0, len(colors))
    t = np.linspace(0.0, 1.0, 256)
    ch = [np.interp(t, pos, [c.red() for c in colors]),
          np.interp(t, pos, [c.green() for c in colors]),
          np.interp(t, pos, [c.blue() for c in colors])]
    r, g, b = (np.clip(np.round(c), 0, 255).astype(np.uint32) for c in ch)
    return (np.uint32(0xFF000000) | (r << 16) | (g << 8) | b).astype(np.uint32)


def _parse_colors(text: str) -> list[QColor]:
    out = []
    for tok in text.replace(",", " ").split():
        c = QColor(tok)
        if c.isValid():
            out.append(c)
    return out


def _accent_stops(accent: QColor, bg: QColor | None = None) -> list[QColor]:
    """Dark, through the accent, to near white: the ramp of a colour."""
    base = bg if bg is not None else QColor(8, 8, 12)
    return [base, accent.darker(300), accent.darker(160), accent, accent.lighter(150),
            accent.lighter(210), QColor(255, 255, 255)]


# frequency axis scales: Hz to a position and back
def _hz_to_mel(f):   return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)
def _mel_to_hz(m):   return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)
def _hz_to_bark(f):
    f = np.asarray(f, dtype=np.float64)
    return 13.0 * np.arctan(0.00076 * f) + 3.5 * np.arctan((f / 7500.0) ** 2)
def _hz_to_erb(f):   return 21.4 * np.log10(1.0 + 0.00437 * np.asarray(f, dtype=np.float64))
def _erb_to_hz(e):   return (10.0 ** (np.asarray(e, dtype=np.float64) / 21.4) - 1.0) / 0.00437


def _axis(scale: str):
    """(forward, inverse) for a scale. Bark has no closed inverse, so it is tabulated."""
    if scale == "Linear":
        return (lambda f: np.asarray(f, dtype=np.float64)), (lambda x: np.asarray(x, dtype=np.float64))
    if scale == "Mel":
        return _hz_to_mel, _mel_to_hz
    if scale == "ERB":
        return _hz_to_erb, _erb_to_hz
    if scale == "Bark":
        grid = np.geomspace(1.0, 24000.0, 4096)
        bk = _hz_to_bark(grid)
        return _hz_to_bark, (lambda x: np.interp(np.asarray(x, dtype=np.float64), bk, grid))
    return (lambda f: np.log2(np.maximum(np.asarray(f, dtype=np.float64), 1.0))), \
           (lambda x: 2.0 ** np.asarray(x, dtype=np.float64))


def make_window(name: str, n: int, beta: float) -> np.ndarray:
    if name == "Kaiser":
        w = _windows.kaiser(n, beta)
    elif name == "Gaussian":
        w = _windows.gaussian(n, n / 6.0)
    elif name == "Rectangular":
        w = np.ones(n)
    else:
        key = {"Hann": "hann", "Hamming": "hamming", "Blackman": "blackman",
               "Blackman-Harris": "blackmanharris", "Nuttall": "nuttall",
               "Flat top": "flattop"}.get(name, "hann")
        w = _windows.get_window(key, n)
    return w.astype(np.float32)


def clean_config(d: dict) -> dict:
    """Defaults, then d, with every value coerced to its type and kept in range."""
    c = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        if k not in d:
            continue
        v = d[k]
        try:
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = int(round(float(v)))
            elif isinstance(default, float):
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        c[k] = v
    if c["fft"] not in FFT_SIZES:
        c["fft"] = min(FFT_SIZES, key=lambda s: abs(s - c["fft"]))
    c["pad"] = c["pad"] if c["pad"] in (1, 2, 4) else 1
    for k, allowed, fallback in (("window", WINDOWS, "Hann"), ("scale", SCALES, "Log"),
                                 ("cmap", CMAPS, "Magma"), ("direction", DIRECTIONS, "Down"),
                                 ("f_smooth", SMOOTHS, "Off")):
        if c[k] not in allowed:
            c[k] = fallback
    c["tilt"] = min(TILTS, key=lambda t: abs(t - c["tilt"]))
    c["rate"] = max(5, min(240, c["rate"]))
    c["fmin"] = max(10, min(c["fmin"], 20000))
    c["fmax"] = max(c["fmin"] + 100, min(c["fmax"], 24000))
    c["floor"] = max(-140.0, min(-20.0, c["floor"]))
    c["ceil"] = max(c["floor"] + 10.0, min(10.0, c["ceil"]))
    c["gamma"] = max(0.3, min(3.0, c["gamma"]))
    c["bright"] = max(-50, min(50, c["bright"]))
    c["kaiser_beta"] = max(0.5, min(20.0, c["kaiser_beta"]))
    c["auto_release"] = max(1.0, min(60.0, c["auto_release"]))
    c["t_avg"] = max(0, min(1000, c["t_avg"]))
    c["strip"] = max(0, min(50, c["strip"]))
    c["fps"] = max(15, min(241, c["fps"]))
    return c


def _hz_to_u(f):
    """Where a frequency falls on the history's columns (a column's edges are whole numbers)."""
    return np.log2(np.maximum(np.asarray(f, dtype=np.float64), _M_LO) / _M_LO) * (_M / _M_OCT)


def _to_u16(db) -> np.ndarray:
    return np.clip((np.asarray(db, dtype=np.float32) + 200.0) * 100.0 + 0.5, 0, 65535).astype(np.uint16)


def _fname(f: float) -> str:
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


def _tname(s: int) -> str:
    return f"-{s}s" if s < 120 else f"-{s // 60}m"


def _tick_candidates(fmin: float, fmax: float) -> list[tuple[float, int]]:
    """(Hz, tier) the frequency axis may label. Tier 0 is the one to keep when room is short,
    and the numbers follow the range on screen, so a zoomed in view gets finer ticks."""
    out: list[tuple[float, int]] = []
    if fmax / max(fmin, 1.0) >= 3.0:
        for dec in (10, 100, 1000, 10000):
            for m, tier in ((1, 0), (1.5, 2), (2, 1), (3, 2), (4, 2), (5, 1), (6, 2), (8, 2)):
                if fmin <= m * dec <= fmax:
                    out.append((float(m * dec), tier))
    else:
        span = fmax - fmin
        step = 5000
        for s in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
            if span / s <= 12:
                step = s
                break
        f = math.ceil(fmin / step) * step
        while f <= fmax:
            k = round(f / step)
            out.append((float(f), 0 if k % 5 == 0 else (1 if k % 2 == 0 else 2)))
            f += step
    return out


class _ViewMap:
    """How the history's columns become the columns on screen, for one axis and range:
    a column that spans several history columns takes their loudest, a narrower one is
    interpolated, so a zoom in shows what was stored and a zoom out loses no peak."""

    def __init__(self, scale: str, fmin: float, fmax: float, cols: int):
        fwd, inv = _axis(scale)
        lo, hi = float(fwd(fmin)), float(fwd(fmax))
        edges = np.asarray(inv(np.linspace(lo, hi, cols + 1)), dtype=np.float64)
        u = _hz_to_u(edges)
        a = np.clip(np.floor(u[:-1]).astype(np.int64), 0, _M - 1)
        b = np.clip(np.ceil(u[1:]).astype(np.int64), 1, _M)
        self.cols = cols
        self.edges = edges
        self.centres = np.sqrt(np.maximum(edges[:-1], 1.0) * np.maximum(edges[1:], 1.0))
        wide = (b - a) >= 2
        self.w_idx = np.flatnonzero(wide)
        self.n_idx = np.flatnonzero(~wide)
        pairs = np.empty(2 * len(self.w_idx), dtype=np.int64)
        pairs[0::2] = a[self.w_idx]
        pairs[1::2] = np.minimum(b[self.w_idx], _M - 1)
        self.pairs = pairs
        cu = (0.5 * (u[:-1] + u[1:]) - 0.5)[self.n_idx]
        i0 = np.clip(np.floor(cu), 0, _M - 1).astype(np.int64)
        self.i0 = i0
        self.i1 = np.minimum(i0 + 1, _M - 1)
        self.wt = np.clip(cu - i0, 0.0, 1.0).astype(np.float32)
        self.key = None
        self._tilt: tuple[float, np.ndarray] | None = None

    def apply(self, block: np.ndarray) -> np.ndarray:
        """(n, _M) levels to (n, cols) float32, in the units the block was in."""
        out = np.empty((block.shape[0], self.cols), dtype=np.float32)
        if len(self.w_idx):
            out[:, self.w_idx] = np.maximum.reduceat(block, self.pairs, axis=1)[:, ::2]
        if len(self.n_idx):
            x0 = block[:, self.i0].astype(np.float32)
            x1 = block[:, self.i1].astype(np.float32)
            out[:, self.n_idx] = x0 + (x1 - x0) * self.wt
        return out

    def tilt(self, db_per_octave: float) -> np.ndarray:
        if self._tilt is None or self._tilt[0] != db_per_octave:
            t = db_per_octave * np.log2(np.maximum(self.centres, 20.0) / 1000.0)
            self._tilt = (db_per_octave, t.astype(np.float32))
        return self._tilt[1]


# the settings dialog is built from this: (key, label, kind, options)
_SECTIONS = [
    ("FFT", [
        ("fft", "FFT size", "combo", FFT_SIZES),
        ("window", "Window", "combo", WINDOWS),
        ("kaiser_beta", "Kaiser beta", "dspin", (0.5, 20.0, 1, 0.5)),
        ("pad", "Zero padding", "combo", (1, 2, 4)),
        ("rate", "Rows per second", "spin", (5, 240, "")),
    ]),
    ("Frequency", [
        ("scale", "Axis", "combo", SCALES),
        ("fmin", "Lowest", "spin", (10, 2000, " Hz")),
        ("fmax", "Highest", "spin", (1000, 24000, " Hz")),
    ]),
    ("Level", [
        ("floor", "Floor", "dspin", (-140.0, -20.0, 0, 5.0)),
        ("ceil", "Ceiling", "dspin", (-80.0, 10.0, 0, 5.0)),
        ("gamma", "Gamma", "dspin", (0.3, 3.0, 2, 0.05)),
        ("bright", "Brightness", "spin", (-50, 50, " %")),
        ("tilt", "Tilt (dB per octave)", "combo", TILTS),
        ("auto", "Auto range", "check", None),
        ("auto_release", "Auto range release (s)", "dspin", (1.0, 60.0, 1, 1.0)),
    ]),
    ("Smoothing", [
        ("t_avg", "Time averaging", "spin", (0, 1000, " ms")),
        ("f_smooth", "Frequency smoothing", "combo", SMOOTHS),
    ]),
    ("Colour", [
        ("follow", "Follow the app colours", "check", None),
        ("cmap", "Colour map", "combo", CMAPS),
        ("reverse", "Reverse", "check", None),
        ("custom", "Custom colours", "text", None),
    ]),
    ("Display", [
        ("direction", "History goes", "combo", DIRECTIONS),
        ("labels", "Frequency and time labels", "check", None),
        ("legend", "Level legend", "check", None),
        ("grid", "Grid lines", "check", None),
        ("strip_on", "Spectrum strip", "check", None),
        ("strip", "Spectrum strip height", "spin", (5, 50, " %")),
        ("peak", "Peak hold line", "check", None),
    ]),
    ("Speed", [
        ("fps", "Frame rate", "fps", None),
        ("vsync", "V-Sync (follow the display)", "check", None),
    ]),
]


class _ConfigDialog(QDialog):
    """Every setting, applied as it changes, with named presets."""

    def __init__(self, panel: "WaterfallWidget"):
        super().__init__(panel)
        self.setWindowTitle("Waterfall settings")
        self._panel = panel
        self._widgets: dict[str, QWidget] = {}
        self._loading = False
        root = QVBoxLayout(self)

        preset_row = QHBoxLayout()
        self._presets = QComboBox()
        self._presets.setMinimumWidth(150)
        preset_row.addWidget(QLabel("Preset"))
        preset_row.addWidget(self._presets, 1)
        for label, fn in (("Load", self._load_preset), ("Save…", self._save_preset),
                          ("Delete", self._delete_preset)):
            b = QPushButton(label)
            b.setMinimumWidth(76)
            b.clicked.connect(fn)
            preset_row.addWidget(b)
        root.addLayout(preset_row)

        body = QWidget()
        form = QFormLayout(body)
        form.setLabelAlignment(Qt.AlignRight)
        for title, fields in _SECTIONS:
            head = QLabel(title)
            head.setObjectName("settings_section")
            form.addRow(head)
            for key, label, kind, opt in fields:
                w = self._make(key, kind, opt)
                self._widgets[key] = w
                form.addRow(label, w)
        self._widgets["strip_on"].setToolTip("The strip sits above a waterfall whose frequency axis runs across "
                                             "(History goes Down or Up)")
        self._widgets["follow"].setToolTip("Use the album art colours, or the accent when there is no art, "
                                           "whatever colour map is chosen")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        row = QHBoxLayout()
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(reset)
        row.addStretch(1)
        row.addWidget(close)
        root.addLayout(row)
        self.resize(470, 660)
        self._fill()

    def _make(self, key, kind, opt):
        if kind == "combo":
            w = QComboBox()
            for o in opt:
                w.addItem(str(o), o)
            w.currentIndexChanged.connect(lambda _, k=key, w=w: self._set(k, w.currentData()))
        elif kind == "spin":
            w = QSpinBox()
            w.setRange(opt[0], opt[1])
            w.setSuffix(opt[2])
            w.valueChanged.connect(lambda v, k=key: self._set(k, v))
        elif kind == "fps":
            w = _FpsSpin()
            w.setRange(15, 241)
            w.valueChanged.connect(lambda v, k=key: self._set(k, v))
        elif kind == "dspin":
            w = QDoubleSpinBox()
            w.setRange(opt[0], opt[1])
            w.setDecimals(opt[2])
            w.setSingleStep(opt[3])
            w.valueChanged.connect(lambda v, k=key: self._set(k, v))
        elif kind == "check":
            w = QCheckBox()
            w.toggled.connect(lambda v, k=key: self._set(k, v))
        else:
            w = QLineEdit()
            w.setToolTip("Hex colours separated by commas or spaces, dark to bright")
            w.editingFinished.connect(lambda k=key, w=w: self._set(k, w.text()))
        return w

    def _set(self, key, value):
        if not self._loading:
            self._panel.set_option(key, value)
            self._sync_enabled()

    def _sync_enabled(self):
        """Settings that do nothing while another one is set are greyed out."""
        cfg, w = self._panel.config(), self._widgets
        across = cfg["direction"] in ("Down", "Up")
        w["fps"].setEnabled(not cfg["vsync"])
        w["cmap"].setEnabled(not cfg["follow"])
        w["custom"].setEnabled(cfg["cmap"] == "Custom" and not cfg["follow"])
        w["kaiser_beta"].setEnabled(cfg["window"] == "Kaiser")
        w["auto_release"].setEnabled(cfg["auto"])
        w["strip_on"].setEnabled(across)
        w["strip"].setEnabled(across and cfg["strip_on"])

    def _fill(self):
        """Show the panel's current config."""
        self._loading = True
        cfg = self._panel.config()
        for key, w in self._widgets.items():
            v = cfg[key]
            if isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(v)))
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.setValue(v)
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            else:
                w.setText(str(v))
        self._presets.clear()
        for name in sorted(self._panel.presets()):
            self._presets.addItem(name)
        self._loading = False
        self._sync_enabled()

    def _reset(self):
        self._panel.apply_config(dict(DEFAULTS), save=True)
        self._fill()

    def _load_preset(self):
        name = self._presets.currentText()
        if name:
            self._panel.apply_config(self._panel.presets()[name], save=True)
            self._fill()

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Name:")
        if ok and name.strip():
            self._panel.save_preset(name.strip())
            self._fill()
            self._presets.setCurrentText(name.strip())

    def _delete_preset(self):
        name = self._presets.currentText()
        if name:
            self._panel.delete_preset(name)
            self._fill()


class WaterfallWidget(QWidget):
    config_changed = Signal(dict)       # persisted by the main window
    presets_changed = Signal(dict)

    def __init__(self, analyzer: Analyzer, player: Player, parent=None):
        super().__init__(parent)
        # paintEvent fills the whole widget, so the window need not paint the panel frame and the window under it first
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self._analyzer = analyzer
        self._player = player
        self._cfg = clean_config({})
        self._presets: dict[str, dict] = {}
        self._palette: dict | None = None            # album art colours
        self._default_accent = QColor(_DEFAULT_ACCENT)
        self._system_accent = False
        self._suspended = False
        self._frozen = False
        self._hover: QPointF | None = None
        self._dialog: _ConfigDialog | None = None
        self._gcache = None                               # see _geom
        self._label_boxes: list[QRectF] = []              # every text drawn by the last paint

        # the history, newest row at _shead, allocated when the first row arrives
        self._store: np.ndarray | None = None
        self._shead = _STORE_ROWS - 1
        self._sn = 0

        # what is on screen, derived from the history so a zoom reaches the rows already there
        self._img = QImage(8, 8, QImage.Format_RGB32)
        self._arr = None
        self._idx = np.zeros((8, 8), dtype=np.uint8)      # each pixel's level, for a new colour map
        self._ihead = 0                                   # newest row of the picture, older ones follow
        self._img_size = (0, 0)                           # (columns, rows) of the picture
        self._dpr = 1.0
        self._vmap: _ViewMap | None = None

        self._fs = _RATE                                  # sample rate of the audio, see _sync_rate
        self._lut = np.zeros(256, dtype=np.uint32)
        self._prev: np.ndarray | None = None              # last row after averaging, in dB
        self._peak: np.ndarray | None = None
        self._live_db: np.ndarray | None = None           # the newest row, for the strip
        self._silence = np.full(_M, _SILENT_DB, dtype=np.float32)
        self._auto = (self._cfg["floor"], self._cfg["ceil"])
        self._acc = 0.0                                   # fractional rows owed
        self._last_t = time.perf_counter()
        self._last_pos = -1
        self._last_paint = 0.0                            # for V-Sync pacing
        self._marker = False

        self.setMinimumSize(64, 40)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx_menu)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._save_timer = QTimer(self)                   # a wheel zoom changes the range many times a second
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._emit_config)
        self._refit = QTimer(self)                        # a window being dragged resizes many times a second
        self._refit.setSingleShot(True)
        self._refit.setInterval(60)
        self._refit.timeout.connect(self._fit_image)
        self._drop = QTimer(self)                         # a panel hidden for a while gives its history back
        self._drop.setSingleShot(True)
        self._drop.setInterval(60_000)
        self._drop.timeout.connect(self._drop_history)
        power.watch().changed.connect(lambda _: self._sync_timer())
        player.track_changed.connect(lambda _t: setattr(self, "_marker", True))
        self._rebuild_analysis()
        self._lut = self._make_lut()
        self._fit_image()

    # config

    def config(self) -> dict:
        return dict(self._cfg)

    def presets(self) -> dict:
        return dict(self._presets)

    def set_presets(self, presets: dict):
        self._presets = {str(k): clean_config(v) for k, v in (presets or {}).items()
                         if isinstance(v, dict)}

    def save_preset(self, name: str):
        self._presets[name] = dict(self._cfg)
        self.presets_changed.emit(dict(self._presets))

    def delete_preset(self, name: str):
        self._presets.pop(name, None)
        self.presets_changed.emit(dict(self._presets))

    def apply_config(self, d: dict, save: bool = False):
        old = self._cfg
        new = clean_config(d or {})
        if new != old:
            self._cfg = new
            changed = {k for k in new if new[k] != old.get(k)}
            if changed & _ANALYSIS_KEYS:
                self._rebuild_analysis(clear="rate" in changed)
                self._acc = 0.0
            if "auto" in changed and new["auto"]:
                self._auto = (new["floor"], new["ceil"])
            if changed & _COLOUR_KEYS:
                self._lut = self._make_lut()
            redrawn = False
            if changed & _LAYOUT_KEYS:
                self._gcache = None
                redrawn = self._fit_image()              # a new picture if the plot changed size
            if not redrawn:
                if changed & _VIEW_KEYS:
                    self._rebuild_image()
                elif changed & _COLOUR_KEYS:
                    self._apply_lut()
            if changed & _TIMER_KEYS:
                self._sync_timer()
            self.update()
        if save:
            self._save_timer.start()

    def _emit_config(self):
        self.config_changed.emit(self.config())

    def flush(self):
        """Write out a change that is still waiting its turn (the app is closing)."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._emit_config()

    def set_option(self, key: str, value):
        d = dict(self._cfg)
        d[key] = value
        self.apply_config(d, save=True)

    def open_settings(self):
        if self._dialog is None:
            self._dialog = _ConfigDialog(self)
        self._dialog._fill()
        self._dialog.show()
        self._dialog.raise_()

    def _rebuild_analysis(self, clear: bool = False):
        """Tables that depend on the FFT: window, how its bins fall on the history's columns."""
        c = self._cfg
        n = c["fft"]
        self._nfft = n * c["pad"]
        self._win = make_window(c["window"], n, c["kaiser_beta"])
        self._wnorm = np.float32(2.0 / max(1e-9, float(self._win.sum())))
        self._bin_f = np.fft.rfftfreq(self._nfft, 1.0 / self._fs)
        nb = len(self._bin_f)
        self._m_ia = np.clip(np.searchsorted(self._bin_f, _M_EDGES[:-1], "left"), 0, nb - 1)
        self._m_ib = np.clip(np.searchsorted(self._bin_f, _M_EDGES[1:], "left"), 0, nb)
        self._m_wide = (self._m_ib - self._m_ia) >= 2
        self._m_beyond = _M_CENTRES > self._bin_f[-1]          # above what the audio holds
        self._m_smooth = max(1, int(round(_SMOOTH_OCT[c["f_smooth"]] * _M / _M_OCT)))
        self._prev = None
        self._peak = None
        if clear:
            self._clear_history()
        self.update()

    def _make_lut(self) -> np.ndarray:
        c = self._cfg
        name = c["cmap"]
        if c["follow"] or name == "Album art":
            stops = self._art_stops()
        elif name in _MAPS:
            stops = [QColor(t) for t in _MAPS[name].split()]
        elif name == "Accent":
            stops = _accent_stops(self._idle_accent())
        else:
            stops = _parse_colors(c["custom"]) or [QColor("#000000"), QColor("#ffffff")]
        lut = _stops_lut(stops)
        return lut[::-1].copy() if c["reverse"] else lut

    def _art_stops(self) -> list[QColor]:
        p = self._palette
        if p:
            return [QColor(p.get("bg", "#0a0a0f")), QColor(p.get("accent_d", "#20204a")),
                    QColor(p.get("accent", "#6060c0")), QColor(p.get("accent_l", "#a0a0f0")),
                    QColor(255, 255, 255)]
        return _accent_stops(self._idle_accent())

    def _idle_accent(self) -> QColor:
        if self._system_accent:
            c = self.palette().highlight().color()
            if c.isValid():
                return c
        return QColor(self._default_accent)

    def _theme_colours(self) -> tuple[QColor, QColor]:
        """The accent and a lighter one, for what is drawn over the picture (the strip)."""
        p = self._palette
        if p:
            acc = QColor(p.get("accent", _DEFAULT_ACCENT))
            return acc, QColor(p.get("accent_l", acc.lighter(160).name()))
        acc = self._idle_accent()
        return acc, acc.lighter(165)

    # accent colour, like the other visuals

    def _theme_changed(self, picture: bool):
        if picture:
            self._lut = self._make_lut()
            self._apply_lut()
        self.update()

    def set_accent(self, palette: dict | None):
        self._palette = palette or None
        c = self._cfg
        self._theme_changed(c["follow"] or c["cmap"] == "Album art")

    def set_system_accent(self, on: bool):
        self._system_accent = bool(on)
        c = self._cfg
        self._theme_changed(c["follow"] or c["cmap"] in ("Album art", "Accent"))

    def set_default_accent(self, color):
        self._default_accent = QColor(color)
        c = self._cfg
        self._theme_changed((c["follow"] or c["cmap"] in ("Album art", "Accent")) and not self._system_accent)

    # running only while visible

    def set_suspended(self, suspended: bool):
        self._suspended = bool(suspended)
        self._sync_timer()

    def _interval(self) -> int:
        fps = self._cfg["fps"]
        return power.frame_ms(0 if fps > 240 else max(1, round(1000 / fps)))

    def _sync_timer(self):
        if self.isVisible() and not self._suspended:
            # V-Sync draws from paintEvent; the timer is then only a slow watchdog
            self._timer.setInterval(100 if self._cfg["vsync"] else self._interval())
            if not self._timer.isActive():
                self._last_t = time.perf_counter()
                self._timer.start()
            if self._cfg["vsync"]:
                self.update()
        else:
            self._timer.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._drop.stop()
        self._sync_rate()
        self._fit_image()
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_timer()
        self._drop.start()

    def _drop_history(self):
        """Hidden for a minute: the 17 MB history and the picture go, and a fresh one starts when the panel is shown."""
        if self.isVisible() or self._store is None:
            return
        self._store, self._sn = None, 0
        self._prev = self._peak = self._live_db = None
        self._img = QImage(8, 8, QImage.Format_RGB32)
        self._img_size, self._arr, self._vmap = (0, 0), None, None
        self._idx = np.zeros((8, 8), dtype=np.uint8)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.PaletteChange):
            self._gcache = None
            if self._img_size != (0, 0):
                self._refit.start()
            self.update()

    # history and picture

    def _clear_history(self):
        if self._store is not None:
            self._store[:] = 0
        self._sn = 0
        self._prev = None
        self._peak = None
        self._live_db = None
        if self._img_size != (0, 0):
            self._rebuild_image()

    def _store_row(self, row: np.ndarray):
        if self._store is None:
            self._store = np.zeros((_STORE_ROWS, _M), dtype=np.uint16)
        self._shead = (self._shead + 1) % _STORE_ROWS
        self._store[self._shead] = row
        self._sn = min(self._sn + 1, _STORE_ROWS)

    def _recent(self, start: int, n: int) -> np.ndarray:
        """Rows start .. start + n of the history, newest first, as stored."""
        if self._store is None:
            return np.zeros((n, _M), dtype=np.uint16)
        return self._store[(self._shead - start - np.arange(n)) % _STORE_ROWS]

    def _get_vmap(self) -> _ViewMap:
        c = self._cfg
        key = (c["scale"], c["fmin"], c["fmax"], self._img_size[0])
        if self._vmap is None or self._vmap.key != key:
            self._vmap = _ViewMap(c["scale"], float(c["fmin"]), float(c["fmax"]), self._img_size[0])
            self._vmap.key = key
        return self._vmap

    def _levels_range(self) -> tuple[float, float]:
        c = self._cfg
        return self._auto if c["auto"] else (c["floor"], c["ceil"])

    def _level_idx(self, db: np.ndarray) -> np.ndarray:
        """Display dB (tilt included) to the 0..255 colour index."""
        c = self._cfg
        lo, hi = self._levels_range()
        v = np.clip((db - lo) / max(1.0, hi - lo), 0.0, 1.0)
        if c["gamma"] != 1.0:
            v = v ** c["gamma"]
        if c["bright"]:
            v = np.clip(v + c["bright"] / 100.0, 0.0, 1.0)
        return (v * 255.0 + 0.5).astype(np.uint8)

    def _rows(self) -> np.ndarray:
        if self._arr is None:
            bits = self._img.bits()
            stride = self._img.bytesPerLine() // 4
            self._arr = np.ndarray((self._img.height(), stride), dtype=np.uint32, buffer=bits)
        return self._arr

    def _fit_image(self) -> bool:
        """The picture the rows are drawn into: one column per frequency pixel, one row per
        time pixel, whichever way round that is on screen. Returns True when it was redrawn
        from the history, which is what keeps it across a resize."""
        self._refit.stop()
        dpr = self.devicePixelRatioF()
        plot = self._geom().plot
        horiz = self._cfg["direction"] in ("Left", "Right")
        cols = int(plot.height() * dpr) if horiz else int(plot.width() * dpr)
        rows = int(plot.width() * dpr) if horiz else int(plot.height() * dpr)
        cols, rows = max(8, min(_M, cols)), max(8, min(_STORE_ROWS, rows))
        if (cols, rows) == self._img_size and dpr == self._dpr:
            return False
        self._img = QImage(cols, rows, QImage.Format_RGB32)
        self._img.fill(QColor(0, 0, 0))
        self._img.setDevicePixelRatio(1.0)
        self._img_size, self._dpr = (cols, rows), dpr
        self._arr = None
        self._idx = np.zeros((rows, cols), dtype=np.uint8)
        self._vmap = None
        self._rebuild_image()
        self.update()
        return True

    def _rebuild_image(self):
        """Draw the picture again from the history, for the axis, range and levels now set."""
        cols, rows = self._img_size
        if cols == 0:
            return
        vm = self._get_vmap()
        tilt = vm.tilt(self._cfg["tilt"])
        for s in range(0, rows, 256):
            n = min(256, rows - s)
            db = vm.apply(self._recent(s, n)) * np.float32(0.01) - np.float32(200.0) + tilt
            self._idx[s:s + n] = self._level_idx(db)
        self._ihead = 0
        self._apply_lut()

    def _apply_lut(self):
        cols = self._img_size[0]
        if cols:
            self._rows()[:, :cols] = self._lut[self._idx]
            self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._gcache = None
        if self._img_size == (0, 0) or not self.isVisible():
            self._fit_image()
        else:
            self._refit.start()                  # until then the picture is stretched to the new size

    # the audio

    def _tick(self):
        if self._cfg["vsync"]:
            # paintEvent advances and paces itself; this only starts it again after it went idle
            if time.perf_counter() - self._last_paint > 0.25 and self._player.is_playing:
                self.update()
            return
        if self._advance_safe():
            self.update()           # a stopped or paused picture stays as it is: no repaint sixty times a second

    def _advance_safe(self) -> bool:
        """True when the picture changed."""
        try:
            return bool(self._advance())
        except Exception:
            from applog import log
            log.exception("waterfall frame failed")
            return False

    def _sync_rate(self):
        """The engine plays at the output device's rate, which is not always the analyzer's."""
        fs = _RATE
        if getattr(self._analyzer, "_engine_src", None) is not None:
            fs = int(getattr(self._player, "sample_rate", 0) or _RATE)
        if fs != self._fs:
            self._fs = fs
            self._rebuild_analysis()

    def _advance(self):
        self._sync_rate()
        now = time.perf_counter()
        dt, self._last_t = now - self._last_t, now
        if self._frozen or not self._player.is_playing:
            self._acc = 0.0
            self._last_pos = -1
            return False
        c = self._cfg
        rate = c["rate"]
        self._acc += dt * rate
        n = int(self._acc)
        self._acc -= n
        if n <= 0:
            return False
        pos = self._player.position_ms
        if self._last_pos >= 0 and (pos < self._last_pos - 200 or pos > self._last_pos + 1500 + dt * 1000):
            self._marker = True                    # a seek or a jump: show where the picture breaks
        self._last_pos = pos
        live = getattr(self._analyzer, "_live", False)
        n = min(n, 1 if live else _MAX_ROWS_PER_FRAME)
        step = 1000.0 / rate
        span_ms = c["fft"] * 1000.0 / self._fs
        for k in range(n):
            t_ms = pos - (n - 1 - k) * step
            win = self._analyzer.get_audio_window(int(max(0, t_ms - span_ms)), c["fft"])
            if win is None or len(win) < c["fft"]:
                self._write_row(self._silence)
                continue
            self._write_row(self._spectrum(win))
        return True

    def _spectrum(self, win: np.ndarray) -> np.ndarray:
        """One FFT as dB on the history's columns."""
        x = win[:self._cfg["fft"]].astype(np.float32, copy=False) * self._win
        mag = np.abs(np.fft.rfft(x, n=self._nfft)) * self._wnorm
        return self._to_master(20.0 * np.log10(mag + 1e-9))

    def _to_master(self, db: np.ndarray) -> np.ndarray:
        interp = np.interp(_M_CENTRES, self._bin_f, db)
        if self._m_wide.any():
            end = int(max(self._m_ib[-1], self._m_ia[-1] + 1))
            pooled = np.maximum.reduceat(db[:end], np.minimum(self._m_ia, end - 1))
            interp = np.where(self._m_wide, pooled, interp)
        interp[self._m_beyond] = _SILENT_DB
        k = self._m_smooth
        if k > 1:
            pad = np.pad(interp, (k // 2, k - 1 - k // 2), mode="edge")
            cs = np.concatenate(([0.0], np.cumsum(pad, dtype=np.float64)))
            interp = ((cs[k:] - cs[:-k]) / k)[:_M]
        return interp.astype(np.float32)

    def _write_row(self, db: np.ndarray):
        c = self._cfg
        a = math.exp(-(1.0 / c["rate"]) / (c["t_avg"] / 1000.0)) if c["t_avg"] > 0 else 0.0
        if self._prev is None or len(self._prev) != len(db):
            self._prev = db.copy()
        else:
            db = a * self._prev + (1.0 - a) * db
            self._prev = db
        self._live_db = db
        if c["peak"]:
            fall = 40.0 / c["rate"]                            # 40 dB a second
            self._peak = db.copy() if self._peak is None or len(self._peak) != len(db) \
                else np.maximum(db, self._peak - fall)
        mark, self._marker = self._marker, False
        self._store_row(np.full(_M, _MARK_U16, dtype=np.uint16) if mark else _to_u16(db))
        if self._img_size == (0, 0):
            return
        cols = self._img_size[0]
        if mark:
            self._push_idx(np.full(cols, 255, dtype=np.uint8))
            return
        vm = self._get_vmap()
        view = vm.apply(db[None, :])[0] + vm.tilt(c["tilt"])
        if c["auto"]:
            top, bottom = float(np.percentile(view, 99.5)), float(np.percentile(view, 10))
            alo, ahi = self._auto
            up = 0.5
            down = 1.0 - math.exp(-(1.0 / c["rate"]) / c["auto_release"])
            ahi += (top - ahi) * (up if top > ahi else down)
            alo += (bottom - alo) * (up if bottom < alo else down)
            self._auto = (alo, max(ahi, alo + 20.0))
        self._push_idx(self._level_idx(view))

    def _push_idx(self, idx: np.ndarray):
        cols, rows = self._img_size
        self._ihead = (self._ihead - 1) % rows
        self._idx[self._ihead, :cols] = idx
        self._rows()[self._ihead, :cols] = self._lut[idx]

    # layout

    def _label_px(self, height: int) -> int:
        base = QFontInfo(self.font()).pixelSize() or 12
        px = max(8, min(11, base - 2))
        return min(px, 9) if height < 140 else px

    def _geom(self) -> SimpleNamespace:
        """Where everything goes at this size and with these settings. Labels, legend and strip
        give way, the least important first, when the panel is too small to hold them."""
        c = self._cfg
        W, H = self.width(), self.height()
        f = self.font()
        key = (W, H, c["direction"], c["labels"], c["legend"], c["strip_on"], c["strip"], c["fmin"], c["fmax"],
               f.family(), f.pixelSize(), f.pointSizeF())
        g = self._gcache
        if g is not None and g.key == key:
            return g
        horiz = c["direction"] in ("Left", "Right")
        font = QFont(f)
        font.setPixelSize(self._label_px(H))
        fm = QFontMetrics(font)
        labels, legend, ltext = bool(c["labels"]), bool(c["legend"]), bool(c["legend"]) and H >= 96
        strip_on = bool(c["strip_on"] and c["strip"] and not horiz)
        names = [_fname(f_) for f_, _ in _tick_candidates(c["fmin"], c["fmax"])] or ["20k"]
        while True:
            left = (max(fm.horizontalAdvance(n) for n in names) + 8) if (labels and horiz) else 0
            bottom = (fm.height() + 4) if (labels and not horiz) else 0
            right = (3 + 8 + 3 + (fm.horizontalAdvance("-140") + 3 if ltext else 0)) if legend else 0
            strip_h = int(max(0, H - bottom) * c["strip"] / 100.0) if strip_on else 0
            if strip_h and H - bottom - strip_h < 48:
                strip_h = 0
            pw, ph = W - left - right, H - strip_h - bottom
            if (pw >= 56 and ph >= 36) or not (ltext or legend or labels or strip_on):
                break
            if ltext:
                ltext = False
            elif legend:
                legend = False
            elif labels:
                labels = False
            else:
                strip_on = False
        bar = None
        if legend and H - bottom >= 36:
            bar = QRectF(W - right + 3, 4, 8, H - bottom - 8)
        g = SimpleNamespace(key=key, font=font, fm=fm, labels=labels, legend=legend and bar is not None,
                            legend_text=ltext and bar is not None, strip_h=strip_h, bar=bar,
                            plot=QRectF(left, strip_h, max(1, pw), max(1, ph)))
        self._gcache = g
        return g

    def _hz_to_pos(self, f) -> np.ndarray:
        fwd, _ = _axis(self._cfg["scale"])
        lo, hi = float(fwd(self._cfg["fmin"])), float(fwd(self._cfg["fmax"]))
        return (np.asarray(fwd(f), dtype=np.float64) - lo) / (hi - lo)

    # painting

    def paintEvent(self, _):
        vsync = self._cfg["vsync"]
        if vsync:
            self._last_paint = time.perf_counter()
            self._advance_safe()
        self._label_boxes = []
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().base())
        c = self._cfg
        g = self._geom()
        plot = g.plot
        self._paint_waterfall(p, plot)
        if g.strip_h:
            self._paint_strip(p, QRectF(plot.left(), 0, plot.width(), g.strip_h))
        if c["grid"] or g.labels:
            self._paint_axes(p, g)
        if g.legend:
            self._paint_legend(p, g)
        tag = self._paint_hover(p, g) if self._hover is not None and plot.contains(self._hover) else None
        if self._frozen:
            self._paint_frozen(p, g, tag)
        p.end()
        if vsync:
            self._next_frame()

    def _next_frame(self):
        """V-Sync: ask for the next frame one display refresh after this one, and only while
        there is something to animate."""
        if not self.isVisible() or self._suspended or not self._player.is_playing:
            return                                       # idle: the watchdog tick restarts it
        scr = self.screen()
        rate = max(24.0, scr.refreshRate() if scr is not None else 60.0)
        gap = max(0.98 / rate, power.frame_ms(0) / 1000.0)
        wait = self._last_paint + gap - time.perf_counter()
        if wait <= 0.0005:
            self.update()
        else:
            QTimer.singleShot(int(wait * 1000), self.update)

    def _paint_waterfall(self, p: QPainter, plot: QRectF):
        cols, rows = self._img_size
        if cols == 0:
            return
        direction = self._cfg["direction"]
        head = self._ihead
        p.save()
        if direction in ("Left", "Right"):
            p.translate(plot.left(), plot.bottom())
            p.rotate(-90)
            tw, th = plot.height(), plot.width()
        else:
            p.translate(plot.left(), plot.top())
            tw, th = plot.width(), plot.height()
        if direction in ("Up", "Left"):                # the newest row goes last
            p.translate(0, th)
            p.scale(1, -1)
        sy = th / rows
        y = 0.0
        for r0, r1 in ((head, rows), (0, head)):       # newest to oldest, around the ring
            if r1 <= r0:
                continue
            h = (r1 - r0) * sy
            p.drawImage(QRectF(0, y, tw, h), self._img, QRectF(0, r0, cols, r1 - r0))
            y += h
        p.restore()

    def _label(self, p: QPainter, g, text: str, x: float, cy: float, align: str, color: QColor,
               halo: bool = False):
        """Text with its vertical centre at cy; x is its left edge, right edge or centre. It is kept
        inside the panel, so a label that would be cut off slides in instead."""
        tw, h = g.fm.horizontalAdvance(text), g.fm.height()
        left = x if align == "left" else (x - tw if align == "right" else x - tw / 2)
        left = min(max(left, 0.0), max(0.0, self.width() - tw))
        top = min(max(cy - h / 2, 0.0), max(0.0, self.height() - h))
        base = QPointF(left, top + g.fm.ascent())
        if halo:
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(base + QPointF(1, 1), text)
        p.setPen(color)
        p.drawText(base, text)
        self._label_boxes.append(QRectF(left, top, tw, h))

    def _paint_strip(self, p: QPainter, r: QRectF):
        db = self._live_db
        if db is None or r.height() < 6:
            return
        c = self._cfg
        vm = self._get_vmap()
        tilt = vm.tilt(c["tilt"])
        live = vm.apply(db[None, :])[0] + tilt
        lo, hi = self._levels_range()
        accent, accent_l = self._theme_colours()
        h = r.height()

        def pts(values):
            xs = np.linspace(r.left(), r.right(), len(values))
            ys = r.bottom() - np.clip((values - lo) / max(1.0, hi - lo), 0, 1) * (h - 2)
            return xs, ys

        def runs(values):
            """The trace as lines, leaving out what sits on the floor so no line lies along the base."""
            xs, ys = pts(values)
            on = values > lo + 0.5
            out, cur = [], []
            for x, y, ok in zip(xs[::2], ys[::2], on[::2]):
                if ok:
                    cur.append(QPointF(float(x), float(y)))
                elif cur:
                    out.append(cur)
                    cur = []
            if cur:
                out.append(cur)
            return [r_ for r_ in out if len(r_) > 1]

        xs, ys = pts(live)
        line = [QPointF(float(x), float(y)) for x, y in zip(xs[::2], ys[::2])]
        fill = QColor(accent)
        fill.setAlpha(80)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPolygon([QPointF(r.left(), r.bottom())] + line + [QPointF(r.right(), r.bottom())])
        p.setPen(QPen(accent_l, 1.2))
        p.setBrush(Qt.NoBrush)
        for seg in runs(live):
            p.drawPolyline(seg)
        if c["peak"] and self._peak is not None:
            peak = vm.apply(self._peak[None, :])[0] + tilt
            pen = QColor(accent_l)
            pen.setAlpha(210)
            p.setPen(QPen(pen, 1.0))
            for seg in runs(peak):
                p.drawPolyline(seg)
        base = self.palette().text().color()
        base.setAlpha(40)
        p.setPen(base)
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))

    def _tick_plan(self, g, horiz: bool) -> list[tuple]:
        """(position, major, name, label span or None, draw a line) along the frequency axis. A label
        that would overlap another is left out and one that would run off an end slides in, so
        however small the panel nothing clashes or is cut."""
        c, plot, fm = self._cfg, g.plot, g.fm
        extent = plot.height() if horiz else plot.width()
        items = []
        for f, tier in _tick_candidates(c["fmin"], c["fmax"]):
            t = float(self._hz_to_pos(f))
            if 0.0 <= t <= 1.0:
                items.append((tier, (1.0 - t) * extent if horiz else t * extent, f))
        items.sort(key=lambda it: (it[0], it[1]))
        pad = 2.0 if horiz else 8.0
        taken: list[tuple[float, float]] = []
        lines: list[float] = []
        out = []
        for tier, pos, f in items:
            name = _fname(f)
            span = None
            if g.labels:
                size = fm.height() if horiz else fm.horizontalAdvance(name)
                s0, s1 = pos - size / 2, pos + size / 2
                shift = -s0 if s0 < 0 else (extent - s1 if s1 > extent else 0.0)
                if abs(shift) <= size / 2:
                    s0, s1 = s0 + shift, s1 + shift
                    if all(s1 + pad <= a or s0 >= b + pad for a, b in taken):
                        span = (s0, s1)
                        taken.append(span)
            line = bool(c["grid"]) and (span is not None
                                        or (tier <= 1 and all(abs(pos - q) >= 8 for q in lines)))
            if line:
                lines.append(pos)
            if span is not None or line:
                out.append((pos, tier == 0, name, span, line))
        return out

    def _paint_axes(self, p: QPainter, g):
        c, plot = self._cfg, g.plot
        horiz = c["direction"] in ("Left", "Right")
        text = self.palette().text().color()
        grid = QColor(text)
        grid.setAlpha(34)
        label = QColor(text)
        label.setAlpha(170)
        p.setFont(g.font)
        for pos, major, name, span, line in self._tick_plan(g, horiz):
            if line:
                p.setPen(QPen(grid, 1.6 if major else 1.0))
                if horiz:
                    y = plot.top() + pos
                    p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                else:
                    x = plot.left() + pos
                    p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            if span is not None:
                mid = (span[0] + span[1]) / 2
                if horiz:
                    self._label(p, g, name, plot.left() - 4, plot.top() + mid, "right", label)
                else:
                    self._label(p, g, name, plot.left() + mid, plot.bottom() + 2 + g.fm.height() / 2,
                                "center", label)
        self._paint_time(p, g, grid, label)

    def _paint_time(self, p: QPainter, g, grid: QColor, label: QColor):
        """Lines every few seconds back from the newest row, labelled where there is room."""
        c, plot, fm = self._cfg, g.plot, g.fm
        horiz = c["direction"] in ("Left", "Right")
        extent = plot.width() if horiz else plot.height()
        px_per_s = c["rate"] * extent / max(1, self._img_size[1])
        span = extent / px_per_s
        need = (fm.horizontalAdvance("-120s") + 16) if horiz else (fm.height() + 6)
        step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300) if s * px_per_s >= need), None)
        if step is None:
            return
        newest_first = c["direction"] in ("Down", "Right")      # the newest row is at the top or left
        s = step
        while s < span:
            d = s * px_per_s
            name = _tname(s)
            tw = fm.horizontalAdvance(name)
            if horiz:
                x = plot.left() + d if newest_first else plot.right() - d
                if c["grid"]:
                    p.setPen(grid)
                    p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                if g.labels:
                    if x + 3 + tw <= plot.right() - 2:
                        self._label(p, g, name, x + 3, plot.top() + 2 + fm.height() / 2, "left", label, True)
                    elif x - 3 - tw >= plot.left() + 2:
                        self._label(p, g, name, x - 3, plot.top() + 2 + fm.height() / 2, "right", label, True)
            else:
                y = plot.top() + d if newest_first else plot.bottom() - d
                if c["grid"]:
                    p.setPen(grid)
                    p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                if g.labels and tw + 8 <= plot.width():
                    cy = y + 1 + fm.height() / 2 if newest_first else y - 1 - fm.height() / 2
                    if plot.top() <= cy - fm.height() / 2 and cy + fm.height() / 2 <= plot.bottom():
                        self._label(p, g, name, plot.left() + 4, cy, "left", label, True)
            s += step

    def _paint_legend(self, p: QPainter, g):
        c = self._cfg
        bar = g.bar
        grad = QLinearGradient(0, bar.top(), 0, bar.bottom())
        for i in range(0, 256, 17):
            col = int(self._lut[255 - i])
            grad.setColorAt(i / 255.0, QColor((col >> 16) & 255, (col >> 8) & 255, col & 255))
        col = int(self._lut[0])
        grad.setColorAt(1.0, QColor((col >> 16) & 255, (col >> 8) & 255, col & 255))
        p.fillRect(bar, grad)
        if not g.legend_text:
            return
        text = self.palette().text().color()
        text.setAlpha(170)
        p.setFont(g.font)
        lo, hi = self._levels_range()
        x = bar.right() + 3
        h = g.fm.height()
        self._label(p, g, f"{hi:.0f}", x, bar.top() + h / 2 - 1, "left", text)
        self._label(p, g, f"{lo:.0f}", x, bar.bottom() - h / 2 + 1, "left", text)

    def _hover_info(self, plot: QRectF):
        """Frequency, level and age under the pointer; the level is None where nothing was stored."""
        c = self._cfg
        horiz = c["direction"] in ("Left", "Right")
        pos = self._hover
        t = (plot.bottom() - pos.y()) / plot.height() if horiz else (pos.x() - plot.left()) / plot.width()
        fwd, inv = _axis(c["scale"])
        lo, hi = float(fwd(c["fmin"])), float(fwd(c["fmax"]))
        freq = float(inv(lo + min(1.0, max(0.0, t)) * (hi - lo)))
        rows = self._img_size[1]
        extent = plot.width() if horiz else plot.height()
        along = (pos.x() - plot.left()) if horiz else (pos.y() - plot.top())
        if c["direction"] in ("Up", "Left"):
            along = extent - along
        age = int(max(0, min(rows - 1, along / extent * rows)))
        db = None
        if self._store is not None and age < self._sn:
            col = int(min(_M - 1, max(0, float(_hz_to_u(freq)))))
            v = float(self._store[(self._shead - age) % _STORE_ROWS, col]) * 0.01 - 200.0
            if v < 100.0:                                  # a marker row has no level
                db = v + c["tilt"] * math.log2(max(freq, 20.0) / 1000.0)
        return freq, db, age / c["rate"]

    def _paint_hover(self, p: QPainter, g) -> QRectF | None:
        """The guide line and a read out kept inside the plot; returns where the read out is."""
        plot = g.plot
        freq, db, age = self._hover_info(plot)
        guide = QColor(255, 255, 255, 90)
        p.setPen(guide)
        if self._cfg["direction"] in ("Left", "Right"):
            p.drawLine(QPointF(plot.left(), self._hover.y()), QPointF(plot.right(), self._hover.y()))
        else:
            p.drawLine(QPointF(self._hover.x(), plot.top()), QPointF(self._hover.x(), plot.bottom()))
        f_txt = f"{freq / 1000:.2f} kHz" if freq >= 1000 else f"{freq:.0f} Hz"
        level = f"{db:.0f} dB" if db is not None else "no data"
        fm = g.fm
        fits = [t for t in (f"{f_txt}   {level}   {age:.1f} s ago", f"{f_txt}   {level}", f_txt)
                if fm.horizontalAdvance(t) + 12 <= plot.width() - 4]
        if not fits or plot.height() < fm.height() + 10:
            return None
        text = fits[0]
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 6
        x = min(max(self._hover.x() + 12, plot.left() + 2), plot.right() - w - 2)
        y = min(max(self._hover.y() - h - 6, plot.top() + 2), plot.bottom() - h - 2)
        box = self.palette().window().color()
        box.setAlpha(225)
        p.setPen(Qt.NoPen)
        p.setBrush(box)
        r = QRectF(x, y, w, h)
        p.drawRoundedRect(r, 4, 4)
        p.setFont(g.font)
        self._label(p, g, text, x + w / 2, y + h / 2, "center", self.palette().text().color())
        return r

    def _paint_frozen(self, p: QPainter, g, avoid: QRectF | None):
        """A small tag in the bottom corner, left out when the read out is there or there is no room."""
        plot, fm = g.plot, g.fm
        w, h = fm.horizontalAdvance("Frozen") + 12, fm.height() + 4
        if plot.width() < w + 24 or plot.height() < h + 24:
            return
        r = QRectF(plot.right() - w - 4, plot.bottom() - h - 4, w, h)
        if avoid is not None and r.intersects(avoid):
            return
        box = self.palette().window().color()
        box.setAlpha(210)
        p.setPen(Qt.NoPen)
        p.setBrush(box)
        p.drawRoundedRect(r, 4, 4)
        p.setFont(g.font)
        self._label(p, g, "Frozen", r.center().x(), r.center().y(), "center", self.palette().text().color())

    # pointer

    def mouseMoveEvent(self, e):
        self._hover = e.position()
        self.update()

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._frozen = not self._frozen
            self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._frozen = not self._frozen           # the first press of the pair toggled it already
            d = dict(self._cfg)
            d["fmin"], d["fmax"] = DEFAULTS["fmin"], DEFAULTS["fmax"]
            self.apply_config(d, save=True)

    def wheelEvent(self, e):
        """Zoom the frequency axis about the pointer, or pan it with Shift. What is already on
        screen zooms with it, because the picture is drawn again from the history."""
        plot = self._geom().plot
        if not plot.contains(e.position()):
            return
        c = self._cfg
        delta = e.angleDelta().y() or e.angleDelta().x()
        if not delta:
            return
        horiz = c["direction"] in ("Left", "Right")
        t = ((plot.bottom() - e.position().y()) / plot.height() if horiz
             else (e.position().x() - plot.left()) / plot.width())
        fwd, inv = _axis(c["scale"])
        lo, hi = float(fwd(c["fmin"])), float(fwd(c["fmax"]))
        if e.modifiers() & Qt.ShiftModifier:
            shift = (hi - lo) * 0.1 * (1 if delta > 0 else -1)
            nlo, nhi = lo + shift, hi + shift
        else:
            k = 0.85 ** (delta / 120.0)
            centre = lo + t * (hi - lo)
            nlo, nhi = centre - (centre - lo) * k, centre + (hi - centre) * k
        fmin = max(10.0, float(inv(nlo)))
        fmax = min(min(24000.0, self._fs / 2.0), float(inv(nhi)))
        if fmax - fmin < 100.0:
            return                                      # as far in as the settings allow
        d = dict(c)
        d["fmin"], d["fmax"] = fmin, fmax
        self.apply_config(d, save=True)
        e.accept()

    def _ctx_menu(self, pos):
        m = QMenu(self)
        m.addAction("Resume" if self._frozen else "Freeze", lambda: setattr(self, "_frozen", not self._frozen))
        m.addAction("Clear", self.clear)
        m.addSeparator()
        c = self._cfg

        def toggle(label, key):
            a = m.addAction(label, lambda: self.set_option(key, not self._cfg[key]))
            a.setCheckable(True)
            a.setChecked(bool(c[key]))

        toggle("Follow the app colours", "follow")
        cm = m.addMenu("Colour map")
        for name in CMAPS:
            a = cm.addAction(name, lambda n=name: self.set_option("cmap", n))
            a.setCheckable(True)
            a.setChecked(name == c["cmap"])
        dm = m.addMenu("History goes")
        for name in DIRECTIONS:
            a = dm.addAction(name, lambda n=name: self.set_option("direction", n))
            a.setCheckable(True)
            a.setChecked(name == c["direction"])
        m.addSeparator()
        toggle("Spectrum strip", "strip_on")
        toggle("Labels", "labels")
        toggle("Legend", "legend")
        toggle("Grid", "grid")
        toggle("Peak hold", "peak")
        toggle("V-Sync", "vsync")
        if self._presets:
            pm = m.addMenu("Presets")
            for name in sorted(self._presets):
                pm.addAction(name, lambda n=name: self.apply_config(self._presets[n], save=True))
        m.addSeparator()
        m.addAction("Settings…", self.open_settings)
        m.exec(self.mapToGlobal(pos))

    def clear(self):
        self._clear_history()
        self.update()
