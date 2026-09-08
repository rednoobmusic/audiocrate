from __future__ import annotations
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QPushButton,
)
from player import Player


class UpNextWidget(QWidget):
    """Dedicated, dockable view of the upcoming play order.

    Mirrors the Playlist panel's inline queue but lives in its own dock so it
    can be floated into a separate window, resized freely, and toggled via the
    View menu. Double-click (or Enter) jumps straight to a track.
    """

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player = player
        self._indices: list[int] = []
        self._dirty = False
        self._build()

        player.track_changed.connect(self._refresh)
        player.queue_changed.connect(self._refresh)
        player.order_changed.connect(self._refresh)
        self._refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        self._heading = QLabel("Up Next")
        self._heading.setObjectName("track_sub")
        hdr.addWidget(self._heading)
        hdr.addStretch()
        self._clear_btn = QPushButton("Clear queue")
        self._clear_btn.setFlat(True)
        self._clear_btn.setToolTip("Remove every track from the queue")
        self._clear_btn.clicked.connect(self._player.clear)
        hdr.addWidget(self._clear_btn)
        root.addLayout(hdr)

        self._now = QLabel("")
        self._now.setObjectName("track_title")
        self._now.setWordWrap(True)
        root.addWidget(self._now)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setSelectionMode(QListWidget.NoSelection)
        self._list.itemActivated.connect(self._jump)
        self._list.itemDoubleClicked.connect(self._jump)
        root.addWidget(self._list, 1)

    def _refresh(self, *_):
        if not self.isVisible():
            self._dirty = True      # dock hidden — rebuild on next show instead
            return
        self._dirty = False
        self._list.clear()
        self._indices = []
        q = self._player.queue

        cur = self._player.current
        self._now.setText(f"▶  {cur.display()}" if cur else "Nothing playing")

        upcoming = self._player.upcoming(500)
        for n, idx in enumerate(upcoming, start=1):
            if not 0 <= idx < len(q):
                continue
            t = q[idx]
            item = QListWidgetItem(f"{n:>3}.  {t.display()}")
            item.setToolTip(str(t.path))
            self._list.addItem(item)
            self._indices.append(idx)

        cnt = len(self._indices)
        if cnt:
            self._heading.setText(f"Up Next · {cnt} track{'s' if cnt != 1 else ''}")
        else:
            self._heading.setText("Up Next · end of queue")

    def showEvent(self, e):
        super().showEvent(e)
        if self._dirty:
            self._refresh()

    def _jump(self, item):
        row = self._list.row(item)
        if 0 <= row < len(self._indices):
            self._player.play_track(self._indices[row])
