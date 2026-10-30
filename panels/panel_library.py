from __future__ import annotations
import json
import safe_write
import os
import stat
import threading
import time
from collections import Counter
from pathlib import Path

from PySide6.QtCore import (QEvent, QFileSystemWatcher, QItemSelection, QItemSelectionModel,
                            QPoint, QRectF, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QActionGroup, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPainter,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QListWidget, QTreeWidgetItem, QFileDialog, QStackedWidget, QDialog,
    QDialogButtonBox, QMenu,
)

import plat_paths
from player import Player, PLAYABLE_EXTS as AUDIO_EXTS
from library_covers import CoverCache
from library_model import AlbumModel, ArtistModel, LibData, TrackModel
from library_views import AlbumView, ArtistView, FolderTree, TrackView, ROLE_COUNT, ROLE_DIR
from library_widgets import (EmptyState, ModeSwitch, PillButton, ProgressLine, Scheme, StatusLine,
                             ToolButton, draw_glyph)

_LIB_CFG      = plat_paths.config_dir() / "library.json"
_PLACEHOLDER  = "__placeholder__"
_last_lib_dir: str = str(Path.home())

TRACKS, ALBUMS, ARTISTS, FOLDERS, EMPTY = range(5)    # stack pages; all but the last are also the modes
_WATCH_MAX = 4000                  # folders watched at most: inotify has a per-user limit

_SORTS = {
    TRACKS: [("name", "File name"), ("artist", "Artist"), ("album", "Album"),
             ("title", "Title"), ("newest", "Newest")],
    ALBUMS: [("artist", "Artist"), ("album", "Album"), ("newest", "Newest")],
}


# folder config dialog

class _FolderDialog(QDialog):
    def __init__(self, folders: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library folders")
        self.resize(480, 280)
        self._folders = list(folders)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        self._lw = QListWidget()
        for f in self._folders:
            self._lw.addItem(str(f))
        lay.addWidget(self._lw, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        add = QPushButton("Add folder…")
        rem = QPushButton("Remove selected")
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _add(self):
        global _last_lib_dir
        d = QFileDialog.getExistingDirectory(self, "Add library folder", _last_lib_dir)
        if d:
            _last_lib_dir = d
            p = Path(d)
            if p not in self._folders:
                self._folders.append(p)
                self._lw.addItem(str(p))

    def _remove(self):
        for item in reversed(self._lw.selectedItems()):
            row = self._lw.row(item)
            self._lw.takeItem(row)
            if row < len(self._folders):
                self._folders.pop(row)

    def result_folders(self) -> list[Path]:
        return list(self._folders)


def _dir_counts(files: list[Path]) -> Counter:
    """Scanned tracks under every folder, for the counts in the folder tree."""
    out: Counter = Counter()
    for d, n in Counter(p.parent for p in files).items():
        out[d] += n
        for anc in d.parents:
            out[anc] += n
    return out


# main widget

class LibraryWidget(QWidget):
    add_to_playlist = Signal(list)
    convert_requested = Signal(list)                  # paths, or folders, to convert to another format
    _scan_done      = Signal(object)
    _scan_progress  = Signal(str, float)

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player    = player
        self._folders:   list[Path] = []
        self._all_files: list[Path] = []
        self._data:      LibData | None = None   # the last scan, as parallel lists
        self._counts:    Counter = Counter()      # folder -> tracks under it
        self._bad_folders = 0
        self._scanning  = False
        self._again     = False                   # a rescan was asked for mid-scan
        self._watch_dirs: set[str] = set()        # every folder that holds music, and the folders above them
        self._fresh_write = False                 # a file changed in the last few seconds, so it may still be growing
        self._query     = ""
        self._mode      = TRACKS
        self._sort      = {TRACKS: "name", ALBUMS: "artist"}
        self._left      = ""                      # status text, left side
        self._empty_key = None                    # which empty state is up, if any
        self._hits:      list[int] | None = None  # track rows the search matches, None without one
        self._artists_stale = True                # the artist list is built when it is first shown
        self._built     = False

        self._restyle_timer = QTimer(self)
        self._restyle_timer.setSingleShot(True)
        self._restyle_timer.timeout.connect(self._restyle)
        self._load_config()
        self._build()

        self._watcher = QFileSystemWatcher(self)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(1500)
        self._rescan_timer.timeout.connect(self._refresh)
        self._watcher.directoryChanged.connect(self._rescan_timer.start)
        self._watcher.fileChanged.connect(self._rescan_timer.start)

        self._scan_done.connect(self._apply_scan)
        self._scan_progress.connect(self._on_progress)
        self._set_mode(self._mode)
        if self._folders:
            self._refresh()

    # config

    def _load_config(self):
        try:
            d = json.loads(_LIB_CFG.read_text(encoding="utf-8"))
            paths = [Path(p) for p in d.get("folders", [])]
            self._folders = [p for p in paths if p.is_dir()]
            self._absent = [p for p in paths if not p.is_dir()]     # a drive that is not plugged in is kept
            if d.get("mode") in (TRACKS, ALBUMS, ARTISTS, FOLDERS):
                self._mode = d["mode"]
        except Exception:
            self._folders = []

    def _save_config(self):
        try:
            _LIB_CFG.parent.mkdir(parents=True, exist_ok=True)
            safe_write.write_text(_LIB_CFG, json.dumps(
                {"folders": [str(f) for f in (*self._folders, *(p for p in getattr(self, "_absent", [])
                                                                if p not in self._folders))],
                 "mode": self._mode}, indent=2))
        except Exception:
            from applog import log
            log.exception("failed to save library config to %s", _LIB_CFG)

    # UI build

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search library")
        self._search.setToolTip("Matches title, artist, album and file name")
        sc = self._sc = Scheme(self, self._search)
        self._covers = CoverCache(self)
        self._covers.ready.connect(self._repaint_views)

        # header: the mode switch and the two actions, then search and view options
        head = QVBoxLayout()
        head.setContentsMargins(8, 6, 8, 6)
        head.setSpacing(5)
        top = QHBoxLayout()
        top.setSpacing(4)
        self._seg = ModeSwitch(sc, ["Tracks", "Albums", "Artists", "Folders"], ["note", "disc", "artist", "folder"],
                               ["Tracks", "Albums", "Folders"])
        self._seg.changed.connect(self._pick_mode)
        b_folders = ToolButton(sc, "folder")
        b_folders.setToolTip("Library folders")
        b_folders.clicked.connect(self._edit_folders)
        b_refresh = ToolButton(sc, "refresh")
        b_refresh.setToolTip("Rescan library")
        b_refresh.clicked.connect(self._refresh)
        self._seg.setMaximumWidth(400)
        top.addWidget(self._seg, 100)
        top.addStretch(1)               # a wide panel keeps the switch compact and the actions at the edge
        top.addWidget(b_folders)
        top.addWidget(b_refresh)
        head.addLayout(top)

        self._row2 = QWidget()
        row = QHBoxLayout(self._row2)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._act_find  = self._search.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._act_clear = self._search.addAction(QIcon(), QLineEdit.TrailingPosition)
        self._act_clear.setVisible(False)
        self._act_clear.triggered.connect(self._search.clear)
        self._act_clear.setToolTip("Clear search")
        # debounced, so typing does not re-filter a huge library on every keystroke
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._apply_filter)
        self._search.textChanged.connect(self._on_text)
        for key, fn in ((Qt.Key_Escape, self._search.clear), (Qt.Key_Down, self._focus_list)):
            sh = QShortcut(QKeySequence(key), self._search)
            sh.setContext(Qt.WidgetShortcut)
            sh.activated.connect(fn)
        self._b_sort = ToolButton(sc, "sort", _SORTS[TRACKS][0][1], arrow=True)
        self._b_sort.clicked.connect(self._sort_menu)
        self._b_view = ToolButton(sc, "rows")
        self._b_view.clicked.connect(self._toggle_view)
        row.addWidget(self._search, 1)
        row.addWidget(self._b_sort)
        row.addWidget(self._b_view)
        head.addWidget(self._row2)
        hw = QWidget()
        hw.setLayout(head)
        root.addWidget(hw)

        self._progress = ProgressLine(sc)
        root.addWidget(self._progress)

        # pages
        self._tm, self._am, self._rm = TrackModel(self), AlbumModel(self), ArtistModel(self)
        self._tv = TrackView(sc, self._tm, self._covers)
        self._av = AlbumView(sc, self._am, self._covers)
        self._rv = ArtistView(sc, self._rm, self._covers)
        self._fv = FolderTree(sc)
        self._models = {TRACKS: self._tm, ALBUMS: self._am, ARTISTS: self._rm}
        self._lists = {TRACKS: self._tv, ALBUMS: self._av, ARTISTS: self._rv}
        self._empty = EmptyState(sc)
        self._empty.action.connect(self._empty_action)
        self._stack = QStackedWidget()
        for w in (self._tv, self._av, self._rv, self._fv, self._empty):   # TRACKS, ALBUMS, ARTISTS, FOLDERS, EMPTY
            self._stack.addWidget(w)
        root.addWidget(self._stack, 1)

        self._tv.doubleClicked.connect(self._track_dbl_click)
        self._av.doubleClicked.connect(self._album_dbl_click)
        self._rv.doubleClicked.connect(self._artist_dbl_click)
        self._fv.itemDoubleClicked.connect(self._tree_dbl_click)
        self._fv.itemExpanded.connect(self._on_expand)
        for v in self._lists.values():
            v.selectionModel().selectionChanged.connect(self._sel_changed)
            v.customContextMenuRequested.connect(lambda pos, v=v: self._context(v, pos))
            v.on_enter = self._add_selected
        self._fv.itemSelectionChanged.connect(self._sel_changed)
        self._fv.customContextMenuRequested.connect(lambda pos: self._context(self._fv, pos))
        self._fv.on_enter = self._add_selected

        # footer: counts, then the add actions
        foot = QVBoxLayout()
        foot.setContentsMargins(8, 4, 8, 8)
        foot.setSpacing(5)
        self._status = StatusLine(sc)
        foot.addWidget(self._status)
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self._b_sel = PillButton(sc, "Add selected", primary=True)
        self._b_all = PillButton(sc, "Add all")
        self._b_sel.clicked.connect(self._add_selected)
        self._b_all.clicked.connect(self._add_all)
        btns.addWidget(self._b_sel, 1)
        btns.addWidget(self._b_all, 1)
        foot.addLayout(btns)
        fw = QWidget()
        fw.setLayout(foot)
        root.addWidget(fw)
        self._painted = [self._seg, b_folders, b_refresh, self._b_sort, self._b_view, self._progress,
                         self._status, self._empty, self._b_sel, self._b_all]
        self._built = True
        self._restyle_timer.start(0)

    # theme

    def _restyle(self):
        """Re-read the live theme and repaint. Runs a tick after a style change, once the children are polished."""
        sc = self._sc
        sc.refresh()
        self._act_find.setIcon(self._glyph_icon("search", sc.dimmer))
        self._act_clear.setIcon(self._glyph_icon("close", sc.dim))
        for w in self._painted:
            w.update()
        self._repaint_views()
        self._fv.viewport().update()

    def _glyph_icon(self, name: str, color) -> QIcon:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(14 * dpr), int(14 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        draw_glyph(p, name, QRectF(0, 0, 14, 14), color)
        p.end()
        return QIcon(pm)

    def changeEvent(self, e):
        if e.type() in (QEvent.PaletteChange, QEvent.StyleChange, QEvent.ApplicationPaletteChange,
                        QEvent.FontChange):
            self._restyle_timer.start(0)
        super().changeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._restyle_timer.start(0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._built:
            self._sort_button()

    def _repaint_views(self):
        for v in self._lists.values():
            v.viewport().update()

    # mode switching

    def _pick_mode(self, idx: int):
        self._set_mode(idx)
        self._save_config()

    def _set_mode(self, idx: int):
        self._mode = idx
        self._seg.set_current(idx)
        self._b_view.setVisible(idx == ALBUMS)
        self._b_sort.setVisible(idx in _SORTS)
        self._sort_button()
        self._view_button()
        if idx == FOLDERS and self._fv.topLevelItemCount() == 0:
            self._populate_tree()
        if idx == ARTISTS and self._artists_stale:
            self._fill_artists()
        self._sync()

    def _sort_label(self) -> str:
        opts = _SORTS.get(self._mode)
        return dict(opts)[self._sort[self._mode]] if opts else ""

    def _sort_button(self):
        """Its text is the current sort, dropped when the panel is too narrow for it."""
        self._b_sort.setText(self._sort_label() if self.width() >= 290 else "")
        self._b_sort.setToolTip(f"Sort by {self._sort_label()}")
        self._b_sort.updateGeometry()

    def _view_button(self):
        grid = self._av.grid
        self._b_view.glyph = "rows" if grid else "grid"
        self._b_view.setToolTip("Show as list" if grid else "Show as grid")
        self._b_view.update()

    def _sort_menu(self):
        menu = QMenu(self)
        group = QActionGroup(menu)
        for key, label in _SORTS[self._mode]:
            a = menu.addAction(label)
            a.setActionGroup(group)
            a.setCheckable(True)
            a.setChecked(key == self._sort[self._mode])
            a.triggered.connect(lambda _=False, k=key: self._set_sort(k))
        menu.exec(self._b_sort.mapToGlobal(QPoint(0, self._b_sort.height())))

    def _set_sort(self, key: str):
        self._sort[self._mode] = key
        self._sort_button()
        self._apply_filter()

    def _toggle_view(self):
        self._av.set_grid(not self._av.grid)
        self._view_button()
        cur = self._av.currentIndex()
        if cur.isValid():
            self._av.scrollTo(cur)

    def _on_text(self, text: str):
        self._act_clear.setVisible(bool(text))
        if text:
            self._filter_timer.start()
        else:
            self._filter_timer.stop()
            self._apply_filter()

    def _focus_list(self):
        v = self._lists.get(self._mode)
        if v is not None and v.model().rowCount():
            if not v.currentIndex().isValid():
                v.setCurrentIndex(v.model().index(0))
            v.setFocus()

    # folder management

    def _edit_folders(self):
        dlg = _FolderDialog(self._folders, self)
        if dlg.exec():
            self._folders = dlg.result_folders()
            self._save_config()
            self._refresh()

    def _quick_add_folder(self):
        global _last_lib_dir
        d = QFileDialog.getExistingDirectory(self, "Add library folder", _last_lib_dir)
        if d:
            _last_lib_dir = d
            if Path(d) not in self._folders:
                self._folders.append(Path(d))
                self._save_config()
            self._refresh()

    def _empty_action(self):
        kind = self._empty_key[0] if self._empty_key else None
        {"folders": self._quick_add_folder, "empty": self._edit_folders,
         "match": self._search.clear}.get(kind, lambda: None)()

    # scanning

    def _refresh(self):
        if not self._folders:
            self._sync()
            return
        if self._scanning:
            self._again = True
            return
        self._scanning = True
        n = len(self._folders)
        self._progress.set(-1)
        self._left = f"Scanning {n} folder{'s' if n != 1 else ''}..."
        self._sync()
        threading.Thread(target=self._bg_scan, args=(list(self._folders),), daemon=True).start()

    def _on_progress(self, text: str, frac: float):
        self._left = text
        self._progress.set(frac)
        self._sel_changed()

    def _bg_scan(self, folders: list[Path]):
        try:
            res = self._scan(folders)
        except Exception:
            from applog import log
            log.exception("library scan failed")
            res = ([], LibData([], {}, {}), Counter(), len(folders))
        self._scan_done.emit(res)

    def _scan(self, folders: list[Path]):
        files: list[Path] = []
        mtimes: dict[Path, float] = {}
        dirs: set[Path] = set()
        bad_folders = 0
        for folder in folders:
            try:
                for p in folder.rglob("*"):
                    if p.suffix.lower() not in AUDIO_EXTS:
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        files.append(p)
                        mtimes[p] = st.st_mtime
                        dirs.add(p.parent)
                        if len(files) % 500 == 0:
                            self._scan_progress.emit(f"Scanning... {len(files):,} files found", -1.0)
            except Exception:
                bad_folders += 1
        files.sort(key=lambda p: p.name.lower())
        roots = set(folders)
        watch = {str(f) for f in folders}
        for d in dirs:
            watch.add(str(d))
            for up in d.parents:
                if up in roots:
                    break
                watch.add(str(up))
        self._watch_dirs = watch
        self._fresh_write = any(m > time.time() - 6 for m in mtimes.values())
        # Read tags via the persistent cache (only stale files actually hit disk).
        # Chunked so the status line can show progress on a first big scan.
        meta: dict[Path, dict] = {}
        try:
            import library_cache
            for i in range(0, len(files), 500):
                chunk = files[i:i + 500]
                meta.update(library_cache.get_many(chunk))
                if len(files) > 500:
                    done = min(i + 500, len(files))
                    self._scan_progress.emit(f"Reading tags... {done:,} of {len(files):,}",
                                             done / len(files))
        except Exception:
            pass
        return files, LibData(files, meta, mtimes), _dir_counts(files), bad_folders

    def regroup(self):
        """Build the lists again after the artist settings changed. The tags come from the cache, no file is read."""
        if self._scanning:
            self._again = True
        elif self._data is not None:
            threading.Thread(target=self._bg_regroup, args=(self._data,), daemon=True).start()

    def _bg_regroup(self, d: LibData):
        import library_cache
        meta = library_cache.get_many(d.paths, read_missing=False)
        data = LibData(d.paths, meta, dict(zip(d.paths, d.mtimes)))
        self._scan_done.emit((self._all_files, data, self._counts, self._bad_folders))

    def _apply_scan(self, res):
        files, data, counts, bad = res
        self._scanning = False
        self._progress.set(None)
        self._left = ""
        self._all_files, self._data, self._counts, self._bad_folders = files, data, counts, bad
        self._fv.clear()
        if self._mode == FOLDERS:
            self._populate_tree()
        self._apply_filter(keep_scroll=True)
        # QFileSystemWatcher reports only what happens directly inside a folder, so every folder that holds music
        # is watched, not just the library folders: an album that lands three levels down must be seen too.
        want = {str(f) for f in self._folders} | self._watch_dirs
        if len(want) > _WATCH_MAX:
            want = set(sorted(want, key=lambda d: d.count(os.sep))[:_WATCH_MAX])
        have = set(self._watcher.directories())
        if have - want:
            self._watcher.removePaths(sorted(have - want))
        if want - have:
            self._watcher.addPaths(sorted(want - have))
        if self._fresh_write:
            QTimer.singleShot(6000, self, self._refresh)      # a file still being written is read again once it has settled
        if self._again:
            self._again = False
            self._refresh()

    # tracks and albums: filtering, sorting, keeping the selection

    def _apply_filter(self, keep_scroll: bool = False):
        self._query = self._search.text().strip().lower()
        d = self._data
        snap = self._snapshot()
        hits = d.match(self._query) if d else None
        self._tm.reset(d, d.track_rows(hits, self._sort[TRACKS]) if d else [])
        self._am.reset(d, d.album_rows(hits, self._sort[ALBUMS]) if d else [])
        self._hits, self._artists_stale = hits, True
        if self._mode == ARTISTS:
            self._fill_artists()
        else:
            self._rm.reset(None, [])                # built when the list is shown, not holding the old scan meanwhile
        self._restore(snap, keep_scroll)
        self._sync()

    def _fill_artists(self):
        """The artists that have a track the search matches, or all of them."""
        d = self._data
        hit = d.artist_hits(self._hits) if d else None
        self._rm.reset(d, list(hit) if hit is not None else list(range(len(d.r_name))) if d else [], hit)
        self._artists_stale = False

    def _sel_rows(self, view) -> list[int]:
        return sorted({i.row() for i in view.selectionModel().selectedIndexes()})

    def _snapshot(self):
        """What the user was doing in both lists: selection by identity, and scroll."""
        tm, am = self._tm, self._am
        tsel = {tm.d.paths[tm.rows[i]] for i in self._sel_rows(self._tv)} if tm.d else set()
        asel = {(am.d.a_artist[am.rows[i]], am.d.a_album[am.rows[i]])
                for i in self._sel_rows(self._av)} if am.d else set()
        rm = self._rm
        rsel = {rm.key(rm.rows[i]) for i in self._sel_rows(self._rv)} if rm.d and self._mode == ARTISTS else set()
        return (tsel, asel, rsel, self._tv.verticalScrollBar().value(), self._av.verticalScrollBar().value(),
                self._rv.verticalScrollBar().value())

    def _restore(self, snap, keep_scroll: bool):
        tsel, asel, rsel, tscroll, ascroll, rscroll = snap
        d = self._data
        if d is None:
            return
        for view, model, wanted, ident, scroll in (
                (self._tv, self._tm, tsel, lambda r: d.paths[r], tscroll),
                (self._av, self._am, asel, lambda a: (d.a_artist[a], d.a_album[a]), ascroll),
                (self._rv, self._rm, rsel, self._rm.key, rscroll)):
            sel, run = QItemSelection(), None
            if wanted:
                for i, r in enumerate(model.rows):
                    if ident(r) in wanted:
                        if run is None:
                            run = i
                    elif run is not None:
                        sel.select(model.index(run), model.index(i - 1))
                        run = None
                if run is not None:
                    sel.select(model.index(run), model.index(len(model.rows) - 1))
                view.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)
            if keep_scroll:
                view.doItemsLayout()
            view.verticalScrollBar().setValue(scroll if keep_scroll else 0)

    # what is on screen, which page, and the footer

    def _sync(self):
        d, mode, e = self._data, self._mode, self._empty
        shown = self._models[mode].rows if mode != FOLDERS else None
        key = None
        if not self._folders:
            key = ("folders", "No library folders yet",
                   "Add a folder with your music and it shows up here.", "Add folder")
        elif d is None and mode != FOLDERS:
            key = ("scan", "Scanning your folders", "", "")
        elif mode != FOLDERS and not d.paths:
            key = ("empty", "No music found", "Nothing playable in your library folders.", "Library folders")
        elif mode != FOLDERS and not shown:
            key = ("match", f"No matches for “{self._search.text().strip()}”",
                   "Try a title, artist or album.", "Clear search")
        if key != self._empty_key:
            self._empty_key = key
            if key:
                glyph = {"scan": "disc", "match": "search"}.get(key[0], "folder")
                e.show_message(glyph, *key[1:])
        self._stack.setCurrentIndex(EMPTY if key else mode)
        self._row2.setVisible(mode != FOLDERS and bool(self._folders))

        if self._scanning:
            pass                                    # progress text arrives with each update
        elif not self._folders:
            self._left = ""
        elif d is not None:
            n = len(d.paths)
            nf = len(self._folders)
            if mode == FOLDERS:
                self._left = f"{n:,} tracks in {nf} folder{'s' if nf != 1 else ''}"
            elif mode == ARTISTS:
                na, nh = len(d.r_name), len(self._hits or ())
                self._left = (f"{len(self._rm.ids):,} of {na:,} artists, {nh:,} track{'s' if nh != 1 else ''}" if self._query
                              else f"{n:,} tracks, {na:,} artist{'s' if na != 1 else ''}")
            elif self._query:
                self._left = (f"{len(self._tm.rows):,} of {n:,} tracks, "
                              f"{len(self._am.rows):,} album{'s' if len(self._am.rows) != 1 else ''}")
            else:
                self._left = f"{n:,} tracks, {len(d.a_rows):,} albums"
            if self._bad_folders:
                b = self._bad_folders
                self._left += f", {b} folder{'s' if b != 1 else ''} unreadable"
        else:
            self._left = ""
        self._sel_changed()

    def _sel_count(self) -> int:
        if self._mode == FOLDERS:
            return len(self._fv.selectedItems())
        v = self._lists[self._mode]
        return sum(r.height() for r in v.selectionModel().selection())

    def _sel_changed(self, *_):
        n = self._sel_count()
        self._b_sel.setEnabled(n > 0)
        has = bool(self._models[self._mode].rows) if self._mode != FOLDERS \
            else self._fv.topLevelItemCount() > 0
        self._b_all.setEnabled(has)
        self._status.set_texts(self._left, f"{n:,} selected" if n else "")

    # tree view (lazy expand)

    def _populate_tree(self):
        self._fv.clear()
        for folder in self._folders:
            top = QTreeWidgetItem([folder.name])
            top.setToolTip(0, str(folder))
            top.setData(0, Qt.UserRole, folder)
            top.setData(0, ROLE_DIR, True)
            top.setData(0, ROLE_COUNT, self._counts.get(folder))
            self._maybe_add_placeholder(top, folder)
            self._fv.addTopLevelItem(top)

    def _maybe_add_placeholder(self, item: QTreeWidgetItem, path: Path):
        try:
            for e in path.iterdir():
                if e.is_dir() or (e.is_file() and e.suffix.lower() in AUDIO_EXTS):
                    item.addChild(QTreeWidgetItem([_PLACEHOLDER]))
                    return
        except Exception:
            pass

    def _on_expand(self, item: QTreeWidgetItem):
        if item.childCount() == 1 and item.child(0).text(0) == _PLACEHOLDER:
            item.takeChildren()
            path = item.data(0, Qt.UserRole)
            if path and Path(path).is_dir():
                self._load_children(item, Path(path))

    def _load_children(self, parent: QTreeWidgetItem, directory: Path):
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda e: (e.is_file(), e.name.lower()),
            )
            for e in entries:
                if e.is_dir():
                    child = QTreeWidgetItem([e.name])
                    child.setToolTip(0, str(e))
                    child.setData(0, Qt.UserRole, e)
                    child.setData(0, ROLE_DIR, True)
                    child.setData(0, ROLE_COUNT, self._counts.get(e))
                    self._maybe_add_placeholder(child, e)
                    parent.addChild(child)
                elif e.is_file() and e.suffix.lower() in AUDIO_EXTS:
                    child = QTreeWidgetItem([e.name])
                    child.setToolTip(0, str(e))
                    child.setData(0, Qt.UserRole, e)
                    parent.addChild(child)
        except Exception:
            pass

    def _tree_dbl_click(self, item: QTreeWidgetItem, _col: int):
        p = item.data(0, Qt.UserRole)
        if p and Path(p).is_file():
            self._emit([Path(p)])

    # add actions

    def _track_dbl_click(self, index):
        tm = self._tm
        self._emit([tm.d.paths[tm.rows[index.row()]]])

    def _album_dbl_click(self, index):
        self._emit(self._album_files(self._am.rows[index.row()]))

    def _artist_dbl_click(self, index):
        self._emit(self._artist_files([self._rm.rows[index.row()]]))

    def _album_files(self, a: int) -> list[Path]:
        """An album's tracks, in title order, skipping files that have gone."""
        return [p for p in self._data.album_paths(a) if p.is_file()]

    def _artist_files(self, rows: list) -> list[Path]:
        """The files of artist-list rows, each once, in the order of the rows, skipping files that have gone."""
        rm = self._rm
        tracks = dict.fromkeys(i for r in rows for i in rm.row_tracks(r))
        return [p for p in (rm.d.paths[i] for i in tracks) if p.is_file()]

    @staticmethod
    def _descendant_paths(item: QTreeWidgetItem) -> list[Path]:
        """All file paths under an item (skips group rows and folder rows)."""
        out: list[Path] = []
        p = item.data(0, Qt.UserRole)
        if p and Path(p).is_file():
            out.append(Path(p))
        for i in range(item.childCount()):
            out.extend(LibraryWidget._descendant_paths(item.child(i)))
        return out

    def _collect_folder(self, directory: Path) -> list[Path]:
        result: list[Path] = []
        try:
            for p in sorted(directory.rglob("*"), key=lambda x: x.name.lower()):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    result.append(p)
        except Exception:
            pass
        return result

    def _selected_paths(self) -> list[Path]:
        mode = self._mode
        if mode == TRACKS:
            tm = self._tm
            return [tm.d.paths[tm.rows[i]] for i in self._sel_rows(self._tv)]
        if mode == ALBUMS:
            paths: list[Path] = []
            seen: set[Path] = set()
            for i in self._sel_rows(self._av):
                for p in self._album_files(self._am.rows[i]):
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
            return paths
        if mode == ARTISTS:
            return self._artist_files([self._rm.rows[i] for i in self._sel_rows(self._rv)])
        paths = []
        for item in self._fv.selectedItems():
            p = item.data(0, Qt.UserRole)
            if not p:
                continue
            p = Path(p)
            if p.is_file():
                paths.append(p)
            elif p.is_dir():
                paths.extend(self._collect_folder(p))
        return paths

    def _all_paths(self) -> list[Path]:
        mode = self._mode
        if mode == TRACKS:
            # rows hidden by the filter don't count as "all"
            tm = self._tm
            return [tm.d.paths[r] for r in tm.rows] if tm.d else []
        if mode == ALBUMS:
            paths: list[Path] = []
            for a in self._am.rows:
                paths.extend(self._album_files(a))
            return paths
        if mode == ARTISTS:
            return self._artist_files(self._rm.ids)
        paths = []
        for i in range(self._fv.topLevelItemCount()):
            paths.extend(self._descendant_paths(self._fv.topLevelItem(i)))
        return paths

    def _emit(self, paths: list[Path], play: bool = False):
        if not paths:
            return
        self.add_to_playlist.emit(paths)
        if play:
            # the playlist has taken them by now: play the first one, as opening a file does
            try:
                q = self._player.queue
                for i in range(len(q) - 1, -1, -1):
                    if q[i].path == paths[0]:
                        self._player.play_track(i)
                        break
            except Exception:
                pass

    def _add_selected(self, *_, play: bool = False):
        self._emit(self._selected_paths(), play)

    def _add_all(self):
        self._emit(self._all_paths())

    # context menu

    def _context(self, view, pos):
        menu = self._menu_for(view, pos)
        if menu is not None:
            menu.exec(view.viewport().mapToGlobal(pos))

    def _menu_for(self, view, pos):
        """The right-click menu for what is under pos, after selecting it if it was not selected."""
        if view is self._fv:
            item = view.itemAt(pos)
            if item is None:
                return None
            if not item.isSelected():
                view.clearSelection()
                item.setSelected(True)
        else:
            ix = view.indexAt(pos)
            if not ix.isValid():
                return None
            sm = view.selectionModel()
            if not sm.isSelected(ix):
                sm.select(ix, QItemSelectionModel.ClearAndSelect)
            sm.setCurrentIndex(ix, QItemSelectionModel.NoUpdate)
        menu = QMenu(self)
        menu.addAction("Add to playlist", self._add_selected)
        menu.addAction("Add and play", lambda: self._add_selected(play=True))
        menu.addSeparator()
        menu.addAction("Convert…", self._convert_selected)
        if self._target_paths():
            menu.addAction("Show in file manager", self._reveal)
            menu.addAction("Copy path", self._copy_path)
        return menu

    def _convert_selected(self):
        """Tracks and albums as the files they hold, folders as they are: the converter walks them off the GUI thread."""
        paths = self._target_paths() if self._mode == FOLDERS else self._selected_paths()
        if paths:
            self.convert_requested.emit(paths)

    def _target_paths(self) -> list[Path]:
        """Files for a track selection, the album folder for an album, the item for a folder. For an artist the
        folder its tracks share, when they have one inside the library."""
        mode, out = self._mode, []
        if mode == TRACKS:
            tm = self._tm
            out = [tm.d.paths[tm.rows[i]] for i in self._sel_rows(self._tv)]
        elif mode == ALBUMS:
            am = self._am
            for i in self._sel_rows(self._av):
                folder = am.d.a_first[am.rows[i]].parent
                if folder not in out:
                    out.append(folder)
        elif mode == ARTISTS:
            rm = self._rm
            for i in self._sel_rows(self._rv):
                r = rm.rows[i]
                at = (self._artist_folder(r) if type(r) is int else rm.d.a_first[r[2]].parent if r[0] == 1
                      else rm.d.paths[r[3]])
                if at is not None and at not in out:
                    out.append(at)
        else:
            out = [Path(it.data(0, Qt.UserRole)) for it in self._fv.selectedItems()
                   if it.data(0, Qt.UserRole)]
        return out

    def _artist_folder(self, k: int) -> Path | None:
        """The folder an artist's tracks all sit in or under, if it is inside a library folder (a library folder
        itself would only mean the tracks are spread over it)."""
        d = self._data
        common = Path(os.path.commonpath({d.paths[i].parent for i in d.r_rows[k]}))
        return common if any(f in common.parents for f in self._folders) else None

    def _reveal(self):
        t = self._target_paths()
        if t:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(t[0] if t[0].is_dir() else t[0].parent)))

    def _copy_path(self):
        t = self._target_paths()
        if t:
            QGuiApplication.clipboard().setText("\n".join(str(p) for p in t))
