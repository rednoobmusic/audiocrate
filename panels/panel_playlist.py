from __future__ import annotations
import json
import sys
import subprocess
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QFileDialog, QInputDialog,
    QMenu, QDialog, QFormLayout, QLineEdit, QDialogButtonBox, QMessageBox,
    QApplication,
)
from PySide6.QtCore import Qt, Signal, QUrl, QTimer
from PySide6.QtGui  import QShortcut, QKeySequence, QDragEnterEvent, QDropEvent
from player import Player, PLAYABLE_EXTS
import threading
import library_cache

# Columns. The leading one is the order the rows were dragged into, and sorting
# by it is what takes the view back to that order.
_COL_NUM, _COL_TITLE, _COL_ARTIST, _COL_ALBUM, _COL_TIME = range(5)
_COL_LABELS = ("#", "Title", "Artist", "Album", "Time")


def _fmt_time(secs: float) -> str:
    """mm:ss, zero padded so the column sorts on its own text.

    Qt sorts a column by comparing what it displays, and "9:00" lands before
    "10:00" when that comparison is done on strings. Padding the minutes fixes
    the order for anything under 100 minutes, which is every track in practice,
    and costs a leading zero on the short ones.
    """
    if not secs or secs <= 0:
        return ""
    m, s = divmod(int(secs), 60)
    return f"{m:02d}:{s:02d}"


def _refused_message(refused: list[Path]) -> str:
    """Name the count and the suffix, since the suffix is usually the surprise."""
    n = len(refused)
    files = f"{n} file" + ("" if n == 1 else "s")
    suffixes = {p.suffix.lower() for p in refused}
    if len(suffixes) > 1:
        return f"Skipped {files} across {len(suffixes)} unrecognised extensions"
    only = suffixes.pop()
    if not only:
        return f"Skipped {files} with no extension"
    return f"Skipped {files}, unrecognised extension: {only}"


class _PlaylistTab(QTreeWidget):
    """One playlist tab.

    Two orders are kept. `_manual` is the order the rows were dragged into and
    is what gets saved, and `_tracks` is what is currently on screen, which is
    the same thing until a column header is clicked. Playback follows the
    visible order, so `_tracks` stays row-for-row with the tree and every index
    that comes out of a click means the same thing it always did.
    """

    play_requested = Signal(int)
    notify         = Signal(str)         # one-liners for the status bar
    _tags_ready    = Signal(object)      # {Path: tags}, from the reader thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(_COL_LABELS))
        self.setHeaderLabels(_COL_LABELS)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setDragDropMode(QTreeWidget.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.setSelectionBehavior(QTreeWidget.SelectRows)
        self.setAcceptDrops(True)

        hdr = self.header()
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(True)
        hdr.setSectionResizeMode(_COL_TITLE,  QHeaderView.Stretch)
        hdr.setSectionResizeMode(_COL_ARTIST, QHeaderView.Interactive)
        hdr.setSectionResizeMode(_COL_ALBUM,  QHeaderView.Interactive)
        hdr.setSectionResizeMode(_COL_NUM,    QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(_COL_TIME,   QHeaderView.ResizeToContents)
        hdr.sortIndicatorChanged.connect(self._on_sort_changed)
        self.setSortingEnabled(True)
        self.sortByColumn(_COL_NUM, Qt.AscendingOrder)

        self._tracks:  list[Path]       = []    # visible order
        self._manual:  list[Path]       = []    # dragged order, the one that saves
        self._history: list[tuple[list[Path], list[Path]]] = []
        self.itemDoubleClicked.connect(
            lambda item, _c=0: self.play_requested.emit(self.indexOfTopLevelItem(item))
        )
        # Enter/Return plays the selected row (double-click only before)
        self.itemActivated.connect(
            lambda item, _c=0: self.play_requested.emit(self.indexOfTopLevelItem(item))
        )
        self._tags_ready.connect(self._apply_tags)
        # A cross-tab drag removes the source row after the drop finishes,
        # detect the resulting desync and rebuild _tracks from the items.
        self.model().rowsRemoved.connect(
            lambda *_: QTimer.singleShot(0, self._sync_if_desynced))

    # rows

    def count(self) -> int:
        """Kept from the QListWidget this used to be, so callers read the same."""
        return self.topLevelItemCount()

    def item(self, i: int):
        return self.topLevelItem(i)

    def row(self, item) -> int:
        return self.indexOfTopLevelItem(item)

    def takeItem(self, i: int):
        return self.takeTopLevelItem(i)

    def _make_item(self, p: Path) -> QTreeWidgetItem:
        item = QTreeWidgetItem([""] * len(_COL_LABELS))
        item.setText(_COL_TITLE, p.stem)
        item.setData(_COL_NUM, Qt.UserRole, p)   # identity survives drag-reorder
        item.setTextAlignment(_COL_NUM,  Qt.AlignRight  | Qt.AlignVCenter)
        item.setTextAlignment(_COL_TIME, Qt.AlignRight  | Qt.AlignVCenter)
        if not p.exists():
            self._mark_missing(item, p)
        return item

    def _mark_missing(self, item: QTreeWidgetItem, p: Path):
        """Grey out a row whose file isn't currently on disk. The entry is kept
        (and saved), never silently dropped; it plays again when the drive is back."""
        f = item.font(_COL_TITLE)
        f.setItalic(True)
        grey = self.palette().color(
            self.palette().ColorGroup.Disabled, self.palette().ColorRole.Text)
        for c in range(len(_COL_LABELS)):
            item.setFont(c, f)
            item.setForeground(c, grey)
        item.setToolTip(_COL_TITLE, f"File missing:\n{p}")

    def _path_of(self, item) -> Path | None:
        return item.data(_COL_NUM, Qt.UserRole) if item is not None else None

    def _renumber(self):
        """Put each row's place in the dragged order into the leading column."""
        order = {p: i for i, p in enumerate(self._manual)}
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            pos = order.get(self._path_of(it), i)
            # an int, not its text: Qt compares the display value as a
            # QVariant, so this sorts 2 before 10 with no help from us
            it.setData(_COL_NUM, Qt.DisplayRole, pos + 1)

    # sorting

    def manual_mode(self) -> bool:
        """True while the view is showing the dragged order, which is the only
        time rearranging rows by hand means anything."""
        return (self.sortColumn() == _COL_NUM
                and self.header().sortIndicatorOrder() == Qt.AscendingOrder)

    def _on_sort_changed(self, *_):
        manual = self.manual_mode()
        self.setDragDropMode(QTreeWidget.DragDrop if manual else QTreeWidget.DropOnly)
        QTimer.singleShot(0, self._sync_from_items)

    # tags

    def _request_tags(self, paths: list[Path]):
        """Fill Artist, Album and Time from the tag cache, off the UI thread.

        A cold cache means one mutagen read per file, which is why adding a large
        folder used to freeze the window.
        """
        if not paths:
            return
        def _run():
            try:
                tags = library_cache.get_many(list(paths))
            except Exception:
                return
            self._tags_ready.emit(tags)
        threading.Thread(target=_run, daemon=True).start()

    def _apply_tags(self, tags: dict):
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            t  = tags.get(self._path_of(it))
            if not t:
                continue
            title = t.get("title") or it.text(_COL_TITLE)
            for col, val in ((_COL_TITLE, title),
                             (_COL_ARTIST, t.get("artist") or ""),
                             (_COL_ALBUM,  t.get("album")  or "")):
                if not it.text(_COL_TITLE).startswith(">> ") or col != _COL_TITLE:
                    it.setText(col, val)
            dur = float(t.get("duration") or 0.0)
            it.setText(_COL_TIME, _fmt_time(dur))

    def missing_count(self) -> int:
        return sum(1 for p in self._manual if not p.exists())

    def remove_missing(self) -> list[Path]:
        """Explicitly prune rows whose files are gone. Returns the removed paths."""
        gone = [p for p in self._manual if not p.exists()]
        if not gone:
            return []
        self._snapshot()
        gone_set = set(gone)
        for i in range(self.topLevelItemCount() - 1, -1, -1):
            if self._path_of(self.topLevelItem(i)) in gone_set:
                self.takeTopLevelItem(i)
        self._manual = [p for p in self._manual if p not in gone_set]
        self._sync_from_items()
        self._renumber()
        return gone

    def _sync_from_items(self):
        self._tracks = [
            self._path_of(self.topLevelItem(i))
            for i in range(self.topLevelItemCount())
            if self._path_of(self.topLevelItem(i)) is not None
        ]

    def _sync_if_desynced(self):
        if self.topLevelItemCount() != len(self._tracks):
            self._sync_from_items()
            self._manual = [p for p in self._manual if p in set(self._tracks)]
            self._renumber()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e: QDropEvent):
        if e.mimeData().hasUrls():
            paths = [
                Path(QUrl(u).toLocalFile())
                for u in e.mimeData().urls()
                if QUrl(u).isLocalFile()
            ]
            # files go through as they are so add_paths can report the refusals;
            # only a folder needs unpacking first
            audio = [p for p in paths if p.is_file()]
            for p in paths:
                if p.is_dir():
                    audio.extend(sorted(
                        (f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in PLAYABLE_EXTS),
                        key=lambda x: x.name.lower(),
                    ))
            if audio:
                self.add_paths(audio)
            e.acceptProposedAction()
        else:
            # Internal drag-reorder. Keep _tracks in step with the visible rows,
            # otherwise double-click plays the pre-reorder track at that row, and
            # take the new arrangement as the dragged order worth saving.
            self._snapshot()
            was_manual = self.manual_mode()
            self.setSortingEnabled(False)      # a live sort would undo the drop
            super().dropEvent(e)
            self._sync_from_items()
            if was_manual:
                self._manual = list(self._tracks)
            self._renumber()
            self.setSortingEnabled(True)

    def _snapshot(self):
        self._history.append((list(self._tracks), list(self._manual)))
        if len(self._history) > _MAX_UNDO:
            self._history.pop(0)

    def undo(self):
        if not self._history:
            return
        tracks, manual = self._history.pop()
        self.clear()
        self._tracks = tracks
        self._manual = manual
        self.setSortingEnabled(False)
        for p in self._tracks:
            self.addTopLevelItem(self._make_item(p))
        self._renumber()
        self.setSortingEnabled(True)
        self._request_tags(self._tracks)

    def add_paths(self, paths: list[Path]) -> list[Path]:
        """Add what can be played and say so when something cannot.

        A file whose suffix is not one we know used to be dropped without a
        word, so a name like "track.ogg Variation 2", whose suffix is the whole
        of ".ogg Variation 2", looked exactly like the format not being
        supported at all.
        """
        playable, refused = [], []
        for p in paths:
            (playable if p.suffix.lower() in PLAYABLE_EXTS else refused).append(p)
        if refused:
            self.notify.emit(_refused_message(refused))

        added = [p for p in playable if p not in self._manual]
        if added:
            self._snapshot()
        self.setSortingEnabled(False)
        for p in added:
            self._manual.append(p)
            self.addTopLevelItem(self._make_item(p))
        self._sync_from_items()
        self._renumber()
        self.setSortingEnabled(True)
        self._request_tags(added)
        return added

    def remove_selected(self):
        rows = sorted([self.indexOfTopLevelItem(i) for i in self.selectedItems()],
                      reverse=True)
        if rows:
            self._snapshot()
        for r in rows:
            gone = self._path_of(self.topLevelItem(r))
            self.takeTopLevelItem(r)
            if gone in self._manual:
                self._manual.remove(gone)
        self._sync_from_items()
        self._renumber()

    def remove_at(self, idx: int):
        """Drop one visible row, keeping the dragged order in step."""
        gone = self._path_of(self.topLevelItem(idx))
        self.takeTopLevelItem(idx)
        if gone in self._manual:
            self._manual.remove(gone)
        self._sync_from_items()
        self._renumber()

    def rename_at(self, idx: int, new_path: Path):
        """Point one visible row at a file that has just been renamed on disk."""
        it  = self.topLevelItem(idx)
        old = self._path_of(it)
        if it is None:
            return
        it.setData(_COL_NUM, Qt.UserRole, new_path)
        it.setText(_COL_TITLE, new_path.stem)
        if old in self._manual:
            self._manual[self._manual.index(old)] = new_path
        self._sync_from_items()

    def clear_all(self):
        if self._manual:
            self._snapshot()
        self.clear()
        self._tracks.clear()
        self._manual.clear()

    def highlight(self, idx: int):
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            base = item.text(_COL_TITLE).removeprefix(">> ")
            item.setText(_COL_TITLE, (">> " + base) if i == idx else base)
            f = item.font(_COL_TITLE)
            f.setBold(i == idx)
            for c in range(len(_COL_LABELS)):
                item.setFont(c, f)

    def paths(self) -> list[Path]:
        """What is on screen, which is what plays."""
        return list(self._tracks)

    def manual_paths(self) -> list[Path]:
        """The dragged order, which is what gets written to disk."""
        return list(self._manual)

    def to_m3u(self) -> str:
        lines = ["#EXTM3U"]
        for p in self._manual:
            lines.append(f"#EXTINF:-1,{p.stem}")
            lines.append(str(p))
        return "\n".join(lines)

    def load_m3u(self, text: str):
        # Missing files are kept (greyed) rather than dropped. Dropping them and
        # re-saving used to permanently shrink playlists when a drive was unplugged.
        paths = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                p = Path(line)
                if p.suffix.lower() in PLAYABLE_EXTS:
                    paths.append(p)
        self.add_paths(paths)

    def to_json(self) -> str:
        return json.dumps([str(p) for p in self._manual], indent=2)

    def load_json(self, text: str):
        self.add_paths([Path(s) for s in json.loads(text)])


class PlaylistPanel(QWidget):
    play_track = Signal(int)
    notify     = Signal(str)   # user-facing one-liners (shown in the status bar)

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player     = player
        self._active_tab = 0
        self._clipboard: list[Path] = []
        self._build()

        player.track_changed.connect(self._on_track_changed)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        top = QHBoxLayout()
        new_btn  = QPushButton("+")
        new_btn.setFixedWidth(28)
        new_btn.setToolTip("New playlist")
        new_btn.clicked.connect(self._new_tab)

        save_btn = QPushButton("Save")
        load_btn = QPushButton("Load")
        save_btn.clicked.connect(self._save)
        load_btn.clicked.connect(self._load)

        top.addWidget(new_btn)
        top.addStretch()
        top.addWidget(load_btn)
        top.addWidget(save_btn)
        root.addLayout(top)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter…")
        self._filter.textChanged.connect(self._apply_filter)
        root.addWidget(self._filter)

        # Up Next now lives in its own dockable panel (panel_upnext.py).
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._on_tab_change)
        # keep the typed filter across tab switches; re-apply it to the new tab
        self._tabs.currentChanged.connect(lambda _: self._apply_filter())
        self._tabs.tabBarDoubleClicked.connect(self._rename_tab)
        root.addWidget(self._tabs, 1)

        btn_row = QHBoxLayout()
        rem_btn = QPushButton("Remove")
        clr_btn = QPushButton("Clear")
        rem_btn.clicked.connect(self._remove_selected)
        clr_btn.clicked.connect(self._clear)
        btn_row.addWidget(rem_btn, 1)
        btn_row.addWidget(clr_btn, 1)
        root.addLayout(btn_row)

        for seq, slot in [
            ("Ctrl+Z", self._undo),
            ("Ctrl+C", self._copy_selected),
            ("Ctrl+V", self._paste),
            ("Delete", self._remove_selected),
        ]:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

        if not self._load_session():
            self._new_tab("Queue")

    def _apply_filter(self):
        q = self._filter.text().strip().lower()
        lst = self._current_list()
        if not lst:
            return
        for i in range(lst.count()):
            item = lst.item(i)
            if i < len(lst._tracks):
                # the filename plus whatever the tag columns are showing
                text = " ".join([lst._tracks[i].name.lower()]
                                + [item.text(c).lower()
                                   for c in (_COL_TITLE, _COL_ARTIST, _COL_ALBUM)])
                item.setHidden(bool(q) and q not in text)

    def _current_list(self) -> _PlaylistTab | None:
        w = self._tabs.currentWidget()
        return w if isinstance(w, _PlaylistTab) else None

    def _make_tab(self) -> _PlaylistTab:
        lst = _PlaylistTab()
        lst.play_requested.connect(self._on_play_from_tab)
        lst.notify.connect(self.notify)
        lst.setContextMenuPolicy(Qt.CustomContextMenu)
        lst.customContextMenuRequested.connect(lambda pos, l=lst: self._ctx_menu(pos, l))
        return lst

    def _new_tab(self, name: str = ""):
        if not name:
            name, ok = QInputDialog.getText(self, "New Playlist", "Name:")
            if not ok or not name.strip():
                name = f"Playlist {self._tabs.count() + 1}"
        lst = self._make_tab()
        self._tabs.addTab(lst, name)
        self._tabs.setCurrentWidget(lst)

    def _close_tab(self, idx: int):
        if self._tabs.count() > 1:
            self._tabs.removeTab(idx)

    def _rename_tab(self, idx: int):
        if idx < 0:
            return
        cur = self._tabs.tabText(idx)
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=cur)
        if ok and name.strip():
            self._tabs.setTabText(idx, name.strip())

    def _load_session(self) -> bool:
        if not _SESSION.exists():
            return False
        try:
            data = json.loads(_SESSION.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not data:
            return False
        missing = 0
        total = 0
        for entry in data:
            name  = entry.get("name", "Playlist")
            # keep unavailable tracks (greyed) — never silently shrink a saved list
            paths = [Path(s) for s in entry.get("paths", [])]
            lst   = self._make_tab()
            lst.add_paths(paths)
            self._tabs.addTab(lst, name)
            missing += lst.missing_count()
            total   += len(paths)
        self._tabs.setCurrentIndex(0)
        first = self._tabs.widget(0)
        if isinstance(first, _PlaylistTab) and first.paths():
            self._player.add_many(first.paths())
        if missing:
            # deferred: this runs during __init__, before the window connects notify
            msg = (f"{missing} of {total} playlist track{'s' if total != 1 else ''} "
                   f"currently unavailable (kept, shown greyed)")
            QTimer.singleShot(0, lambda m=msg: self.notify.emit(m))
        return True

    def save_session(self):
        _SESSION.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for i in range(self._tabs.count()):
            lst = self._tabs.widget(i)
            if isinstance(lst, _PlaylistTab):
                data.append({
                    "name":  self._tabs.tabText(i),
                    "paths": [str(p) for p in lst.manual_paths()],
                })
        _SESSION.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _on_tab_change(self, idx: int):
        self._active_tab = idx

    def _on_play_from_tab(self, idx: int):
        lst = self._current_list()
        if not lst:
            return
        if [t.path for t in self._player.queue] != lst.paths():
            self._player.clear()
            self._player.add_many(lst.paths())
        self._player.play_track(idx)

    def add_paths(self, paths: list[Path]):
        lst = self._current_list()
        if lst:
            added = lst.add_paths(paths)
            self._player.add_many(added)

    def _undo(self):
        lst = self._current_list()
        if lst:
            lst.undo()

    def _copy_selected(self):
        lst = self._current_list()
        if not lst:
            return
        sel = [lst._tracks[r]
               for i in lst.selectedItems()
               if 0 <= (r := lst.row(i)) < len(lst._tracks)]
        if sel:
            self._clipboard = sel
            QApplication.clipboard().setText("\n".join(str(p) for p in sel))

    def _paste(self):
        lst = self._current_list()
        if not lst or not self._clipboard:
            return
        lst.add_paths(self._clipboard)
        self._player.add_many(self._clipboard)

    def _remove_selected(self):
        lst = self._current_list()
        if lst:
            rows = sorted(
                [lst.row(i) for i in lst.selectedItems()], reverse=True
            )
            for r in rows:
                self._player.remove(r)
            lst.remove_selected()

    def _clear(self):
        lst = self._current_list()
        if lst:
            lst.clear_all()
            self._player.clear()

    def restore_selection(self, path: Path):
        for ti in range(self._tabs.count()):
            lst = self._tabs.widget(ti)
            if not isinstance(lst, _PlaylistTab):
                continue
            for i, p in enumerate(lst.paths()):
                if p == path:
                    lst.highlight(i)
                    self._tabs.setCurrentIndex(ti)
                    return

    # context menu

    def _ctx_menu(self, pos, lst: _PlaylistTab):
        item = lst.itemAt(pos)
        idx  = lst.row(item) if item else -1
        path = lst._tracks[idx] if 0 <= idx < len(lst._tracks) else None

        m = QMenu(self)

        if path:
            m.addAction("\U000f040a  Play Now",       lambda: self._play_ctx(idx, lst))
            m.addSeparator()
            m.addAction("\U000f018f  Copy  Ctrl+C",   lambda: self._copy_selected())
        paste_act = m.addAction("\U000f0192  Paste  Ctrl+V", lambda: self._paste())
        paste_act.setEnabled(bool(self._clipboard))
        if path:
            m.addSeparator()
            m.addAction("\U000f0770  Open Folder",    lambda: _open_folder(path))
            m.addAction("\U000f0339  Copy Path",      lambda: QApplication.clipboard().setText(str(path)))
            m.addSeparator()
            m.addAction("\U000f0fab  Edit Tags…",     lambda: self._edit_tags(path))
            m.addAction("\U000f0fe5  Rename File…",   lambda: self._rename_on_disk(path, lst, idx))
            m.addAction("\U000f01b4  Delete File…",   lambda: self._delete_on_disk(path, lst, idx))
            m.addSeparator()
            m.addAction("\U000f0410  Remove from Playlist", lambda: self._remove_at(idx, lst))

        m.addSeparator()
        rm_missing = m.addAction("\U000f0413  Remove Missing Tracks",
                                 lambda: self._remove_missing(lst))
        rm_missing.setEnabled(lst.missing_count() > 0)
        m.addAction("\U000f05e8  Clear Playlist", lambda: self._clear())

        m.exec(lst.mapToGlobal(pos))

    def _remove_missing(self, lst: _PlaylistTab):
        """Explicit prune of unavailable tracks — the only way they leave a list."""
        gone = lst.remove_missing()
        if not gone:
            return
        gone_set = set(gone)
        # also drop them from the play queue (indices descending; remove() keeps
        # the current-track bookkeeping straight)
        for i in range(len(self._player.queue) - 1, -1, -1):
            if self._player.queue[i].path in gone_set:
                self._player.remove(i)
        self.notify.emit(f"Removed {len(gone)} missing track{'s' if len(gone) != 1 else ''}")

    def _play_ctx(self, idx: int, lst: _PlaylistTab):
        self._player.clear()
        self._player.add_many(lst.paths())
        self._player.play_track(idx)

    def _remove_at(self, idx: int, lst: _PlaylistTab):
        lst._snapshot()
        self._player.remove(idx)
        lst.remove_at(idx)

    def _edit_tags(self, path: Path):
        _TagEditDialog(path, self).exec()

    def _rename_on_disk(self, path: Path, lst: _PlaylistTab, idx: int):
        new_name, ok = QInputDialog.getText(
            self, "Rename File", "New filename (without extension):",
            text=path.stem,
        )
        if not ok or not new_name.strip():
            return
        new_path = path.with_name(new_name.strip() + path.suffix)
        try:
            path.rename(new_path)
        except Exception as e:
            QMessageBox.warning(self, "Rename Failed", str(e))
            return
        # update playlist entry
        lst.rename_at(idx, new_path)
        # update player queue (keeps the dedupe set in sync)
        self._player.rename_track(idx, new_path)

    def _delete_on_disk(self, path: Path, lst: _PlaylistTab, idx: int):
        ans = QMessageBox.question(
            self, "Delete File",
            f"Permanently delete\n{path.name}\nfrom disk?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        # if it's the playing track, move off it first — otherwise the engine
        # keeps reading from a deleted file
        cur = self._player.current
        if cur is not None and cur.path == path:
            if len(self._player.queue) > 1:
                self._player.next()
            else:
                self._player.stop()
        try:
            path.unlink()
        except Exception as e:
            QMessageBox.warning(self, "Delete Failed", str(e))
            return
        self._remove_at(idx, lst)

    # track changed

    def _on_track_changed(self, track):
        lst = self._current_list()
        if lst:
            lst.highlight(self._player.index)

    def _save(self):
        global _last_dir
        lst = self._current_list()
        if not lst or not lst.paths():
            return
        name = self._tabs.tabText(self._tabs.currentIndex())
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Playlist", str(Path(_last_dir) / f"{name}.m3u"),
            "M3U Playlist (*.m3u);;JSON Playlist (*.json)"
        )
        if not path:
            return
        p = Path(path)
        _last_dir = str(p.parent)
        if p.suffix.lower() == ".json":
            p.write_text(lst.to_json(), encoding="utf-8")
        else:
            p.write_text(lst.to_m3u(), encoding="utf-8")

    def _load(self):
        global _last_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Playlist", _last_dir,
            "Playlists (*.m3u *.json);;M3U (*.m3u);;JSON (*.json)"
        )
        if not path:
            return
        p = Path(path)
        _last_dir = str(p.parent)
        text = p.read_text(encoding="utf-8")
        name = p.stem

        lst = self._make_tab()
        if p.suffix.lower() == ".json":
            lst.load_json(text)
        else:
            lst.load_m3u(text)

        self._tabs.addTab(lst, name)
        self._tabs.setCurrentWidget(lst)
