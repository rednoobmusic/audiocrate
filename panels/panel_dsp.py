"""DSP panel: the module chain as a list, an Add menu, presets, and the controls of the selected module.

It talks to a DspChain only through that class's public methods and builds every control from the module's Param list,
so a new module needs no panel code. Nothing here keeps a module object: the chain may rebuild its modules (a new
output rate), so each read goes through chain.module() again. A module with many bands (see _band_keys) and the tone
control get a small copy of the Equalizer window (mini_eq) in place of the response curve, and the bands have no
sliders. The one thing read from inside the chain is the limiter's state for the light, until stats() carries limiter_db.
"""
from __future__ import annotations

import inspect
import json
import math
import re
import time
from pathlib import Path

import numpy as np

from PySide6.QtCore import QEvent, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget,
)

import dsp_base
import dsp_presets
import plat_fs
import plat_paths
from library_widgets import Scheme, scaled_font
from mini_eq import MiniEq, handles_of
from panel_eq import _Flow

_WIDE = 520             # at this width and above the list sits left of the controls
_CELL_W = 250           # one column of controls needs about this much room
_CONTROL_H = 26         # sliders, lists and path fields all stand this high, so a row of them lines up
_CURVE_FREQS = np.geomspace(20.0, 20000.0, 160)
_FILE_EXTS = ("wav", "flac", "ogg", "mp3", "aiff", "aif", "opus", "m4a")


def _state_file() -> Path:
    return plat_paths.config_dir() / "dsp_ui.json"


# how a setting maps onto a slider
def _decimals(p) -> int:
    if p.kind == "int":
        return 0
    if p.step:
        return max(0, min(3, math.ceil(-math.log10(p.step) - 1e-9)))
    return -1                       # by magnitude, see fmt_value


def fmt_value(p, v) -> str:
    """The value as the readout shows it, with its unit."""
    unit = p.unit
    d = _decimals(p)
    if unit == "Hz" and abs(v) >= 1000:
        v, unit, d = v / 1000.0, "kHz", 2
    elif d < 0:
        d = 0 if abs(v) >= 100 else 1 if abs(v) >= 10 else 2
    s = "%.*f" % (d, v)
    if "." in s and (p.step is None or unit == "kHz"):
        s = s.rstrip("0").rstrip(".")
    if unit == "dB" and v > 0:
        s = "+" + s
    if not unit:
        return s
    return s + unit if unit == "%" or unit.startswith(":") else "%s %s" % (s, unit)


_BAND_KEY = re.compile(r"g(?:10|31)_")


def _band_keys(m) -> list:
    """The settings that are one band each: what the module's own band_keys() says, else the float settings whose
    hint names a layout, else keys that start with g10_ or g31_. Empty when the module has no bands."""
    by = {p.key: p for p in m.params}
    f = getattr(m, "band_keys", None)
    if callable(f):
        try:
            keys = [k for k in f() if k in by and by[k].kind in ("float", "int")]
        except Exception:
            keys = []
        if len(keys) >= 3:
            return keys
    ranged = [p for p in m.params if p.kind == "float" and p.lo is not None and p.hi is not None]
    for pick in (lambda p: p.hint, lambda p: _BAND_KEY.match(p.key)):
        keys = [p.key for p in ranged if pick(p)]
        if len(keys) >= 5:
            return keys
    return []


class _Axis:
    """Slider positions 0..n for a float or int setting, and back."""

    def __init__(self, p):
        self.p = p
        self.lo, self.hi = float(p.lo), float(p.hi)
        self.log = p.scale == "log" and self.lo > 0 and self.hi > self.lo
        step = max(1.0, round(p.step or 1.0)) if p.kind == "int" else p.step
        self.step = step
        if self.log:
            self.n = 1000
        else:
            self.n = max(1, min(4000, round((self.hi - self.lo) / step) if step else 1000))

    def pos(self, v) -> int:
        span = self.hi - self.lo
        if self.log:
            t = math.log(min(self.hi, max(self.lo, v)) / self.lo) / math.log(self.hi / self.lo)
        else:
            t = (min(self.hi, max(self.lo, v)) - self.lo) / span if span else 0.0
        return round(t * self.n)

    def value(self, pos):
        t = pos / self.n
        if self.log:
            v = self.lo * (self.hi / self.lo) ** t
            if self.step:
                v = round(v / self.step) * self.step
        else:
            v = self.lo + (self.hi - self.lo) * t
        if self.p.kind == "int":
            v = self.lo + round((v - self.lo) / self.step) * self.step
        elif self.step:
            v = round(v, max(0, _decimals(self.p)) + 1)
        else:
            v = float("%.4g" % v)               # no step: keep saved values short
        return self.p.clamp(v)


class _ParamSlider(QSlider):
    """Plain wheel scrolls the panel past it; Ctrl and wheel moves one step. Double or right click resets."""
    reset = Signal()

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            d = e.angleDelta().y() or e.angleDelta().x()
            if d:
                self.setValue(self.value() + (1 if d > 0 else -1))
            e.accept()
        else:
            e.ignore()

    def mouseDoubleClickEvent(self, e):
        self.reset.emit()
        e.accept()

    def contextMenuEvent(self, e):
        self.reset.emit()
        e.accept()


def _file_filter(hint: str) -> str:
    """A file dialog filter from the setting's hint, which names the kinds of file ("WAV and FLAC files")."""
    pats = re.findall(r"\*\.\w+", hint or "")
    if not pats:
        pats = ["*." + w.lower() for w in re.findall(r"[A-Za-z0-9]+", hint or "") if w.lower() in _FILE_EXTS]
    if not pats:
        return "All files (*)"
    label = re.sub(r"\s*\(.*?\)", "", re.sub(r"\*\.\w+", "", hint)).strip(" ,:;") or "Files"
    return "%s (%s);;All files (*)" % (label, " ".join(dict.fromkeys(pats)))


class _Cell(QWidget):
    """One setting: its label, the control and, for sliders, the readout. changed(key, value) fires on user edits only."""
    changed = Signal(str, object)

    def __init__(self, p, value):
        super().__init__()
        self.p = p
        self.in_use = True              # False while the module says this setting does nothing now
        self._axis = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        if p.kind in ("float", "int") and p.lo is not None and p.hi is not None:
            self._axis = _Axis(p)
            self._read = QLabel()
            head = QHBoxLayout()
            head.setSpacing(6)
            self._name = QLabel(p.label)
            self._read.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            head.addWidget(self._name, 1)
            head.addWidget(self._read)
            self._w = _ParamSlider()
            self._w.setRange(0, self._axis.n)
            self._w.setSingleStep(1)
            self._w.setPageStep(max(1, self._axis.n // 10))
            self._w.setFixedHeight(_CONTROL_H)
            lay.addLayout(head)
            lay.addWidget(self._w)
            self._w.setAccessibleName(p.label)
            self._w.setToolTip("Double click or right click to reset. Ctrl and wheel for fine steps.")
            self._w.valueChanged.connect(self._on_slider)
            self._w.reset.connect(self._on_reset)
        elif p.kind == "bool":
            self._w = QCheckBox(p.label)
            self._w.toggled.connect(lambda on: self.changed.emit(p.key, bool(on)))
            lay.addWidget(self._w)
        elif p.kind == "choice":
            lay.addWidget(QLabel(p.label))
            self._w = QComboBox()
            self._w.setFixedHeight(_CONTROL_H)
            self._w.addItems([str(c) for c in p.choices])
            self._w.setAccessibleName(p.label)
            self._w.currentTextChanged.connect(lambda t: self.changed.emit(p.key, t))
            lay.addWidget(self._w)
        elif p.kind == "file":
            lay.addWidget(QLabel(p.label))
            row = QHBoxLayout()
            row.setSpacing(4)
            self._w = QLineEdit()
            self._w.setFixedHeight(_CONTROL_H)
            self._w.setPlaceholderText("No file")
            self._w.setAccessibleName(p.label)
            self._w.setToolTip(p.hint)
            self._w.editingFinished.connect(lambda: self.changed.emit(p.key, self._w.text().strip()))
            browse = QPushButton("Browse")
            browse.clicked.connect(self._browse)
            clear = QPushButton("Clear")
            clear.clicked.connect(lambda: self._set_path(""))
            for b in (browse, clear):
                b.setFixedHeight(_CONTROL_H)
            row.addWidget(self._w, 1)
            row.addWidget(browse)
            row.addWidget(clear)
            lay.addLayout(row)
        else:
            lay.addWidget(QLabel(p.label))
            self._w = None
        self.set_value(value)

    def value(self):
        return self._axis.value(self._w.value())

    def set_value(self, v):
        w, p = self._w, self.p
        if w is None:
            return
        w.blockSignals(True)
        if self._axis is not None:
            w.setValue(self._axis.pos(v))
            self._read.setText(fmt_value(p, v))
        elif p.kind == "bool":
            w.setChecked(bool(v))
        elif p.kind == "choice":
            w.setCurrentText(str(v))
        elif p.kind == "file":
            w.setText(str(v or ""))
            w.setToolTip(str(v or "") or p.hint)
        w.blockSignals(False)

    def _on_slider(self, pos):
        v = self._axis.value(pos)
        self._read.setText(fmt_value(self.p, v))
        self.changed.emit(self.p.key, v)

    def _on_reset(self):
        self.set_value(self.p.default)
        self.changed.emit(self.p.key, self.p.default)

    def _set_path(self, path):
        self._w.setText(path)
        self._w.setToolTip(path or self.p.hint)
        self.changed.emit(self.p.key, path)

    def _browse(self):
        cur = Path(self._w.text()).expanduser() if self._w.text() else Path.home()
        start = str(cur if cur.is_dir() else cur.parent if cur.parent.is_dir() else Path.home())
        path, _ = QFileDialog.getOpenFileName(self, self.p.label, start, _file_filter(self.p.hint))
        if path:
            self._set_path(path)


class _Caption(QLabel):
    """One line that is cut to the room it has instead of asking for more."""

    def __init__(self, text="", name="track_sub"):
        super().__init__()
        self._full = ""
        self.setObjectName(name)
        self.set_full(text)

    def set_full(self, text: str):
        self._full = text
        self._cut()

    def full(self) -> str:
        return self._full

    def _cut(self):
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(1, self.width())))

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._cut()


class _EqBank(QWidget):
    """The small equalizer under a line that names the handle in use. A band equalizer also gets a button that puts every
    band back to flat."""
    reset = Signal()

    def __init__(self, mini):
        super().__init__()
        self.mini = mini
        self._help = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._caption = _Caption()
        self._flat = QPushButton("Reset bands")
        self._flat.setFixedHeight(_CONTROL_H)
        self._flat.setToolTip("Put every band back to 0 dB.")
        self._flat.clicked.connect(self.reset)
        head.addWidget(self._caption, 1)
        head.addWidget(self._flat)
        lay.addLayout(head)
        lay.addWidget(mini, 1)

    def fit(self, grows: bool, compact: bool = False):
        """A band equalizer takes the room it is given. The graph of any other module has a height of its own."""
        self._flat.setVisible(grows)
        self._help = ("Drag a handle. Double click puts it back." if grows
                      else "Drag a handle. Sideways changes the corner frequency.")
        self.say("")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if grows else QSizePolicy.Fixed)
        if grows:
            self.mini.setMinimumHeight(90)
            self.mini.setMaximumHeight(440)
            self.setMaximumHeight(_CONTROL_H + 2 + 440)
        else:
            self.mini.setFixedHeight(90 if compact else 170)
            self.setMaximumHeight(16777215)

    def say(self, text: str):
        self._caption.set_full(text or self._help)


def _said(h) -> str:
    """What a handle holds, for the line above the graph: 8 kHz: +3.5 dB."""
    if h.fp is None:
        return "%s: %s" % (h.label, fmt_value(h.gp, h.gain))
    return "%s: %s, %s" % (h.label, fmt_value(h.fp, h.freq), fmt_value(h.gp, h.gain))


class _Cells(QWidget):
    """The settings of one module in the module's own order. A run of switches shares one row, sliders and lists sit in
    as many columns as fit. The curve goes above the controls on a tall panel and below them on a short one. The bank
    of a band equalizer comes after the controls and takes all the room that is left."""

    def __init__(self):
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(8)
        self._cells: list = []
        self._curve = None
        self._bank = None
        self._holders: list = []            # (row widget, its cells) for each run of switches
        self._curve_first = True
        self._cols = 0

    @property
    def has_bank(self) -> bool:
        return self._bank is not None

    def set_cells(self, cells: list, curve=None, bank=None):
        """The curve and the bank belong to the panel and come back later, so they are hidden here, not deleted."""
        self._release()
        for c in self._cells:
            c.setParent(None)
            c.deleteLater()
        for w in (self._curve, self._bank):
            if w is not None and w is not curve and w is not bank:
                w.hide()
        self._cells, self._curve, self._bank = cells, curve, bank
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if bank is not None else QSizePolicy.Preferred)
        self.relayout()

    def set_curve_first(self, first: bool):
        if first != self._curve_first:
            self._curve_first = first
            self.relayout()

    def relayout(self):
        self._cols = 0
        self._place()

    def _release(self):
        """Take the cells out of the rows that hold them and drop those rows."""
        for holder, cs in self._holders:
            for c in cs:
                c.setParent(self)
            self._grid.removeWidget(holder)
            holder.deleteLater()
        self._holders = []
        for w in (*self._cells, self._curve, self._bank):
            if w is not None:
                self._grid.removeWidget(w)

    def _place(self):
        cols = max(1, min(3, self.width() // _CELL_W)) if self.width() > 1 else 1
        if cols == self._cols:
            return
        self._cols = cols
        g = self._grid
        self._release()
        shown = [c for c in self._cells if getattr(c, "in_use", True)]
        for c in self._cells:
            if c not in shown:
                c.hide()
        runs: list = []                     # (kind, cells): the kinds that sit differently
        for c in shown:
            p = getattr(c, "p", None)
            kind = "switches" if p is not None and p.kind == "bool" else "rows"
            if runs and runs[-1][0] == kind:
                runs[-1][1].append(c)
            else:
                runs.append((kind, [c]))
        for r in range(g.rowCount()):
            g.setRowStretch(r, 0)
        row = 0
        curve = self._curve
        if curve is not None and self._curve_first:
            g.addWidget(curve, row, 0, 1, cols)
            row += 1
        for kind, cs in runs:
            if kind == "switches":
                holder = QWidget()
                flow = _Flow(holder, 14, 4)
                flow.setContentsMargins(0, 0, 0, 0)
                for c in cs:
                    flow.addWidget(c)
                    c.show()
                self._holders.append((holder, cs))
                g.addWidget(holder, row, 0, 1, cols, Qt.AlignTop)
                row += 1
            else:
                for i, c in enumerate(cs):
                    g.addWidget(c, row + i // cols, i % cols, Qt.AlignTop)
                    c.show()
                row += -(-len(cs) // cols)
        if curve is not None and not self._curve_first:
            g.addWidget(curve, row, 0, 1, cols)
            row += 1
        if self._bank is not None:
            g.addWidget(self._bank, row, 0, 1, cols)
            g.setRowStretch(row, 100)
            g.setRowStretch(row + 1, 1)     # room beyond what the bank can use goes here, not between the rows
        for k in range(3):
            g.setColumnStretch(k, 1 if k < cols else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()


class _Curve(QWidget):
    """The frequency response of a module that can say it (response_db), drawn from the theme's colours."""

    def __init__(self, scheme):
        super().__init__()
        self._scheme = scheme               # a callable, the live Scheme
        self._db = None
        self._caption = ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_compact(False)

    def set_compact(self, compact: bool):
        """A thin strip, for a short panel."""
        h = 56 if compact else 84
        self.setMinimumHeight(h)
        self.setFixedHeight(h)

    def set_response(self, db, caption: str = ""):
        self._db = None if db is None else np.clip(np.nan_to_num(np.asarray(db, float)), -36.0, 36.0)
        self._caption = caption
        self.update()

    def paintEvent(self, _):
        sc = self._scheme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, sc.base)
        top = 6.0 if self._db is None else max(6.0, math.ceil(float(np.abs(self._db).max()) / 6.0) * 6.0)
        x = lambda f: (math.log10(f) - math.log10(20.0)) / 3.0 * (w - 1)           # noqa: E731
        y = lambda d: (0.5 - d / (2.0 * top)) * (h - 1)                            # noqa: E731
        p.setFont(scaled_font(self.font(), 0.8))
        fm = QFontMetrics(p.font())
        for f, lab in ((100, "100"), (1000, "1k"), (10000, "10k")):
            p.setPen(QPen(sc.line, 1))
            p.drawLine(int(x(f)), 0, int(x(f)), h)
            p.setPen(sc.dimmer)
            p.drawText(int(x(f)) + 3, h - 3, lab)
        for d in (-top, 0.0, top):
            p.setPen(QPen(sc.dimmer if d == 0 else sc.line, 1))
            p.drawLine(0, int(y(d)), w, int(y(d)))
        p.setPen(sc.dimmer)
        p.drawText(3, fm.ascent() + 1, "%+g dB" % top)
        if self._caption:
            p.drawText(w - fm.horizontalAdvance(self._caption) - 4, fm.ascent() + 1, self._caption)
        if self._db is not None:
            pts = [QPointF(x(f), y(d)) for f, d in zip(_CURVE_FREQS, self._db)]
            fill = QPolygonF(pts + [QPointF(pts[-1].x(), y(0.0)), QPointF(pts[0].x(), y(0.0))])
            soft = QColor(sc.accent)
            soft.setAlpha(60)
            p.setPen(Qt.NoPen)
            p.setBrush(soft)
            p.drawPolygon(fill)
            p.setPen(QPen(sc.accent, 2))
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(QPolygonF(pts))
        p.setPen(QPen(sc.line, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


_FAILS = (("NaN or infinity", "It produced a sound that is not a number."),
          ("wrong kind or shape", "It handed back audio in the wrong shape."),
          ("rebuilt for the new sample rate", "It could not be set up again for the new sample rate."))


def _failure_text(why: str) -> str:
    """What the chain recorded when a module failed, in plain words."""
    for key, text in _FAILS:
        if key in why:
            return text
    return "It hit an error (%s)." % why


class _Banner(QWidget):
    """A row with a sentence and one button, painted in the theme's accent so it is not missed."""

    def __init__(self, scheme):
        super().__init__()
        self._scheme = scheme               # a callable, the live Scheme
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 4, 4)
        row.setSpacing(8)
        self.text = QLabel()
        self.text.setObjectName("dsp_banner")
        self.text.setWordWrap(True)
        self.button = QPushButton()
        row.addWidget(self.text, 1)
        row.addWidget(self.button, 0, Qt.AlignVCenter)

    def paintEvent(self, _):
        sc = self._scheme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(sc.accent, 1))
        p.setBrush(sc.sel)
        p.drawRoundedRect(QRect(0, 0, self.width() - 1, self.height() - 1), 4, 4)
        p.end()


class _Lamp(QWidget):
    """A small round light: filled in the accent colour while on, an outline while off."""

    def __init__(self, scheme):
        super().__init__()
        self._scheme = scheme               # a callable, the live Scheme
        self.on = False
        self.setFixedSize(14, 14)

    def set_on(self, on: bool):
        if bool(on) != self.on:
            self.on = bool(on)
            self.update()

    def paintEvent(self, _):
        sc = self._scheme()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(sc.accent if self.on else sc.dimmer, 1.4))
        p.setBrush(sc.accent if self.on else Qt.NoBrush)
        p.drawEllipse(QPointF(7, 7), 4.6, 4.6)
        p.end()


def _active_keys(m):
    """The settings that matter right now, for a module that says so (active_keys); None means all of them."""
    f = getattr(m, "active_keys", None)
    try:
        return set(f()) if callable(f) else None
    except Exception:
        return None


def _limiter_db(chain, st) -> float:
    """How far the safety limiter pulls the level down right now, in dB. The chain's stats say so when they carry it
    (limiter_db, or limiting as a yes or no); until they do, the limiter's own release state is read. Zero while the
    chain is idle or the limiter is off, since a stopped limiter keeps its last state."""
    if not chain.master or not chain.limiter:
        return 0.0
    for key in ("limiter_db", "limiting"):
        if key in st:
            return max(0.0, float(st[key] or 0.0))
    if not st.get("avg_ms", 0.0):
        return 0.0
    d = float(getattr(getattr(chain, "_lim", None), "dr", 0.0) or 0.0)
    return -20.0 * math.log10(max(1e-6, 1.0 - d)) if d > 0.0 else 0.0


def _is_on(v) -> bool:
    return getattr(v, "value", v) == Qt.Checked.value


class _RowDelegate(QStyledItemDelegate):
    """Rows with an on/off box drawn from the theme's colours: the style's own box vanishes on the dark style sheet."""

    def __init__(self, view, scheme):
        super().__init__(view)
        self._scheme = scheme               # a callable, the live Scheme

    @staticmethod
    def _box(r: QRect) -> QRect:
        return QRect(r.left() + 6, r.center().y() - 7, 14, 14)

    def paint(self, p, opt, idx):
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        w = o.widget
        text, o.text = o.text, ""
        o.features &= ~QStyleOptionViewItem.HasCheckIndicator
        (w.style() if w else QApplication.style()).drawControl(QStyle.CE_ItemViewItem, o, p, w)
        sc = self._scheme()
        on = _is_on(idx.data(Qt.CheckStateRole))
        box = self._box(o.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(sc.accent if on else sc.dimmer, 1))
        p.setBrush(sc.accent if on else sc.base)
        p.drawRoundedRect(box.adjusted(0, 0, -1, -1), 3, 3)
        if on:
            path = QPainterPath()
            path.moveTo(box.left() + 3.5, box.top() + 7.5)
            path.lineTo(box.left() + 6, box.top() + 10)
            path.lineTo(box.left() + 10.5, box.top() + 4)
            p.setPen(QPen(sc.on_accent, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        fg = idx.data(Qt.ForegroundRole)
        if isinstance(fg, QBrush):
            color = fg.color()
        else:
            color = o.palette.color(QPalette.HighlightedText if o.state & QStyle.State_Selected else QPalette.Text)
        p.setPen(color)
        p.setFont(o.font)
        r = o.rect.adjusted(box.width() + 14, 0, -6, 0)
        p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, p.fontMetrics().elidedText(text, Qt.ElideRight, r.width()))
        p.restore()

    def editorEvent(self, ev, model, opt, idx):
        t = ev.type()
        if t in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            if ev.button() == Qt.LeftButton and self._box(opt.rect).adjusted(-4, -4, 4, 4).contains(ev.position().toPoint()):
                if t == QEvent.MouseButtonRelease:
                    on = _is_on(idx.data(Qt.CheckStateRole))
                    model.setData(idx, Qt.Unchecked if on else Qt.Checked, Qt.CheckStateRole)
                return t != QEvent.MouseButtonPress         # a press still selects the row
            return False
        return super().editorEvent(ev, model, opt, idx)


class _ChainList(QListWidget):
    """The chain. Rows drag to reorder. Keys: Alt and Up or Down move the row, Delete removes it, Ctrl and D duplicates
    it, Space switches it on or off. The panel does every change; a drag never removes a row by itself."""
    dropped = Signal(int, int)          # a row was dragged: from row, to row (where it ends up)
    dragFinished = Signal()             # a drag ended, wherever it was dropped
    removeRequested = Signal()
    moveRequested = Signal(int)
    duplicateRequested = Signal()
    toggleRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setDragDropMode(QListWidget.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setDropIndicatorShown(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setMinimumHeight(36)

    def sizeHint(self):
        # a few rows: the splitter starts from this, and the controls below get the rest
        rows = min(max(self.count(), 2), 5)
        return QSize(160, rows * (self.fontMetrics().height() + 10) + 2 * self.frameWidth() + 4)

    def startDrag(self, actions):
        # Qt deletes the dragged row when the drop was accepted as a Move, wherever it happened. The chain is not touched
        # by that, so the panel puts the row back from the chain once the drag is over.
        try:
            super().startDrag(actions)
        finally:
            self.dragFinished.emit()

    def dropEvent(self, e):
        # The chain does the move and the panel redraws from it, so the view's own row shuffling is not used.
        if e.source() not in (None, self):
            e.ignore()
            return
        src = self.currentRow()
        pos = e.position().toPoint()
        it = self.itemAt(pos)
        if it is None:
            ins = self.count()
        else:
            ins = self.row(it) + (1 if pos.y() >= self.visualItemRect(it).center().y() else 0)
        e.accept()
        e.setDropAction(Qt.IgnoreAction)    # a Move would make the drag source delete the dragged row
        self.setState(QListWidget.NoState)
        self.stopAutoScroll()
        self.viewport().update()
        if src >= 0:
            self.dropped.emit(src, ins - 1 if src < ins else ins)

    def keyPressEvent(self, e):
        mods, key = e.modifiers(), e.key()
        if key == Qt.Key_Delete:
            self.removeRequested.emit()
        elif mods & Qt.AltModifier and key in (Qt.Key_Up, Qt.Key_Down):
            self.moveRequested.emit(-1 if key == Qt.Key_Up else 1)
        elif mods & Qt.ControlModifier and key == Qt.Key_D:
            self.duplicateRequested.emit()
        elif key == Qt.Key_Space:           # the view would also toggle the box for Ctrl and Space, which selects nothing here
            if not mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
                self.toggleRequested.emit()
            e.accept()
        else:
            super().keyPressEvent(e)


class DspWidget(QWidget):
    def __init__(self, chain, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._busy = False
        self._suspended = False
        self._sel = -1
        self._sel_uid = 0
        self._shown_id = ""             # module whose controls are on screen
        self._shown_bands = ()          # the settings of it that are bands
        self._own: set = set()          # the bands the small equalizer holds, which have no slider
        self._mini_on = False           # whether the small equalizer draws the selected module
        self._app_accent = None         # the accent the main window gave, which the small equalizer shares with the real one
        self._curve_fn = None
        self._curve_volume = None       # the player volume the curve was drawn for, None when its module ignores volume
        self._vol_fn = None
        self._probe = None
        self._list_sig = None
        self._cell_by_key: dict = {}
        self._values_sig = None
        self._preset = ""
        self._preset_sig = None
        self._list_pref = None          # None: the list hides itself on a short panel. True or False: what the user chose
        self._list_shown = True
        self._tall = True
        self._lamp_until = 0.0
        self._trip_load = 0.0           # the load in percent when the overload guard last switched the chain off
        self._live = True               # whether the chain is running: switched on and not tripped
        self._build()
        self._load_state()
        self._apply_list()
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._lamp_timer = QTimer(self)     # the limiter reacts in about 100 ms, so the light looks that often
        self._lamp_timer.setInterval(100)
        self._lamp_timer.timeout.connect(self._update_lamp)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save_state)
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(6000)
        self._note_timer.timeout.connect(lambda: self._note.setVisible(False))
        QApplication.instance().aboutToQuit.connect(self.save_state)    # a change in the last moments still gets saved
        chain.overloaded.connect(self._show_overload)
        chain.recovered.connect(self._update_status)
        chain.module_failed.connect(lambda _uid, name, _why: self._say("%s stopped working and is bypassed." % name))
        self.refresh()

    # layout
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        # Compact buttons for this panel only: the app-wide button has padding and a minimum width made for toolbars.
        self.setStyleSheet("QPushButton { padding: 4px 10px; min-width: 0px; } QLabel#dsp_banner { background: transparent; }")

        top = QHBoxLayout()
        top.setSpacing(6)
        self._master = QCheckBox("DSP on")
        self._master.setToolTip("Turn the whole DSP chain on or off. Off plays the music untouched, whatever the modules say.")
        self._master.setAccessibleName("DSP on or off")
        self._master.toggled.connect(self._on_master)
        self._preset_btn = QPushButton("Presets")
        self._preset_btn.setToolTip("Load or save a whole chain.")
        self._preset_menu = QMenu(self._preset_btn)
        self._preset_menu.aboutToShow.connect(self._fill_preset_menu)
        self._preset_btn.setMenu(self._preset_menu)
        self._preset_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._add_btn = QPushButton("Add")
        self._add_btn.setToolTip("Add a module at the end of the chain.")
        self._add_menu = QMenu(self._add_btn)
        self._add_menu.setToolTipsVisible(True)
        self._add_menu.aboutToShow.connect(lambda: self._fill_add_menu(self._add_menu))
        self._add_btn.setMenu(self._add_menu)
        top.addWidget(self._master)
        top.addWidget(self._preset_btn, 1)
        top.addWidget(self._add_btn)
        root.addLayout(top)

        self._banner = _Banner(lambda: self._scheme)    # why nothing is heard: the chain is off, or the guard stopped it
        self._banner.button.clicked.connect(self._on_banner)
        self._banner.hide()
        root.addWidget(self._banner)

        self._note = QLabel()
        self._note.setObjectName("track_sub")
        self._note.setWordWrap(True)
        self._note.hide()
        root.addWidget(self._note)

        self._body = QStackedWidget()
        root.addWidget(self._body, 1)

        self._empty = QLabel("The chain is empty. Press Add to pick a module, or choose a preset.")
        self._empty.setObjectName("track_sub")
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignCenter)
        self._body.addWidget(self._empty)

        self._split = QSplitter(Qt.Vertical)
        self._split.setChildrenCollapsible(False)
        self._split.setHandleWidth(4)
        self._body.addWidget(self._split)

        left = QWidget()
        self._left = left
        lh = QHBoxLayout(left)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.setSpacing(4)
        self._list = _ChainList()
        self._list.setAccessibleName("DSP chain")
        self._list.currentRowChanged.connect(self._on_row)
        self._list.itemChanged.connect(self._on_item_changed)
        self._list.dropped.connect(self._on_dropped)
        self._list.dragFinished.connect(self._after_drag)
        self._list.removeRequested.connect(self._remove_selected)
        self._list.moveRequested.connect(self._move_selected)
        self._list.duplicateRequested.connect(self._duplicate_selected)
        self._list.toggleRequested.connect(self._toggle_selected)
        self._list.customContextMenuRequested.connect(self._list_menu)
        lh.addWidget(self._list, 1)
        side = QVBoxLayout()
        side.setSpacing(2)
        self._up = QPushButton("Up")
        self._down = QPushButton("Down")
        self._remove = QPushButton("Remove")
        self._up.setToolTip("Move the selected module up. Rows can also be dragged, or moved with Alt and Up or Down.")
        self._down.setToolTip("Move the selected module down.")
        self._remove.setToolTip("Take the selected module out of the chain (Delete).")
        self._up.clicked.connect(lambda: self._move_selected(-1))
        self._down.clicked.connect(lambda: self._move_selected(1))
        self._remove.clicked.connect(self._remove_selected)
        for b in (self._up, self._down, self._remove):
            side.addWidget(b)
        side.addStretch(1)
        lh.addLayout(side)
        self._split.addWidget(left)

        pane = QWidget()
        pl = QVBoxLayout(pane)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._list_toggle = QPushButton("List")
        self._list_toggle.setCheckable(True)
        self._list_toggle.setToolTip("Show or hide the list of modules. Hidden, the controls get the whole height.")
        self._list_toggle.setAccessibleName("Show the module list")
        self._list_toggle.toggled.connect(self._on_list_toggle)
        self._title = _Caption("", "settings_section")
        self._picker = QComboBox()          # stands in for the list while it is hidden
        self._picker.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._picker.setMinimumContentsLength(5)
        self._picker.setAccessibleName("Module")
        self._picker.currentIndexChanged.connect(self._on_picker)
        self._reset = QPushButton("Reset")
        self._reset.setToolTip("Reset module: put every setting of this module back to its default.")
        self._reset.setAccessibleName("Reset module")
        self._reset.clicked.connect(self._reset_selected)
        head.addWidget(self._list_toggle)
        head.addWidget(self._title, 1)
        head.addWidget(self._picker, 1)
        head.addWidget(self._reset)
        pl.addLayout(head)
        self._mstate = QWidget()            # says so when the selected module is switched off or stopped working
        mrow = QHBoxLayout(self._mstate)
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.setSpacing(6)
        self._mstate_text = QLabel()
        self._mstate_text.setObjectName("track_sub")
        self._mstate_text.setWordWrap(True)
        self._mstate_btn = QPushButton()
        self._mstate_btn.clicked.connect(self._on_mstate)
        mrow.addWidget(self._mstate_text, 1)
        mrow.addWidget(self._mstate_btn, 0, Qt.AlignTop)
        self._mstate.hide()
        pl.addWidget(self._mstate)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setMinimumHeight(32)
        page = QWidget()
        pv = QVBoxLayout(page)
        self._pv = pv
        pv.setContentsMargins(0, 0, 4, 0)
        pv.setSpacing(6)
        self._summary = QLabel()
        self._summary.setObjectName("track_sub")
        self._summary.setWordWrap(True)
        pv.addWidget(self._summary)
        self._curve = _Curve(lambda: self._scheme)
        self._curve.hide()
        self._mini = MiniEq(self._mini_response)
        self._mini.moved.connect(self._on_mini)
        self._mini.said.connect(lambda h: self._bank.say(_said(h)))
        self._bank = _EqBank(self._mini)
        self._bank.reset.connect(self._reset_bands)
        self._bank.hide()
        self._cells = _Cells()
        pv.addWidget(self._cells)
        self._extra = QLabel()              # what a module wants to say, such as how its file loaded
        self._extra.setObjectName("track_sub")
        self._extra.setWordWrap(True)
        self._extra.hide()
        pv.addWidget(self._extra)
        pv.addStretch(1)
        self._scroll.setWidget(page)
        pl.addWidget(self._scroll, 1)
        self._split.addWidget(pane)
        self._split.setStretchFactor(0, 2)
        self._split.setStretchFactor(1, 3)

        foot = QWidget()
        fl = _Flow(foot, 12, 2)
        fl.setContentsMargins(0, 0, 0, 0)
        self._load = QLabel("CPU 0%")
        self._load.setToolTip("How much of the time for each block of sound the chain needs. "
                              "If it keeps needing too much, the chain switches itself off.")
        lim = QWidget()
        lim_row = QHBoxLayout(lim)
        lim_row.setContentsMargins(0, 0, 0, 0)
        lim_row.setSpacing(4)
        self._limiter = QCheckBox("Safety limiter")
        self._limiter.setToolTip("Stops boosts from clipping at the end of the chain. The light shows when it is "
                                 "pulling the level down.")
        self._limiter.toggled.connect(self._on_limiter)
        self._lamp = _Lamp(lambda: self._scheme)
        self._lamp.setAccessibleName("Limiter active")
        lim_row.addWidget(self._limiter)
        lim_row.addWidget(self._lamp)
        fl.addWidget(self._load)
        fl.addWidget(lim)
        root.addWidget(foot)

        for kind in (QPushButton, QComboBox):
            for w in self.findChildren(kind):
                w.setFixedHeight(_CONTROL_H)
        self._scheme = Scheme(self, self._list)
        self._mini.set_default_accent(self._idle_accent())
        self._list.setItemDelegate(_RowDelegate(self._list, lambda: self._scheme))
        self._set_orientation(Qt.Vertical)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        w = e.size().width()
        want = Qt.Horizontal if w >= (_WIDE - 40 if self._split.orientation() == Qt.Horizontal else _WIDE) else Qt.Vertical
        if want != self._split.orientation():
            self._set_orientation(want)
        self._fit_preset_label()
        self._apply_list()
        self._set_tall(self.height() >= 300)

    def _set_orientation(self, o):
        self._split.setOrientation(o)
        if o == Qt.Horizontal:
            w = max(1, self.width() - 12)
            left = min(280, max(180, w * 3 // 10))
            self._split.setSizes([left, w - left])
        else:
            h = max(1, self.height())
            self._split.setSizes([h * 2 // 5, h * 3 // 5])

    # the list, which a short panel can do without
    def _list_wanted(self) -> bool:
        if self._list_pref is not None:
            return self._list_pref
        return not (self._split.orientation() == Qt.Vertical and self.height() < 300)

    def _apply_list(self):
        show = self._list_wanted()
        if show != self._list_shown or self._list_toggle.isChecked() != show:
            self._list_shown = show
            self._left.setVisible(show)
            self._title.setVisible(show)
            self._picker.setVisible(not show)
            self._list_toggle.blockSignals(True)
            self._list_toggle.setChecked(show)
            self._list_toggle.blockSignals(False)

    def _on_list_toggle(self, on):
        self._list_pref = bool(on)
        self._apply_list()
        self._queue_save()

    def _set_tall(self, tall: bool):
        """On a short panel the controls come first and the summary and the curve after them."""
        if tall != self._tall:
            self._tall = tall
            self._pv.removeWidget(self._summary)
            self._pv.insertWidget(0 if tall else 1, self._summary)
            self._cells.set_curve_first(tall)
            if self._mini_on and not self._cells.has_bank:
                self._bank.fit(False, not tall)
        self._curve.set_compact(not tall)

    def _sync_header(self):
        """The module chooser, which stands in for the list while it is hidden. It says which modules are off."""
        n = self._list.count()
        self._busy = True
        try:
            self._picker.clear()
            for k in range(n):
                it = self._list.item(k)
                self._picker.addItem("%d. %s%s" % (k + 1, it.text(), "" if it.checkState() == Qt.Checked else " (off)"))
            self._picker.setCurrentIndex(self._sel if 0 <= self._sel < n else -1)
            self._picker.setEnabled(n > 0)
        finally:
            self._busy = False
        self._sync_state()

    def _sync_state(self):
        """The title of the controls and the row under it: a module that is switched off or stopped working is not
        heard, and the row says so with a button that puts it right."""
        es = self._chain.entries()
        e = es[self._sel] if 0 <= self._sel < len(es) else None
        if e is None:
            self._title.set_full("")
            self._mstate.hide()
            return
        off, failed = not e["enabled"], e["failed"]
        self._title.set_full(e["name"] + (" (stopped working)" if failed else " (switched off)" if off else ""))
        if failed:
            self._mstate_text.setText("This module stopped working, so it is skipped. %s" % _failure_text(failed))
            self._mstate_btn.setText("Try again")
        elif off:
            self._mstate_text.setText("Switched off, so it is not heard.")
            self._mstate_btn.setText("Switch on")
        self._mstate.setVisible(bool(failed or off))

    def _on_mstate(self):
        i = self._sel
        if i >= 0:
            self._chain.set_enabled(i, True)        # for a stopped module this is another try
            self._wake()
            self.refresh()
            self._queue_save()

    def _on_picker(self, i):
        if not self._busy and i >= 0:
            self._list.setCurrentRow(i)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (e.Type.StyleChange, e.Type.PaletteChange) and hasattr(self, "_scheme"):
            self._scheme.refresh(probe=self.isVisible() and self._themed())
            self._mini.set_default_accent(self._idle_accent())
            self._paint_rows()
            self._list.viewport().update()
            self._curve.update()
            self._lamp.update()

    def _themed(self) -> bool:
        """True when a style sheet from the app or a panel above this one sets the look. Then the accent is what a
        checked button paints (Scheme probes for it). This panel's own sheet does not count: under the system look it
        would send the probe to a native button."""
        w = self.parentWidget()
        while w is not None:
            if w.styleSheet():
                return True
            w = w.parentWidget()
        return bool(QApplication.instance().styleSheet())

    # the accent of the small equalizer follows the real one: the main window calls these for both
    def set_accent(self, palette):
        self._mini.set_accent(QColor(palette["accent"]) if palette else None)

    def set_system_accent(self, on: bool):
        self._mini.set_system_accent(on)

    def set_default_accent(self, color):
        self._app_accent = QColor(color)
        self._mini.set_default_accent(self._app_accent)

    def _idle_accent(self):
        return self._app_accent if self._app_accent is not None else self._scheme.accent

    # run only while shown
    def set_suspended(self, suspended: bool):
        self._suspended = bool(suspended)
        self._mini.set_suspended(self._suspended)
        self._sync_timer()

    def _sync_timer(self):
        run = self.isVisible() and not self._suspended
        if run and not self._timer.isActive():
            self.refresh()
            self._timer.start()
            self._lamp_timer.start()
        elif not run and self._timer.isActive():
            self._timer.stop()
            self._lamp_timer.stop()
            self._lamp.set_on(False)

    def showEvent(self, e):
        super().showEvent(e)
        self._scheme.refresh(probe=self._themed())
        self._mini.set_default_accent(self._idle_accent())
        self._mini.set_player(self._window_player())
        self._list.viewport().update()
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_timer()

    def _tick(self):
        if self._structure() != self._list_sig:
            i = self._chain.index_of(self._sel_uid)
            if i >= 0:
                self._sel = i               # the module the user had selected moved
            self.refresh()
        else:
            m = self._chain.module(self._sel) if self._sel >= 0 else None
            if m is not None and self._values_of(m) != self._values_sig:
                self._push_values(m)
            elif m is not None:
                self._update_status_line(m)         # a file may have finished loading
                if self._curve_volume is not None and abs(self._volume() - self._curve_volume) > 0.004:
                    self._update_extras(m)          # the player volume moved: so does a curve that depends on it
        self._update_status()

    # reading the chain
    def _structure(self) -> list:
        return [(e["uid"], e["enabled"], e["failed"]) for e in self._chain.entries()]

    @staticmethod
    def _values_of(m) -> tuple:
        return tuple(m.get(p.key) for p in m.params)

    def _chain_sig(self) -> list:
        return [[e["id"], e["enabled"], [[k, v] for k, v in sorted(self._chain.settings(i).items())]]
                for i, e in enumerate(self._chain.entries())]

    def refresh(self):
        """Bring every part of the panel in line with the chain."""
        entries = self._chain.entries()
        self._busy = True
        try:
            self._master.setChecked(bool(self._chain.master))
            self._limiter.setChecked(bool(self._chain.limiter))
            lst = self._list
            lst.clear()
            for k, e in enumerate(entries):
                it = QListWidgetItem(e["name"] + (" (bypassed)" if e["failed"] else ""))
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
                            | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
                it.setCheckState(Qt.Checked if e["enabled"] else Qt.Unchecked)
                it.setData(Qt.UserRole, k)
                it.setToolTip(self._row_tip(k, e))
                lst.addItem(it)
            self._list_sig = self._structure()
            if entries:
                self._sel = min(max(self._sel, 0), len(entries) - 1)
                lst.setCurrentRow(self._sel)
            else:
                self._sel = -1
            self._sel_uid = self._chain.uid(self._sel)
            self._paint_rows()
        finally:
            self._busy = False
        self._body.setCurrentIndex(1 if entries else 0)
        self._show_module()
        self._fit_preset_label()
        self._update_status()
        self._sync_buttons()

    def _row_tip(self, k: int, e: dict, st: dict | None = None) -> str:
        m = self._chain.module(k)
        tip = m.summary if m is not None else ""
        if e["failed"]:
            return "%s\nThis module stopped working and is bypassed (%s). Switch it off and on to try again." % (
                tip, e["failed"])
        if st:
            tip += "\nAverage %.2f ms, peak %.2f ms per block." % (st.get("avg_ms", 0.0), st.get("peak_ms", 0.0))
        return tip

    def _paint_rows(self):
        dim = QBrush(self._scheme.dim)
        self._busy = True
        try:
            for k in range(self._list.count()):
                it = self._list.item(k)
                off = it.checkState() != Qt.Checked or "(bypassed)" in it.text() or not self._live
                it.setData(Qt.ForegroundRole, dim if off else None)
        finally:
            self._busy = False
        self._dim_mini()

    def _dim_mini(self):
        """The small equalizer is drawn like a bypassed Equalizer while its module is not heard."""
        es = self._chain.entries()
        e = es[self._sel] if 0 <= self._sel < len(es) else None
        self._mini.set_live(e is not None and e["enabled"] and not e["failed"] and self._live)

    def _sync_buttons(self):
        n = self._list.count()
        self._up.setEnabled(self._sel > 0)
        self._down.setEnabled(0 <= self._sel < n - 1)
        self._remove.setEnabled(self._sel >= 0)

    def _show_module(self):
        m = self._chain.module(self._sel) if self._sel >= 0 else None
        self._sync_header()
        if m is None:
            self._shown_id = ""
            self._shown_bands, self._own, self._mini_on = (), set(), False
            self._picker.setToolTip("")
            self._summary.setText("")
            self._cells.set_cells([], self._curve)
            self._cell_by_key = {}
            self._reset.setEnabled(False)
            self._curve_fn = None
            self._curve_volume = None
            self._curve.hide()
            self._extra.hide()
            return
        self._reset.setEnabled(True)
        if (self._shown_id != m.id or len(self._cell_by_key) + len(self._own) != len(m.params)
                or self._shown_bands != tuple(_band_keys(m))):
            self._build_cells(m)
        self._push_values(m)

    def _build_cells(self, m):
        self._shown_id = m.id
        self._shown_bands = tuple(_band_keys(m))
        self._title.setToolTip(m.summary)
        self._picker.setToolTip(m.summary)
        self._summary.setText(m.summary)
        handles = handles_of(m, self._shown_bands, _active_keys(m))
        bands = bool(handles) and all(h.fp is None for h in handles)
        self._mini_on = bool(handles)
        self._own = set(self._shown_bands) if bands else set()
        cells = []
        self._cell_by_key = {}
        for p in m.params:
            if p.key in self._own:
                continue
            c = _Cell(p, m.get(p.key))
            c.changed.connect(self._on_param)
            cells.append(c)
            self._cell_by_key[p.key] = c
        if not cells:
            note = QLabel("This module has no settings.")
            note.setObjectName("track_sub")
            cells.append(note)
        self._cells.set_cells(cells, self._bank if self._mini_on and not bands else self._curve,
                              self._bank if bands else None)
        self._bank.fit(bands, not self._tall)
        self._bank.setVisible(self._mini_on)
        self._mini.set_handles([])          # the handles are the module's now, and arrive with the values
        self._pv.setStretchFactor(self._cells, 10 if self._cells.has_bank else 0)
        self._curve.set_compact(not self._tall)

    def _push_values(self, m, skip=None):
        """Show the module's settings in the controls, all but `skip` (the one being dragged). A change to one
        setting can move others (a preset), so this reads all of them, and it hides the ones that do nothing now."""
        active = _active_keys(m)
        moved = False
        for p in m.params:
            c = self._cell_by_key.get(p.key)
            if c is None:
                continue
            if p.key != skip:
                c.set_value(m.get(p.key))
            use = active is None or p.key in active
            if c.in_use != use:
                c.in_use, moved = use, True
        if moved:
            self._cells.relayout()
        self._values_sig = self._values_of(m)
        self._update_extras(m)
        if self._shown_bands != tuple(_band_keys(m)):
            QTimer.singleShot(0, self._show_module)         # the module now means other settings by its bands

    def _window_player(self):
        """The player of the window this panel is docked in (a floating dock still has the main window as its parent)."""
        w = self
        while w is not None:
            pl = getattr(w, "_player", None)
            if pl is not None:
                return pl
            w = w.parentWidget()
        return None

    def _window_volume(self):
        pl = self._window_player()
        return getattr(pl, "volume", None) if pl is not None else None

    def _volume(self) -> float:
        """The player volume the Loudness curve is drawn for: the source set with set_volume_source, else the window's
        player, else what the chain last saw (last_volume). 1 when nobody says."""
        for src in (self._vol_fn, self._window_volume, lambda: getattr(self._chain, "last_volume", None)):
            try:
                v = src() if src is not None else None
                if v is not None:
                    return min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError, RuntimeError):
                pass
        return 1.0

    def set_volume_source(self, fn):
        """fn() returns the player volume from 0 to 1. A module whose response depends on it, such as the loudness
        compensation, is drawn for that volume."""
        self._vol_fn = fn

    def _response(self, m):
        """A function from frequencies to dB and the player volume it was set for (None when the module ignores it),
        for a module that can say its response."""
        f = getattr(m, "response_db", None)
        if not callable(f):
            return None, None
        try:
            wants = "volume" in inspect.signature(f).parameters
        except (TypeError, ValueError):
            wants = False
        if not wants:
            return f, None
        vol = self._volume()
        probe = self._probe_of(m)
        return (lambda fr: probe.response_db(fr, volume=vol)), vol

    def _probe_of(self, m):
        """A private copy of the module with its settings applied, to read a response that depends on the volume. The
        live one only applies a setting in the audio thread's next block, which may never come while nothing plays."""
        pr = self._probe
        if pr is None or pr.id != m.id or pr.fs != m.fs:
            pr = self._probe = dsp_base.create(m.id, m.fs)
        pr.load_settings(m.settings())
        pr.prepare()
        return pr

    def _mini_response(self, freqs):
        """The response the small equalizer draws, read from the module that is selected now."""
        m = self._chain.module(self._sel) if self._sel >= 0 else None
        fn = self._response(m)[0] if m is not None else None
        return fn(freqs) if fn is not None else np.zeros(len(freqs))

    def _update_extras(self, m):
        """The response curve, then the sentence a module offers about itself."""
        if self._mini_on:
            self._curve_volume = None
            self._mini.set_handles(handles_of(m, self._shown_bands, _active_keys(m)))
            self._mini.set_values(m.get)
            self._dim_mini()
            self._update_status_line(m)
            return
        fn, vol = self._response(m)
        db = None
        if fn is not None:
            try:
                db = fn(_CURVE_FREQS)
            except Exception:
                fn = None
        self._curve_fn = fn
        self._curve_volume = vol if fn is not None else None
        self._curve.set_response(db, "" if self._curve_volume is None else "at volume %d%%" % round(vol * 100))
        self._curve.setVisible(fn is not None)
        self._update_status_line(m)

    def _update_status_line(self, m):
        status = getattr(m, "status", "")
        text = status if isinstance(status, str) else ""
        if self._extra.text() != text:
            self._extra.setText(text)
        self._extra.setVisible(bool(text))

    # list actions
    def select(self, i: int):
        if 0 <= i < self._list.count():
            self._list.setCurrentRow(i)

    @property
    def selected(self) -> int:
        return self._sel

    def _on_row(self, row):
        if self._busy:
            return
        self._sel = row
        self._show_module()
        self._sync_buttons()
        self._queue_save()

    def _on_item_changed(self, item):
        if self._busy:
            return
        on = item.checkState() == Qt.Checked
        self._chain.set_enabled(self._list.row(item), on)
        if on:
            self._wake()
        if "(bypassed)" in item.text():
            QTimer.singleShot(0, self.refresh)      # switching a failed module on gives it another try
        else:
            self._paint_rows()
            self._list_sig = self._structure()
            self._sync_header()
        self._edited()

    def _on_param(self, key, value):
        self._set_param(key, value, key)

    def _on_mini(self, key, value):
        self._set_param(key, value)

    def _set_param(self, key, value, skip=None):
        """One setting changed by the user. `skip` is the control that was dragged and already shows it."""
        if self._sel < 0:
            return
        self._chain.set(self._sel, key, value)
        self._wake()
        m = self._chain.module(self._sel)
        if m is not None:
            self._push_values(m, skip=skip)
        self._edited()

    def _edited(self):
        self._fit_preset_label()

    def add_module(self, module_id: str):
        i = self._chain.add(module_id)
        if i < 0:
            self._say("The chain is full, or that module is not available.")
            return
        self._wake()
        self._sel = i
        self.refresh()
        self._queue_save()

    def _remove_selected(self):
        i = self._sel
        if i < 0:
            return
        self._chain.remove(i)
        self._sel = min(i, self._chain.count() - 1)
        self.refresh()
        self._queue_save()

    def _move_selected(self, d: int):
        i = self._sel
        j = i + d
        if i < 0 or not 0 <= j < self._list.count():
            return
        self._chain.move(i, j)
        self._sel = j
        self.refresh()
        self._queue_save()

    def _on_dropped(self, i: int, j: int):
        if i != j:
            self._chain.move(i, j)
            self._sel = j
            self.refresh()
            self._queue_save()

    def _after_drag(self):
        """Whatever a drag did to the list view, the chain is what counts: draw the rows from it again."""
        if self._list.count() != self._chain.count():
            self.refresh()

    def _toggle_selected(self):
        if 0 <= self._sel < self._list.count():
            it = self._list.item(self._sel)
            it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def _duplicate_selected(self):
        n = self._chain.duplicate(self._sel)
        if n < 0:
            self._say("The chain is full.")
            return
        self._wake()
        self._sel = n
        self.refresh()
        self._queue_save()

    def _reset_selected(self):
        if self._sel >= 0:
            self._chain.reset_module(self._sel)
            self._wake()
            self._show_module()
            self._edited()

    def _reset_bands(self):
        """Every band of the selected module back to its default, and its preset to Flat when it has one."""
        m = self._chain.module(self._sel) if self._sel >= 0 else None
        if m is None:
            return
        by = {p.key: p for p in m.params}
        if "Flat" in getattr(by.get("preset"), "choices", ()):
            self._chain.set(self._sel, "preset", "Flat")
        for k in self._shown_bands:
            if m.get(k) != by[k].default:
                self._chain.set(self._sel, k, by[k].default)
        self._wake()
        self._show_module()
        self._edited()

    def _list_menu(self, pos):
        it = self._list.itemAt(pos)
        menu = self._context_menu(self._list.row(it) if it is not None else -1)
        menu.exec(self._list.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _context_menu(self, row: int) -> QMenu:
        """The menu for a row, or for the empty part of the list when row is -1."""
        menu = QMenu(self)
        if row < 0:
            add = menu.addMenu("Add")
            add.setToolTipsVisible(True)
            self._fill_add_menu(add)
            menu.addAction("Reset all settings", self.reset_all)
            return menu
        self._list.setCurrentRow(row)
        on = self._list.item(row).checkState() == Qt.Checked

        def act(text, fn, keys=""):
            a = menu.addAction(text, fn)
            if keys:                        # shown in the menu so the keyboard way can be found; the list does the work
                a.setShortcut(QKeySequence(keys))
                a.setShortcutVisibleInContextMenu(True)
            return a
        act("Turn off" if on else "Turn on", lambda: self._list.item(row).setCheckState(Qt.Unchecked if on else Qt.Checked),
            "Space")
        menu.addSeparator()
        act("Move up", lambda: self._move_selected(-1), "Alt+Up").setEnabled(row > 0)
        act("Move down", lambda: self._move_selected(1), "Alt+Down").setEnabled(row < self._list.count() - 1)
        act("Duplicate", self._duplicate_selected, "Ctrl+D")
        act("Reset module", self._reset_selected)
        act("Reset all settings", self.reset_all)
        menu.addSeparator()
        act("Remove", self._remove_selected, "Delete")
        return menu

    # menus
    def _fill_add_menu(self, menu: QMenu):
        menu.clear()
        groups = {}
        for cls in dsp_base.REGISTRY.values():
            groups.setdefault(cls.group if cls.group in dsp_base.GROUPS else "Other", []).append(cls)
        if not groups:
            menu.addAction("No modules available").setEnabled(False)
            return
        for g in (*dsp_base.GROUPS, "Other"):
            if g not in groups:
                continue
            sub = menu.addMenu(g)
            sub.setToolTipsVisible(True)
            for cls in sorted(groups[g], key=lambda c: c.name.lower()):
                a = sub.addAction(cls.name)
                a.setToolTip(cls.summary)
                a.triggered.connect(lambda _=False, mid=cls.id: self.add_module(mid))

    def _fill_preset_menu(self):
        menu = self._preset_menu
        menu.clear()
        for name in dsp_presets.names():
            menu.addAction(name, lambda n=name: self.load_builtin(n))
        menu.addSeparator()
        mine = self._chain.preset_names()
        if mine:
            sub = menu.addMenu("My presets")
            for name in mine:
                sub.addAction(name, lambda n=name: self.load_user(n))
        menu.addAction("Save as…", self.save_as)
        dele = menu.addMenu("Delete")
        dele.setEnabled(bool(mine))
        for name in mine:
            dele.addAction(name, lambda n=name: self.delete_user(n))
        menu.addSeparator()
        menu.addAction("Reset all settings", self.reset_all)

    def load_builtin(self, name: str):
        d = dsp_presets.get(name)
        if d is None:
            return
        self._loaded(name, self._chain.load_dict(d), bool(d.get("modules")))

    def load_user(self, name: str):
        notes = self._chain.load_preset(name)
        if notes is None:
            self._say(self._chain.last_error or "Could not load the preset %s." % name)
            return
        self._loaded(name, notes, True)

    def _loaded(self, name: str, notes: list, switch_on: bool):
        woke = switch_on and self._wake()       # presets do not carry the master switch
        if notes:
            self._say(("DSP turned on. " if woke else "") + " ".join(notes))
        self._preset = name
        self._sel = 0
        self.refresh()
        self._preset_sig = self._chain_sig()
        self._fit_preset_label()
        self._queue_save()

    def save_as(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        name = re.sub(r'[\\/:*?"<>|]', "", name).strip(" .")[:60] if ok else ""
        if not name:
            return
        if name.lower() in (n.lower() for n in dsp_presets.names()):
            self._say("That name belongs to a built in preset.")
            return
        if name in self._chain.preset_names() and QMessageBox.question(
                self, "Save preset", "Replace the preset '%s'?" % name) != QMessageBox.Yes:
            return
        if not self._chain.save_preset(name):
            self._say(self._chain.last_error or "Could not save the preset.")
            return
        self._preset = name
        self._preset_sig = self._chain_sig()
        self._fit_preset_label()
        self._queue_save()

    def delete_user(self, name: str):
        if QMessageBox.question(self, "Delete preset", "Delete the preset '%s'?" % name) != QMessageBox.Yes:
            return
        if not self._chain.delete_preset(name):
            self._say(self._chain.last_error or "Could not delete the preset.")
            return
        if self._preset == name:
            self._preset, self._preset_sig = "", None
            self._fit_preset_label()
            self._queue_save()

    def reset_all(self):
        n = self._chain.count()
        if not n or QMessageBox.question(
                self, "Reset all settings", "Put every setting of every module back to its default?") != QMessageBox.Yes:
            return
        for i in range(n):
            self._chain.reset_module(i)
        self._show_module()
        self._edited()

    def _fit_preset_label(self):
        name = self._preset
        if name and self._preset_sig is not None and self._chain_sig() != self._preset_sig:
            name += " (edited)"
        text = name or "Presets"
        room = max(20, self._preset_btn.width() - 34)
        self._preset_btn.setText(self._preset_btn.fontMetrics().elidedText(text, Qt.ElideRight, room))
        self._preset_btn.setToolTip(text if name else "Load or save a whole chain.")

    # footer and notes
    def _on_master(self, on):
        if not self._busy:
            self._chain.set_master(bool(on))
            self._update_status()

    def _wake(self) -> bool:
        """An edit the user means to hear switches the chain on, and the status line says so. True when it did."""
        if self._chain.master:
            return False
        self._chain.set_master(True)
        self._say("DSP turned on")
        self._update_status()
        return True

    def _on_limiter(self, on):
        if not self._busy:
            self._chain.set_limiter(bool(on))

    def _on_reenable(self):
        self._chain.reenable()
        self._update_status()

    def _on_banner(self):
        if self._chain.tripped:
            self._on_reenable()
        else:
            self._wake()

    def _show_overload(self, load: float = 0.0):
        self._trip_load = load
        self._update_status()

    def _sync_banner(self):
        """One row at the top says why nothing is heard: the guard stopped the chain, or the chain is switched off."""
        b = self._banner
        if self._chain.tripped:
            text = ("The chain used %d%% of the time it had, so it was switched off and the effects are not heard."
                    % round(self._trip_load) if self._trip_load
                    else "The chain used too much CPU, so it was switched off and the effects are not heard.")
            label = "Re-enable"
        elif not self._chain.master:
            text, label = "The DSP chain is off, so the effects below are not heard.", "Turn on"
        else:
            if not b.isHidden():
                b.hide()
            return
        if b.text.text() != text:
            b.text.setText(text)
        if b.button.text() != label:
            b.button.setText(label)
        if b.isHidden():
            b.show()

    def _update_status(self):
        st = self._chain.stats()
        master = bool(self._chain.master)
        self._load.setText("CPU %d%%" % round(st["load"]) if master else "DSP off")
        self._busy = True               # a tooltip change would otherwise look like a check box change
        try:
            self._master.setChecked(master)
            self._master.setText("DSP on" if master else "DSP off")
            for k, mod in enumerate(st["modules"]):
                it = self._list.item(k)
                if it is not None:
                    tip = self._row_tip(k, mod, mod if master else None)
                    if it.toolTip() != tip:
                        it.setToolTip(tip)
        finally:
            self._busy = False
        live = master and not self._chain.tripped
        if live != self._live:
            self._live = live
            self._paint_rows()          # the rows go dim while nothing runs
        self._sync_banner()

    def _update_lamp(self):
        """The limiter light stays on for 300 ms after the last reduction, so a short peak can still be seen."""
        db = _limiter_db(self._chain, self._chain.stats())
        now = time.monotonic()
        if db >= 0.1:
            self._lamp_until = now + 0.3
            self._lamp.setToolTip("The safety limiter is pulling the level down by %.1f dB." % db)
        lit = now < self._lamp_until
        self._lamp.set_on(lit)
        if not lit:
            self._lamp.setToolTip("Lights while the safety limiter is pulling the level down.")

    def _say(self, text: str):
        self._note.setText(text)
        self._note.setVisible(True)
        self._note_timer.start()

    # saved state
    def _queue_save(self):
        self._save_timer.start()

    def save_state(self):
        m = self._chain.module(self._sel) if self._sel >= 0 else None
        d = {"selected": self._sel, "id": m.id if m is not None else "",
             "preset": self._preset, "preset_sig": self._preset_sig,
             "list": None if self._list_pref is None else ("shown" if self._list_pref else "hidden")}
        try:
            f = _state_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            plat_fs.replace(tmp, f)
        except OSError:
            pass

    def _load_state(self):
        try:
            d = json.loads(_state_file().read_text(encoding="utf-8"))
            i = int(d.get("selected", -1))
            m = self._chain.module(i)
            if m is not None and m.id == d.get("id"):
                self._sel = i
            self._preset = str(d.get("preset") or "")
            self._preset_sig = d.get("preset_sig") if self._preset else None
            self._list_pref = {"shown": True, "hidden": False}.get(d.get("list"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
