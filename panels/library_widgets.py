"""Small painted widgets for the library panel.

Everything here draws itself from a Scheme (colours read from the live theme) and
from QPainter paths, so it follows the dark, art-palette and system looks without
an icon font. None of these are QPushButtons: main_window restyles every checkable
QPushButton on a theme change and would overwrite them.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QAbstractButton, QMenu, QPushButton, QSizePolicy, QWidget


def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF()[:3], b.getRgbF()[:3])))


def _lum(c: QColor) -> float:
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c.redF()) + 0.7152 * f(c.greenF()) + 0.0722 * f(c.blueF())


def _contrast(a: QColor, b: QColor) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def scaled_font(f: QFont, k: float) -> QFont:
    f = QFont(f)
    if f.pixelSize() > 0:
        f.setPixelSize(max(8, round(f.pixelSize() * k)))
    else:
        f.setPointSizeF(max(6.0, f.pointSizeF() * k))
    return f


class Scheme:
    """Colours for everything painted in the panel, read from whichever theme is live.

    A stylesheet theme leaves palette().highlight() at the OS colour, so there the
    accent is whatever a checked push button paints. The field is the search box,
    which carries the theme's list background.
    """

    def __init__(self, owner: QWidget, field: QWidget):
        self._owner, self._field, self._probe = owner, field, None
        self.refresh(probe=False)

    def refresh(self, probe: bool = True):
        w = self._owner
        pal = w.palette()
        win, fg = pal.color(QPalette.Window), pal.color(QPalette.WindowText)
        base = self._field.palette().color(QPalette.Base)
        acc = pal.color(QPalette.Highlight)
        if probe and w.isVisible() and w.style().metaObject().className() == "QStyleSheetStyle":
            if self._probe is None:
                self._probe = QPushButton(w)
                self._probe.setCheckable(True)
                self._probe.setChecked(True)
                self._probe.setFixedSize(40, 24)
                self._probe.hide()
            c = self._probe.grab().toImage().pixelColor(20, 12)
            if c.alpha() == 255:
                acc = c
        self.win, self.base, self.fg, self.accent = win, base, fg, acc
        self.on_accent = max((fg, win), key=lambda c: _contrast(acc, c))
        self.sel      = mix(base, acc, 0.30)
        self.hover    = mix(base, fg, 0.07)
        self.line     = mix(base, fg, 0.10)
        self.dim      = mix(base, fg, 0.66)
        self.dimmer   = mix(base, fg, 0.48)
        self.ph       = mix(base, fg, 0.11)        # cover placeholder
        self.folder   = mix(base, acc, 0.80)
        self.folder_fill = QColor(self.folder)
        self.folder_fill.setAlpha(46)
        # header controls sit on the window colour
        self.well       = mix(win, fg, 0.08)
        self.well_line  = mix(win, fg, 0.16)
        self.well_hover = mix(win, fg, 0.13)
        self.win_dim    = mix(win, fg, 0.66)


def draw_glyph(p: QPainter, name: str, r: QRectF, color: QColor, fill: QColor | None = None):
    """Line icons on a 16 unit grid scaled into r."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(color, max(1.2, r.width() / 11), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(fill if fill is not None else Qt.NoBrush)
    u = min(r.width(), r.height()) / 16.0
    x0, y0 = r.x() + (r.width() - 16 * u) / 2, r.y() + (r.height() - 16 * u) / 2
    P = lambda a, b: QPointF(x0 + a * u, y0 + b * u)
    R = lambda a, b, w, h: QRectF(x0 + a * u, y0 + b * u, w * u, h * u)
    path = QPainterPath()
    if name == "folder":
        path.moveTo(P(1.5, 3.5)); path.lineTo(P(6, 3.5)); path.lineTo(P(7.6, 5.5))
        path.lineTo(P(14.5, 5.5)); path.lineTo(P(14.5, 13)); path.lineTo(P(1.5, 13))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "refresh":
        p.drawArc(R(2.5, 2.5, 11, 11), 40 * 16, 280 * 16)
        path.moveTo(P(11.2, 1.8)); path.lineTo(P(11.9, 4.9)); path.lineTo(P(8.8, 5.0))
        p.drawPath(path)
    elif name == "grid":
        for a in (2, 9):
            for b in (2, 9):
                p.drawRoundedRect(R(a, b, 5, 5), 1.2 * u, 1.2 * u)
    elif name == "rows":
        for b in (3.2, 7.2, 11.2):
            p.drawRoundedRect(R(2, b, 2, 2), 0.5 * u, 0.5 * u)
            p.drawLine(P(6.5, b + 1), P(14, b + 1))
    elif name == "search":
        p.drawEllipse(R(2, 2, 9, 9))
        p.drawLine(P(9.6, 9.6), P(14, 14))
    elif name == "close":
        p.drawLine(P(4, 4), P(12, 12)); p.drawLine(P(12, 4), P(4, 12))
    elif name == "down":
        path.moveTo(P(4.2, 6)); path.lineTo(P(8, 9.8)); path.lineTo(P(11.8, 6))
        p.drawPath(path)
    elif name == "right":
        path.moveTo(P(6, 4.2)); path.lineTo(P(9.8, 8)); path.lineTo(P(6, 11.8))
        p.drawPath(path)
    elif name == "sort":
        for b, w in ((3.5, 11), (7.5, 8), (11.5, 5)):
            p.drawLine(P(2, b), P(2 + w, b))
    elif name == "note":
        p.setBrush(color)
        p.drawEllipse(R(3, 10, 5, 4))
        p.drawLine(P(7.6, 11.6), P(7.6, 3))
        path.moveTo(P(7.6, 3)); path.cubicTo(P(10, 3.4), P(12, 5), P(12, 7.5))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    elif name == "disc":
        p.drawEllipse(R(1.5, 1.5, 13, 13))
        p.drawEllipse(R(6, 6, 4, 4))
    elif name == "artist":
        p.drawEllipse(R(5.4, 2.6, 5.2, 5.2))
        path.moveTo(P(2.8, 13.6)); path.cubicTo(P(2.8, 9.8), P(13.2, 9.8), P(13.2, 13.6))
        p.drawPath(path)
    p.restore()


class _Button(QAbstractButton):
    """Shared by the painted buttons: hand cursor, a focus ring for the keyboard only, Enter clicks."""

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self._ring = sc, False
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)

    def focusInEvent(self, e):
        self._ring = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        super().focusInEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
        else:
            super().keyPressEvent(e)

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()


class ToolButton(_Button):
    """Flat glyph button, with optional text and a menu arrow."""

    def __init__(self, sc: Scheme, glyph: str, text: str = "", arrow: bool = False, parent=None):
        super().__init__(sc, parent)
        self.glyph, self.arrow = glyph, arrow
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self):
        w = 6 + 16 + 6
        if self.text():
            w += 5 + self.fontMetrics().horizontalAdvance(self.text())
        if self.arrow:
            w += 12
        return QSize(w, 26)

    def paintEvent(self, _):
        sc, h = self.sc, self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse() or self.isDown()
        if hot or (self.hasFocus() and self._ring):
            p.setPen(Qt.NoPen)
            p.setBrush(sc.well_line if self.isDown() else sc.well_hover)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4.5, 4.5)
        col = sc.fg if hot else sc.win_dim
        x = 6
        draw_glyph(p, self.glyph, QRectF(x, (h - 16) / 2, 16, 16), col)
        x += 16
        if self.text():
            p.setPen(col)
            p.drawText(QRect(x + 5, 0, self.width() - x - 5, h), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        if self.arrow:
            draw_glyph(p, "down", QRectF(self.width() - 18, (h - 12) / 2, 12, 12), col)


class PillButton(_Button):
    """Text button for the footer: filled with the accent when primary."""

    def __init__(self, sc: Scheme, text: str, primary: bool = False, parent=None):
        super().__init__(sc, parent)
        self.primary = primary
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 28, 28)

    def minimumSizeHint(self):
        return QSize(40, 28)

    def changeEvent(self, e):
        if e.type() == e.Type.EnabledChange:
            self.update()
        super().changeEvent(e)

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isEnabled()
        hot = on and (self.underMouse() or self.isDown())
        if self.primary:
            bg = sc.accent.lighter(112 if hot else 100)
            fg, edge = sc.on_accent, bg
        else:
            bg = sc.well_hover if hot else sc.well
            fg, edge = sc.fg, sc.well_line
        if not on:
            p.setOpacity(0.4)
        if self.hasFocus() and self._ring:
            edge = sc.accent
        p.setPen(QPen(edge, 1))
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        p.setPen(fg)
        t = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width() - 12)
        p.drawText(self.rect(), Qt.AlignCenter, t)


class Segmented(QWidget):
    """The mode switch: one pill, the current segment filled with the accent."""
    changed = Signal(int)

    def __init__(self, sc: Scheme, labels: list[str], parent=None):
        super().__init__(parent)
        self.sc, self.labels, self.cur, self._hot = sc, labels, 0, -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def minimumSizeHint(self):
        fm = self.fontMetrics()
        return QSize(4 + sum(fm.horizontalAdvance(t) + 16 for t in self.labels), 28)

    def set_current(self, i: int):
        self.cur = i
        self.update()

    def _at(self, x: float) -> int:
        n = len(self.labels)
        return max(0, min(n - 1, int((x - 2) * n / max(1, self.width() - 4))))

    def mousePressEvent(self, e):
        i = self._at(e.position().x())
        if e.button() == Qt.LeftButton and i != self.cur:
            self.set_current(i)
            self.changed.emit(i)

    def mouseMoveEvent(self, e):
        i = self._at(e.position().x())
        if i != self._hot:
            self._hot = i
            self.update()

    def leaveEvent(self, e):
        self._hot = -1
        self.update()

    def paintEvent(self, _):
        sc, n = self.sc, len(self.labels)
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(sc.well_line, 1))
        p.setBrush(sc.well)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 6, 6)
        sw = (w - 4) / n
        for i, text in enumerate(self.labels):
            r = QRectF(2 + i * sw, 2, sw, h - 4)
            if i == self.cur:
                p.setPen(Qt.NoPen)
                p.setBrush(sc.accent)
                p.drawRoundedRect(r, 4.5, 4.5)
                col = sc.on_accent
            else:
                col = sc.fg if i == self._hot else sc.win_dim
            p.setPen(col)
            p.drawText(r, Qt.AlignCenter, self.fontMetrics().elidedText(text, Qt.ElideRight, int(sw) - 8))


class ModeSwitch(QWidget):
    """The library's mode switch: the pill when its segments fit, else a button that names the current mode and
    opens a menu of them. Its minimum width is what the pill needs for the labels in base, so a mode added
    beyond them does not make the panel wider."""
    changed = Signal(int)

    def __init__(self, sc: Scheme, labels: list[str], glyphs: list[str], base: list[str], parent=None):
        super().__init__(parent)
        self.labels, self.glyphs, self.base, self.cur = labels, glyphs, base, 0
        self._pill = Segmented(sc, labels, self)
        self._pill.changed.connect(self._chose)
        self._drop = ToolButton(sc, glyphs[0], labels[0], arrow=True, parent=self)
        self._drop.setToolTip("Library view")
        self._drop.clicked.connect(self._menu)
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def minimumSizeHint(self):
        fm = self.fontMetrics()
        return QSize(4 + sum(fm.horizontalAdvance(t) + 16 for t in self.base), 28)

    def set_current(self, i: int):
        self.cur = i
        self._pill.set_current(i)
        self._drop.glyph = self.glyphs[i]
        self._drop.setText(self.labels[i])
        self._drop.update()
        self._place()

    def _place(self):
        fm, w = self.fontMetrics(), self.width()
        pill = w >= 4 + len(self.labels) * (max(fm.horizontalAdvance(t) for t in self.labels) + 12)
        self._pill.setVisible(pill)
        self._drop.setVisible(not pill)
        self._pill.setGeometry(0, 0, w, self.height())
        self._drop.setGeometry(0, 0, min(self._drop.sizeHint().width(), w), self.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange:
            self._place()
        super().changeEvent(e)

    def _chose(self, i: int):
        if i != self.cur:
            self.set_current(i)
            self.changed.emit(i)

    def _menu(self):
        menu = QMenu(self)
        for i, text in enumerate(self.labels):
            a = menu.addAction(text)
            a.setCheckable(True)
            a.setChecked(i == self.cur)
            a.triggered.connect(lambda _=False, i=i: self._chose(i))
        menu.exec(self._drop.mapToGlobal(QPoint(0, self._drop.height())))


class ProgressLine(QWidget):
    """The hairline under the header; it turns into a slim progress bar while scanning."""

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.frac = sc, None          # None idle, -1 unknown, else 0..1
        self.setFixedHeight(2)
        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self.update)

    def set(self, frac):
        self.frac = frac
        if frac == -1:
            self._tick.start()
        else:
            self._tick.stop()
        self.update()

    def paintEvent(self, _):
        sc, w, h = self.sc, self.width(), self.height()
        p = QPainter(self)
        if self.frac is None:
            p.fillRect(0, h - 1, w, 1, sc.line)
            return
        p.fillRect(0, 0, w, h, sc.line)
        if self.frac == -1:
            seg = max(24, w // 4)
            x = int((time.perf_counter() * 0.9 % 1.0) * (w + seg)) - seg
            p.fillRect(max(0, x), 0, min(w, x + seg) - max(0, x), h, sc.accent)
        else:
            p.fillRect(0, 0, int(w * self.frac), h, sc.accent)


class StatusLine(QWidget):
    """One dim line of small text: counts on the left, the selection on the right."""

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.left, self.right = sc, "", ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self):
        return QSize(100, QFontMetrics(scaled_font(self.font(), 0.88)).height() + 2)

    def minimumSizeHint(self):
        return QSize(60, self.sizeHint().height())

    def set_texts(self, left: str, right: str = ""):
        if (left, right) != (self.left, self.right):
            self.left, self.right = left, right
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        f = scaled_font(self.font(), 0.88)
        p.setFont(f)
        p.setPen(self.sc.dim)
        fm = p.fontMetrics()
        rw = fm.horizontalAdvance(self.right) + 8 if self.right else 0
        p.drawText(QRect(0, 0, self.width() - rw, self.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self.left, Qt.ElideRight, self.width() - rw))
        if self.right:
            p.setPen(self.sc.fg)
            p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignRight, self.right)


class EmptyState(QWidget):
    """A centred glyph, a line, a hint and an optional button, for pages with nothing to show."""
    action = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc
        self.glyph, self.title, self.hint = "folder", "", ""
        self.button = PillButton(sc, "", primary=True, parent=self)
        self.button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.button.clicked.connect(self.action)
        self.button.hide()

    def show_message(self, glyph: str, title: str, hint: str = "", button: str = ""):
        self.glyph, self.title, self.hint = glyph, title, hint
        self.button.setText(button)
        self.button.setVisible(bool(button))
        self.button.setFixedSize(self.button.sizeHint().width() + 12, 30)
        self._layout()
        self.update()

    def _layout(self):
        w = min(260, max(80, self.width() - 32))
        f = scaled_font(self.font(), 0.92)
        self._hint_f = f
        hint_h = QFontMetrics(f).boundingRect(QRect(0, 0, w, 2000), Qt.AlignHCenter | Qt.TextWordWrap,
                                              self.hint).height() if self.hint else 0
        title_h = self.fontMetrics().height() + 2
        total = 44 + 14 + title_h + (6 + hint_h if hint_h else 0) + (16 + 30 if self.button.isVisible() or self.button.text() else 0)
        y = max(8, (self.height() - total) // 2 - 12)
        cx = self.width() // 2
        self._glyph_r = QRectF(cx - 22, y, 44, 44)
        y += 44 + 14
        self._title_r = QRect(cx - w // 2, y, w, title_h)
        y += title_h + (6 if hint_h else 0)
        self._hint_r = QRect(cx - w // 2, y, w, hint_h)
        y += hint_h + 16
        self.button.move(cx - self.button.width() // 2, y)

    def resizeEvent(self, e):
        self._layout()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.fillRect(self.rect(), sc.base)
        draw_glyph(p, self.glyph, self._glyph_r, sc.dimmer)
        p.setPen(sc.fg)
        p.drawText(self._title_r, Qt.AlignHCenter | Qt.AlignVCenter, self.title)
        if self.hint:
            p.setFont(self._hint_f)
            p.setPen(sc.dim)
            p.drawText(self._hint_r, Qt.AlignHCenter | Qt.TextWordWrap, self.hint)
