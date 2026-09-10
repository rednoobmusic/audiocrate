"""Motion taken from the site's UI, rewritten for widgets.

The site does all of this with CSS transitions and keyframes. A Qt widget has
neither, so each effect here is either an animation driven by the property
system or something painted by hand on a timer.

The one thing worth carrying across whole is the curve. Nearly every hover and
flyout on the site eases with cubic-bezier(0.34, 1.56, 0.64, 1), which overshoots
its target and settles back. That is QEasingCurve.OutBack with a gentler
overshoot than the default, and it is most of why the site feels the way it does.
"""
from __future__ import annotations

import random

from PySide6.QtCore import (QEasingCurve, QObject, QEvent, QPointF,
                            QPropertyAnimation, QRect, QTimer, Qt)
from PySide6.QtGui import QColor, QCursor, QLinearGradient, QPainter
from PySide6.QtWidgets import QMenu, QWidget

try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None

# cubic-bezier(0.34, 1.56, 0.64, 1). OutBack's default overshoot of 1.70158
# throws further than the site does, so it is dialled back to match.
_OVERSHOOT = 1.10


def spring(overshoot: float = _OVERSHOOT) -> QEasingCurve:
    """The site's springy curve, for anything arriving somewhere."""
    c = QEasingCurve(QEasingCurve.OutBack)
    c.setOvershoot(overshoot)
    return c


# The entrance cascade that used to live here is gone. It put a
# QGraphicsOpacityEffect on every visible dock, and three of them wrap a native
# surface: Video and Now Playing hold a QVideoWidget, Milkdrop holds a
# QOpenGLWidget. Qt does not support a graphics effect over any of those and
# segfaults rather than ignoring it. Nothing below uses QGraphicsEffect at all.


# flyouts

def warp_in(widget: QWidget, ms: int = 180):
    """The flyout-in warp, for menus and dropdowns.

    The site scales from 0.55 with a tilt. A popup cannot be tilted without
    rendering it itself, so this keeps the scale and the overshoot and lets the
    fade stand in for the rotation.
    """
    end = widget.geometry()
    if end.isEmpty():
        return None
    # setWindowOpacity, not a graphics effect: a menu is a top level popup, and
    # an effect on one is both unsupported and a lifetime problem, since the
    # animation would outlive the object it animates.
    widget.setWindowOpacity(0.0)
    fade = QPropertyAnimation(widget, b"windowOpacity", widget)
    fade.setDuration(ms)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve(QEasingCurve.OutCubic))
    fade.start(QPropertyAnimation.DeleteWhenStopped)
    cx, cy = end.x(), end.y()          # menus grow from their top corner
    small = QRect(cx, cy, max(1, int(end.width() * 0.86)), max(1, int(end.height() * 0.7)))
    anim = QPropertyAnimation(widget, b"geometry", widget)
    anim.setDuration(ms)
    anim.setStartValue(small)
    anim.setEndValue(end)
    anim.setEasingCurve(spring())
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    return anim


class _MenuWarp(QObject):
    """Warps every menu the application shows.

    Installed once on the application rather than on each menu, so context menus
    built fresh on every right-click are covered without every panel having to
    remember to ask.
    """

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QMenu):
            QTimer.singleShot(0, lambda m=obj: self._warp(m))
        return False

    @staticmethod
    def _warp(menu):
        # A context menu is built fresh per right click and destroyed after, so
        # the C++ object behind this wrapper may already be gone by now.
        if not _qt_valid(menu):
            return
        try:
            warp_in(menu)
        except RuntimeError:
            pass


_menu_warp = _MenuWarp()


def install_menu_warp(app) -> None:
    """Give every menu in the application the flyout animation."""
    app.installEventFilter(_menu_warp)


# shine

class Shine(QWidget):
    """A band of light that sweeps across whatever it covers.

    The site's @keyframes shine, a 105 degree gradient running transparent,
    white at the middle, transparent. Used here to say a widget is busy without
    adding a spinner.
    """

    _PERIOD_MS = 1400

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._pos = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, ev):
        if obj is self.parent() and ev.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def start(self):
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _advance(self):
        self._pos = (self._pos + 16.0 / self._PERIOD_MS) % 1.6
        self.update()

    def paintEvent(self, _):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        p = QPainter(self)
        # 105 degrees, so the band leans forward as it crosses
        span = w + h
        x = (self._pos - 0.3) * span
        g = QLinearGradient(x, 0, x + span * 0.35, h)
        for stop, a in ((0.0, 0), (0.5, 90), (1.0, 0)):
            g.setColorAt(stop, QColor(255, 255, 255, a))
        p.fillRect(0, 0, w, h, g)
        p.end()


# cursor particles

class CursorParticles(QWidget):
    """The site's cursor trail, as a transparent layer over a window.

    The web version listens for mousemove. A transparent overlay never receives
    those, and child widgets would swallow them anyway, so the pointer is polled
    instead. That also means the trail carries on over buttons and lists rather
    than dying wherever a child grabs the mouse.
    """

    _PER_MOVE = 2          # spawned per frame the pointer actually moved
    _GRAVITY  = 0.025
    _MAX      = 400        # a ceiling, in case a frame is dropped mid-sweep

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._parts: list[list[float]] = []      # x, y, vx, vy, life, decay, size
        self._last = QPointF(-1.0, -1.0)
        self._colour = QColor(255, 255, 255)
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.show()
        self.raise_()

    def set_accent(self, palette: dict | None):
        if palette:
            self._colour = QColor(palette.get("accent", palette.get("accent_l", "#ffffff")))
        else:
            self._colour = QColor(255, 255, 255)

    def eventFilter(self, obj, ev):
        if obj is self.parent() and ev.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def _tick(self):
        pos = self.mapFromGlobal(QCursor.pos())
        inside = self.rect().contains(pos)
        moved = (abs(pos.x() - self._last.x()) + abs(pos.y() - self._last.y())) > 0.5
        if inside and moved and len(self._parts) < self._MAX:
            for _ in range(self._PER_MOVE):
                self._parts.append([
                    float(pos.x()), float(pos.y()),
                    (random.random() - 0.5) * 1.2,
                    (random.random() - 0.5) * 1.2 - 0.4,
                    1.0,
                    0.038 + random.random() * 0.025,
                    1.2 + random.random() * 1.6,
                ])
        self._last = QPointF(pos)

        alive = []
        for q in self._parts:
            q[0] += q[2]
            q[1] += q[3]
            q[3] += self._GRAVITY
            q[4] -= q[5]
            if q[4] > 0.0:
                alive.append(q)
        self._parts = alive
        if self._parts or moved:
            self.update()

    def paintEvent(self, _):
        if not self._parts:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        c = QColor(self._colour)
        for x, y, _vx, _vy, life, _decay, size in self._parts:
            c.setAlphaF(max(0.0, min(1.0, life * 0.55)))
            p.setBrush(c)
            r = max(size * life, 0.3)
            p.drawEllipse(QPointF(x, y), r, r)
        p.end()
