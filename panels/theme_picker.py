"""The theme picker in Settings: a grid of small cards, each painted in the colours of the look it chooses."""
from __future__ import annotations

import random

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QGridLayout, QSizePolicy, QWidget

import theme_presets as tp

_DEFAULT_BG, _DEFAULT_TEXT = "#0e0c14", "#d4cce6"      # what AudioCrate's own theme uses when it is not given colours
_CARD_H = 54


class _Card(QAbstractButton):
    def __init__(self, p: tp.Preset):
        super().__init__()
        self._p = p
        self.setCheckable(True)
        self.setAccessibleName(p.name)
        self.setToolTip(p.name)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(_CARD_H)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def _look(self) -> tuple[QColor, QColor, QColor]:
        """Background, text and accent of the card."""
        p = self._p
        if p.kind == "aero":
            return QColor("#8cc8f4"), QColor("#0b3a66"), QColor("#ffffff")
        return QColor(p.bg or _DEFAULT_BG), QColor(p.text or _DEFAULT_TEXT), QColor(p.accent)

    @staticmethod
    def _flair(pt: QPainter, rect: QRectF, style: str, bg: QColor, accent: QColor):
        """A hint of the style's texture, so the card looks like the theme does."""
        x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
        if style == "terminal":
            for y in range(int(y0) + 2, int(y1), 3):
                pt.fillRect(QRectF(x0, y, rect.width(), 1), QColor(0, 0, 0, 80))
        elif style == "carbon":
            for y in range(int(y0), int(y1), 4):
                for x in range(int(x0), int(x1), 4):
                    pt.fillRect(QRectF(x, y, 4, 4), QColor(255, 255, 255, 14) if ((x - int(x0)) // 4 + (y - int(y0)) // 4) % 2 else QColor(0, 0, 0, 40))
            metal = QLinearGradient(0, y0, 0, y1)
            metal.setColorAt(0, QColor(255, 255, 255, 40))
            metal.setColorAt(1, QColor(0, 0, 0, 0))
            pt.fillRect(rect, metal)
        elif style == "chiptune":
            for y in range(int(y0), int(y1), 6):
                for x in range(int(x0), int(x1), 6):
                    if ((x - int(x0)) // 6 + (y - int(y0)) // 6) % 2:
                        pt.fillRect(QRectF(x, y, 3, 3), QColor(255, 255, 255, 18))
            pt.fillRect(QRectF(x0, y1 - 3, rect.width(), 3), accent)
        elif style == "parchment":
            rnd = random.Random(5)
            for _ in range(160):
                dot = QColor(0, 0, 0, 26) if rnd.random() < 0.65 else QColor(255, 255, 255, 60)
                pt.fillRect(QRectF(x0 + rnd.random() * rect.width(), y0 + rnd.random() * rect.height(), 1, 1), dot)
            sheen = QLinearGradient(0, y0, 0, y1)
            sheen.setColorAt(0, QColor(255, 255, 255, 70))
            sheen.setColorAt(1, QColor(120, 80, 30, 34))
            pt.fillRect(rect, sheen)

    def paintEvent(self, _):
        p = self._p
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        square = p.style in ("terminal", "chiptune")        # the styles with square corners
        shape = QPainterPath()
        shape.addRoundedRect(rect, 3 if square else 8, 3 if square else 8)
        bg, fg, accent = self._look()

        pt.save()
        pt.setClipPath(shape)
        if p.kind == "aero":
            sky = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            sky.setColorAt(0, QColor("#c9e8ff"))
            sky.setColorAt(1, QColor("#5aa9e6"))
            pt.fillRect(rect, sky)
            shine = QColor("#ffffff")
            shine.setAlpha(80)
            pt.fillRect(QRectF(rect.left(), rect.top(), rect.width(), rect.height() / 2), shine)
        elif p.kind == "system":
            pt.fillRect(rect, QColor("#f1f1f1"))                    # light on one side, dark on the other: it follows the system
            dark = QPainterPath()
            dark.moveTo(rect.right(), rect.top())
            dark.lineTo(rect.right(), rect.bottom())
            dark.lineTo(rect.left() + rect.width() * 0.45, rect.bottom())
            dark.lineTo(rect.left() + rect.width() * 0.7, rect.top())
            dark.closeSubpath()
            pt.fillPath(dark, QColor("#2b2b2b"))
            fg, accent = QColor("#222222"), QColor("#3584e4")
        else:
            pt.fillRect(rect, bg)
            if p.style != "default":
                self._flair(pt, rect, p.style, bg, accent)
        pt.restore()

        # a name, and the look of a bit of the window: an accent pill and two faint lines
        f = self.font()
        f.setBold(True)
        f.setPixelSize(11)
        pt.setFont(f)
        pt.setPen(fg)
        name_rect = QRectF(rect.left() + 9, rect.top() + 6, rect.width() - 30, 16)
        pt.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, pt.fontMetrics().elidedText(p.name, Qt.ElideRight, int(name_rect.width())))
        pt.setPen(Qt.NoPen)
        y = rect.bottom() - 15
        pt.setBrush(accent)
        rad = 0 if square else 3.5
        pt.drawRoundedRect(QRectF(rect.left() + 9, y, rect.width() * 0.34, 7), rad, rad)
        faint = QColor(fg)
        faint.setAlpha(110)
        pt.setBrush(faint)
        pt.drawRoundedRect(QRectF(rect.left() + 9 + rect.width() * 0.34 + 6, y, rect.width() * 0.26, 7), rad, rad)

        # the edge: the accent and a tick when it is the one in use
        if self.isChecked():
            edge = QColor(accent if p.kind != "aero" else QColor("#1c6fb8"))
            pt.setPen(QPen(edge, 2.2))
            pt.setBrush(Qt.NoBrush)
            pt.drawPath(shape)
            pt.setPen(Qt.NoPen)
            pt.setBrush(edge)
            c = QPointF(rect.right() - 11, rect.top() + 12)
            pt.drawEllipse(c, 7, 7)
            pt.setPen(QPen(bg if p.kind == "own" else QColor("#ffffff"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            pt.drawLine(QPointF(c.x() - 3, c.y()), QPointF(c.x() - 0.8, c.y() + 2.4))
            pt.drawLine(QPointF(c.x() - 0.8, c.y() + 2.4), QPointF(c.x() + 3, c.y() - 2.4))
        else:
            edge = QColor(self.palette().windowText().color())
            edge.setAlpha(150 if self.underMouse() else 55)
            pt.setPen(QPen(edge, 1))
            pt.setBrush(Qt.NoBrush)
            pt.drawPath(shape)
        if self.hasFocus():
            ring = QColor(self.palette().highlight().color())
            pt.setPen(QPen(ring, 1.5, Qt.DotLine))
            pt.setBrush(Qt.NoBrush)
            pt.drawRoundedRect(QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75), 9, 9)


class ThemeNow(_Card):
    """The theme in use as one card, which opens the rest when it is clicked."""

    def __init__(self):
        super().__init__(tp.PRESETS[0])
        self.setChecked(True)
        self.setToolTip("The theme in use. Click to choose another.")

    def nextCheckState(self):
        pass                                        # it always shows as the one in use

    def show_for(self, cfg: dict):
        key = tp.current(cfg)
        p = tp.preset(key) or tp.Preset("custom", "Custom", cfg.get("accent_color") or "#8040c8", cfg.get("bg_color") or "",
                                        cfg.get("text_color") or "", style=cfg.get("theme_style") or "default")
        self._p = p
        self.setAccessibleName(p.name)
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        _bg, fg, _acc = self._look()
        if self._p.kind == "system":
            fg = QColor("#ffffff")                  # the arrow sits on the dark half of this card
        fg.setAlpha(190)
        pt.setPen(Qt.NoPen)
        pt.setBrush(fg)
        x, y = self.width() - 32, self.height() - 22                  # a small arrow down: there is more below
        pt.drawPolygon([QPointF(x, y), QPointF(x + 10, y), QPointF(x + 5, y + 6)])


class ThemePicker(QWidget):
    """Cards for every preset in core/theme_presets.py. A click says which one through `chosen`; the settings that
    follow the click call `select` with the preset they add up to ("custom" leaves every card unmarked)."""
    chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self._cards = {}
        for p in tp.PRESETS:
            card = _Card(p)
            card.clicked.connect(lambda _=False, k=p.key: self._clicked(k))
            self._cards[p.key] = card
        self._cols = 0
        self._place(3)

    def _place(self, cols: int):
        if cols == self._cols:
            return
        self._cols = cols
        for card in self._cards.values():
            self._grid.removeWidget(card)
        for i, card in enumerate(self._cards.values()):
            self._grid.addWidget(card, i // cols, i % cols)
        for c in range(3):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place(3 if e.size().width() >= 300 else 2)

    def _clicked(self, key: str):
        self.select(key)
        self.chosen.emit(key)

    def select(self, key: str):
        for k, card in self._cards.items():
            card.setChecked(k == key)
