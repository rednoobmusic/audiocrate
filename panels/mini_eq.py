"""The Equalizer window in small, for the DSP panel: the same grid, response curve, handles and spectrum backdrop
(panel_eq._EqCurve), with handles that edit the settings of one DSP module instead of the bands of the parametric EQ.

A band equalizer gets one handle per band at its centre frequency, dragged up and down. The tone control gets a handle
for each shelf, dragged sideways for the corner frequency and up and down for the gain.
"""
from __future__ import annotations

import math
import re

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QAbstractSlider

from dsp_base import Param
from panel_eq import _EqCurve, _GRID_FREQS

_HZ = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(k?)Hz\s*")
_SHELVES = (("bass", "lowshelf"), ("treble", "highshelf"))
_MARGIN_DB = 3.0                # room above and below the range of the settings, so a handle at the end is not cut off
_ARROW_DB, _PAGE_DB = 0.5, 3.0
_MAX_R = 6.0                    # the radius of the biggest handle


class Handle:
    """One point on the graph and the settings it edits. _EqCurve reads type, freq, gain, q, enabled and slope."""
    q, enabled, slope = 1.0, True, 12

    def __init__(self, label: str, gain: Param, freq, kind: str = "peaking"):
        self.label, self.gp, self.type = label, gain, kind
        self.fp = freq if isinstance(freq, Param) else None         # None: the band has a fixed centre
        self.freq = float(self.fp.default if self.fp else freq)
        self.gain = float(gain.default)


def _centre(label: str):
    """31.5 Hz gives 31.5 and 16 kHz gives 16000, None for a label that names no frequency."""
    m = _HZ.fullmatch(label)
    return float(m.group(1)) * (1000.0 if m.group(2) else 1.0) if m else None


def handles_of(m, band_keys, active=None) -> list:
    """The handles of a module: one per band of band_keys that is in use, or the two shelves of the tone control.
    Empty for any other module, and for bands that are not named by a frequency."""
    by = {p.key: p for p in m.params}
    if m.id == "tone":
        if all(pre + suffix in by for pre, _ in _SHELVES for suffix in ("_freq", "_gain")):
            return [Handle(by[pre + "_gain"].label, by[pre + "_gain"], by[pre + "_freq"], kind) for pre, kind in _SHELVES]
        return []
    bands = [by[k] for k in band_keys if k in by and (active is None or k in active)]
    centres = [_centre(p.label) for p in bands]
    if not bands or not all(centres):
        return []
    return [Handle(p.label, p, f) for p, f in zip(bands, centres)]


def _snap(p: Param, v: float) -> float:
    """v on the setting's grid and inside its range."""
    if p.step:
        v = p.lo + round((v - p.lo) / p.step) * p.step
    return p.clamp(round(v, 6))


class _Model:
    """What _EqCurve asks of the parametric EQ, answered from the handles of one module."""
    input_gain = 0.0

    def __init__(self, response):
        self.bands: list = []
        self.enabled = True                     # False draws the curve the way the Equalizer does when it is bypassed
        self.output_gain = 0.0                  # the preamp: it lifts the whole curve and is part of its signature
        self._response = response

    def response_db(self, freqs):
        try:
            return np.nan_to_num(np.asarray(self._response(freqs), float))
        except Exception:
            return np.zeros(len(freqs))


class _KeyHolder(QAbstractSlider):
    """Holds the keyboard focus for the graph. The window's hotkeys leave the arrow keys alone while a slider has the
    focus (see _handle_media_key in main_window), so the focus sits on one. It is one pixel, paints nothing and passes
    every key on to the graph."""

    def __init__(self, graph):
        super().__init__(graph)
        self._graph = graph
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(graph.accessibleName())
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(0, 0, 1, 1)

    def keyPressEvent(self, e):
        e.ignore()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self._graph.focusInEvent(e)


class MiniEq(_EqCurve):
    """moved(key, value) says which setting the user changed. said(handle) is for the line above the graph."""
    moved = Signal(str, object)
    said = Signal(object)
    _pad = int(_MAX_R) + 4              # room for the handle that is selected, with its ring

    def __init__(self, response, player=None, parent=None):
        model = _Model(response)
        super().__init__(model, player, parent)
        self._model = model
        self._r = _MAX_R
        self._top = 12
        self._drag = False
        self._hover = -1
        self.setMinimumSize(180, 90)
        self.setMaximumHeight(440)
        self.setAccessibleName("Equalizer graph")
        self.setToolTip("Drag a handle. Arrow keys and Page keys move it, Home and a double click put it back.")
        self.selectionChanged.connect(self._say_selected)
        self.setFocusProxy(_KeyHolder(self))

    def sizeHint(self):
        return QSize(300, 130)

    def minimumSizeHint(self):
        return QSize(180, 90)

    # what the graph shows
    def set_player(self, player):
        self._player = player
        self._sync_timer()

    def _sync_timer(self):
        if self._player is not None:
            super()._sync_timer()

    def set_live(self, on: bool):
        if self._model.enabled != bool(on):
            self._model.enabled = bool(on)
            self.update()

    def set_handles(self, handles: list):
        if [h.gp.key for h in handles] == [h.gp.key for h in self._model.bands]:
            return
        self._model.bands = handles
        self._sel, self._hover, self._drag = -1, -1, False
        self._fit()
        self.selectionChanged.emit()

    def set_values(self, get):
        """Move the handles to the settings `get(key)` returns and redraw the curve."""
        for h in self._model.bands:
            h.gain = float(get(h.gp.key))
            if h.fp is not None:
                h.freq = float(get(h.fp.key))
        pre = get("preamp")
        self._model.output_gain = float(pre) if isinstance(pre, (int, float)) else 0.0
        self._fit()

    def _fit(self):
        """The dB range (a little wider than the settings allow, and shifted for a preamp), the size of the handles
        and the grid that go with the handles now."""
        bands = self._model.bands
        top = max([max(abs(h.gp.lo), abs(h.gp.hi)) for h in bands] or [12.0])
        self._top = int(math.ceil(top / 6.0) * 6 + math.ceil(abs(self._model.output_gain) / 6.0) * 6)
        self._gmax, self._gmin = self._top + _MARGIN_DB, -self._top - _MARGIN_DB
        xs = sorted(self._f2x(h.freq) for h in bands)
        gap = min((b - a for a, b in zip(xs, xs[1:])), default=60.0)
        self._r = min(_MAX_R, max(2.5, gap * 0.42))
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _grid_gains(self):
        return list(range(-self._top, self._top + 1, 6))

    def _grid_freqs(self, W):
        """The lines that have room for their label."""
        f = QFont()
        f.setPointSize(7)
        fm = QFontMetrics(f)
        out, edge = [], 0
        for fr in _GRID_FREQS:
            x = self._f2x(fr)
            end = x + 2 + fm.horizontalAdvance(f"{fr // 1000}k" if fr >= 1000 else str(fr))
            if x >= edge and end <= W:
                out.append(fr)
                edge = end + 4
        return out

    def _paint_handles(self, p):
        for i, b in enumerate(self._eq.bands):
            self._paint_disc(p, self._handle_pos(b), self._r + (2 if i == self._sel else 0), i == self._sel,
                             self._model.enabled)

    # picking
    def _bands_only(self) -> bool:
        return all(h.fp is None for h in self._model.bands)

    def _pick(self, pos):
        """The handle under the pointer, and whether the pointer was in no handle's reach. In a band equalizer a press
        anywhere on the graph belongs to the band whose column it is in."""
        i = self._band_at(pos)
        if i >= 0 or not self._bands_only() or not self._model.bands:
            return i, False
        xs = [self._handle_pos(b).x() for b in self._model.bands]
        return min(range(len(xs)), key=lambda k: abs(xs[k] - pos.x())), True

    def _say_selected(self):
        if 0 <= self._sel < len(self._model.bands):
            self.said.emit(self._model.bands[self._sel])

    # changing
    def _put(self, i: int, gain=None, freq=None):
        """Set a handle's level and corner frequency, each on its setting's grid, and tell the panel what moved."""
        h = self._model.bands[i]
        moves = []
        if freq is not None and h.fp is not None and _snap(h.fp, freq) != h.freq:
            h.freq = _snap(h.fp, freq)
            moves.append((h.fp.key, h.freq))
        if gain is not None and _snap(h.gp, gain) != h.gain:
            h.gain = _snap(h.gp, gain)
            moves.append((h.gp.key, h.gain))
        for k, v in moves:
            self.moved.emit(k, v)
        if moves:
            self.said.emit(h)
            self.update()

    def _reset(self, i: int):
        h = self._model.bands[i]
        self._put(i, h.gp.default, h.fp.default if h.fp is not None else None)

    def _follow(self, i: int, pos):
        self._put(i, self._y2g(pos.y()), self._x2f(pos.x()) if not self._bands_only() else None)

    # interaction
    def mousePressEvent(self, e):
        pos = e.position()
        i, jump = self._pick(pos)
        if e.button() == Qt.LeftButton:
            self.select(i)
            self._drag = i >= 0
            if jump:
                self._follow(i, pos)
        elif e.button() == Qt.RightButton and i >= 0:
            self.select(i)
            self._reset(i)
        e.accept()

    def mouseMoveEvent(self, e):
        if self._drag and self._sel >= 0:
            self._follow(self._sel, e.position())
            return
        i = self._band_at(e.position())
        if i != self._hover:
            self._hover = i
            if i >= 0:
                self.said.emit(self._model.bands[i])

    def mouseReleaseEvent(self, e):
        self._drag = False

    def mouseDoubleClickEvent(self, e):
        i, _ = self._pick(e.position())
        if i >= 0:
            self.select(i)
            self._reset(i)
        e.accept()

    def contextMenuEvent(self, e):
        e.accept()

    def wheelEvent(self, e):
        e.ignore()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        if self._sel < 0 and self._model.bands and e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason):
            self.select(0)

    def keyPressEvent(self, e):
        n, i, key = len(self._model.bands), self._sel, e.key()
        if not 0 <= i < n:
            super().keyPressEvent(e)
            return
        h = self._model.bands[i]
        if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
            step = _PAGE_DB if key in (Qt.Key_PageUp, Qt.Key_PageDown) else _ARROW_DB
            self._put(i, gain=h.gain + (step if key in (Qt.Key_Up, Qt.Key_PageUp) else -step))
        elif key in (Qt.Key_Left, Qt.Key_Right) and e.modifiers() & Qt.ShiftModifier and h.fp is not None:
            self._put(i, freq=h.freq * 2.0 ** ((1 if key == Qt.Key_Right else -1) / 12.0))
        elif key in (Qt.Key_Left, Qt.Key_Right):
            self.select(min(n - 1, max(0, i + (1 if key == Qt.Key_Right else -1))))
        elif key == Qt.Key_Home:
            self._reset(i)
        else:
            super().keyPressEvent(e)
            return
        e.accept()
