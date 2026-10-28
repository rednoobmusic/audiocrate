"""Two small animations that run on top of what the widgets already do.

A widget takes its new state at once; these only hide the jump for a moment by painting over it, and every
one ends by putting away what it drew. The switch is Settings > System > Animations; AUDIOCRATE_FX=0 turns
it off for the process, as it does for the status bar shine.

set_text/hold/release dissolve the old look of a widget into the new one (a title, the cover, the play
glyph); Glide carries a play head from step to step. The old look is painted by the widget's own drawing
code, never by rendering the widget: render() and grab() flush layout work of their own and move a
window's minimum size, which these must not do.
"""
from __future__ import annotations

import os
import time
import weakref

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QTimer, QVariantAnimation, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget

TEXT_MS = 120       # a label taking its new text
ART_MS = 220        # a new cover
HOLD_MS = 450       # the old cover stays this long while the new one is read

_FX_OFF = os.environ.get("AUDIOCRATE_FX", "").strip().lower() in ("0", "off", "none")
_on = not _FX_OFF
_live = weakref.WeakSet()    # animations in flight, so the switch can end them


def enabled() -> bool:
    return _on


def set_enabled(on: bool) -> None:
    """The Settings switch. Turning it off ends everything running right now."""
    global _on
    _on = bool(on) and not _FX_OFF
    if not _on:
        finish_all()


def finish_all() -> None:
    for a in list(_live):
        try:
            a.finish()
        except RuntimeError:        # its widget is gone
            pass
    _live.clear()


def _clock(owner: QObject, ms: int, easing=QEasingCurve.OutCubic, start=0.0, end=1.0) -> QVariantAnimation:
    a = QVariantAnimation(owner)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setDuration(ms)
    a.setEasingCurve(QEasingCurve(easing))
    return a


class _Ghost(QWidget):
    """A child of the faded widget that paints how it looked, on top and fading."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.draw = None
        self.alpha = 1.0
        self.hide()

    def paintEvent(self, _):
        if self.draw is not None:
            p = QPainter(self)
            p.setOpacity(self.alpha)
            self.draw(p)
            p.end()


class _Fade(QObject):
    """One per faded widget, made on first use and owned by it."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("anim_fade")
        self._ghost = _Ghost(host)
        self._run = _clock(self, TEXT_MS, start=1.0, end=0.0)
        self._run.valueChanged.connect(self._frame)
        self._run.finished.connect(self.finish)
        self._wait = QTimer(self)
        self._wait.setSingleShot(True)
        self._wait.timeout.connect(self.go)
        host.installEventFilter(self)

    def snap(self, draw, ms: int, hold_ms: int = 0) -> None:
        """Paint `draw(painter)`, the widget's old look, over the widget and dissolve it. Call it just before
        the change. With hold_ms the old look stays whole until go() or until that time is up."""
        host = self.parent()
        if not _on or not host.isVisible() or host.width() < 2 or host.height() < 2:
            self.finish()
            return
        self._run.stop()
        self._wait.stop()
        g = self._ghost
        g.draw, g.alpha = draw, 1.0
        g.setGeometry(host.rect())
        g.show()
        g.raise_()
        self._run.setDuration(ms)
        _live.add(self)
        if hold_ms:
            self._wait.start(hold_ms)
        else:
            self._run.start()

    def go(self) -> None:
        self._wait.stop()
        if self._ghost.draw is not None and self._run.state() != QVariantAnimation.Running:
            self._run.start()

    def _frame(self, v) -> None:
        self._ghost.alpha = float(v)
        self._ghost.update()

    def finish(self) -> None:
        self._run.stop()
        self._wait.stop()
        self._ghost.draw = None
        self._ghost.hide()
        _live.discard(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Resize, QEvent.Hide):       # never animate across a layout change
            self.finish()
        return False


def _fade_of(host: QWidget) -> _Fade:
    return host.findChild(_Fade, "anim_fade", Qt.FindDirectChildrenOnly) or _Fade(host)


def set_text(label, text: str) -> None:
    """label.setText(text), with the old text dissolving into the new."""
    if (label.fullText() if hasattr(label, "fullText") else label.text()) != text:
        old, font, color = label.text(), label.font(), label.palette().color(label.foregroundRole())
        flags = int(label.alignment() if hasattr(label, "alignment") else Qt.AlignCenter)
        if getattr(label, "wordWrap", lambda: False)():
            flags |= Qt.TextWordWrap

        def draw(p):
            p.setFont(font)
            p.setPen(color)
            p.drawText(label.contentsRect(), flags, old)
        _fade_of(label).snap(draw, TEXT_MS)
    label.setText(text)


def hold(host: QWidget, draw) -> None:
    """Call before clearing a widget that is about to get new content: `draw(painter)` paints its old look,
    which stays until release()."""
    _fade_of(host).snap(draw, ART_MS, HOLD_MS)


def release(host: QWidget) -> None:
    f = host.findChild(_Fade, "anim_fade", Qt.FindDirectChildrenOnly)
    if f is not None:
        f.go()


class Glide(QObject):
    """Carries a value to the next one it is given, for a position that arrives in steps. `apply` shows the
    value and is called with the final one when the glide ends or is cut short."""

    def __init__(self, host: QWidget, apply):
        super().__init__(host)
        self._host, self._apply = host, apply
        self._cur, self._from, self._goal, self._last = 0.0, 0.0, None, 0.0
        self._run = _clock(self, 100, QEasingCurve.Linear)
        self._run.valueChanged.connect(self._frame)
        self._run.finished.connect(self.finish)

    def to(self, value: float, scale: float) -> None:
        """Move to `value`; `scale` is the pixels a whole unit covers. A step under two pixels, a jump back
        and a long jump just happen."""
        now = time.monotonic()
        gap, self._last = (now - self._last) * 1000.0, now
        px = (value - self._cur) * scale
        self.stop()
        if not _on or not self._host.isVisible() or not 2.0 <= px <= 80.0:
            self._cur = value
            self._apply(value)
            return
        self._from, self._goal = self._cur, value
        self._run.setDuration(int(max(40, min(150, gap))))       # about one step, the pace of the updates
        _live.add(self)
        self._run.start()

    def stop(self) -> None:
        """The value was placed directly, or a new step starts; the running glide gives way."""
        self._run.stop()
        self._goal = None
        _live.discard(self)

    def place(self, value: float) -> None:
        self._cur = value

    def _frame(self, v) -> None:
        if self._goal is not None:
            self._cur = self._from + (self._goal - self._from) * float(v)
            self._apply(self._cur)

    def finish(self) -> None:
        goal = self._goal
        self.stop()
        if goal is not None:
            self._cur = goal
            self._apply(goal)
