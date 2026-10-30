"""User parametric EQ panel: an interactive frequency-response editor.

Drag a point to set its frequency (x) and gain (y), right-click the graph to add a band there,
double-click a point to remove it, scroll over a point to change its Q. Each band has type, freq,
gain, Q and slope controls, and there are input and output trims and a bypass. Presets are
named files in an EQPresets folder in Documents; the current state is kept in eq.json in the config folder.
"""
from __future__ import annotations
import json
import safe_write
import math

import numpy as np
from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QDoubleSpinBox, QInputDialog, QMessageBox, QSizePolicy, QLayout,
)

import plat_paths
import power
from applog import log
from dsp_chain import (
    ParametricEQ, EQ_TYPES, EQ_FMIN, EQ_FMAX, EQ_GMIN, EQ_GMAX, EQ_QMIN, EQ_QMAX,
    EQ_SLOPES, EQ_SLOPE_TYPES,
)

_CFG_DIR     = plat_paths.config_dir()
_EQ_STATE    = _CFG_DIR / "eq.json"
# Presets live in an EQPresets folder in Documents, one .json each, so they are easy to find, share and back up.
_PRESET_DIR  = plat_paths.documents_dir() / "EQPresets"
_GRID_FREQS  = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
_GRID_GAINS  = [-24, -18, -12, -6, 0, 6, 12, 18, 24]
_HIT_PX      = 16
_GAIN_TYPES  = ("peaking", "lowshelf", "highshelf")   # types whose handle Y = gain
# Log-spaced frequency grid for the response curve, computed once at import.
_RESP_FREQS  = np.logspace(math.log10(EQ_FMIN), math.log10(EQ_FMAX), 240)
_RESP_XN     = np.log(_RESP_FREQS / EQ_FMIN) / math.log(EQ_FMAX / EQ_FMIN)   # 0..1 across the width


class _EqCurve(QWidget):
    edited           = Signal()        # any band value changed (persist + refresh controls)
    selectionChanged = Signal()        # a different band became selected
    _gmin, _gmax = EQ_GMIN, EQ_GMAX    # the dB range the graph shows
    _pad = 0                           # pixels kept free at the left and right edge of the frequency axis

    def __init__(self, eq: ParametricEQ, player=None, parent=None):
        super().__init__(parent)
        self._eq = eq
        self._player = player
        self._sel: int = -1            # selected band index
        self._drag = False
        self._accent = QColor("#8040c8")
        self._default_accent = QColor("#8040c8")  # app accent (user-customisable)
        self._system_accent = False    # follow OS highlight on system theme
        self._has_art = False          # album-art palette overrides
        # live analyzer (post-EQ output spectrum)
        self._spec_db = None
        self._spec_freqs = None
        self._spec_win = np.hanning(2048).astype(np.float32)
        self._suspended = False    # set by the window when minimized/unfocused
        self._resp_sig = None      # cache key for the curve layer
        self._curve_pm = None
        self._resp_db  = None      # cached response_db over _RESP_FREQS
        self._grid_pm  = None      # grid and labels, painted once per size/colour
        self._grid_key = None
        self._spec_map = None      # (width, bin count) -> column starts for the backdrop
        self._spec_timer = QTimer(self)
        self._spec_timer.setInterval(power.frame_ms(40))   # 25 fps, plenty for a backdrop
        power.watch().changed.connect(
            lambda _: self._spec_timer.setInterval(power.frame_ms(40)))
        self._spec_timer.timeout.connect(self._spec_tick)
        # Runs only while visible and not suspended (see _sync_timer / set_suspended).
        self.setMinimumHeight(220)
        # Capped because the tabbed dock group (shared with Controls, Oscilloscope and
        # Milkdrop) can over-allocate this tab's share after a shrink and regrow: Qt's
        # dock layout does not keep per-tab stretch, so an unbounded Expanding widget
        # balloons when the dock area grows again.
        self.setMaximumHeight(600)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_suspended(self, suspended: bool):
        self._suspended = bool(suspended)
        self._sync_timer()

    def _sync_timer(self):
        run = self.isVisible() and not self._suspended
        if run and not self._spec_timer.isActive():
            self._spec_timer.start()
        elif not run and self._spec_timer.isActive():
            self._spec_timer.stop()
            self._spec_db = None     # drop spectrum state so it doesn't resume stale
            self.update()            # one idle frame (flat backdrop)

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_timer()

    def _spec_tick(self):
        samples = self._player.output_window(2048) if self._player is not None else None
        if samples is None or len(samples) < 2048:
            if self._spec_db is not None:
                self._spec_db = self._spec_db - 4.0          # decay away when idle
                if np.all(self._spec_db < -95.0):
                    self._spec_db = None
                self.update()
            return
        mag = np.abs(np.fft.rfft(samples * self._spec_win)) / 1024.0
        db = 20.0 * np.log10(mag + 1e-7)
        if self._spec_db is None or len(self._spec_db) != len(db):
            self._spec_db = db
            self._spec_freqs = np.fft.rfftfreq(2048, 1.0 / getattr(getattr(self._player, "_dsp", None), "fs", 44100))
        else:                                                # fast rise, slow fall
            self._spec_db = np.where(db > self._spec_db, db, self._spec_db * 0.8 + db * 0.2)
        self.update()

    def set_accent(self, color: QColor | None):
        if color is not None:
            self._has_art = True
            self._accent = QColor(color)
        else:
            self._has_art = False
            self._accent = self._idle_accent()
        self.update()

    def set_system_accent(self, on: bool):
        self._system_accent = bool(on)
        if not self._has_art:
            self._accent = self._idle_accent()
            self.update()

    def set_default_accent(self, color):
        """The idle colour from the app theme."""
        self._default_accent = QColor(color)
        if not self._has_art and not self._system_accent:
            self._accent = self._idle_accent()
            self.update()

    def _idle_accent(self) -> QColor:
        if self._system_accent:
            c = self.palette().highlight().color()
            if c.isValid():
                return QColor(c)
        return QColor(self._default_accent)

    @property
    def selected(self) -> int:
        return self._sel

    def select(self, idx: int):
        self._sel = idx if 0 <= idx < len(self._eq.bands) else -1
        self.selectionChanged.emit()
        self.update()

    # coordinate mapping
    def _f2x(self, f):
        W = max(1, self.width() - 2 * self._pad)
        return math.log(max(EQ_FMIN, f) / EQ_FMIN) / math.log(EQ_FMAX / EQ_FMIN) * W + self._pad

    def _x2f(self, x):
        W = max(1, self.width() - 2 * self._pad)
        return EQ_FMIN * (EQ_FMAX / EQ_FMIN) ** (min(max(x - self._pad, 0), W) / W)

    def _g2y(self, g):
        H = max(1, self.height())
        return (self._gmax - g) / (self._gmax - self._gmin) * H

    def _y2g(self, y):
        H = max(1, self.height())
        return self._gmax - (min(max(y, 0), H) / H) * (self._gmax - self._gmin)

    def _handle_pos(self, b):
        y = self._g2y(b.gain if b.type in _GAIN_TYPES else 0.0)
        return QPointF(self._f2x(b.freq), y)

    def _eq_signature(self):
        """Hashable snapshot of everything response_db() depends on, so the curve is
        only recomputed when a band or gain changes."""
        eq = self._eq
        return (eq.enabled, eq.input_gain, eq.output_gain,
                tuple((b.type, b.freq, b.gain, b.q, b.enabled, b.slope)
                      for b in eq.bands))

    def _band_at(self, pos):
        best, bestd = -1, _HIT_PX
        for i, b in enumerate(self._eq.bands):
            hp = self._handle_pos(b)
            d = math.hypot(hp.x() - pos.x(), hp.y() - pos.y())
            if d < bestd:
                best, bestd = i, d
        return best

    # painting
    def paintEvent(self, _):
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        p = QPainter(self)
        try:
            self._paint(p, W, H)
        finally:
            p.end()

    def _grid(self, W, H):
        """Background, grid lines and their labels, painted once into a pixmap. They only
        change with the size or the colours, and drawing about twenty text labels per
        frame was most of what this panel cost."""
        pal = self.palette()
        dpr = self.devicePixelRatioF()
        key = (W, H, dpr, pal.base().color().rgba(), pal.text().color().rgba(), self._gmin, self._gmax)
        if key != self._grid_key:
            pm = QPixmap(int(W * dpr), int(H * dpr))
            pm.setDevicePixelRatio(dpr)
            g = QPainter(pm)
            g.setRenderHint(QPainter.Antialiasing, True)
            g.fillRect(0, 0, W, H, pal.base().color())
            grid = QColor(pal.text().color()); grid.setAlpha(28)
            zero = QColor(pal.text().color()); zero.setAlpha(70)
            txt  = QColor(pal.text().color()); txt.setAlpha(120)
            f = QFont(); f.setPointSize(7); g.setFont(f)
            for fr in self._grid_freqs(W):
                x = self._f2x(fr)
                g.setPen(QPen(grid, 1)); g.drawLine(int(x), 0, int(x), H)
                g.setPen(txt)
                lbl = f"{fr//1000}k" if fr >= 1000 else str(fr)
                g.drawText(int(x) + 2, 10, lbl)
            for gn in self._grid_gains():
                y = self._g2y(gn)
                g.setPen(QPen(zero if gn == 0 else grid, 1))
                g.drawLine(0, int(y), W, int(y))
                if gn not in (self._gmin, self._gmax):
                    g.setPen(txt); g.drawText(2, int(y) - 2, f"{gn:+d}")
            g.end()
            self._grid_pm, self._grid_key = pm, key
        return self._grid_pm

    def _grid_freqs(self, W):
        return _GRID_FREQS

    def _grid_gains(self):
        return _GRID_GAINS

    def _backdrop_points(self, W, H):
        """The live spectrum as one point per 2 px column (the loudest bin in it)."""
        floor, top = -84.0, 0.0
        sf = self._spec_freqs
        m = self._spec_map
        if m is None or m[0] != W or m[1] != len(sf):
            sel = (sf >= EQ_FMIN) & (sf <= EQ_FMAX)
            xs = np.log(np.maximum(sf[sel], EQ_FMIN) / EQ_FMIN) / math.log(EQ_FMAX / EQ_FMIN)
            xs = xs * (W - 2 * self._pad) + self._pad
            starts = np.flatnonzero(np.diff(np.floor(xs / 2.0), prepend=-1.0))
            m = self._spec_map = (W, len(sf), sel, xs[starts], starts)
        _, _, sel, xs, starts = m
        db = np.maximum.reduceat(np.clip(self._spec_db[sel], floor, top), starts)
        ys = (1.0 - (db - floor) / (top - floor)) * H
        pts = list(map(QPointF, xs.tolist(), ys.tolist()))
        if self._pad:
            pts = [QPointF(0.0, pts[0].y()), *pts, QPointF(float(W), pts[-1].y())]
        return pts

    def _paint(self, p, W, H):
        p.setRenderHint(QPainter.Antialiasing, True)
        p.drawPixmap(0, 0, self._grid(W, H))

        # live spectrum backdrop (post-EQ output, moves with the song and the EQ).
        # The only thing that changes every frame, so it is the only thing drawn
        # per frame: the grid above and the curve layer below are cached pixmaps.
        if self._spec_db is not None and self._spec_freqs is not None:
            pts = self._backdrop_points(W, H)
            if len(pts) > 2:
                fillp = QPolygonF(pts)
                fillp.append(QPointF(pts[-1].x(), H)); fillp.append(QPointF(pts[0].x(), H))
                sc = QColor(self._accent); sc.setAlpha(45)
                # Un-antialiased: a translucent fill over most of a wide panel is
                # the costly part, and its soft edge is not worth the pixels.
                p.setRenderHint(QPainter.Antialiasing, False)
                p.setPen(Qt.NoPen); p.setBrush(sc); p.drawPolygon(fillp)
                p.setRenderHint(QPainter.Antialiasing, True)
                sl = QColor(self._accent); sl.setAlpha(110)
                p.setPen(QPen(sl, 1)); p.setBrush(Qt.NoBrush); p.drawPolyline(QPolygonF(pts))

        p.drawPixmap(0, 0, self._curve_layer(W, H))

    def _curve_layer(self, W, H):
        """Response curve and band handles on a transparent pixmap, repainted only
        when the EQ, the selection, the colours or the size change (response_db
        cascades biquads over 240 frequencies, and the handles draw text)."""
        pal = self.palette()
        dpr = self.devicePixelRatioF()
        sig = (W, H, dpr, self._eq_signature(), self._accent.rgba(), self._sel,
               pal.text().color().rgba(), self._gmin, self._gmax)
        if sig == self._resp_sig:
            return self._curve_pm
        self._resp_sig = sig
        self._resp_db = np.clip(self._eq.response_db(_RESP_FREQS), self._gmin, self._gmax)
        pm = QPixmap(int(W * dpr), int(H * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        xs = (_RESP_XN * (W - 2 * self._pad) + self._pad).tolist()
        ys = ((self._gmax - self._resp_db) / (self._gmax - self._gmin) * H).tolist()
        if self._pad:
            xs, ys = [0.0, *xs, float(W)], [ys[0], *ys, ys[-1]]
        line = QPolygonF(list(map(QPointF, xs, ys)))
        fill = QPolygonF(line)
        y0 = self._g2y(0.0)
        fill.append(QPointF(xs[-1], y0)); fill.append(QPointF(xs[0], y0))
        fc = QColor(self._accent); fc.setAlpha(70 if self._eq.enabled else 24)
        p.setPen(Qt.NoPen); p.setBrush(fc); p.drawPolygon(fill)
        lc = QColor(self._accent) if self._eq.enabled else QColor(pal.text().color())
        if not self._eq.enabled:
            lc.setAlpha(90)
        pen = QPen(lc, 2.0); pen.setCosmetic(True); p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawPolyline(line)

        self._paint_handles(p)
        p.end()
        self._curve_pm = pm
        return pm

    def _paint_handles(self, p):
        for i, b in enumerate(self._eq.bands):
            hp = self._handle_pos(b)
            self._paint_disc(p, hp, 9 if i == self._sel else 7, i == self._sel, b.enabled)
            p.setPen(QColor("#ffffff")); p.setFont(QFont("", 7, QFont.Bold))
            p.drawText(int(hp.x()) - 4, int(hp.y()) + 4, str(i + 1))

    def _paint_disc(self, p, hp, r, selected, enabled=True):
        col = QColor(self._accent)
        if not enabled:
            col.setAlpha(80)
        p.setBrush(col)
        p.setPen(QPen(QColor("#ffffff") if selected else col.darker(140), 2))
        p.drawEllipse(hp, float(r), float(r))

    # interaction
    def mousePressEvent(self, e):
        pos = e.position()
        if e.button() == Qt.LeftButton:
            self.select(self._band_at(pos))
            self._drag = self._sel >= 0
        elif e.button() == Qt.RightButton:
            self._eq.add_band(self._x2f(pos.x()),
                              gain=self._y2g(pos.y()), q=1.0, btype="peaking")
            self.select(len(self._eq.bands) - 1)
            self.edited.emit()
        self.update()

    def mouseDoubleClickEvent(self, e):
        idx = self._band_at(e.position())
        if idx >= 0:
            self._eq.remove_band(self._eq.bands[idx])
            self._sel = -1
            self.selectionChanged.emit()
            self.edited.emit()
            self.update()

    def mouseMoveEvent(self, e):
        if not self._drag or self._sel < 0:
            return
        b = self._eq.bands[self._sel]
        b.freq = float(min(EQ_FMAX, max(EQ_FMIN, self._x2f(e.position().x()))))
        if b.type in _GAIN_TYPES:
            b.gain = float(min(EQ_GMAX, max(EQ_GMIN, self._y2g(e.position().y()))))
        b.refresh()
        self.edited.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = False

    def wheelEvent(self, e):
        idx = self._band_at(e.position())
        if idx < 0:
            idx = self._sel
        if idx < 0:
            return
        b = self._eq.bands[idx]
        step = 1.18 if e.angleDelta().y() > 0 else (1 / 1.18)   # up = narrower (higher Q)
        b.q = float(min(EQ_QMAX, max(EQ_QMIN, b.q * step)))
        b.refresh()
        if idx != self._sel:
            self.select(idx)
        self.edited.emit()
        self.update()
        e.accept()


class _Flow(QLayout):
    """Lays items out left to right and wraps to a new line when the row is full.

    Items keep their natural size, so a narrow dock does not squeeze the controls
    below their text width and clip the labels.
    """

    def __init__(self, parent=None, hspace: int = 14, vspace: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._h, self._v = hspace, vspace
        self._need = 0          # height the rows need at the width last laid out

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._lay(QRect(0, 0, w, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        need = self._lay(rect, True)
        if need != self._need:
            # Report the height so the dock reserves room for the wrapped rows instead
            # of clipping the bottom ones. Settles on the next pass.
            self._need = need
            self.invalidate()

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.sizeHint())
        m = self.contentsMargins()
        size = size + QSize(m.left() + m.right(), m.top() + m.bottom())
        return QSize(size.width(), max(size.height(), self._need))

    def _lay(self, rect, place: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = area.x(), area.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if line and x + hint.width() > area.right() + 1:
                x, y, line = area.x(), y + line + self._v, 0
            if place:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line = max(line, hint.height())
        return y + line - rect.y() + m.bottom()


def _field(label: str, widget: QWidget) -> QWidget:
    """A label and the control it names, kept together when a row wraps."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    lay.addWidget(QLabel(label))
    lay.addWidget(widget)
    return box


class EqWidget(QWidget):
    status = Signal(str)         # a line for the main window's status bar

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self._player = player
        self._eq: ParametricEQ = player.eq
        self._loading = False
        self._build()
        self._load_state()
        self._refresh_presets()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_state)

    # ui
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._curve = _EqCurve(self._eq, self._player)
        self._curve.edited.connect(self._on_curve_edited)
        self._curve.selectionChanged.connect(self._sync_band_controls)
        root.addWidget(self._curve, 1)

        # Compact buttons for this panel only. The app-wide button has generous
        # padding and a minimum width that suits the toolbars.
        self.setStyleSheet("QPushButton { padding: 4px 11px; min-width: 0px; }")

        # selected band: the bypass switch, then the band's own fields
        bands = QWidget()
        row = _Flow(bands)
        self._bypass = QPushButton("EQ")
        self._bypass.setCheckable(True)
        self._bypass.setToolTip("Enable / bypass the parametric EQ")
        self._bypass.toggled.connect(self._on_bypass)
        row.addWidget(self._bypass)

        self._type = QComboBox()
        self._type.addItems(EQ_TYPES)
        self._type.currentTextChanged.connect(self._on_type)
        row.addWidget(_field("Type", self._type))

        self._freq = QDoubleSpinBox(); self._freq.setRange(EQ_FMIN, EQ_FMAX)
        self._freq.setDecimals(0); self._freq.setSuffix(" Hz")
        self._freq.valueChanged.connect(lambda v: self._set_field("freq", v))
        row.addWidget(_field("Freq", self._freq))

        self._gain = QDoubleSpinBox(); self._gain.setRange(EQ_GMIN, EQ_GMAX)
        self._gain.setDecimals(1); self._gain.setSuffix(" dB")
        self._gain.valueChanged.connect(lambda v: self._set_field("gain", v))
        row.addWidget(_field("Gain", self._gain))

        self._q = QDoubleSpinBox(); self._q.setRange(EQ_QMIN, EQ_QMAX)
        self._q.setDecimals(2); self._q.setSingleStep(0.1)
        self._q.valueChanged.connect(lambda v: self._set_field("q", v))
        row.addWidget(_field("Q", self._q))

        self._slope = QComboBox()
        for sl in EQ_SLOPES:
            self._slope.addItem(f"{sl} dB/oct", sl)
        self._slope.setToolTip(
            "How steeply a highpass or lowpass rolls off past its cutoff, or a shelf\n"
            "changes level around its frequency, in dB per octave. 12 = gentle,\n"
            "48 = very steep. (Not for peaking or notch bands.)")
        self._slope.currentIndexChanged.connect(self._on_slope)
        row.addWidget(_field("Slope", self._slope))

        self._del = QPushButton("Remove"); self._del.clicked.connect(self._remove_selected)
        row.addWidget(self._del)
        root.addWidget(bands)

        # global trim and presets
        glob = QWidget()
        row2 = _Flow(glob)
        self._in = QDoubleSpinBox(); self._in.setRange(-24, 24); self._in.setDecimals(1); self._in.setSuffix(" dB")
        self._in.valueChanged.connect(self._on_input)
        row2.addWidget(_field("In", self._in))
        self._out = QDoubleSpinBox(); self._out.setRange(-24, 24); self._out.setDecimals(1); self._out.setSuffix(" dB")
        self._out.valueChanged.connect(self._on_output)
        row2.addWidget(_field("Out", self._out))

        self._preset = QComboBox(); self._preset.setMinimumWidth(130)
        self._preset.setToolTip("Presets")
        row2.addWidget(self._preset)
        for text, slot, tip in (("Load", self._load_preset, ""),
                                ("Save", self._save_preset, ""),
                                ("Del", self._delete_preset, ""),
                                ("Flat", self._flat, "Reset to a flat EQ")):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            row2.addWidget(b)
        root.addWidget(glob)
        for kind in (QPushButton, QComboBox, QDoubleSpinBox):
            for w in self.findChildren(kind):
                w.setFixedHeight(26)     # spin boxes, combos and buttons on one baseline

    # selection / field sync
    def _sync_band_controls(self):
        self._loading = True
        idx = self._curve.selected
        has = 0 <= idx < len(self._eq.bands)
        for w in (self._type, self._freq, self._gain, self._q, self._slope, self._del):
            w.setEnabled(has)
        if has:
            b = self._eq.bands[idx]
            self._type.setCurrentText(b.type)
            self._freq.setValue(b.freq)
            self._gain.setValue(b.gain)
            self._q.setValue(b.q)
            self._gain.setEnabled(b.type in _GAIN_TYPES)
            self._slope.setCurrentIndex(max(0, self._slope.findData(b.slope)))
            self._slope.setEnabled(b.type in EQ_SLOPE_TYPES)
        self._loading = False

    def _on_slope(self, _i):
        if self._loading:
            return
        idx = self._curve.selected
        if 0 <= idx < len(self._eq.bands):
            self._eq.bands[idx].slope = int(self._slope.currentData() or 12)
            self._eq.bands[idx].refresh()
            self._curve.update()
            self._queue_save()

    def _set_field(self, name, value):
        if self._loading:
            return
        idx = self._curve.selected
        if not (0 <= idx < len(self._eq.bands)):
            return
        setattr(self._eq.bands[idx], name, float(value))
        self._eq.bands[idx].refresh()
        self._curve.update()
        self._queue_save()

    def _on_type(self, t):
        if self._loading:
            return
        idx = self._curve.selected
        if 0 <= idx < len(self._eq.bands):
            self._eq.bands[idx].type = t
            self._eq.bands[idx].refresh()
            self._gain.setEnabled(t in _GAIN_TYPES)
            self._slope.setEnabled(t in EQ_SLOPE_TYPES)
            self._curve.update()
            self._queue_save()

    def _on_curve_edited(self):
        self._sync_band_controls()
        self._queue_save()

    def _remove_selected(self):
        idx = self._curve.selected
        if 0 <= idx < len(self._eq.bands):
            self._eq.remove_band(self._eq.bands[idx])
            self._curve.select(-1)
            self._curve.update()
            self._queue_save()

    def _set_defaults(self):
        """Flat EQ with no bands. Right-click the graph to add points."""
        self._eq.bands = []
        self._eq.input_gain = 0.0
        self._eq.output_gain = 0.0
        self._eq.enabled = True

    def _flat(self):
        self._set_defaults()
        self._curve.select(-1)
        self._apply_to_ui()
        self._queue_save()

    def _on_bypass(self, on):
        self._eq.enabled = on
        self._curve.update()
        self._queue_save()

    def _on_input(self, v):
        if not self._loading:
            self._eq.input_gain = float(v); self._queue_save()

    def _on_output(self, v):
        if not self._loading:
            self._eq.output_gain = float(v); self._curve.update(); self._queue_save()

    def set_accent(self, palette):
        if palette:
            self._curve.set_accent(QColor(palette.get("accent", "#8040c8")))
        else:
            self._curve.set_accent(None)

    def set_system_accent(self, on: bool):
        self._curve.set_system_accent(on)

    def set_default_accent(self, color):
        self._curve.set_default_accent(color)

    def set_suspended(self, suspended: bool):
        self._curve.set_suspended(suspended)

    # persistence
    def _queue_save(self):
        if not self._loading:
            self._save_timer.start()

    def _apply_to_ui(self):
        self._loading = True
        self._bypass.setChecked(self._eq.enabled)
        self._in.setValue(self._eq.input_gain)
        self._out.setValue(self._eq.output_gain)
        self._loading = False
        self._curve.select(-1)
        self._curve.update()

    def _save_state(self):
        try:
            _CFG_DIR.mkdir(parents=True, exist_ok=True)
            safe_write.write_text(_EQ_STATE, json.dumps(self._eq.to_dict(), indent=2))
        except Exception:
            pass

    def _load_state(self):
        loaded = False
        try:
            if _EQ_STATE.exists():
                self._eq.load_dict(json.loads(_EQ_STATE.read_text(encoding="utf-8")))
                loaded = True
        except Exception:
            pass
        if not loaded:
            self._set_defaults()   # nothing saved yet: flat, no bands
        self._apply_to_ui()

    # presets
    @staticmethod
    def _migrate_presets():
        """One-time move of presets from the old config dir to the EQPresets folder in Documents."""
        old = _CFG_DIR / "eq_presets"
        try:
            if old.is_dir():
                _PRESET_DIR.mkdir(parents=True, exist_ok=True)
                for f in old.glob("*.json"):
                    dst = _PRESET_DIR / f.name
                    if not dst.exists():
                        dst.write_bytes(f.read_bytes())
        except Exception:
            pass

    def _refresh_presets(self):
        self._migrate_presets()
        self._preset.blockSignals(True)
        self._preset.clear()
        try:
            if _PRESET_DIR.is_dir():
                for f in sorted(_PRESET_DIR.glob("*.json")):
                    self._preset.addItem(f.stem)
        except Exception:
            pass
        self._preset.blockSignals(False)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Save EQ preset", "Preset name:")
        if not ok or not name.strip():
            return
        try:
            _PRESET_DIR.mkdir(parents=True, exist_ok=True)
            (_PRESET_DIR / f"{name.strip()}.json").write_text(
                json.dumps(self._eq.to_dict(), indent=2), encoding="utf-8")
            self._refresh_presets()
            self._preset.setCurrentText(name.strip())
        except Exception:
            log.exception("could not save the equalizer preset %s", name.strip())
            self.status.emit(f"Could not save the preset {name.strip()}.")

    def _load_preset(self):
        name = self._preset.currentText()
        if not name:
            return
        try:
            d = json.loads((_PRESET_DIR / f"{name}.json").read_text(encoding="utf-8"))
            self._eq.load_dict(d)
            self._apply_to_ui()
            self._save_state()
        except Exception:
            log.exception("could not load the equalizer preset %s", name)
            self.status.emit(f"Could not load the preset {name}.")

    def _delete_preset(self):
        name = self._preset.currentText()
        if not name:
            return
        if QMessageBox.question(self, "Delete preset", f"Delete preset '{name}'?") == QMessageBox.Yes:
            try:
                (_PRESET_DIR / f"{name}.json").unlink(missing_ok=True)
            except Exception:
                log.exception("could not delete the equalizer preset %s", name)
                self.status.emit(f"Could not delete the preset {name}.")
            self._refresh_presets()
