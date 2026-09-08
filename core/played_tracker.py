"""Shared "has this track actually been listened to" logic.

Both scrobbling and local play-stats need the same answer to "did the user
really play this track, or just glance at it" — the standard rule (matching
Last.fm and fooyin's PlaybackProgressTracker) is 50% of the track's duration,
capped at 4 minutes, whichever comes first. Forward jumps larger than a
normal position tick are treated as seeks and excluded from listened time, so
scrubbing through a track doesn't fake a full play.
"""
from __future__ import annotations

PLAYED_THRESHOLD_FRACTION = 0.5
PLAYED_THRESHOLD_TIME_MS = 240_000       # 4 minutes
MIN_TRACKED_DURATION_MS = 30_000         # shorter tracks are never counted as "played"
_SEEK_JUMP_MS = 5000                     # gap larger than this is a seek, not a tick


class PlayedThresholdTracker:
    """One instance per "listening session" (reset on every track change).
    Feed it position updates; it reports the moment the played threshold is
    first crossed (exactly once per session) and, on every call, how many ms
    of real forward playback that update represents (0 for a seek jump) — so
    a caller wanting a continuously-accumulating total isn't limited to
    threshold-gated tracks the way the scrobble-trigger use is."""

    def __init__(self):
        self._listened_ms = 0
        self._last_pos_ms = -1
        self._played = False

    def reset(self):
        self._listened_ms = 0
        self._last_pos_ms = -1
        self._played = False

    @property
    def played(self) -> bool:
        return self._played

    @property
    def listened_ms(self) -> int:
        return self._listened_ms

    def update(self, pos_ms: int, dur_ms: int) -> tuple[bool, int]:
        """Feed a position update. Returns (just_crossed_threshold, delta_ms) —
        just_crossed_threshold is True exactly once, on the update where the
        played threshold is first reached; delta_ms is the real forward
        playback this update represents (0 if it looked like a seek)."""
        delta_ms = 0
        if self._last_pos_ms >= 0 and 0 < pos_ms - self._last_pos_ms < _SEEK_JUMP_MS:
            delta_ms = pos_ms - self._last_pos_ms
        self._last_pos_ms = pos_ms

        if self._played or dur_ms <= 0:
            return False, delta_ms

        self._listened_ms += delta_ms
        threshold = min(dur_ms * PLAYED_THRESHOLD_FRACTION, PLAYED_THRESHOLD_TIME_MS)
        if dur_ms >= MIN_TRACKED_DURATION_MS and self._listened_ms >= threshold:
            self._played = True
            return True, delta_ms
        return False, delta_ms
