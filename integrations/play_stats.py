"""Local listening statistics: most-played tracks and artists, and total listening
time. Off by default (opt-in in Settings). Everything lives in one JSON file under
~/.config/audiocrate/ and nothing is sent anywhere; this module imports no
networking library.

Two countings, both fed by the same PlayedThresholdTracker:
 - Total listening time (and each track's own listened_ms) adds every second of
   real forward playback. Pausing and seeking add nothing, but a track you stop
   halfway through still counts what you heard.
 - Play count (and so "top tracks" and "top artists") goes up once a track has
   been heard for 50% of its duration, capped at 4 minutes. Scrobbling uses the
   same rule, so a quick skip is never a play.

A per-day history (plays and listened ms per track, by local date) feeds the
Stats panel's over-time chart. It starts empty and fills from the day it was
added; nothing is back-filled.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from artists import split_artists
from player import Player
from played_tracker import PlayedThresholdTracker
from safe_write import write_text

_STATS_FILE = Path.home() / ".config" / "audiocrate" / "play_stats.json"
_SAVE_DEBOUNCE_MS = 2000


def _track_key(track) -> str:
    """Group by artist+title (not file path) so re-tagged/re-encoded copies of
    the same song still accumulate one history, matching how Last.fm groups."""
    artist = (track.artist or "").strip().lower()
    title = (track.title or "").strip().lower()
    return f"{artist}\x1f{title}" if artist else f"\x1f{title}"


class PlayStatsTracker(QObject):
    stats_changed = Signal()   # emitted (debounced) after any update, for a live panel

    def __init__(self, player: Player, parent: QObject | None = None):
        super().__init__(parent)
        self._player = player
        self._enabled = False
        self._tracker = PlayedThresholdTracker()
        self._data = self._load()
        self._dirty = False   # unsaved changes pending (coalesced by _save_timer)
        self._save_failed = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._on_save_timer)

        # position ticks arrive about every 100 ms and each can add listened time,
        # but the panel only needs to repaint a couple of times a second
        self._live_update_timer = QTimer(self)
        self._live_update_timer.setSingleShot(True)
        self._live_update_timer.setInterval(500)
        self._live_update_timer.timeout.connect(self.stats_changed.emit)

        player.track_changed.connect(self._on_track_changed)
        player.position_changed.connect(self._on_position)

    def set_enabled(self, on: bool):
        self._enabled = bool(on)
        if self._enabled:
            self._tracker.reset()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # persistence
    @staticmethod
    def _read(path: Path):
        """The stats in `path`, or None when it is missing or is not a stats file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            data = None
        return data if isinstance(data, dict) and isinstance(data.get("tracks"), dict) else None

    def _load(self) -> dict:
        """Last session's stats. A file that cannot be read is put aside under another name instead of being overwritten
        by the empty stats of the next save, and the copy from the start of the last session takes its place."""
        backup = _STATS_FILE.with_name(_STATS_FILE.name + ".bak")
        data = self._read(_STATS_FILE)
        if data is None and _STATS_FILE.exists():
            from applog import log
            aside = _STATS_FILE.with_name(f"{_STATS_FILE.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(_STATS_FILE, aside)
            except OSError:
                aside = _STATS_FILE
            log.warning("the play stats could not be read; the file is kept as %s", aside)
            data = self._read(backup)
        elif data is not None:
            try:
                write_text(backup, json.dumps(data))
            except OSError:
                pass
        if data is None:
            data = {"tracks": {}, "total_listened_ms": 0}
        data.setdefault("total_listened_ms", 0)
        data.setdefault("daily", {})   # absent in files from before the history existed
        return data

    def _save(self):
        try:
            _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_text(_STATS_FILE, json.dumps(self._data))
        except Exception:
            if not self._save_failed:
                from applog import log
                log.exception("failed to save the play stats to %s", _STATS_FILE)
            self._save_failed = True        # one line in the log, then it is tried again with the next change
        else:
            self._save_failed = False

    def _queue_save(self):
        self._dirty = True
        if not self._save_timer.isActive():
            self._save_timer.start()

    def _on_save_timer(self):
        if self._dirty:
            self._save()

    def flush(self):
        """Save immediately (called on app shutdown so nothing is lost)."""
        self._save_timer.stop()
        if self._dirty:
            self._save()

    # tracking
    def _on_track_changed(self, _track):
        self._tracker.reset()

    def _on_position(self, _frac: float, pos_ms: int, dur_ms: int):
        if not self._enabled:
            return
        just_played, delta_ms = self._tracker.update(pos_ms, dur_ms)
        if delta_ms > 0:
            self._add_listened_time(delta_ms)
        if just_played:
            self._record_play()

    def _track_entry(self, track) -> dict:
        key = _track_key(track)
        entry = self._data["tracks"].setdefault(key, {
            "artist": track.artist or "", "title": track.title,
            "play_count": 0, "listened_ms": 0,
        })
        # Keep the most recently seen artist/title in case tags changed.
        entry["artist"] = track.artist or entry["artist"]
        if track.artists:
            entry["artists"] = list(track.artists)
        entry["title"] = track.title
        return entry

    def _bump_daily(self, track, plays: int, ms: int):
        day = self._data["daily"].setdefault(datetime.date.today().isoformat(), {})
        cell = day.setdefault(_track_key(track), [0, 0])
        cell[0] += plays
        cell[1] += ms

    def _add_listened_time(self, delta_ms: int):
        """Every real forward-playback ms, whether or not the current track has
        crossed the played threshold yet."""
        self._data["total_listened_ms"] = self._data.get("total_listened_ms", 0) + delta_ms
        track = self._player.current
        if track is not None and track.title:
            self._track_entry(track)["listened_ms"] += delta_ms
            self._bump_daily(track, 0, delta_ms)
        self._queue_save()
        if not self._live_update_timer.isActive():
            self._live_update_timer.start()

    def _record_play(self):
        track = self._player.current
        if track is None or not track.title:
            return
        self._track_entry(track)["play_count"] += 1
        self._bump_daily(track, 1, 0)
        self._queue_save()
        self.stats_changed.emit()

    # queries (for the Stats panel)
    def total_listened_ms(self) -> int:
        return int(self._data.get("total_listened_ms", 0))

    def top_tracks(self, limit: int = 25) -> list[dict]:
        # a skimmed track (never past the played threshold) has listened_ms for
        # the raw total but does not belong in "top tracks"
        items = [e for e in self._data["tracks"].values() if e["play_count"] > 0]
        items.sort(key=lambda e: e["play_count"], reverse=True)
        return items[:limit]

    @staticmethod
    def _names(e: dict) -> list[str]:
        """Who a track is by, one name each. Entries saved before the names were kept are split from their line."""
        return e.get("artists") or split_artists([e["artist"]]) or ["Unknown"]

    def top_artists(self, limit: int = 25) -> list[dict]:
        by_artist: dict[str, dict] = {}
        for e in self._data["tracks"].values():
            if e["play_count"] == 0:
                continue
            for artist in self._names(e):          # a play counts for each of the artists
                a = by_artist.setdefault(artist, {"artist": artist, "play_count": 0, "listened_ms": 0})
                a["play_count"] += e["play_count"]
                a["listened_ms"] += e["listened_ms"]
        items = list(by_artist.values())
        items.sort(key=lambda e: e["play_count"], reverse=True)
        return items[:limit]

    def track_count(self) -> int:
        return sum(1 for e in self._data["tracks"].values() if e["play_count"] > 0)

    def history(self, by: str = "artist", measure: str = "plays",
                days: int | None = None, top: int = 8):
        """Cumulative per-day series for the over-time chart.

        Returns (dates, series): every date from the start of the range to
        today (at least two, so a single day still draws a line) and up to
        `top` (label, values) pairs, one value per date, highest final value
        first. Plays are counts, time is minutes. `days=None` starts at the
        first recorded day.
        """
        daily = self._data["daily"]
        tracks = self._data["tracks"]
        today = datetime.date.today()
        if days is not None:
            start = today - datetime.timedelta(days=days - 1)
        else:
            start = datetime.date.fromisoformat(min(daily)) if daily else today
        start = min(start, today - datetime.timedelta(days=1))
        dates = [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]

        col = 0 if measure == "plays" else 1
        labels: dict[str, list[str]] = {}
        per_label: dict[str, dict[int, int]] = {}
        for i, d in enumerate(dates):
            for key, cell in daily.get(d.isoformat(), {}).items():
                if not cell[col] or key not in tracks:
                    continue
                names = labels.get(key)
                if names is None:
                    e = tracks[key]
                    if by == "artist":
                        names = self._names(e)
                    else:
                        names = [f"{e['artist']} - {e['title']}" if e["artist"] else e["title"]]
                    labels[key] = names
                for label in names:
                    per_day = per_label.setdefault(label, {})
                    per_day[i] = per_day.get(i, 0) + cell[col]

        totals = {label: sum(per_day.values()) for label, per_day in per_label.items()}
        series = []
        for label in sorted(totals, key=lambda l: (-totals[l], l))[:top]:
            running, values = 0, []
            for i in range(len(dates)):
                running += per_label[label].get(i, 0)
                values.append(running if col == 0 else running / 60000)   # time is ms, charted as minutes
            series.append((label, values))
        return dates, series

    def clear(self):
        self._data = {"tracks": {}, "total_listened_ms": 0, "daily": {}}
        self._save()
        self.stats_changed.emit()
