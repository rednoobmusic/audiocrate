"""Whether the machine is in a power saving profile, so the animated panels can
drop to a lower frame rate there instead of fighting for a throttled CPU."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

_PROFILE = "/sys/firmware/acpi/platform_profile"
_SAVING = {"low-power", "quiet"}
_SAVER_MS = 33     # frame interval ceiling while saving (30 fps)


def _read() -> bool:
    try:
        with open(_PROFILE) as f:
            return f.read().strip() in _SAVING
    except OSError:
        return False


class _Watch(QObject):
    changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.saving = _read()
        t = QTimer(self)
        t.setInterval(3000)
        t.timeout.connect(self._poll)
        t.start()

    def _poll(self):
        s = _read()
        if s != self.saving:
            self.saving = s
            self.changed.emit(s)


_watch: _Watch | None = None


def watch() -> _Watch:
    """The shared watcher. Needs a QApplication, so it is made on first use."""
    global _watch
    if _watch is None:
        _watch = _Watch()
    return _watch


def frame_ms(base_ms: int) -> int:
    """base_ms, or a 30 fps interval if that is slower and the machine is saving power."""
    return max(base_ms, _SAVER_MS) if watch().saving else base_ms
