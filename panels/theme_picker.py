"""The theme picker in Settings: a grid of small cards, each painted in the colours of the look it chooses."""
from __future__ import annotations

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

    def paintEvent(self, _):
        p = self._p
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        shape = QPainterPath()
        shape.addRoundedRect(rect, 8, 8)
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
        pt.drawRoundedRect(QRectF(rect.left() + 9, y, rect.width() * 0.34, 7), 3.5, 3.5)
        faint = QColor(fg)
        faint.setAlpha(110)
        pt.setBrush(faint)
        pt.drawRoundedRect(QRectF(rect.left() + 9 + rect.width() * 0.34 + 6, y, rect.width() * 0.26, 7), 3.5, 3.5)

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
