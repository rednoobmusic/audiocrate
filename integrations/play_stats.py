"""Local-only listening statistics — most-played tracks/artists and total
listening time. Off by default (opt-in in Settings). Everything here is a
plain JSON file under ~/.config/audiocrate/ — nothing is ever sent anywhere;
this module doesn't import urllib or any networking library at all.

Two different countings, both fed by the same PlayedThresholdTracker:
 - Total listening time (and each track's own listened_ms) accumulates every
   real forward-playback second, continuously — pausing/seeking doesn't add
   time, but a track you stop halfway through still counts what you heard.
 - Play count (and therefore "top tracks"/"top artists") only increments once
   a track has been listened to for 50% of its duration (capped at 4
   minutes) — the same rule scrobbling uses — so a quick skip never counts
   as a "play".
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from player import Player
from played_tracker import PlayedThresholdTracker

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

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._on_save_timer)

        # Live-time UI updates are coalesced too — position ticks arrive every
        # ~100ms and every one of them can add listened time, but the panel
        # only needs to repaint a few times a second, not 10x/sec.
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

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            return json.loads(_STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"tracks": {}, "total_listened_ms": 0}

    def _save(self):
        try:
            _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATS_FILE.write_text(json.dumps(self._data), encoding="utf-8")
        except Exception:
            pass

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

    # ── tracking ─────────────────────────────────────────────────────────
    def _on_track_changed(self, _track):
        self._tracker.reset()

    def _on_position(self, _frac: float, pos_ms: int, dur_ms: int):
        if not self._enabled:
            return
        just_played, delta_ms = self._tracker.update(pos_ms, dur_ms)
        if delta_ms > 0:
            self._add_listened_time(delta_ms)
        if just_played:
            self._record_play(dur_ms)

    def _track_entry(self, track) -> dict:
        key = _track_key(track)
        entry = self._data["tracks"].setdefault(key, {
            "artist": track.artist or "", "title": track.title,
            "play_count": 0, "listened_ms": 0,
        })
        # Keep the most recently seen artist/title in case tags changed.
        entry["artist"] = track.artist or entry["artist"]
        entry["title"] = track.title
        return entry

    def _add_listened_time(self, delta_ms: int):
        """Continuous total — every real forward-playback ms, regardless of
        whether the current track has crossed the played threshold yet."""
        self._data["total_listened_ms"] = self._data.get("total_listened_ms", 0) + delta_ms
        track = self._player.current
        if track is not None and track.title:
            self._track_entry(track)["listened_ms"] += delta_ms
        self._queue_save()
        if not self._live_update_timer.isActive():
            self._live_update_timer.start()

    def _record_play(self, dur_ms: int):
        track = self._player.current
        if track is None or not track.title:
            return
        self._track_entry(track)["play_count"] += 1
        self._queue_save()
        self.stats_changed.emit()

    # ── queries (for the Stats panel) ───────────────────────────────────
    def total_listened_ms(self) -> int:
        return int(self._data.get("total_listened_ms", 0))

    def top_tracks(self, limit: int = 25) -> list[dict]:
        # A track that was only skimmed (never crossed the played threshold)
        # gets a listened_ms entry for the raw-time total but shouldn't show
        # up in "top tracks" — that list is about what you actually played.
        items = [e for e in self._data["tracks"].values() if e["play_count"] > 0]
        items.sort(key=lambda e: e["play_count"], reverse=True)
        return items[:limit]

    def top_artists(self, limit: int = 25) -> list[dict]:
        by_artist: dict[str, dict] = {}
        for e in self._data["tracks"].values():
            if e["play_count"] == 0:
                continue
            artist = e["artist"] or "Unknown"
            a = by_artist.setdefault(artist, {"artist": artist, "play_count": 0, "listened_ms": 0})
            a["play_count"] += e["play_count"]
            a["listened_ms"] += e["listened_ms"]
        items = list(by_artist.values())
        items.sort(key=lambda e: e["play_count"], reverse=True)
        return items[:limit]

    def track_count(self) -> int:
        return sum(1 for e in self._data["tracks"].values() if e["play_count"] > 0)

    def clear(self):
        self._data = {"tracks": {}, "total_listened_ms": 0}
        self._save()
        self.stats_changed.emit()
