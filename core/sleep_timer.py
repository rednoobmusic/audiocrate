"""Sleep timer — pause playback after a fixed delay.

The timed countdown lives here; the "end of current track" mode is handled by
the Player (set_stop_after_current), since only the player knows when a track
truly ends (and it must disable gapless preload so the track doesn't roll on).
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, QTimer


class SleepTimer(QObject):
    tick          = Signal(int)    # whole seconds remaining
    triggered     = Signal()       # countdown reached zero
    state_changed = Signal(bool)   # active / inactive

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)
        self._remaining = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def remaining(self) -> int:
        return self._remaining if self._active else 0

    def start(self, minutes: float):
        """Begin a countdown of `minutes`; emits `triggered` when it elapses."""
        self._remaining = max(1, int(round(minutes * 60)))
        self._active = True
        self._timer.start()
        self.state_changed.emit(True)
        self.tick.emit(self._remaining)

    def cancel(self):
        if not self._active:
            return
        self._timer.stop()
        self._active = False
        self._remaining = 0
        self.state_changed.emit(False)

    def _on_tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self._active = False
            self.triggered.emit()
            self.state_changed.emit(False)
        else:
            self.tick.emit(self._remaining)

    @staticmethod
    def fmt(seconds: int) -> str:
        m, s = divmod(max(0, seconds), 60)
        return f"{m}:{s:02d}"
