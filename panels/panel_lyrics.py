from __future__ import annotations
import re
import threading
from bisect import bisect_right
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QMenu, QSlider, QWidgetAction, QHBoxLayout,
)
from PySide6.QtCore import (
    Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve, QRect,
)
from PySide6.QtGui  import QFont, QColor, QPainter, QFontMetrics

from player import Player
from steady_label import SteadyLabel

try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None


_TS_RE   = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d+))?\]')
_META_RE = re.compile(r'^\[(?:ar|ti|al|by|offset|length|re|ve)\s*:', re.IGNORECASE)


def _parse_lrc(text: str) -> list[tuple[int, str]]:
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or _META_RE.match(raw):
            continue
        timestamps: list[int] = []
        last_end = 0
        for m in _TS_RE.finditer(raw):
            mm      = int(m.group(1))
            ss      = int(m.group(2))
            frac    = m.group(3) or "0"
            frac_ms = int(frac.ljust(3, "0")[:3])
            timestamps.append((mm * 60 + ss) * 1000 + frac_ms)
            last_end = m.end()
        if not timestamps:
            continue
        lyric = raw[last_end:].strip()
        if lyric:
            for ms in timestamps:
                lines.append((ms, lyric))
    return sorted(lines, key=lambda x: x[0])


def _try_lrc_file(path: Path) -> list[tuple[int, str]]:
    lrc_path = path.with_suffix(".lrc")
    if not lrc_path.exists():
        return []
    try:
        raw    = lrc_path.read_text(encoding="utf-8-sig", errors="replace")
        parsed = _parse_lrc(raw)
        if parsed:
            return parsed
        stripped = raw.strip()
        if stripped:
            return [(0, stripped)]
    except Exception:
        pass
    return []


def _extract_lyrics(path: Path) -> list[tuple[int, str]]:
    lrc = _try_lrc_file(path)
    if lrc:
        return lrc
    try:
        from mutagen import File as MFile
        f = MFile(str(path))
        if f is None or f.tags is None:
            return []
        tags = f.tags

        for key in tags.keys():
            if key.startswith("SYLT"):
                synced = getattr(tags[key], "text", None)
                if synced and isinstance(synced, (list, tuple)):
                    try:
                        pairs = [(ms, txt) for txt, ms in synced
                                 if isinstance(txt, str) and isinstance(ms, int) and txt.strip()]
                        if pairs:
                            return sorted(pairs, key=lambda x: x[0])
                    except (TypeError, ValueError):
                        pass

        for key in tags.keys():
            if key.startswith("USLT"):
                raw = getattr(tags[key], "text", None)
                if raw and raw.strip():
                    parsed = _parse_lrc(raw)
                    if parsed:
                        return parsed
                    return [(0, raw.strip())]

        for lrc_key in ("©lyr", "lyrics", "LYRICS"):
            if lrc_key in tags:
                val = tags[lrc_key]
                raw = str(val[0]) if isinstance(val, list) else str(val)
                if raw.strip():
                    parsed = _parse_lrc(raw)
                    if parsed:
                        return parsed
                    return [(0, raw.strip())]

    except Exception:
        pass
    return []


def _active_idx(lines: list[tuple[int, str]], pos_ms: int) -> int:
    idx = -1
    for i, (ms, _) in enumerate(lines):
        if ms <= pos_ms:
            idx = i
        else:
            break
    return idx


_PADDING_H = 28
_PADDING_V = 16
_LINE_GAP  = 14


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red()   + (b.red()   - a.red())   * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue()  + (b.blue()  - a.blue())  * t),
    )


def _target_color(dist: int, palette, synced: bool) -> QColor:
    if not synced or dist == 0:
        return palette.text().color()
    # Lerp from the text colour toward the window colour. Kept fully opaque so
    # far lines never fade into the backdrop.
    fg  = palette.text().color()
    bg  = palette.window().color()
    # blend factors per distance: 1=30%, 2=50%, 3=65%, 4+=75% toward bg
    blends = [0.30, 0.50, 0.65, 0.75]
    t = blends[min(dist - 1, len(blends) - 1)]
    return QColor(
        int(fg.red()   + (bg.red()   - fg.red())   * t),
        int(fg.green() + (bg.green() - fg.green()) * t),
        int(fg.blue()  + (bg.blue()  - fg.blue())  * t),
    )


def _target_size(dist: int, base_size: int, synced: bool) -> float:
    if not synced or dist == 0:
        return float(base_size + 4)
    return float(base_size)


class _AnimState:
    def __init__(self):
        self.color_t: float = 0.0
        self.size_t:  float = 0.0
        self.from_color: QColor | None = None
        self.to_color:   QColor | None = None
        self.from_size:  float = 0.0
        self.to_size:    float = 0.0
        self.cur_color:  QColor | None = None
        self.cur_size:   float = 0.0


class _LyricsCanvas(QWidget):
    line_clicked = Signal(int)          # a click on a line of synced lyrics: its index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lines:      list[str]       = []
        self._active:     int             = -1
        self._prev_active: int            = -1
        self._font_size:  int             = 18
        self._synced:     bool            = False
        self._row_tops:   list[int]       = []
        self._row_heights: list[int]      = []
        self._anim_states: list[_AnimState] = []
        self._anim_t:     float           = 0.0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(100)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._anim_tick)

    def set_lines(self, lines: list[str], synced: bool):
        self._lines      = lines
        self._synced     = synced
        self._active     = -1
        self._prev_active = -1
        self._anim_states = [_AnimState() for _ in lines]
        self._anim_timer.stop()
        if synced:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        self._relayout()

    def set_active(self, idx: int):
        if idx == self._active:
            return
        self._prev_active = self._active
        self._active      = idx
        self._start_anim()
        self._relayout()

    def set_font_size(self, pt: int):
        self._font_size = pt
        self._anim_states = [_AnimState() for _ in self._lines]
        self._relayout()

    def _active_font_size(self) -> float:
        return float(self._font_size + 4)

    def _norm_font_size(self) -> float:
        return float(self._font_size)

    def _make_font(self, size_pt: float, bold: bool) -> QFont:
        f = QFont()
        f.setPointSizeF(max(1.0, size_pt))
        f.setBold(bold)
        return f

    def _text_height(self, text: str, font: QFont, avail_w: int) -> int:
        fm = QFontMetrics(font)
        r  = fm.boundingRect(QRect(0, 0, max(1, avail_w), 9999),
                              Qt.TextWordWrap | Qt.AlignLeft, text)
        return r.height()

    def _start_anim(self):
        pal = self.palette()
        for i, st in enumerate(self._anim_states):
            new_dist  = abs(i - self._active) if (self._synced and self._active >= 0) else 0
            prev_dist = abs(i - self._prev_active) if (self._synced and self._prev_active >= 0) else 0

            st.from_color = st.cur_color if st.cur_color else _target_color(prev_dist, pal, self._synced)
            st.to_color   = _target_color(new_dist, pal, self._synced)
            st.from_size  = st.cur_size if st.cur_size > 0 else _target_size(prev_dist, self._font_size, self._synced)
            st.to_size    = _target_size(new_dist, self._font_size, self._synced)
            st.color_t    = 0.0
            st.size_t     = 0.0

        self._anim_timer.start()

    def _anim_tick(self):
        speed = 0.09
        all_done = True
        for st in self._anim_states:
            st.color_t = min(1.0, st.color_t + speed)
            st.size_t  = min(1.0, st.size_t  + speed)
            if st.color_t < 1.0 or st.size_t < 1.0:
                all_done = False
            if st.from_color and st.to_color:
                st.cur_color = _lerp_color(st.from_color, st.to_color, st.color_t)
            else:
                st.cur_color = st.to_color
            if st.from_size > 0:
                st.cur_size = st.from_size + (st.to_size - st.from_size) * st.size_t
            else:
                st.cur_size = st.to_size
        if all_done:
            self._anim_timer.stop()
        self._relayout()

    def _relayout(self):
        w     = max(1, self.width())
        avail = max(1, w - _PADDING_H * 2)
        tops  = []
        heights = []
        y     = _PADDING_V

        for i, txt in enumerate(self._lines):
            tops.append(y)
            size = self._cur_size(i)
            bold = (not self._synced) or (i == self._active)
            font = self._make_font(size, bold)
            heights.append(self._text_height(txt, font, avail))
            y   += heights[-1] + _LINE_GAP

        self._row_tops = tops
        self._row_heights = heights
        self.setMinimumHeight(y + _PADDING_V)
        self.update()

    def _cur_size(self, i: int) -> float:
        if i < len(self._anim_states) and self._anim_states[i].cur_size > 0:
            return self._anim_states[i].cur_size
        dist = abs(i - self._active) if (self._synced and self._active >= 0) else 0
        return _target_size(dist, self._font_size, self._synced)

    def _cur_color(self, i: int) -> QColor:
        if i < len(self._anim_states) and self._anim_states[i].cur_color:
            return self._anim_states[i].cur_color
        pal  = self.palette()
        dist = abs(i - self._active) if (self._synced and self._active >= 0) else 0
        return _target_color(dist, pal, self._synced)

    def resizeEvent(self, e):
        self._relayout()
        super().resizeEvent(e)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self._synced:
            return super().mousePressEvent(e)
        y = int(e.position().y())
        i = bisect_right(self._row_tops, y) - 1             # the gap under a line counts as the line
        if 0 <= i < len(self._lines) and y < self._row_tops[i] + self._row_heights[i] + _LINE_GAP:
            self.line_clicked.emit(i)

    def paintEvent(self, _):
        if not self._lines:
            return
        p     = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        w     = self.width()
        avail = max(1, w - _PADDING_H * 2)

        for i, txt in enumerate(self._lines):
            if i >= len(self._row_tops):
                break
            size  = self._cur_size(i)
            bold  = (not self._synced) or (i == self._active)
            font  = self._make_font(size, bold)
            color = self._cur_color(i)
            h     = self._text_height(txt, font, avail)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRect(_PADDING_H, self._row_tops[i], avail, h),
                       Qt.TextWordWrap | Qt.AlignLeft, txt)

        p.end()

    def row_center_y(self, idx: int) -> int:
        if not self._row_tops or not (0 <= idx < len(self._row_tops)):
            return 0
        size  = self._cur_size(idx)
        bold  = (not self._synced) or (idx == self._active)
        font  = self._make_font(size, bold)
        avail = max(1, self.width() - _PADDING_H * 2)
        h     = self._text_height(self._lines[idx], font, avail)
        return self._row_tops[idx] + h // 2


class LyricsWidget(QWidget):
    _lyrics_ready = Signal(list, object)   # (lines, error-string-or-None)

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player    = player
        self._lines:    list[tuple[int, str]] = []
        self._cur_idx   = -2
        self._synced    = False
        self._font_size = 18
        self._build()

        self._lyrics_ready.connect(self._apply_lyrics)
        player.track_changed.connect(self._on_track_changed)
        player.position_changed.connect(self._on_position_changed)
        self._canvas.line_clicked.connect(self._on_line_clicked)

    def _on_line_clicked(self, idx: int):
        dur = self._player.duration_ms
        if dur <= 0:
            return
        ms = min(self._lines[idx][0] + 20, dur)             # a hair into the line, so the seek cannot land before it and light the line above
        self._player.seek(ms / dur)
        self._on_position_changed(0.0, ms, dur)             # also moves the highlight while paused

    def _on_track_changed(self, track):
        self._cur_idx = -2
        self._cur_path = track.path
        # clear the old track's lyrics right away and show an in-flight state, so
        # a slow fetch doesn't masquerade as "this song has no lyrics"
        self._scroll.hide()
        self._no_lyr.setText("Searching for lyrics…")
        self._no_lyr.show()
        threading.Thread(target=self._load, args=(track.path,), daemon=True).start()

    def _on_position_changed(self, frac: float, pos_ms: int, dur_ms: int):
        if not self._lines or not self._synced:
            return
        idx = _active_idx(self._lines, pos_ms)
        if idx == self._cur_idx:
            return
        self._cur_idx = idx
        self._canvas.set_active(idx)
        if idx >= 0:
            self._scroll_to(idx)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._no_lyr = SteadyLabel()          # its text changes with the search, which must not change the panel's size
        self._no_lyr.setText("No lyrics")
        self._no_lyr.setAlignment(Qt.AlignCenter)
        self._no_lyr.setStyleSheet("font-style: italic;")
        root.addWidget(self._no_lyr, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._canvas = _LyricsCanvas()
        self._scroll.setWidget(self._canvas)
        self._scroll.hide()
        root.addWidget(self._scroll, 1)

        self._scroll_anim = QPropertyAnimation(self._scroll.verticalScrollBar(), b"value", self)
        self._scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._scroll_anim.setDuration(380)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx_menu)

    def _ctx_menu(self, pos):
        m   = QMenu(self)
        wa  = QWidgetAction(m)
        row = QWidget()
        hl  = QHBoxLayout(row)
        hl.setContentsMargins(12, 6, 12, 6)
        lbl = QLabel(f"Font size: {self._font_size}pt")
        lbl.setStyleSheet("min-width: 110px;")
        sl  = QSlider(Qt.Horizontal)
        sl.setRange(10, 36)
        sl.setValue(self._font_size)
        sl.setFixedWidth(130)
        sl.valueChanged.connect(lambda v: (lbl.setText(f"Font size: {v}pt"),
                                           self._set_font_size(v)))
        hl.addWidget(lbl)
        hl.addWidget(sl)
        wa.setDefaultWidget(row)
        m.addAction(wa)
        m.addSeparator()
        m.addAction("Retry lyrics search", self.retry_search)
        m.exec(self.mapToGlobal(pos))

    def set_font_size(self, pt: int):
        self._font_size = pt
        self._canvas.set_font_size(pt)
        if self._cur_idx >= 0:
            self._scroll_to(self._cur_idx, animate=False)

    def _set_font_size(self, pt: int):
        self.set_font_size(pt)

    def _load(self, path: Path):
        result, err = _extract_lyrics(path), None
        if not result:
            result, err = self._fetch_online(path)
        # drop stale results: a slow fetch for a track the user already skipped
        if _qt_valid(self) and path == getattr(self, "_cur_path", path):
            self._lyrics_ready.emit(result, err)

    def _fetch_online(self, path: Path) -> tuple[list, str | None]:
        """Ask LRCLIB when there are no local or embedded lyrics (cached, runs on this worker).
        Returns (lines, error) so offline reads differently from 'no lyrics'."""
        try:
            import library_cache, lyrics_fetch
            t = library_cache.get_tags(path)
            raw = t.get("raw") or [""]
            lrc, err = lyrics_fetch.fetch(raw[0], t.get("title", "") or path.stem,
                                          t.get("album", ""), int(t.get("duration", 0) or 0),
                                          also=(t.get("artist", ""), *t.get("artists", [])[:1]))
            if lrc:
                parsed = _parse_lrc(lrc)
                return (parsed if parsed else [(0, lrc.strip())]), None
            return [], err
        except Exception:
            return [], None

    def retry_search(self):
        """Look the current track up again, for example after the network came back."""
        p = getattr(self, "_cur_path", None)
        if p is None:
            return
        self._scroll.hide()
        self._no_lyr.setText("Searching for lyrics…")
        self._no_lyr.show()
        threading.Thread(target=self._load, args=(p,), daemon=True).start()

    def _apply_lyrics(self, lines: list, err=None):
        self._lines   = lines
        self._cur_idx = -2

        if not lines:
            self._scroll.hide()
            self._no_lyr.setText(
                "Couldn't reach LRCLIB. Check your connection.\n(right-click, then Retry)"
                if err == "network" else "No lyrics found")
            self._no_lyr.show()
            return

        self._synced = len(lines) > 1 or lines[0][0] > 0      # one line at 0:00 is plain text, but a first line at 0:00 is just a title line
        self._canvas.set_font_size(self._font_size)
        self._canvas.set_lines([txt for _, txt in lines], self._synced)
        self._no_lyr.hide()
        self._scroll.show()
        self._scroll.verticalScrollBar().setValue(0)

        if self._synced:
            idx = _active_idx(lines, self._player.position_ms)
            self._cur_idx = idx
            self._canvas.set_active(idx)
            if idx >= 0:
                self._scroll_to(idx, animate=False)

    def _scroll_to(self, idx: int, animate: bool = True):
        self._canvas.set_active(idx)
        center_y   = self._canvas.row_center_y(idx)
        viewport_h = self._scroll.viewport().height()
        target     = max(0, center_y - viewport_h // 2)
        target     = min(target, self._scroll.verticalScrollBar().maximum())
        if animate:
            self._scroll_anim.stop()
            self._scroll_anim.setStartValue(self._scroll.verticalScrollBar().value())
            self._scroll_anim.setEndValue(target)
            self._scroll_anim.start()
        else:
            self._scroll.verticalScrollBar().setValue(target)
