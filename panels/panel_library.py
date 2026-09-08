from __future__ import annotations
import json
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QStackedWidget, QDialog, QDialogButtonBox,
    QLabel, QAbstractItemView,
)
from PySide6.QtCore import Qt, Signal, QFileSystemWatcher, QTimer

from player import Player, PLAYABLE_EXTS as AUDIO_EXTS

_LIB_CFG      = Path.home() / ".config" / "audiocrate" / "library.json"
_PLACEHOLDER  = "__placeholder__"
_last_lib_dir: str = str(Path.home())


# folder config dialog

class _FolderDialog(QDialog):
    def __init__(self, folders: list[Path], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library Folders")
        self.resize(480, 260)
        self._folders = list(folders)

        lay = QVBoxLayout(self)

        self._lw = QListWidget()
        for f in self._folders:
            self._lw.addItem(str(f))
        lay.addWidget(self._lw, 1)

        row = QHBoxLayout()
        add = QPushButton("Add Folder…")
        rem = QPushButton("Remove Selected")
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
        d = QFileDialog.getExistingDirectory(self, "Add Library Folder", _last_lib_dir)
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


# main widget

class LibraryWidget(QWidget):
    add_to_playlist = Signal(list)
    _scan_done      = Signal(list)
    _scan_progress  = Signal(str)

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player    = player
        self._folders:   list[Path] = []
        self._all_files: list[Path] = []
        self._meta:      dict[Path, dict] = {}   # path → cached tags (title/artist/album)
        self._haystacks: list[str] = []          # lowercase search text per list row
        self._bad_folders = 0

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
        self._scan_progress.connect(self._status.setText)
        if self._folders:
            self._refresh()

    # config

    def _load_config(self):
        try:
            d = json.loads(_LIB_CFG.read_text(encoding="utf-8"))
            self._folders = [Path(p) for p in d.get("folders", []) if Path(p).is_dir()]
        except Exception:
            self._folders = []

    def _save_config(self):
        try:
            _LIB_CFG.parent.mkdir(parents=True, exist_ok=True)
            _LIB_CFG.write_text(
                json.dumps({"folders": [str(f) for f in self._folders]}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            from applog import log
            log.exception("failed to save library config to %s", _LIB_CFG)

    # UI build

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # mode + action toolbar
        bar = QHBoxLayout()
        self._btn_list   = QPushButton("List")
        self._btn_tree   = QPushButton("Tree")
        self._btn_albums = QPushButton("Albums")
        for b in (self._btn_list, self._btn_tree, self._btn_albums):
            b.setCheckable(True)
            b.setFixedHeight(24)
        self._btn_list.setChecked(True)
        self._btn_list.clicked.connect(lambda: self._set_mode(0))
        self._btn_tree.clicked.connect(lambda: self._set_mode(1))
        self._btn_albums.clicked.connect(lambda: self._set_mode(2))

        btn_folders = QPushButton("Folders…")
        btn_refresh = QPushButton("\U000f0450")   # nf-md-refresh
        btn_folders.setFixedHeight(24)
        btn_refresh.setFixedHeight(24)
        btn_refresh.setFixedWidth(28)
        btn_folders.clicked.connect(self._edit_folders)
        btn_refresh.clicked.connect(self._refresh)

        bar.addWidget(self._btn_list)
        bar.addWidget(self._btn_tree)
        bar.addWidget(self._btn_albums)
        bar.addStretch()
        bar.addWidget(btn_folders)
        bar.addWidget(btn_refresh)
        root.addLayout(bar)

        # filter (list mode only) — debounced so typing doesn't re-filter a huge
        # library on every keystroke
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search title / artist / album / file…")
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(self._apply_filter)
        self._search.textChanged.connect(lambda _: self._filter_timer.start())
        root.addWidget(self._search)

        # status
        self._status = QLabel("No library folders set - click Folders...")
        root.addWidget(self._status)

        # list view
        self._list_view = QListWidget()
        self._list_view.setAlternatingRowColors(True)
        self._list_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list_view.itemDoubleClicked.connect(
            lambda item: self.add_to_playlist.emit([item.data(Qt.UserRole)])
        )

        # tree view (lazy)
        self._tree_view = QTreeWidget()
        self._tree_view.setHeaderHidden(True)
        self._tree_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree_view.itemExpanded.connect(self._on_expand)
        self._tree_view.itemDoubleClicked.connect(self._tree_dbl_click)

        # albums view: artist → album → tracks (grouped from cached tags)
        self._albums_view = QTreeWidget()
        self._albums_view.setHeaderHidden(True)
        self._albums_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._albums_view.itemDoubleClicked.connect(self._albums_dbl_click)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._list_view)     # 0
        self._stack.addWidget(self._tree_view)     # 1
        self._stack.addWidget(self._albums_view)   # 2
        root.addWidget(self._stack, 1)

        # bottom buttons
        btn_row = QHBoxLayout()
        b_sel = QPushButton("Add Selected")
        b_all = QPushButton("Add All")
        b_sel.clicked.connect(self._add_selected)
        b_all.clicked.connect(self._add_all)
        btn_row.addWidget(b_sel, 1)
        btn_row.addWidget(b_all, 1)
        root.addLayout(btn_row)

    # mode switching

    def _set_mode(self, idx: int):
        self._stack.setCurrentIndex(idx)
        self._btn_list.setChecked(idx == 0)
        self._btn_tree.setChecked(idx == 1)
        self._btn_albums.setChecked(idx == 2)
        if idx == 1 and self._tree_view.topLevelItemCount() == 0:
            self._populate_tree()
        elif idx == 2 and self._albums_view.topLevelItemCount() == 0:
            self._populate_albums()

    # albums view (artist → album → tracks, from cached tags)

    def _populate_albums(self):
        self._albums_view.clear()
        # group: artist → album → [(track_no, title, path)]
        groups: dict[str, dict[str, list]] = {}
        for p in self._all_files:
            m = self._meta.get(p) or {}
            artist = (m.get("artist") or "Unknown Artist").strip() or "Unknown Artist"
            album  = (m.get("album")  or "Unknown Album").strip() or "Unknown Album"
            title  = m.get("title") or p.stem
            groups.setdefault(artist, {}).setdefault(album, []).append((title, p))
        for artist in sorted(groups, key=str.lower):
            a_item = QTreeWidgetItem([artist])
            a_item.setData(0, Qt.UserRole, None)
            for album in sorted(groups[artist], key=str.lower):
                al_item = QTreeWidgetItem([album])
                al_item.setData(0, Qt.UserRole, None)
                for title, p in sorted(groups[artist][album], key=lambda x: x[0].lower()):
                    t_item = QTreeWidgetItem([title])
                    t_item.setToolTip(0, str(p))
                    t_item.setData(0, Qt.UserRole, p)
                    al_item.addChild(t_item)
                a_item.addChild(al_item)
            self._albums_view.addTopLevelItem(a_item)

    def _albums_dbl_click(self, item: QTreeWidgetItem, _col: int):
        paths = self._descendant_paths(item)
        if paths:
            self.add_to_playlist.emit(paths)

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

    # folder management

    def _edit_folders(self):
        dlg = _FolderDialog(self._folders, self)
        if dlg.exec():
            self._folders = dlg.result_folders()
            self._save_config()
            self._refresh()

    # scanning

    def _refresh(self):
        if not self._folders:
            self._status.setText("No library folders set - click Folders...")
            return
        n = len(self._folders)
        self._status.setText(f"Scanning {n} folder{'s' if n != 1 else ''}...")
        threading.Thread(target=self._bg_scan, daemon=True).start()

    def _bg_scan(self):
        files: list[Path] = []
        bad_folders = 0
        for folder in self._folders:
            try:
                for p in folder.rglob("*"):
                    if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                        files.append(p)
                        if len(files) % 500 == 0:
                            self._scan_progress.emit(f"Scanning… {len(files):,} files found")
            except Exception:
                bad_folders += 1
        files.sort(key=lambda p: p.name.lower())
        # Read tags via the persistent cache (only stale files actually hit disk).
        # Chunked so the status label can show progress on a first big scan.
        meta: dict[Path, dict] = {}
        try:
            import library_cache
            for i in range(0, len(files), 500):
                chunk = files[i:i + 500]
                meta.update(library_cache.get_many(chunk))
                if len(files) > 500:
                    self._scan_progress.emit(
                        f"Reading tags… {min(i + 500, len(files)):,} of {len(files):,}")
        except Exception:
            pass
        self._meta = meta
        self._bad_folders = bad_folders
        self._scan_done.emit(files)

    def _apply_scan(self, files: list):
        # Preserve what the user was doing: selection + scroll survive a rescan
        # (a watched-folder touch used to snap the list to the top).
        selected = {str(it.data(Qt.UserRole))
                    for it in self._list_view.selectedItems()}
        scroll = self._list_view.verticalScrollBar().value()
        self._all_files = files
        self._rebuild_list(selected)
        self._list_view.verticalScrollBar().setValue(scroll)
        self._apply_filter()
        self._tree_view.clear()
        self._albums_view.clear()
        if self._stack.currentIndex() == 1:
            self._populate_tree()
        elif self._stack.currentIndex() == 2:
            self._populate_albums()
        paths = [str(f) for f in self._folders]
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        if paths:
            self._watcher.addPaths(paths)

    # list view

    def _rebuild_list(self, reselect: set[str] | None = None):
        """Build list items once per scan; filtering then only hides rows."""
        self._list_view.setUpdatesEnabled(False)
        self._list_view.clear()
        self._haystacks: list[str] = []
        for p in self._all_files:
            m = self._meta.get(p) or {}
            title  = m.get("title") or p.stem
            artist = m.get("artist") or ""
            album  = m.get("album") or ""
            item = QListWidgetItem(f"{artist} — {title}" if artist else title)
            item.setToolTip(str(p))
            item.setData(Qt.UserRole, p)
            self._list_view.addItem(item)
            if reselect and str(p) in reselect:
                item.setSelected(True)
            self._haystacks.append(f"{title}\n{artist}\n{album}\n{p.name}".lower())
        self._list_view.setUpdatesEnabled(True)

    def _apply_filter(self):
        q = self._search.text().strip().lower()
        lv = self._list_view
        if lv.count() != len(self._all_files):
            self._rebuild_list()
        lv.setUpdatesEnabled(False)
        shown = 0
        for i, hay in enumerate(self._haystacks):
            hide = bool(q) and q not in hay
            lv.item(i).setHidden(hide)
            if not hide:
                shown += 1
        lv.setUpdatesEnabled(True)
        total   = len(self._all_files)
        nf      = len(self._folders)
        folders = f" in {nf} folder{'s' if nf != 1 else ''}"
        bad = getattr(self, "_bad_folders", 0)
        suffix = f" — {bad} folder{'s' if bad != 1 else ''} unreadable" if bad else ""
        self._status.setText(
            (f"{shown} of {total} tracks" if q else f"{total} tracks{folders}") + suffix
        )

    # tree view (lazy expand)

    def _populate_tree(self):
        self._tree_view.clear()
        for folder in self._folders:
            top = QTreeWidgetItem([folder.name])
            top.setToolTip(0, str(folder))
            top.setData(0, Qt.UserRole, folder)
            self._maybe_add_placeholder(top, folder)
            self._tree_view.addTopLevelItem(top)

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
            self.add_to_playlist.emit([Path(p)])

    # add actions

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
        mode = self._stack.currentIndex()
        if mode == 0:
            return [
                item.data(Qt.UserRole)
                for item in self._list_view.selectedItems()
                if item.data(Qt.UserRole)
            ]
        if mode == 2:
            paths: list[Path] = []
            for item in self._albums_view.selectedItems():
                for p in self._descendant_paths(item):
                    if p not in paths:
                        paths.append(p)
            return paths
        paths = []
        for item in self._tree_view.selectedItems():
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
        mode = self._stack.currentIndex()
        if mode == 0:
            # rows hidden by the filter don't count as "all"
            return [
                self._list_view.item(i).data(Qt.UserRole)
                for i in range(self._list_view.count())
                if not self._list_view.item(i).isHidden()
                and self._list_view.item(i).data(Qt.UserRole)
            ]
        view = self._albums_view if mode == 2 else self._tree_view
        paths: list[Path] = []
        for i in range(view.topLevelItemCount()):
            paths.extend(self._descendant_paths(view.topLevelItem(i)))
        return paths

    def _add_selected(self):
        paths = self._selected_paths()
        if paths:
            self.add_to_playlist.emit(paths)

    def _add_all(self):
        paths = self._all_paths()
        if paths:
            self.add_to_playlist.emit(paths)
