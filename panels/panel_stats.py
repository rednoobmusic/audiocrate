from __future__ import annotations
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QPushButton, QTabWidget, QMessageBox,
)
from play_stats import PlayStatsTracker


def _fmt_duration(ms: int) -> str:
    """Human-readable elapsed time, using the two largest applicable units —
    scales from seconds up through years for a cumulative listening total
    that can run arbitrarily high over months/years of use."""
    total_s = ms // 1000
    years, rem = divmod(total_s, 31_536_000)     # 365-day year
    months, rem = divmod(rem, 2_592_000)         # 30-day month
    days, rem = divmod(rem, 86_400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)

    if years:
        return f"{years}y {months}mo"
    if months:
        return f"{months}mo {days}d"
    if days:
        return f"{days}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class StatsWidget(QWidget):
    """Local-only listening stats: top tracks, top artists, total time.

    Everything here reads from PlayStatsTracker, which is entirely local
    (a JSON file under ~/.config/audiocrate/) — nothing is ever sent anywhere.
    """

    def __init__(self, stats: PlayStatsTracker, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._build()
        stats.stats_changed.connect(self._refresh)
        self._refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._summary = QLabel("")
        self._summary.setObjectName("track_sub")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._tabs = QTabWidget()
        self._tracks_list = QListWidget()
        self._tracks_list.setAlternatingRowColors(True)
        self._artists_list = QListWidget()
        self._artists_list.setAlternatingRowColors(True)
        self._tabs.addTab(self._tracks_list, "Top Tracks")
        self._tabs.addTab(self._artists_list, "Top Artists")
        root.addWidget(self._tabs, 1)

        row = QHBoxLayout()
        row.addStretch()
        clear_btn = QPushButton("Clear stats…")
        clear_btn.setFlat(True)
        clear_btn.clicked.connect(self._on_clear)
        row.addWidget(clear_btn)
        root.addLayout(row)

    def _refresh(self, *_):
        total = self._stats.total_listened_ms()
        n = self._stats.track_count()
        self._summary.setText(
            f"{_fmt_duration(total)} total listening time · {n} track{'s' if n != 1 else ''} tracked"
        )

        self._tracks_list.clear()
        for i, e in enumerate(self._stats.top_tracks(100), start=1):
            label = f"{e['artist']} - {e['title']}" if e["artist"] else e["title"]
            plays = e["play_count"]
            item = QListWidgetItem(f"{i:>3}.  {label}   ·   {plays} play{'s' if plays != 1 else ''}")
            self._tracks_list.addItem(item)

        self._artists_list.clear()
        for i, e in enumerate(self._stats.top_artists(100), start=1):
            plays = e["play_count"]
            item = QListWidgetItem(
                f"{i:>3}.  {e['artist']}   ·   {plays} play{'s' if plays != 1 else ''}   ·   {_fmt_duration(e['listened_ms'])}"
            )
            self._artists_list.addItem(item)

    def _on_clear(self):
        if QMessageBox.question(
            self, "Clear listening stats",
            "Permanently delete all locally tracked play counts and listening time?"
        ) == QMessageBox.Yes:
            self._stats.clear()
