"""The playlist list itself: painting, selection, drag and drop, the column header.

One QAbstractScrollArea paints the rows straight from `Playlist.display`, so a
playlist of any size costs one repaint of what is on screen and nothing else. No
widget is created per row and nothing walks the list while it paints. The column
header is a real QHeaderView, which brings resizing, reordering and saved state
for free; only its look is drawn here.
"""
from __future__ import annotations

import threading
from bisect import bisect_right
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from PySide6.QtCore import (
    Qt, QEvent, QMimeData, QModelIndex, QObject, QPoint, QPointF, QRect, QRectF, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QDrag, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPainterPath,
    QPalette, QPixmap, QPolygonF, QRegion, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHeaderView, QPushButton, QToolTip,
)

from playlist_model import Group, Playlist, Row, fmt_clock

# key, header label, sort field, right aligned, width, shown at first
COLUMNS = (
    ("num",    "#",     "num",    True,   44, True),
    ("title",  "Title", "title",  False,   0, True),     # takes the width that is left
    ("artist", "Artist", "artist", False, 150, True),
    ("album",  "Album", "album",  False, 150, True),
    ("time",   "Time",  "dur",    True,   62, True),
    ("year",   "Year",  "year",   True,   54, False),
    ("track",  "Track", "track",  True,   56, False),
    ("genre",  "Genre", "genre",  False, 100, False),
    ("name",   "File",  "name",   False, 170, False),
)
_TITLE = 1
# the column a sort field is shown under; the default sort goes by album artist
FIELD_COL = {"num": 0, "title": 1, "artist": 2, "aa": 2, "album": 3, "dur": 4,
              "year": 5, "track": 6, "genre": 7, "name": 8}

DRAG_MIME = "application/x-audiocrate-rows"
_NARROW = 500          # below this the two-line layout takes over in Auto
_COVER_PX = 96         # thumbnails are kept at this size and drawn smaller


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a moved a fraction t of the way to b."""
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


class _Colors:
    """Every colour the list uses, worked out from the widget's own palette and
    the accent, so it follows the dark theme, an album-art theme and the system
    look without a hard-coded value."""

    def __init__(self, pal: QPalette, accent: QColor):
        base = pal.color(QPalette.ColorRole.Base)
        text = pal.color(QPalette.ColorRole.Text)
        self.base = base
        self.text = text
        self.accent = accent
        self.alt = _mix(base, text, 0.035)
        self.hover = _mix(base, text, 0.075)
        self.group = _mix(base, text, 0.06)
        self.head = _mix(base, text, 0.08)
        self.line = _mix(base, text, 0.13)
        self.dim = _mix(base, text, 0.60)
        self.dimmer = _mix(base, text, 0.40)
        self.sel = _mix(base, accent, 0.34)
        self.sel_hover = _mix(base, accent, 0.42)
        self.playing = _mix(base, accent, 0.14)
        self.accent_text = _mix(accent, text, 0.40)


class _Covers(QObject):
    """Album art thumbnails for the group headers.

    The art is read and decoded on one worker thread, newest request first so
    what is on screen wins over what was scrolled past. The GUI thread only turns
    a finished QImage into a pixmap. A key with no art is remembered as None, so
    it is not looked for again.
    """

    ready = Signal()
    _done = Signal(object, object)

    def __init__(self):
        super().__init__()
        self._pix: OrderedDict = OrderedDict()
        self._queued: set = set()
        self._stack: list = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._done.connect(self._store)

    def get(self, key, path: Path):
        """The pixmap, None when there is none, or False while it is still coming."""
        if key in self._pix:
            self._pix.move_to_end(key)
            return self._pix[key]
        with self._lock:
            if key in self._queued:
                return False
            self._queued.add(key)
            self._stack.append((key, path))
            if len(self._stack) > 64:             # scrolled far past these, they can wait
                self._queued.discard(self._stack.pop(0)[0])
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="playlist-covers")
                self._thread.start()
        self._wake.set()
        return False

    def _run(self):
        while True:
            self._wake.wait()
            while True:
                with self._lock:
                    if not self._stack:
                        self._wake.clear()
                        break
                    key, path = self._stack.pop()
                    self._queued.discard(key)
                self._done.emit(key, _thumb(path))

    def _store(self, key, img):
        self._pix[key] = QPixmap.fromImage(img) if img is not None else None
        while len(self._pix) > 400:
            self._pix.popitem(last=False)
        self.ready.emit()


def _thumb(path: Path):
    """The first picture found for a track, as a small square QImage. For a worker
    thread: Pillow decodes and resizes without holding the interpreter lock."""
    try:
        from player import extract_art, decode_art
        data = extract_art(path)
        if not data:
            return None
        try:
            import io
            from PIL import Image, ImageOps
            im = Image.open(io.BytesIO(data))
            im.draft("RGB", (_COVER_PX * 2, _COVER_PX * 2))
            im = ImageOps.fit(im.convert("RGBA"), (_COVER_PX, _COVER_PX), Image.Resampling.LANCZOS)
            return QImage(im.tobytes(), _COVER_PX, _COVER_PX, _COVER_PX * 4,
                          QImage.Format.Format_RGBA8888).copy()
        except Exception:
            img = decode_art(data)
            if img is None:
                return None
            side = min(img.width(), img.height())
            img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
            return img.scaled(_COVER_PX, _COVER_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    except Exception:
        return None


_covers: _Covers | None = None


def covers() -> _Covers:
    global _covers
    if _covers is None:
        _covers = _Covers()
    return _covers


class _Header(QHeaderView):
    """Looks are drawn by the view, so the header and the rows share one palette."""

    def __init__(self, view: "PlaylistView"):
        super().__init__(Qt.Orientation.Horizontal, view)
        self._view = view
        self.setModel(QStandardItemModel(0, len(COLUMNS), self))
        self.setSectionsClickable(True)
        self.setSectionsMovable(True)
        self.setFirstSectionMovable(True)
        self.setHighlightSections(False)
        self.setStretchLastSection(False)
        self.setMinimumSectionSize(34)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def paintSection(self, painter, rect, logical):
        self._view.paint_header(painter, rect, logical)


class PlaylistView(QAbstractItemView):
    """A QAbstractItemView only so the rest of the app treats it as the list it is: the
    main window leaves the plain arrow keys alone when one has focus, and would
    otherwise turn them into volume and seek. There is no model behind it; every
    method Qt insists on is answered with nothing and the painting and hit testing
    below do the work."""

    activated = Signal(int)                    # a track to play, as its place in the order
    selection_changed = Signal()
    menu_requested = Signal(QPoint, object)    # global position, and a Row, a Group or None
    sort_clicked = Signal(str, bool)           # field, and whether Shift was held
    reorder_requested = Signal(object, object, bool)   # rows, the row they go next to, after it
    files_dropped = Signal(object, object, bool)       # paths, the row they go next to, after it
    action_requested = Signal(str)             # move_up, move_down, move_top, move_bottom
    columns_changed = Signal()                 # a column was shown, hidden, moved or resized

    def __init__(self, parent=None):
        super().__init__(parent)
        self.m = Playlist()
        self._sel: set[Row] = set()
        self._cur: Row | None = None
        self._anchor: Row | None = None
        self._hover = None
        self._playing: Row | None = None
        self._paused = False
        self._press = None                     # (item, pos, selection change waits for release)
        self._drop = None                      # (display index, after) while something is dragged over
        self._scroll_dir = 0
        self._layout_pref = "auto"
        self._lines = False
        self._two = False
        self._gut = 30
        self._c: _Colors | None = None
        self._cols: list | None = None
        self._restyle_pending = False
        self._pending_columns: dict | None = None
        self._load_fonts()
        self._cover = 40

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        vp = self.viewport()
        vp.setMouseTracking(True)
        vp.setAcceptDrops(True)
        vp.setAutoFillBackground(False)

        self._c = _Colors(self.palette(), self.palette().color(QPalette.ColorRole.Highlight))
        self._hdr = _Header(self)
        self._hdr.sectionClicked.connect(self._on_header_click)
        self._hdr.sectionResized.connect(self._cols_changed)
        self._hdr.sectionMoved.connect(self._cols_changed)
        self._hdr.customContextMenuRequested.connect(self._header_menu)
        for i, (_k, _l, _f, _r, w, shown) in enumerate(COLUMNS):
            if i == _TITLE:
                self._hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            else:
                self._hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                self._hdr.resizeSection(i, w)
            self._hdr.setSectionHidden(i, not shown)
        # time sits at the right edge whatever order the columns were defined in
        self._hdr.moveSection(self._hdr.visualIndex(4), len(COLUMNS) - 1)

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._auto_scroll)
        covers().ready.connect(self.viewport().update)
        self.verticalScrollBar().rangeChanged.connect(lambda *_: self._place_header())

        self._restyle()

    # what QAbstractItemView requires of a subclass, none of which this view uses

    def indexAt(self, p):
        return QModelIndex()

    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible):
        pass

    def visualRect(self, index):
        return QRect()

    def horizontalOffset(self):
        return 0

    def verticalOffset(self):
        return self.verticalScrollBar().value()

    def moveCursor(self, action, modifiers):
        return QModelIndex()

    def isIndexHidden(self, index):
        return True

    def setSelection(self, rect, command):
        pass

    def visualRegionForSelection(self, selection):
        return QRegion()

    def updateGeometries(self):
        self._update_scroll()

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.viewport().update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.viewport().update()

    # look

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange,
                        QEvent.Type.ApplicationPaletteChange, QEvent.Type.FontChange,
                        QEvent.Type.ParentChange) and not self._restyle_pending:
            # StyleChange arrives before the new sheet has settled on the children
            self._restyle_pending = True
            QTimer.singleShot(0, self._restyle)

    def showEvent(self, e):
        super().showEvent(e)
        self._restyle()
        self._apply_pending_columns()

    def _restyle(self):
        self._restyle_pending = False
        self._c = _Colors(self.palette(), self._accent())
        self._load_fonts()
        self._hdr.setFont(self._f_small)
        self._hdr.setFixedHeight(self._fms.height() + 12)
        self._update_heights()
        self.viewport().update()
        self._hdr.viewport().update()

    def _load_fonts(self):
        f = self.font()
        self._f_text = f
        self._f_bold = QFont(f)
        self._f_bold.setWeight(QFont.Weight.DemiBold)
        self._f_small = QFont(f)
        if f.pixelSize() > 0:
            self._f_small.setPixelSize(max(9, round(f.pixelSize() * 0.88)))
        else:
            self._f_small.setPointSizeF(max(6.0, f.pointSizeF() * 0.88))
        self._f_italic = QFont(f)
        self._f_italic.setItalic(True)
        self._fm = QFontMetrics(f)
        self._fmb = QFontMetrics(self._f_bold)
        self._fms = QFontMetrics(self._f_small)

    def _accent(self) -> QColor:
        """The accent of the theme in use.

        A palette cannot say what it is: the app's dark theme and the album-art
        theme are style sheets, which leave Highlight at the system colour. Every
        checkable button in this app turns the accent when it is on, so a hidden
        one is asked what that looks like. Without a style sheet the palette's
        highlight is the accent.
        """
        w, sheeted = self, bool(QApplication.instance().styleSheet())
        while w is not None and not sheeted:
            sheeted = bool(w.styleSheet())
            w = w.parentWidget()
        if sheeted:
            probe = QPushButton(self)
            probe.setCheckable(True)
            probe.setChecked(True)
            probe.setFixedSize(40, 24)
            probe.hide()
            try:
                c = probe.grab().toImage().pixelColor(20, 12)
            finally:
                probe.setParent(None)
                probe.deleteLater()
            if c.alpha() > 0:
                return c
        return self.palette().color(QPalette.ColorRole.Highlight)

    # layout

    def layout_pref(self) -> str:
        return self._layout_pref

    def set_layout_pref(self, pref: str):
        self._layout_pref = pref if pref in ("auto", "columns", "lines") else "auto"
        self._relayout()

    def _wants_lines(self) -> bool:
        if self._layout_pref != "auto":
            return self._layout_pref == "lines"
        w = self.width()
        return w < (_NARROW + 40 if self._lines else _NARROW)

    def _relayout(self):
        lines = self._wants_lines()
        changed = lines != self._lines
        self._lines = lines
        self._hdr.setVisible(not lines)
        if changed:
            self._update_heights()
        self._place_header()

    def _want_heights(self) -> tuple[int, int]:
        """Row and group header heights for the font and layout in use."""
        fm, fms = self._fm, self._fms
        self._cover = max(40, fm.height() * 2 + 8)
        if not self._lines:
            th = fm.height() + 11
            self._two = False
        elif self.m.group in ("album", "artist_album"):
            th = fm.height() + 12            # the header names the album, so one line is enough
            self._two = False
        else:
            th = fm.height() + fms.height() + 14
            self._two = True
        return th, self._cover + 12

    def _update_heights(self):
        with self.keep_place():
            pass

    def _fit_numbers(self):
        """Room for the biggest number in the first column, so it is never cut short."""
        digits = len(str(max(1, len(self.m.order))))
        self._gut = max(30, self._fms.horizontalAdvance("0" * digits) + 12)
        if not self._lines:
            need = self._fms.horizontalAdvance("0" * digits) + 22
            if self._hdr.sectionSize(0) < need and not self._hdr.isSectionHidden(0):
                self._hdr.resizeSection(0, need)

    def _place_header(self):
        h = self._hdr
        if not self._lines:
            self.setViewportMargins(0, h.height(), 0, 0)
            h.setGeometry(0, 0, self.viewport().width(), h.height())
        else:
            self.setViewportMargins(0, 0, 0, 0)
        self._cols = None
        self._update_scroll()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _update_scroll(self):
        sb = self.verticalScrollBar()
        vh = self.viewport().height()
        sb.setPageStep(vh)
        sb.setSingleStep(self.m.th)
        sb.setRange(0, max(0, self.m.height() - vh))

    @contextmanager
    def keep_place(self):
        """Hold the top row where it is on screen while the model is rebuilt."""
        sb = self.verticalScrollBar()
        m = self.m
        anchor, off = None, 0
        if m.display and sb.value() > 0:
            i = max(0, bisect_right(m.ys, sb.value()) - 1)
            item = m.display[min(i, len(m.display) - 1)]
            anchor = item.first if type(item) is Group else item
            off = m.ys[i] - sb.value()
        try:
            yield
        finally:
            self.m.set_heights(*self._want_heights())
            self._fit_numbers()
            self._update_scroll()
            if anchor is not None and anchor.pos >= 0:
                sb.setValue(m.ys[anchor.pos] - off)
            elif anchor is not None and anchor.grp is not None:
                sb.setValue(m.ys[anchor.grp.pos] - off)
            self._cols = None
            self.viewport().update()

    # columns

    def columns(self) -> list:
        """(key, x, width, right aligned) for each column on screen, left to right."""
        if self._cols is None:
            h = self._hdr
            out = []
            for v in range(h.count()):
                lg = h.logicalIndex(v)
                if not h.isSectionHidden(lg):
                    out.append((COLUMNS[lg][0], h.sectionViewportPosition(lg),
                                h.sectionSize(lg), COLUMNS[lg][3]))
            self._cols = out
        return self._cols

    def _cols_changed(self, *_):
        self._cols = None
        self.viewport().update()
        self.columns_changed.emit()

    def column_visible(self, i: int) -> bool:
        return not self._hdr.isSectionHidden(i)

    def set_column_width(self, i: int, w: int):
        if i != _TITLE:
            self._hdr.resizeSection(i, max(34, w))

    def set_column_visible(self, i: int, on: bool):
        if i == _TITLE:
            return
        self._hdr.setSectionHidden(i, not on)
        if on and self._hdr.sectionSize(i) < 40:
            self._hdr.resizeSection(i, COLUMNS[i][4])
        self._cols_changed()

    def columns_state(self) -> dict:
        h = self._hdr
        return {"qt": bytes(h.saveState().toBase64()).decode("ascii")}

    def set_columns_state(self, state: dict | None):
        """Hold a saved layout and put it on once there is room for it."""
        self._pending_columns = state or None
        self._apply_pending_columns()

    def _apply_pending_columns(self) -> bool:
        st = self._pending_columns
        if not st or self.viewport().width() <= 1:
            return False
        from PySide6.QtCore import QByteArray
        try:
            ok = bool(self._hdr.restoreState(QByteArray.fromBase64(st["qt"].encode("ascii"))))
        except Exception:
            ok = False
        if ok:
            self._pending_columns = None
            self._hdr.setSectionResizeMode(_TITLE, QHeaderView.ResizeMode.Stretch)
            self._hdr.setSectionHidden(_TITLE, False)
            self._cols_changed()
        return ok

    def has_pending_columns(self) -> bool:
        return self._pending_columns is not None

    def _shift_held(self) -> bool:
        return bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)

    def _on_header_click(self, logical: int):
        self.sort_clicked.emit(COLUMNS[logical][2], self._shift_held())

    def _header_menu(self, pos):
        self.menu_requested.emit(self._hdr.mapToGlobal(pos), "header")

    def paint_header(self, p: QPainter, rect: QRect, logical: int):
        c = self._c
        _key, label, _field, right, _w, _s = COLUMNS[logical]
        p.fillRect(rect, c.head)
        p.fillRect(rect.right(), rect.top() + 6, 1, rect.height() - 12, c.line)
        p.fillRect(rect.left(), rect.bottom(), rect.width(), 1, c.line)
        rank = next((i for i, (f, _d) in enumerate(self.m.sort) if FIELD_COL.get(f) == logical), -1)
        fm = self._fms
        p.setFont(self._f_small)
        p.setPen(c.text if rank >= 0 else c.dim)
        pad = 8
        arrow = 14 if rank >= 0 else 0
        base = rect.top() + (rect.height() + fm.ascent() - fm.descent()) // 2
        room = rect.width() - pad * 2 - arrow
        s = label if fm.horizontalAdvance(label) <= room else fm.elidedText(label, Qt.TextElideMode.ElideRight, room)
        tw = fm.horizontalAdvance(s)
        x = rect.right() - pad - arrow - tw + 1 if right else rect.left() + pad
        p.drawText(x, base, s)
        if rank < 0:
            return
        ax, ay = x + tw + 7, rect.top() + rect.height() // 2
        size = 3.6 if rank == 0 else 2.8
        up = self.m.sort[rank][1] is False
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.accent_text)
        if up:
            tri = QPolygonF([QPointF(ax - size, ay + size / 2), QPointF(ax + size, ay + size / 2), QPointF(ax, ay - size)])
        else:
            tri = QPolygonF([QPointF(ax - size, ay - size / 2), QPointF(ax + size, ay - size / 2), QPointF(ax, ay + size)])
        p.drawPolygon(tri)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if rank > 0:
            p.setPen(c.dim)
            p.drawText(int(ax + size + 3), base, str(rank + 1))

    # state the tab sets

    def set_playing(self, row: Row | None):
        old, self._playing = self._playing, row
        if old is not row:
            self._repaint_row(old)
            self._repaint_row(row)

    def set_paused(self, paused: bool):
        if paused != self._paused:
            self._paused = paused
            self._repaint_row(self._playing)

    def playing(self) -> Row | None:
        return self._playing

    def _repaint_row(self, item):
        if item is None:
            return
        pos = item.pos
        if pos < 0 or pos >= len(self.m.display):
            return
        y = self.m.ys[pos] - self.verticalScrollBar().value()
        self.viewport().update(0, y, self.viewport().width(), self.m.ys[pos + 1] - self.m.ys[pos])

    # selection

    def selected_rows(self) -> list[Row]:
        """In the order they are on screen."""
        if not self._sel:
            return []
        sel = self._sel
        return [r for r in self.m.order if r in sel]

    def selection_count(self) -> int:
        return len(self._sel)

    def current_row(self) -> Row | None:
        return self._cur

    def select_rows(self, rows, current: Row | None = None):
        new = set(rows)
        changed = new != self._sel
        self._sel = new
        if current is not None:
            self._cur = self._anchor = current
        self.viewport().update()
        if changed:
            self.selection_changed.emit()

    def select_all(self):
        self.select_rows([r for r in self.m.order if r.pos >= 0], self._cur)

    def clear_selection(self):
        self.select_rows([])

    def forget(self, rows):
        """Rows that left the playlist can no longer be selected, current or playing."""
        gone = set(rows)
        if self._sel & gone:
            self._sel -= gone
            self.selection_changed.emit()
        if self._cur in gone:
            self._cur = None
        if self._anchor in gone:
            self._anchor = None
        if self._playing in gone:
            self._playing = None
        if self._hover in gone:
            self._hover = None

    def scroll_to(self, row: Row, center: bool = False):
        if row is None:
            return
        pos = row.pos
        if pos < 0 and row.grp is not None:
            if row.grp.collapsed:
                self.m.set_collapsed(row.grp, False)
                self._update_scroll()
            pos = row.pos
        if pos < 0:
            return
        m, sb = self.m, self.verticalScrollBar()
        top, bot = m.ys[pos], m.ys[pos + 1]
        vh = self.viewport().height()
        if center:
            sb.setValue(top - (vh - (bot - top)) // 2)
        elif top < sb.value():
            sb.setValue(top)
        elif bot > sb.value() + vh:
            sb.setValue(bot - vh)

    # painting

    def _item_at(self, pos: QPoint):
        return self.m.at_y(pos.y() + self.verticalScrollBar().value())

    def paintEvent(self, e):
        p = QPainter(self.viewport())
        c, m = self._c, self.m
        rect = e.rect()
        p.fillRect(rect, c.base)
        disp, ys = m.display, m.ys
        if not disp:
            self._paint_empty(p)
            p.end()
            return
        y0 = self.verticalScrollBar().value()
        w = self.viewport().width()
        i = max(0, bisect_right(ys, y0 + rect.top()) - 1)
        end = y0 + rect.bottom() + 1
        n = len(disp)
        cols = None if self._lines else self.columns()
        while i < n and ys[i] < end:
            item = disp[i]
            y = ys[i] - y0
            if type(item) is Group:
                self._paint_group(p, item, y, w, i == 0)
            elif self._lines:
                self._paint_lines(p, item, y, w)
            else:
                self._paint_row(p, item, y, w, cols)
            i += 1
        if self._drop is not None:
            self._paint_drop(p, w, y0)
        p.end()

    def _paint_empty(self, p: QPainter):
        c = self._c
        msg = ("No tracks match the filter" if self.m.rows and self.m.query
               else "Drop files here or add tracks from the library")
        p.setFont(self._f_text)
        p.setPen(c.dimmer)
        p.drawText(QRect(12, 24, self.viewport().width() - 24, 60),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap), msg)

    def _row_back(self, p, r: Row, y: int, w: int, h: int):
        """Everything behind the text: stripe, hover, selection, playing tint."""
        c = self._c
        selected = r in self._sel
        if selected:
            p.fillRect(0, y, w, h, c.sel_hover if r is self._hover else c.sel)
        elif r is self._playing:
            p.fillRect(0, y, w, h, c.playing)
        elif r is self._hover:
            p.fillRect(0, y, w, h, c.hover)
        elif r.pos & 1:
            p.fillRect(0, y, w, h, c.alt)
        if r is self._playing:
            p.fillRect(0, y, 3, h, c.accent)
        if r is self._cur and self.hasFocus():
            p.setPen(_mix(c.base, c.accent, 0.7))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(1, y, w - 3, h - 1)
        return selected

    def _paint_row(self, p: QPainter, r: Row, y: int, w: int, cols):
        c, m = self._c, self.m
        h = m.th
        selected = self._row_back(p, r, y, w, h)
        playing = r is self._playing
        fm = self._fm
        base = y + (h + fm.ascent() - fm.descent()) // 2
        main = c.text
        side = c.text if selected else c.dim
        if r.missing:
            p.setFont(self._f_italic)
            main = side = c.dimmer
        else:
            p.setFont(self._f_text)
        pad = 8
        for key, x, cw, right in cols:
            room = cw - pad * 2
            if room <= 4:
                continue
            x += pad
            if key == "num":
                if playing:
                    self._glyph(p, x + room - 9, y + h // 2)
                    continue
                s, color = str(r.num), c.dimmer if not selected else c.dim
            elif key == "title":
                if playing:
                    p.setFont(self._f_bold)
                    p.setPen(c.accent_text)
                    self._cell(p, self._fmb, r.title, x, room, base, False)
                    p.setFont(self._f_italic if r.missing else self._f_text)
                    continue
                s, color = r.title, main
            elif key == "artist":
                s, color = r.artist, side
            elif key == "album":
                s, color = r.album, side
            elif key == "time":
                s, color = fmt_clock(r.dur), side
            elif key == "year":
                s, color = str(r.year) if r.year else "", side
            elif key == "track":
                s, color = f"{r.track:02d}" if r.track else "", side
            elif key == "genre":
                s, color = r.genre, side
            else:
                s, color = r.name, side
            if s:
                p.setPen(color)
                self._cell(p, fm, s, x, room, base, right)

    def _cell(self, p, fm, s, x, room, base, right):
        tw = fm.horizontalAdvance(s)
        if tw > room:
            s = fm.elidedText(s, Qt.TextElideMode.ElideRight, room)
            tw = room
        p.drawText(x + room - tw if right else x, base, s)

    def _paint_lines(self, p: QPainter, r: Row, y: int, w: int):
        """The narrow layout: the title with its time, and the artist and album under it."""
        c, m = self._c, self.m
        h = m.th
        selected = self._row_back(p, r, y, w, h)
        playing = r is self._playing
        fm, fms = self._fm, self._fms
        two = self._two
        left, gut = 10, self._gut
        main = c.dimmer if r.missing else (c.accent_text if playing else c.text)
        dim = c.text if selected else c.dim
        if two:
            b1 = y + 6 + fm.ascent()
            b2 = y + 6 + fm.height() + 2 + fms.ascent()
        else:
            b1 = y + (h + fm.ascent() - fm.descent()) // 2
        # gutter: the play glyph, the track number inside an album, else the place in the list
        if playing:
            self._glyph(p, left + gut - 8, b1 - (fm.ascent() - fm.descent()) // 2)
        else:
            by_track = m.group in ("album", "artist_album") and r.track
            p.setFont(self._f_small)
            p.setPen(c.dim if selected else c.dimmer)
            self._cell(p, fms, f"{r.track:02d}" if by_track else str(r.num), left, gut - 4, b1, True)
        x = left + gut + 6
        right = w - 10
        t = fmt_clock(r.dur)
        if t:
            p.setFont(self._f_small)
            p.setPen(dim)
            p.drawText(right - self._fms.horizontalAdvance(t), b1, t)
            right -= self._fms.horizontalAdvance(t) + 10
        room = right - x
        p.setFont(self._f_italic if r.missing else (self._f_bold if playing else self._f_text))
        p.setPen(main)
        fmt = self._fmb if playing else fm
        if not two:
            # one line: an artist that differs from the rest of the album follows the title
            g = r.grp
            extra = r.artist if g is not None and g.artist is None else ""
            if extra:
                room_t = max(room // 2, room - fm.horizontalAdvance(extra) - 14)
                self._cell(p, fmt, r.title, x, room_t, b1, False)
                used = min(fmt.horizontalAdvance(r.title), room_t) + 12
                p.setFont(self._f_small)
                p.setPen(dim)
                self._cell(p, fms, extra, x + used, room - used, b1, False)
            else:
                self._cell(p, fmt, r.title, x, room, b1, False)
            return
        self._cell(p, fmt, r.title, x, room, b1, False)
        a, al = r.artist, r.album
        if m.group == "artist":
            a = ""                            # the header already says whose they are
        sub = f"{a} - {al}" if a and al else (a or al)
        if sub:
            p.setFont(self._f_small)
            p.setPen(dim)
            self._cell(p, fms, sub, x, right - x, b2, False)

    def _glyph(self, p: QPainter, cx: int, cy: int):
        """Play, or pause when paused, drawn with the painter so no font is involved."""
        c = self._c
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.accent_text)
        if self._paused:
            p.drawRoundedRect(QRectF(cx - 4.5, cy - 4.5, 3.2, 9), 0.8, 0.8)
            p.drawRoundedRect(QRectF(cx + 1.3, cy - 4.5, 3.2, 9), 0.8, 0.8)
        else:
            p.drawPolygon(QPolygonF([QPointF(cx - 4, cy - 5), QPointF(cx - 4, cy + 5), QPointF(cx + 5, cy)]))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_group(self, p: QPainter, g: Group, y: int, w: int, first: bool):
        c, m = self._c, self.m
        h = m.gh
        whole = bool(g.rows) and g.rows[0] in self._sel and g.rows[-1] in self._sel
        bg = c.sel_hover if whole and g is self._hover else (c.sel if whole else (c.hover if g is self._hover else c.group))
        p.fillRect(0, y, w, h, bg)
        if not first:
            p.fillRect(0, y, w, 1, c.line)
        # chevron
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.dim)
        cx, cy = 11, y + h // 2
        if g.collapsed:
            p.drawPolygon(QPolygonF([QPointF(cx - 2.5, cy - 4), QPointF(cx - 2.5, cy + 4), QPointF(cx + 3, cy)]))
        else:
            p.drawPolygon(QPolygonF([QPointF(cx - 4, cy - 2.5), QPointF(cx + 4, cy - 2.5), QPointF(cx, cy + 3)]))
        # cover
        side = self._cover
        cx0, cy0 = 24, y + (h - side) // 2
        first_row = g.first
        key = (first_row.dir if first_row.dir is not None else str(first_row.path.parent), first_row.album)
        pix = covers().get(key, first_row.path)
        art = QRectF(cx0, cy0, side, side)
        path = QPainterPath()
        path.addRoundedRect(art, 4, 4)
        p.save()
        p.setClipPath(path)
        if isinstance(pix, QPixmap):
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(art.toRect(), pix)
        else:
            p.fillRect(art, _mix(c.base, c.text, 0.10))
            if pix is None:                       # looked and found nothing, not still loading
                self._disc(p, art)
        p.restore()
        p.setPen(c.line)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(art.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # text
        title, sub = g.text()
        x = cx0 + side + 12
        room = w - x - 10
        fmb, fms = self._fmb, self._fms
        block = fmb.height() + 2 + fms.height()
        top = y + (h - block) // 2
        playing = self._playing is not None and self._playing.grp is g
        p.setFont(self._f_bold)
        p.setPen(c.accent_text if playing else c.text)
        self._cell(p, fmb, title, x, room, top + fmb.ascent(), False)
        p.setFont(self._f_small)
        p.setPen(c.dim)
        self._cell(p, fms, sub, x, room, top + fmb.height() + 2 + fms.ascent(), False)

    def _disc(self, p: QPainter, art: QRectF):
        """A plain record, for a header with no cover."""
        c = self._c
        ctr = art.center()
        r = art.width() * 0.30
        p.setPen(_mix(c.base, c.text, 0.28))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ctr, r, r)
        p.drawEllipse(ctr, r * 0.38, r * 0.38)

    def _paint_drop(self, p: QPainter, w: int, y0: int):
        i, after = self._drop
        ys = self.m.ys
        if not 0 <= i < len(ys) - 1:
            return
        y = (ys[i + 1] if after else ys[i]) - y0
        c = self._c
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c.accent)
        p.drawRect(QRectF(8, y - 1, w - 12, 2))
        p.drawEllipse(QPointF(6, y), 3.5, 3.5)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    # mouse

    def mousePressEvent(self, e):
        self._press = None
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = e.position().toPoint()
        item = self._item_at(pos)
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if item is None:
            if not ctrl:
                self.clear_selection()
            return
        if type(item) is Group:
            if pos.x() < 22:
                self.toggle_group(item)
                return
            rows = set(item.rows)
            self.select_rows((self._sel | rows) if ctrl else rows, item.first)
            self._press = (item, pos, False)
            return
        wait = False
        if shift and self._anchor is not None and self._anchor.pos >= 0:
            span = self._span(self._anchor, item)
            self.select_rows((self._sel | span) if ctrl else span, None)
            self._cur = item
        elif ctrl:
            self.select_rows(self._sel ^ {item}, None)
            self._cur = self._anchor = item
        else:
            if item in self._sel and len(self._sel) > 1:
                wait = True              # this may be the start of dragging the whole selection
                self._cur = self._anchor = item
            else:
                self.select_rows({item}, item)
        if not shift:
            self._anchor = item
        self._cur = item
        self._press = (item, pos, wait)
        self.viewport().update()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if (e.buttons() & Qt.MouseButton.LeftButton) and self._press is not None:
            if (pos - self._press[1]).manhattanLength() >= QApplication.startDragDistance() and self._sel:
                self._start_drag()
            return
        item = self._item_at(pos)
        if item is not self._hover:
            old, self._hover = self._hover, item
            self._repaint_row(old)
            self._repaint_row(item)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        old, self._hover = self._hover, None
        self._repaint_row(old)

    def mouseReleaseEvent(self, e):
        press, self._press = self._press, None
        if press is not None and press[2] and e.button() == Qt.MouseButton.LeftButton:
            self.select_rows({press[0]}, press[0])

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        item = self._item_at(pos)
        if type(item) is Group:
            if pos.x() >= 22:
                self.activated.emit(self.m.order.index(item.first))
        elif item is not None:
            self.activated.emit(self.m.order.index(item))

    def contextMenuEvent(self, e):
        item = self._item_at(e.pos())
        if type(item) is Row and item not in self._sel:
            self.select_rows({item}, item)
        elif type(item) is Group:
            rows = set(item.rows)
            if not rows <= self._sel:
                self.select_rows(rows, item.first)
        self.menu_requested.emit(e.globalPos(), item)

    def toggle_group(self, g: Group):
        # the model builds new Group objects every time it regroups, so g is stale after this
        with self.keep_place():
            self.m.set_collapsed(g, not g.collapsed)
        if self._cur is not None and self._cur.pos < 0:
            self._cur = None                  # the current row went out of sight with its group

    def _span(self, a: Row, b: Row) -> set:
        """Every row on screen from a to b, whichever comes first."""
        lo, hi = sorted((a.pos, b.pos))
        return {x for x in self.m.display[lo:hi + 1] if type(x) is Row}

    def viewportEvent(self, e):
        if e.type() == QEvent.Type.ToolTip:
            item = self._item_at(e.pos())
            text = self._tip(item)
            if text:
                QToolTip.showText(e.globalPos(), text, self.viewport())
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().viewportEvent(e)

    def _tip(self, item) -> str:
        if type(item) is Row:
            head = f"{item.artist} - {item.title}" if item.artist else item.title
            return f"File missing:\n{item.path}" if item.missing else f"{head}\n{item.path}"
        if type(item) is Group:
            title, sub = item.text()
            return f"{title}\n{sub}"
        return ""

    # keyboard

    def keyPressEvent(self, e):
        k = e.key()
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if e.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return
        if alt and not ctrl and k in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
            self.action_requested.emit({Qt.Key.Key_Up: "move_up", Qt.Key.Key_Down: "move_down",
                                        Qt.Key.Key_Home: "move_top", Qt.Key.Key_End: "move_bottom"}[k])
            return
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._cur is not None and self._cur.pos >= 0:
                self.activated.emit(self.m.order.index(self._cur))
            return
        if k == Qt.Key.Key_Space and ctrl and self._cur is not None:
            self.select_rows(self._sel ^ {self._cur}, None)
            return
        if k == Qt.Key.Key_Escape and self._sel:
            self.clear_selection()
            return
        if k in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                 Qt.Key.Key_Home, Qt.Key.Key_End):
            target = self._nav_target(k)
            if target is None:
                return
            if shift and self._anchor is not None and self._anchor.pos >= 0:
                self.select_rows(self._span(self._anchor, target), None)
            elif not ctrl:
                self.select_rows({target}, None)
                self._anchor = target
            self._cur = target
            self.scroll_to(target)
            self.viewport().update()
            return
        super().keyPressEvent(e)

    def _nav_target(self, k) -> Row | None:
        disp = self.m.display
        n = len(disp)
        if not n:
            return None
        cur = self._cur
        i = cur.pos if cur is not None and cur.pos >= 0 else -1
        page = max(1, self.viewport().height() // max(1, self.m.th) - 1)
        if k == Qt.Key.Key_Home:
            i, d = 0, 1
        elif k == Qt.Key.Key_End:
            i, d = n - 1, -1
        else:
            d = -1 if k in (Qt.Key.Key_Up, Qt.Key.Key_PageUp) else 1
            step = page if k in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown) else 1
            if i < 0:
                i = bisect_right(self.m.ys, self.verticalScrollBar().value()) - 1
                if d < 0:
                    i = bisect_right(self.m.ys, self.verticalScrollBar().value() + self.viewport().height()) - 1
            else:
                i += d * step
            i = max(0, min(n - 1, i))
        for direction in (d, -d):
            j = i
            while 0 <= j < n:
                if type(disp[j]) is Row:
                    return disp[j]
                j += direction
        return None

    # drag and drop

    def _start_drag(self):
        rows = self.selected_rows()
        self._press = None
        if not rows:
            return
        mime = QMimeData()
        mime.setData(DRAG_MIME, str(id(self)).encode())
        if len(rows) <= 500:
            mime.setUrls([QUrl.fromLocalFile(str(r.path)) for r in rows])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self._drag_pixmap(rows))
        drag.setHotSpot(QPoint(14, 16))
        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)
        self._set_drop(None)
        self._scroll_timer.stop()

    def _drag_pixmap(self, rows) -> QPixmap:
        c, fm = self._c, self._fm
        text = rows[0].title if len(rows) == 1 else f"{len(rows):,} tracks"
        w = min(260, fm.horizontalAdvance(text) + 34)
        h = fm.height() + 12
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(c.sel_hover)
        bg.setAlpha(235)
        p.setBrush(bg)
        p.setPen(c.accent)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 5, 5)
        p.setFont(self._f_text)
        p.setPen(c.text)
        p.drawText(QRect(14, 0, w - 22, h), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   fm.elidedText(text, Qt.TextElideMode.ElideRight, w - 24))
        p.end()
        return pm

    def _drop_spot(self, pos: QPoint):
        """Where a drop at pos lands: the row to go next to (None for the end), whether
        after it, and the (display index, below the middle) pair the line is drawn from."""
        m = self.m
        if not m.display:
            return None, False, None
        y = pos.y() + self.verticalScrollBar().value()
        i = bisect_right(m.ys, y) - 1
        if i >= len(m.display):
            return None, False, (len(m.display) - 1, True)
        i = max(i, 0)
        item = m.display[i]
        lower = y >= (m.ys[i] + m.ys[i + 1]) // 2
        mark = (i, lower)
        if type(item) is Group:
            if lower and item.collapsed:
                return item.rows[-1], True, mark
            return item.first, False, mark
        return item, lower, mark

    def _set_drop(self, spot):
        if spot != self._drop:
            self._drop = spot
            self.viewport().update()

    def _accepts(self, mime) -> bool:
        return mime.hasFormat(DRAG_MIME) or any(u.isLocalFile() for u in mime.urls())

    def dragEnterEvent(self, e):
        if self._accepts(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if not self._accepts(e.mimeData()):
            e.ignore()
            return
        e.acceptProposedAction()
        pos = e.position().toPoint()
        self._set_drop(self._drop_spot(pos)[2])
        edge = 28
        self._scroll_dir = -1 if pos.y() < edge else (1 if pos.y() > self.viewport().height() - edge else 0)
        if self._scroll_dir and not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _auto_scroll(self):
        if not self._scroll_dir:
            self._scroll_timer.stop()
            return
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + self._scroll_dir * max(6, self.m.th // 2))

    def dragLeaveEvent(self, e):
        self._set_drop(None)
        self._scroll_timer.stop()
        self._scroll_dir = 0

    def dropEvent(self, e):
        mime = e.mimeData()
        target, after, _mark = self._drop_spot(e.position().toPoint())
        self._set_drop(None)
        self._scroll_timer.stop()
        self._scroll_dir = 0
        if mime.hasFormat(DRAG_MIME):
            if bytes(mime.data(DRAG_MIME)).decode() != str(id(self)):
                e.ignore()
                return
            e.acceptProposedAction()
            self.reorder_requested.emit(self.selected_rows(), target, after)
        elif mime.hasUrls():
            paths = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
            e.acceptProposedAction()
            if paths:
                self.files_dropped.emit(paths, target, after)
        else:
            e.ignore()
