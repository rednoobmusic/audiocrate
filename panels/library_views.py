"""Item views for the library panel: delegates that paint rows and tiles, and the views around them.

Rows are painted straight from the model's parallel lists, so a paint costs a few
list lookups per visible row and a library of any size scrolls the same.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHeaderView, QListView, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QTableView, QTreeWidget)

from library_widgets import Scheme, draw_glyph, mix, scaled_font

ROLE_COUNT = Qt.UserRole + 1     # tracks under a folder item
ROLE_DIR   = Qt.UserRole + 2     # folder item as opposed to a file
PAD = 10
INDENT = 18                      # how far each level of the artist list moves in: a chevron and its gap
CHEV = INDENT - 2


def fmt_duration(sec: float) -> str:
    s = int(sec + 0.5)
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _Fonts:
    """The two text sizes a delegate paints with, rebuilt only when the view's font changes."""

    def __init__(self):
        self._base = None

    def get(self, f: QFont):
        if self._base is None or f != self._base:
            self._base = QFont(f)
            self.f1, self.f2 = self._base, scaled_font(f, 0.86)
            self.m1, self.m2 = QFontMetrics(self.f1), QFontMetrics(self.f2)
        return self


def _fill_row(p: QPainter, sc: Scheme, rc: QRect, sel: bool, hot: bool, bar: bool = True):
    if sel:
        p.fillRect(rc, sc.sel)
        if bar:
            p.fillRect(rc.x(), rc.y(), 3, rc.height(), sc.accent)
    elif hot:
        p.fillRect(rc, sc.hover)


def _cover(p: QPainter, v, rc: QRect, key, path, radius: float, glyph: str = "disc"):
    """The thumbnail if it has loaded, else a plain rounded block (with a glyph once it is known to have no art)."""
    e = v.covers.get(key)
    if e is None:
        v.covers.request(key, path)
    elif e[0] is not None:
        p.drawPixmap(rc, e[0])
        return e[1]
    sc = v.sc
    p.setPen(Qt.NoPen)
    p.setBrush(sc.ph)
    p.drawRoundedRect(QRectF(rc), radius, radius)
    if e is not None:
        s = rc.width() * 0.5
        draw_glyph(p, glyph, QRectF(rc.center().x() - s / 2, rc.center().y() - s / 2, s, s), sc.dimmer)
    return e[1] if e else ""


# tracks

def _track_row(p: QPainter, v, ft, d, r: int, rc: QRect, state, x0: int = 0):
    """A track as a row: cover, title, artist and album, length. x0 moves the content in, for rows in a tree."""
    sc = v.sc
    sel = bool(state & QStyle.State_Selected)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    _fill_row(p, sc, rc, sel, bool(state & QStyle.State_MouseOver))
    if not sel and state & QStyle.State_HasFocus:
        p.setPen(QPen(mix(sc.base, sc.accent, 0.55), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(rc.adjusted(0, 0, -1, -2))
    p.fillRect(rc.x() + PAD, rc.bottom(), rc.width() - 2 * PAD, 1, sc.line)

    a = d.alb_of[r]
    cs = rc.height() - 14
    left = rc.x() + PAD + x0
    _cover(p, v, QRect(left, rc.y() + 7, cs, cs), d.a_key[a], d.a_first[a], cs * 0.1)

    tx = left + cs + 10
    dur = fmt_duration(d.durs[r])
    dw = ft.m2.horizontalAdvance(dur) + 8 if dur else 0
    w = rc.right() - PAD - dw - tx
    ar, al = d.artists[r], d.albums[r]
    sub = f"{ar} - {al}" if ar and al else (ar or al or d.paths[r].parent.name)
    h1, h2 = ft.m1.height(), ft.m2.height()
    y = rc.y() + (rc.height() - h1 - h2 - 1) // 2
    p.setFont(ft.f1)
    p.setPen(sc.fg)
    p.drawText(QRect(tx, y, w, h1), Qt.AlignVCenter | Qt.AlignLeft,
               ft.m1.elidedText(d.titles[r], Qt.ElideRight, w))
    p.setFont(ft.f2)
    p.setPen(sc.dim)
    p.drawText(QRect(tx, y + h1 + 1, w, h2), Qt.AlignVCenter | Qt.AlignLeft,
               ft.m2.elidedText(sub, Qt.ElideRight, w))
    if dur:
        p.setPen(sc.dimmer)
        p.drawText(QRect(rc.right() - PAD - dw + 8, rc.y(), dw - 8, rc.height()),
                   Qt.AlignVCenter | Qt.AlignRight, dur)


class TrackDelegate(QStyledItemDelegate):
    def __init__(self, view, model):
        super().__init__(view)
        self.view, self.model, self._fonts = view, model, _Fonts()


    def paint(self, p, opt, index):
        _track_row(p, self.view, self._fonts.get(opt.font), self.model.d, self.model.rows[index.row()],
                   opt.rect, opt.state)


# albums

def _album_cell(p: QPainter, v, ft, d, a: int, rc: QRect, state, x0: int = 0, part: int | None = None):
    """An album as a tile or, when the view is not a grid, as a row. x0 moves a row's content in, for rows in a
    tree. part is how many of the album's tracks are listed when that is not all of them."""
    sc = v.sc
    sel = bool(state & QStyle.State_Selected)
    hot = bool(state & QStyle.State_MouseOver)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    n = len(d.a_rows[a])
    h1, h2 = ft.m1.height(), ft.m2.height()

    if v.grid:
        m = AlbumDelegate.TILE_PAD
        card = QRectF(rc).adjusted(1.5, 1.5, -1.5, -1.5)
        if sel or hot:
            p.setPen(QPen(sc.accent, 1) if sel else Qt.NoPen)
            p.setBrush(sc.sel if sel else sc.hover)
            p.drawRoundedRect(card, 6, 6)
        cs = rc.width() - 2 * m
        year = _cover(p, v, QRect(rc.x() + m, rc.y() + m, cs, cs), d.a_key[a], d.a_first[a], cs * 0.06)
        tx, w, y = rc.x() + m, cs, rc.y() + m + cs + 6
    else:
        _fill_row(p, sc, rc, sel, hot)
        p.fillRect(rc.x() + PAD, rc.bottom(), rc.width() - 2 * PAD, 1, sc.line)
        cs = rc.height() - 12
        left = rc.x() + PAD + x0
        year = _cover(p, v, QRect(left, rc.y() + 6, cs, cs), d.a_key[a], d.a_first[a], cs * 0.08)
        tx = left + cs + 12
        w = rc.right() - PAD - tx
        y = rc.y() + (rc.height() - h1 - 2 * h2 - 2) // 2

    meta = f"{part} of {n} tracks" if part is not None and part < n else f"{n} track{'s' if n != 1 else ''}"
    if year:
        meta = f"{year}, {meta}"
    p.setFont(ft.f1)
    p.setPen(sc.fg)
    p.drawText(QRect(tx, y, w, h1), Qt.AlignVCenter | Qt.AlignLeft,
               ft.m1.elidedText(d.a_album[a], Qt.ElideRight, w))
    p.setFont(ft.f2)
    p.setPen(sc.dim)
    p.drawText(QRect(tx, y + h1, w, h2), Qt.AlignVCenter | Qt.AlignLeft,
               ft.m2.elidedText(d.a_artist[a], Qt.ElideRight, w))
    p.setPen(sc.dimmer)
    p.drawText(QRect(tx, y + h1 + h2, w, h2), Qt.AlignVCenter | Qt.AlignLeft,
               ft.m2.elidedText(meta, Qt.ElideRight, w))


class AlbumDelegate(QStyledItemDelegate):
    TILE_PAD = 6

    def __init__(self, view, model):
        super().__init__(view)
        self.view, self.model, self._fonts = view, model, _Fonts()

    def sizeHint(self, opt, index):
        return self.view.cell

    def paint(self, p, opt, index):
        _album_cell(p, self.view, self._fonts.get(opt.font), self.model.d, self.model.rows[index.row()],
                    opt.rect, opt.state)


class ArtistDelegate(QStyledItemDelegate):
    """The artist list: an artist is a row with a cover and its counts, an album under it looks as in the album
    list and a track as in the track list, each moved in one step."""

    def __init__(self, view, model):
        super().__init__(view)
        self.view, self.model, self._fonts = view, model, _Fonts()

    def paint(self, p, opt, index):
        v, m, rc = self.view, self.model, opt.rect
        d, r = m.d, m.rows[index.row()]
        ft = self._fonts.get(opt.font)
        depth = m.depth(r)
        x0 = depth * INDENT + (INDENT if depth < 2 else 0)
        if depth == 0:
            self._artist(p, ft, rc, opt.state, r, x0)
        elif depth == 1:
            here = sum(1 for i in m.tracks_of(r[1]) if d.alb_of[i] == r[2])
            _album_cell(p, v, ft, d, r[2], rc, opt.state, x0, here)
        else:
            _track_row(p, v, ft, d, r[3], rc, opt.state, x0)
        if depth < 2:
            draw_glyph(p, "down" if m.is_open(r) else "right",
                       QRectF(rc.x() + PAD + depth * INDENT, rc.center().y() - CHEV / 2, CHEV, CHEV), v.sc.dim)

    def _artist(self, p, ft, rc, state, k: int, x0: int):
        v, m = self.view, self.model
        d, sc = m.d, v.sc
        sel = bool(state & QStyle.State_Selected)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        _fill_row(p, sc, rc, sel, bool(state & QStyle.State_MouseOver))
        p.fillRect(rc.x() + PAD, rc.bottom(), rc.width() - 2 * PAD, 1, sc.line)
        cs = rc.height() - 12
        left = rc.x() + PAD + x0
        a = d.alb_of[d.r_rows[k][0]]
        _cover(p, v, QRect(left, rc.y() + 6, cs, cs), d.a_key[a], d.a_first[a], cs * 0.08, "artist")
        tx = left + cs + 12
        w = rc.right() - PAD - tx
        nt, na = m.counts(k)
        sub = f"{na} album{'s' if na != 1 else ''}, {nt} track{'s' if nt != 1 else ''}"
        h1, h2 = ft.m1.height(), ft.m2.height()
        y = rc.y() + (rc.height() - h1 - h2 - 1) // 2
        p.setFont(ft.f1)
        p.setPen(sc.fg)
        p.drawText(QRect(tx, y, w, h1), Qt.AlignVCenter | Qt.AlignLeft, ft.m1.elidedText(d.r_name[k], Qt.ElideRight, w))
        p.setFont(ft.f2)
        p.setPen(sc.dim)
        p.drawText(QRect(tx, y + h1 + 1, w, h2), Qt.AlignVCenter | Qt.AlignLeft, ft.m2.elidedText(sub, Qt.ElideRight, w))


# folders

class FolderDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view, self._fonts = view, _Fonts()

    def sizeHint(self, opt, index):
        return QSize(10, max(26, self._fonts.get(opt.font).m1.height() + 10))

    def paint(self, p, opt, index):
        sc, ft = self.view.sc, self._fonts.get(opt.font)
        rc = opt.rect
        p.setRenderHint(QPainter.Antialiasing)
        folder = index.data(ROLE_DIR)
        g = QRectF(rc.x() + 2, rc.center().y() - 7, 15, 15)
        if folder:
            draw_glyph(p, "folder", g, sc.folder, sc.folder_fill)
        else:
            draw_glyph(p, "note", g, sc.dim)
        count = index.data(ROLE_COUNT)
        cw = ft.m2.horizontalAdvance(f"{count:,}") + 8 if count else 0
        x = rc.x() + 24
        w = rc.right() - cw - x - 4
        p.setFont(ft.f1)
        p.setPen(sc.fg)
        p.drawText(QRect(x, rc.y(), w, rc.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   ft.m1.elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight, w))
        if count:
            p.setFont(ft.f2)
            p.setPen(sc.dimmer)
            p.drawText(QRect(rc.right() - cw - 2, rc.y(), cw, rc.height()),
                       Qt.AlignVCenter | Qt.AlignRight, f"{count:,}")


# views

class _Base:
    """Background fill and the Enter key, shared by the library's views."""

    def _init_view(self, sc: Scheme):
        self.sc, self.on_enter = sc, None
        self.setFrameShape(QFrame.NoFrame)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

    def paintEvent(self, e):
        p = QPainter(self.viewport())
        p.fillRect(e.rect(), self.sc.base)
        p.end()
        super().paintEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.on_enter:
            self.on_enter()
        else:
            super().keyPressEvent(e)

    def keyboardSearch(self, _):
        pass    # letters are the app's hotkeys, and a match over every row calls back into Python


class TrackView(_Base, QTableView):
    """One column of rows. A table rather than a list view because a reset costs nothing:
    QListView lays out every row (about 2.5 us each) and a filter on 20,000 tracks hung for 100 ms."""

    def __init__(self, sc: Scheme, model, covers):
        super().__init__()
        self._init_view(sc)
        self.covers = covers
        self.setModel(model)
        for h in (self.horizontalHeader(), self.verticalHeader()):
            h.hide()
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setItemDelegate(TrackDelegate(self, model))
        self._fit()

    def _fit(self):
        fm1 = QFontMetrics(self.font())
        fm2 = QFontMetrics(scaled_font(self.font(), 0.86))
        self.verticalHeader().setDefaultSectionSize(max(48, fm1.height() + fm2.height() + 16))

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange:
            self._fit()
        super().changeEvent(e)


class AlbumView(_Base, QListView):
    """Cover grid or cover list over the same model."""
    MIN_TILE, TARGET_TILE = 92, 124

    def __init__(self, sc: Scheme, model, covers):
        super().__init__()
        self._init_view(sc)
        self.covers, self.grid, self.cell = covers, True, QSize(120, 170)
        self.setModel(model)
        self.setUniformItemSizes(True)
        self.setItemDelegate(AlbumDelegate(self, model))
        self.set_grid(True)

    def set_grid(self, on: bool):
        self.grid = on
        self.setViewMode(QListView.IconMode if on else QListView.ListMode)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)
        self.setSpacing(0)
        self.setWrapping(on)
        self.setFlow(QListView.LeftToRight if on else QListView.TopToBottom)
        self.setGridSize(QSize())
        self._fit()

    def _fit(self):
        # Leave room for the scrollbar whether or not it is showing, so a column
        # count that adds a scrollbar cannot flip back and forth.
        avail = self.width() - self.verticalScrollBar().sizeHint().width()
        h1 = QFontMetrics(self.font()).height()
        h2 = QFontMetrics(scaled_font(self.font(), 0.86)).height()
        if self.grid:
            n = max(1, round(avail / self.TARGET_TILE))
            if n > 1 and avail // n < self.MIN_TILE:
                n -= 1
            w = (avail - 2) // n
            cell = QSize(w, w + h1 + 2 * h2 + 12)
        else:
            cell = QSize(avail, max(64, h1 + 2 * h2 + 20))
        if cell != self.cell:
            self.cell = cell
            if self.grid:
                self.setGridSize(cell)
            self.scheduleDelayedItemsLayout()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class ArtistView(_Base, QTableView):
    """The artists with their albums and tracks under them. A table, like the track list, so a reset costs
    nothing; every row is as high as an album row. The chevron, Right and Left open and close a row."""
    grid = False

    def __init__(self, sc: Scheme, model, covers):
        super().__init__()
        self._init_view(sc)
        self.covers = covers
        self.setModel(model)
        for h in (self.horizontalHeader(), self.verticalHeader()):
            h.hide()
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setItemDelegate(ArtistDelegate(self, model))
        self._fit()

    def _fit(self):
        h1 = QFontMetrics(self.font()).height()
        h2 = QFontMetrics(scaled_font(self.font(), 0.86)).height()
        self.verticalHeader().setDefaultSectionSize(max(64, h1 + 2 * h2 + 20))

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange:
            self._fit()
        super().changeEvent(e)

    def _chevron_at(self, e) -> int:
        """The row whose chevron is under the mouse, else -1."""
        pos = e.position().toPoint()
        ix, m = self.indexAt(pos), self.model()
        if not ix.isValid():
            return -1
        r = m.rows[ix.row()]
        left = PAD + m.depth(r) * INDENT
        return ix.row() if m.can_open(r) and left - 2 <= pos.x() <= left + CHEV + 4 else -1

    def mousePressEvent(self, e):
        row = self._chevron_at(e) if e.button() == Qt.LeftButton else -1
        if row >= 0:
            self.model().toggle(row)
        else:
            super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self._chevron_at(e) >= 0 and e.button() == Qt.LeftButton:
            self.mousePressEvent(e)         # a quick second click opens or closes again, it does not add
        else:
            super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e):
        m, row = self.model(), self.currentIndex().row()
        if e.key() == Qt.Key_Right and row >= 0:
            m.toggle(row, True)
        elif e.key() == Qt.Key_Left and row >= 0:
            if not m.toggle(row, False):                # already closed: go to the row it is under
                depth = m.depth(m.rows[row])
                up = next((i for i in range(row - 1, -1, -1) if m.depth(m.rows[i]) < depth), -1)
                if up >= 0:
                    self.setCurrentIndex(m.index(up))
        else:
            super().keyPressEvent(e)


class FolderTree(_Base, QTreeWidget):
    """The folder tree, each row drawn here: background, indent guides, chevron, then the delegate."""

    def __init__(self, sc: Scheme):
        super().__init__()
        self._init_view(sc)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setUniformRowHeights(True)
        self.setAnimated(False)
        self.setItemDelegate(FolderDelegate(self))

    def drawRow(self, p, opt, index):
        sc = self.sc
        rc = QRect(0, opt.rect.y(), self.viewport().width(), opt.rect.height())
        if self.selectionModel().isSelected(index):
            p.fillRect(rc, sc.sel)
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(rc, sc.hover)
        depth, ix = 0, index.parent()
        while ix.isValid():
            depth += 1
            ix = ix.parent()
        ind = self.indentation()
        p.setPen(sc.line)
        for k in range(depth):
            x = k * ind + ind // 2
            p.drawLine(x, rc.top(), x, rc.bottom())
        if self.model().hasChildren(index):
            draw_glyph(p, "down" if self.isExpanded(index) else "right",
                       QRectF(depth * ind + 1, rc.center().y() - ind / 2 + 1, ind - 2, ind - 2), sc.dim)
        o = QStyleOptionViewItem(opt)
        o.rect = QRect(ind * (depth + 1), rc.y(), rc.width() - ind * (depth + 1), rc.height())
        self.itemDelegate().paint(p, o, index)
