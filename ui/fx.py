"""Motion taken from the site's UI, rewritten for widgets.

The site does all of this with CSS transitions and keyframes. A Qt widget has
neither, so each effect here is either an animation driven by the property
system or something painted by hand on a timer.

Nearly every hover and flyout on the site eases with
cubic-bezier(0.34, 1.56, 0.64, 1), which overshoots its target and settles
back. That is QEasingCurve.OutBack with a gentler overshoot than the default.
"""
from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QObject, QEvent, QPropertyAnimation,
                            QRect, QTimer, Qt)
from PySide6.QtGui import QColor, QLinearGradient, QPainter
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


# Nothing here uses a QGraphicsEffect. Three docks wrap a native surface (Video
# and Now Playing hold a QVideoWidget, Milkdrop embeds a QOpenGLWindow), and Qt
# does not support a graphics effect over any of those: it segfaults rather
# than ignoring it.


# flyouts

def warp_in(widget: QWidget, ms: int = 180):
    """The flyout-in warp, for menus and dropdowns.

    The site scales from 0.55 with a tilt. A popup cannot be tilted without
    rendering it itself, so this grows from 86% of the width and 70% of the
    height with the same overshoot, and lets the fade stand in for the rotation.
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
        # No WA_TranslucentBackground here. It is meant for top level windows,
        # and on a child it can force a native window of its own, which is the
        # same thing that crashed when an effect met the GL visualizer.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
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
