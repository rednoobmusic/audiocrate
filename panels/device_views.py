"""The list inside the Devices panel: what a scan found, grouped and filtered, and the view that draws it.

A scan is indexed once on a worker thread (DeviceData). The view shows one flat list of rows, each a song or a
group heading, and an open group simply has its songs listed after it. So a device with tens of thousands of
songs needs no tree items, and a filter is one pass over the list.
"""
from __future__ import annotations

from itertools import accumulate

from PySide6.QtCore import QAbstractTableModel, QItemSelection, QItemSelectionModel, QMimeData, QModelIndex, QRect, QRectF, QUrl, Qt, Signal
from PySide6.QtGui import QFontMetrics, QKeySequence, QPainter
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QStyle, QStyledItemDelegate, QTableView

from artists import split_artists
from device_widgets import draw_icon, plural, tip
from library_views import PAD, _Base, _Fonts, _fill_row, fmt_duration
from library_widgets import Scheme, scaled_font

INDENT = 18
CHEV = 16
MAX_DRAG = 500


class Head:
    """A group heading: an album, an artist, a playlist, a folder or a phone's media folder. Its songs are
    src[lo:hi], as indexes into the scan's item list."""
    __slots__ = ("key", "kind", "text", "sub", "count", "src", "lo", "hi")

    def __init__(self, key: str, kind: str, text: str, sub: str, count: int, src, lo: int = 0, hi: int | None = None):
        self.key, self.kind, self.text, self.sub, self.count = key, kind, text, sub, count
        self.src, self.lo, self.hi = src, lo, len(src) if hi is None else hi

    def members(self) -> list[int]:
        return list(self.src[self.lo:self.hi])


class _Dir:
    __slots__ = ("path", "dirs", "files", "lo", "hi")

    def __init__(self, path: str, lo: int):
        self.path, self.dirs, self.files, self.lo, self.hi = path, {}, [], lo, lo


class DeviceData:
    """One scan, as lists indexed by item. Read only once built, so it can be made on a worker thread."""

    def __init__(self, items: list[dict], info: dict | None = None):
        self.items, self.info = items, info or {}
        it, n, low = items, len(items), str.lower
        self.hay = [f"{x['title']}\n{x['artist']}\n{x['album']}\n{x['rel']}".lower() for x in it]
        self.by_title = sorted(range(n), key=lambda i: (low(it[i]["title"]), low(it[i]["artist"])))

        albums: dict = {}
        artists: dict = {}
        for i, x in enumerate(it):
            aa = (x.get("albumartist") or "").strip()
            names = [aa] if aa else x.get("artists") or split_artists([x["artist"]]) or ["Unknown artist"]
            name = x["album"].strip() or "Unknown album"
            albums.setdefault((low(names[0]), low(name)), [names[0], name, []])[2].append(i)    # an album sits under one name
            for who in names:                       # a song is listed under each of its artists
                artists.setdefault(low(who), [who, []])[1].append(i)
        track = lambda i: (it[i].get("disc") or 0, it[i]["track"] or 0, low(it[i]["title"]))
        self.albums = [(f"album:{k[0]}|{k[1]}", v[0], v[1], sorted(v[2], key=track)) for k, v in sorted(albums.items())]
        n_albums = {k: len({low(it[i]["album"].strip() or "Unknown album") for i in v[1]}) for k, v in artists.items()}
        self.artists = [(f"artist:{k}", v[0], n_albums.get(k, 0),
                         sorted(v[1], key=lambda i: (low(it[i]["album"]), *track(i)))) for k, v in sorted(artists.items())]
        by_id = {x["ipod_id"]: i for i, x in enumerate(it) if x.get("ipod_id") is not None}
        self.playlists = [(f"playlist:{k}:{p['name']}", p["name"], [by_id[s] for s in p["ids"] if s in by_id])
                          for k, p in enumerate(self.info.get("playlists") or [])]

        parts = [tuple(low(s) for s in x["rel"].replace("\\", "/").split("/")) for x in it]
        self.by_path = sorted(range(n), key=parts.__getitem__)
        self.root = _Dir("", 0)
        for pos, i in enumerate(self.by_path):
            node, shown = self.root, it[i]["rel"].replace("\\", "/").split("/")
            for depth, d in enumerate(parts[i][:-1]):
                child = node.dirs.get(d)
                if child is None:
                    child = node.dirs[d] = _Dir(node.path + "/" + shown[depth] if node.path else shown[depth], pos)
                child.hi = pos + 1
                node = child
            node.files.append(pos)

    def default_open(self) -> set:
        return {"dir:" + d.path.lower() for d in self.root.dirs.values()}

    def match(self, query: str):
        """The items whose title, artist, album or path contain the query, None for all of them."""
        q = query.strip().lower()
        return {i for i, h in enumerate(self.hay) if q in h} if q else None

    def rows(self, mode: str, hits, opened: set) -> list[tuple]:
        """The flat list for a mode: (depth, item index) for a song and (depth, Head) for a group. With a query
        every group that has a match is open, so the matches are all in sight."""
        if mode == "songs":
            return [(0, i) for i in self.by_title if hits is None or i in hits]
        out: list[tuple] = []
        if mode == "folders":
            pref = None if hits is None else [0, *accumulate(1 if i in hits else 0 for i in self.by_path)]
            self._walk(self.root, 0, hits, pref, opened, out)
            return out
        for key, kind, text, sub, members in self._groups(mode):
            m = members if hits is None else [i for i in members if i in hits]
            if not m:
                continue
            out.append((0, Head(key, kind, text, sub(len(m)), len(m), m)))
            if hits is not None or key in opened:
                out.extend((1, i) for i in m)
        return out

    def _groups(self, mode: str):
        if mode == "albums":
            return [(k, "album", name, lambda n, who=who: f"{who}, {plural(n, 'song')}", m)
                    for k, who, name, m in self.albums]
        if mode == "artists":
            return [(k, "artist", who, lambda n, a=a: f"{plural(a, 'album')}, {plural(n, 'song')}", m)
                    for k, who, a, m in self.artists]
        return [(k, "playlist", name, lambda n: plural(n, "song"), m) for k, name, m in self.playlists]

    def _walk(self, node: _Dir, depth: int, hits, pref, opened: set, out: list):
        for name, d in node.dirs.items():
            n = d.hi - d.lo if pref is None else pref[d.hi] - pref[d.lo]
            if not n:
                continue
            key = "dir:" + d.path.lower()
            out.append((depth, Head(key, "dir", d.path.rsplit("/", 1)[-1], "", n, self.by_path, d.lo, d.hi)))
            if hits is not None or key in opened:
                self._walk(d, depth + 1, hits, pref, opened, out)
        for pos in node.files:
            i = self.by_path[pos]
            if hits is None or i in hits:
                out.append((depth, i))


def folder_rows(folders: list[dict]) -> list[tuple]:
    """A phone's media folders as group rows. They open by being asked for, not by expanding."""
    return [(0, Head("phone:" + f["name"], "phone", f["name"].rsplit("/", 1)[-1].replace("_", " ")
                     if f["name"] != "iTunes_Control/Music" else "Music",
                     f"{f['what'].capitalize()}, {plural(f['entries'], 'item')}", f["entries"], ()))
            for f in folders]


class RowModel(QAbstractTableModel):
    def __init__(self, view):
        super().__init__(view)
        self.view, self.rows = view, []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.DisplayRole:
            return self.view.title_of(*self.rows[index.row()])
        if role == Qt.ToolTipRole:
            return tip(self.view.tip_of(*self.rows[index.row()]))
        return None

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsDragEnabled if index.isValid() else super().flags(index)

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, indexes):
        return self.view.drag_data(sorted({i.row() for i in indexes}))

    def supportedDragActions(self):
        return Qt.CopyAction

    def reset(self, rows: list[tuple]):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class RowDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view, self._fonts, self._tile = view, _Fonts(), None

    def paint(self, p, opt, index):
        v, rc = self.view, opt.rect
        depth, ref = v.model_.rows[index.row()]
        sc, ft = v.sc, self._fonts.get(opt.font)
        sel = bool(opt.state & QStyle.State_Selected)
        dense = v.mode == "folders"
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        _fill_row(p, sc, rc, sel, bool(opt.state & QStyle.State_MouseOver))
        if dense:
            p.setPen(sc.line)
            for k in range(depth):
                x = rc.x() + PAD + k * INDENT + INDENT // 2
                p.drawLine(x, rc.top(), x, rc.bottom())
        else:
            p.fillRect(rc.x() + PAD, rc.bottom(), rc.width() - 2 * PAD, 1, sc.line)
        x = rc.x() + PAD + depth * INDENT
        if isinstance(ref, Head):
            self._head(p, ft, rc, x, ref, dense)
        else:
            self._song(p, ft, rc, x, v.data.items[ref], dense)

    def _head(self, p, ft, rc, x, h: Head, dense: bool):
        v, sc = self.view, self.view.sc
        expandable = h.kind != "phone"
        if expandable:
            draw_icon(p, "down" if v.is_open(h.key) else "right",
                      QRectF(x, rc.center().y() - CHEV / 2, CHEV, CHEV), sc.dim)
        x += CHEV + 2 if expandable else 0
        glyph = {"album": "album", "artist": "artist", "playlist": "rows", "dir": "folder", "phone": "folder"}[h.kind]
        cs = 15 if dense else rc.height() - 14
        tile = QRectF(x, rc.center().y() - cs / 2, cs, cs)
        if dense:
            draw_icon(p, glyph, tile, sc.folder, sc.folder_fill)
            tx = x + cs + 7
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(sc.folder_fill if h.kind in ("dir", "phone") else sc.ph)
            p.drawRoundedRect(tile, cs * 0.12, cs * 0.12)
            draw_icon(p, glyph, tile.adjusted(cs * 0.22, cs * 0.22, -cs * 0.22, -cs * 0.22),
                      sc.folder if h.kind in ("dir", "phone") else sc.dim,
                      sc.folder_fill if h.kind in ("dir", "phone") else None)
            tx = x + cs + 10
        count = f"{h.count:,}" if dense and h.count else ""
        cw = ft.m2.horizontalAdvance(count) + 8 if count else 0
        w = rc.right() - PAD - cw - tx
        if dense:
            p.setFont(ft.f1)
            p.setPen(sc.fg)
            p.drawText(QRect(tx, rc.y(), w, rc.height()), Qt.AlignVCenter | Qt.AlignLeft,
                       ft.m1.elidedText(h.text, Qt.ElideRight, w))
            if count:
                p.setFont(ft.f2)
                p.setPen(sc.dimmer)
                p.drawText(QRect(rc.right() - PAD - cw, rc.y(), cw, rc.height()), Qt.AlignVCenter | Qt.AlignRight, count)
            return
        h1, h2 = ft.m1.height(), ft.m2.height()
        y = rc.y() + (rc.height() - h1 - h2 - 1) // 2
        p.setFont(ft.f1)
        p.setPen(sc.fg)
        p.drawText(QRect(tx, y, w, h1), Qt.AlignVCenter | Qt.AlignLeft, ft.m1.elidedText(h.text, Qt.ElideRight, w))
        p.setFont(ft.f2)
        p.setPen(sc.dim)
        p.drawText(QRect(tx, y + h1 + 1, w, h2), Qt.AlignVCenter | Qt.AlignLeft, ft.m2.elidedText(h.sub, Qt.ElideRight, w))

    def _song(self, p, ft, rc, x, it: dict, dense: bool):
        v, sc = self.view, self.view.sc
        dur = fmt_duration(it["duration"] or 0)
        dw = ft.m2.horizontalAdvance(dur) + 8 if dur else 0
        if dense:
            if v.has_heads():
                x += CHEV + 2
            draw_icon(p, "note", QRectF(x, rc.center().y() - 7.5, 15, 15), sc.dim)
            tx = x + 22
            w = rc.right() - PAD - dw - tx
            name = it["rel"].replace("\\", "/").rsplit("/", 1)[-1]
            p.setFont(ft.f1)
            p.setPen(sc.fg)
            p.drawText(QRect(tx, rc.y(), w, rc.height()), Qt.AlignVCenter | Qt.AlignLeft,
                       ft.m1.elidedText(name, Qt.ElideRight, w))
        else:
            cs = rc.height() - 14
            tile = QRectF(x, rc.y() + 7, cs, cs)
            p.setPen(Qt.NoPen)
            p.setBrush(sc.ph)
            p.drawRoundedRect(tile, cs * 0.12, cs * 0.12)
            if self._tile is None or self._tile[0] != ft.f1:
                self._tile = (ft.f1, scaled_font(ft.f1, 0.62))
            p.setFont(self._tile[1])
            p.setPen(sc.dimmer)
            p.drawText(tile, Qt.AlignCenter, (it["format"] or "?").upper()[:4])
            tx = x + cs + 10
            w = rc.right() - PAD - dw - tx
            title = it["title"]
            if v.mode == "albums" and it["track"]:
                title = f"{it['track']}. {title}"
            who = it["artist"] if v.mode == "albums" else " - ".join(a for a in (it["artist"], it["album"]) if a)
            sub = who or it["rel"].replace("\\", "/").rsplit("/", 1)[0]
            h1, h2 = ft.m1.height(), ft.m2.height()
            y = rc.y() + (rc.height() - h1 - h2 - 1) // 2
            p.setFont(ft.f1)
            p.setPen(sc.fg)
            p.drawText(QRect(tx, y, w, h1), Qt.AlignVCenter | Qt.AlignLeft, ft.m1.elidedText(title, Qt.ElideRight, w))
            p.setFont(ft.f2)
            p.setPen(sc.dim)
            p.drawText(QRect(tx, y + h1 + 1, w, h2), Qt.AlignVCenter | Qt.AlignLeft, ft.m2.elidedText(sub, Qt.ElideRight, w))
        if dur:
            p.setFont(ft.f2)
            p.setPen(sc.dimmer)
            p.drawText(QRect(rc.right() - PAD - dw + 8, rc.y(), dw - 8, rc.height()), Qt.AlignVCenter | Qt.AlignRight, dur)


class DeviceView(_Base, QTableView):
    """The rows of one scan in one mode. Enter plays, Ctrl+Enter adds, Right and Left open and close a group."""
    play_requested = Signal()
    add_requested = Signal()
    copy_requested = Signal()
    song_activated = Signal(int)         # a double click on a song: its item index
    head_activated = Signal(object)      # a double click on a heading that does not open (a phone folder)
    rows_changed = Signal()

    def __init__(self, sc: Scheme):
        super().__init__()
        self._init_view(sc)
        self.data: DeviceData | None = None
        self.mode, self.hits, self.query = "songs", None, ""
        self.expanded: set[str] = set()
        self._seeded = None
        self.fixed_rows: list[tuple] | None = None      # rows that are not from a scan: a phone's folder list
        self.model_ = RowModel(self)
        self.setModel(self.model_)
        for h in (self.horizontalHeader(), self.verticalHeader()):
            h.hide()
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setCornerButtonEnabled(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setItemDelegate(RowDelegate(self))
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self._fit()

    # what is shown

    @property
    def rows(self) -> list[tuple]:
        return self.model_.rows

    def _fit(self):
        fm1 = QFontMetrics(self.font())
        fm2 = QFontMetrics(scaled_font(self.font(), 0.86))
        h = max(26, fm1.height() + 10) if self.mode == "folders" else max(48, fm1.height() + fm2.height() + 16)
        self.verticalHeader().setDefaultSectionSize(h)

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange:
            self._fit()
        super().changeEvent(e)

    def show_rows(self, data: DeviceData | None, mode: str, fixed: list[tuple] | None = None, query: str = ""):
        """Put a scan on screen in a mode, filtered by a query. fixed is a list of rows that is not from a scan."""
        self.data, self.fixed_rows, self.query = data, fixed, query.strip().lower()
        if data is not None and self._seeded != id(data):
            self.expanded |= data.default_open()
            self._seeded = id(data)
        self.hits = data.match(self.query) if data is not None else None
        if mode != self.mode:
            self.mode = mode
            self._fit()
        self.rebuild()

    def rebuild(self, keep: bool = False):
        snap = self._snapshot() if keep else None
        if self.fixed_rows is not None:
            rows = self.fixed_rows
        else:
            rows = self.data.rows(self.mode, self.hits, self.expanded) if self.data is not None else []
        self.model_.reset(rows)
        if snap:
            self._restore(snap)
        else:
            self.verticalScrollBar().setValue(0)
        self.rows_changed.emit()

    def is_open(self, key: str) -> bool:
        return self.hits is not None or key in self.expanded

    def has_heads(self) -> bool:
        return self.mode in ("albums", "artists", "playlists", "folders")

    def toggle(self, row: int, state: bool | None = None):
        if not 0 <= row < len(self.rows):
            return
        ref = self.rows[row][1]
        if not isinstance(ref, Head) or ref.kind == "phone" or self.hits is not None:
            return
        opened = ref.key in self.expanded
        if state is None or state != opened:
            self.expanded.symmetric_difference_update({ref.key})
            self.rebuild(keep=True)

    def _ident(self, row: tuple):
        return row[1].key if isinstance(row[1], Head) else row[1]

    def _snapshot(self):
        sel = {self._ident(self.rows[i]) for i in self._selected_rows()}
        cur = self.currentIndex()
        return sel, self._ident(self.rows[cur.row()]) if cur.isValid() and cur.row() < len(self.rows) else None, \
            self.verticalScrollBar().value()

    def _restore(self, snap):
        sel, cur, scroll = snap
        s, run = QItemSelection(), None
        for i, row in enumerate(self.rows):
            if self._ident(row) in sel:
                run = i if run is None else run
            elif run is not None:
                s.select(self.model_.index(run, 0), self.model_.index(i - 1, 0))
                run = None
        if run is not None:
            s.select(self.model_.index(run, 0), self.model_.index(len(self.rows) - 1, 0))
        self.selectionModel().select(s, QItemSelectionModel.ClearAndSelect)
        if cur is not None:
            at = next((i for i, row in enumerate(self.rows) if self._ident(row) == cur), -1)
            if at >= 0:
                self.selectionModel().setCurrentIndex(self.model_.index(at, 0), QItemSelectionModel.NoUpdate)
        self.doItemsLayout()
        self.verticalScrollBar().setValue(scroll)

    # what is chosen

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.selectionModel().selectedIndexes()})

    def selected(self) -> list[tuple]:
        return [self.rows[i] for i in self._selected_rows() if i < len(self.rows)]

    def songs_of(self, rows: list[tuple]) -> list[int]:
        """The item indexes of rows, a heading standing for the songs it holds that match the filter, each once."""
        seen, out = set(), []
        for _, ref in rows:
            ids = ref.members() if isinstance(ref, Head) else [ref]
            for i in ids:
                if i not in seen and (self.hits is None or i in self.hits):
                    seen.add(i)
                    out.append(i)
        return out

    def all_songs(self) -> list[int]:
        return self.songs_of(self.rows)

    def title_of(self, depth, ref) -> str:
        return ref.text if isinstance(ref, Head) else self.data.items[ref]["title"]

    def tip_of(self, depth, ref) -> str:
        if isinstance(ref, Head):
            return "\n".join(x for x in (ref.text, ref.sub) if x)
        it = self.data.items[ref]
        lines = [it["title"], " - ".join(x for x in (it["artist"], it["album"]) if x), it["rel"]]
        return "\n".join(x for x in lines if x)

    def drag_data(self, rows: list[int]) -> QMimeData:
        mime = QMimeData()
        ids = self.songs_of([self.rows[i] for i in rows if i < len(self.rows)])
        if 0 < len(ids) <= MAX_DRAG:
            mime.setUrls([QUrl.fromLocalFile(self.data.items[i]["path"]) for i in ids])
        return mime

    # mouse and keys

    def mousePressEvent(self, e):
        ix = self.indexAt(e.position().toPoint())
        if e.button() == Qt.LeftButton and ix.isValid():
            depth, ref = self.rows[ix.row()]
            if isinstance(ref, Head) and ref.kind != "phone":
                x = e.position().toPoint().x()
                left = PAD + depth * INDENT
                if left - 2 <= x <= left + CHEV + 4:
                    self.toggle(ix.row())
                    return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        ix = self.indexAt(e.position().toPoint())
        if e.button() != Qt.LeftButton or not ix.isValid():
            return super().mouseDoubleClickEvent(e)
        ref = self.rows[ix.row()][1]
        if isinstance(ref, Head):
            if ref.kind == "phone":
                self.head_activated.emit(ref)
            else:
                self.toggle(ix.row())
        else:
            self.song_activated.emit(ref)

    def keyPressEvent(self, e):
        k = e.key()
        row = self.currentIndex().row()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            sel = self.selected()
            if len(sel) == 1 and isinstance(sel[0][1], Head) and sel[0][1].kind == "phone":
                self.head_activated.emit(sel[0][1])
            else:
                (self.add_requested if e.modifiers() & Qt.ControlModifier else self.play_requested).emit()
        elif k == Qt.Key_Right and row >= 0:
            self.toggle(row, True)
        elif k == Qt.Key_Left and row >= 0:
            self.toggle(row, False)
        elif e.matches(QKeySequence.Copy):
            self.copy_requested.emit()
        else:
            super().keyPressEvent(e)

