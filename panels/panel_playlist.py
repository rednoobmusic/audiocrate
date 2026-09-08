from __future__ import annotations
import json
import sys
import subprocess
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTabWidget,
    QListWidget, QListWidgetItem, QFileDialog, QInputDialog,
    QMenu, QDialog, QFormLayout, QLineEdit, QDialogButtonBox, QMessageBox,
    QApplication,
)
from PySide6.QtCore import Qt, Signal, QUrl, QTimer
from PySide6.QtGui  import QShortcut, QKeySequence, QDragEnterEvent, QDropEvent
from player import Player, PLAYABLE_EXTS


_last_dir: str = str(Path.home())


def _open_folder(path: Path):

    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(path.parent)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["explorer", "/select,", str(path)])
    except Exception:
        pass


class _TagEditDialog(QDialog):
    _FIELDS = [
        ("title",       "Title"),
        ("artist",      "Artist"),
        ("albumartist", "Album Artist"),
        ("album",       "Album"),
        ("date",        "Year"),
        ("genre",       "Genre"),
        ("tracknumber", "Track #"),
        ("discnumber",  "Disc #"),
        ("composer",    "Composer"),
        ("comment",     "Comment"),
    ]

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Tags — {path.name}")
        self.resize(440, 320)
        self._path = path
        self._mutagen = None

        try:
            from mutagen import File as MFile
            self._mutagen = MFile(str(path), easy=True)
        except Exception:
            pass

        form = QFormLayout(self)
        self._edits: dict[str, QLineEdit] = {}

        for key, label in self._FIELDS:
            val = ""
            if self._mutagen is not None:
                v = self._mutagen.get(key)
                if v:
                    val = str(v[0])
            le = QLineEdit(val)
            self._edits[key] = le
            form.addRow(label + ":", le)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _save(self):
        if self._mutagen is None:
            QMessageBox.warning(self, "Error", "Cannot write tags for this format.")
            return
        try:
            for key, _label in self._FIELDS:
                val = self._edits[key].text().strip()
                if val:
                    self._mutagen[key] = [val]
                elif key in self._mutagen:
                    del self._mutagen[key]
            self._mutagen.save()
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "Save Error", str(e))

_SESSION = Path.home() / ".config" / "audiocrate" / "playlists.json"


_MAX_UNDO = 20


class _PlaylistTab(QListWidget):
    play_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setDragDropMode(QListWidget.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setAcceptDrops(True)
        self._tracks:  list[Path]       = []
        self._history: list[list[Path]] = []
        self.itemDoubleClicked.connect(
            lambda item: self.play_requested.emit(self.row(item))
        )
        # Enter/Return plays the selected row (double-click only before)
        self.itemActivated.connect(
            lambda item: self.play_requested.emit(self.row(item))
        )
        # A cross-tab drag removes the source row after the drop finishes —
        # detect the resulting desync and rebuild _tracks from the items.
        self.model().rowsRemoved.connect(
            lambda *_: QTimer.singleShot(0, self._sync_if_desynced))

    def _make_item(self, p: Path) -> QListWidgetItem:
        item = QListWidgetItem(p.stem)
        item.setData(Qt.UserRole, p)   # the row's identity survives drag-reorder
        if not p.exists():
            self._mark_missing(item, p)
        return item

    def _mark_missing(self, item: QListWidgetItem, p: Path):
        """Grey out a row whose file isn't currently on disk — the entry is kept
        (and saved), never silently dropped; it plays again when the drive is back."""
        f = item.font()
        f.setItalic(True)
        item.setFont(f)
        item.setForeground(self.palette().color(
            self.palette().ColorGroup.Disabled, self.palette().ColorRole.Text))
        item.setToolTip(f"File missing:\n{p}")

    def missing_count(self) -> int:
        return sum(1 for p in self._tracks if not p.exists())

    def remove_missing(self) -> list[Path]:
        """Explicitly prune rows whose files are gone. Returns the removed paths."""
        gone = [p for p in self._tracks if not p.exists()]
        if not gone:
            return []
        self._snapshot()
        gone_set = set(gone)
        for i in range(self.count() - 1, -1, -1):
            if self.item(i).data(Qt.UserRole) in gone_set:
                self.takeItem(i)
        self._tracks = [p for p in self._tracks if p not in gone_set]
        return gone

    def _sync_from_items(self):
        self._tracks = [
            self.item(i).data(Qt.UserRole)
            for i in range(self.count())
            if self.item(i).data(Qt.UserRole) is not None
        ]

    def _sync_if_desynced(self):
        if self.count() != len(self._tracks):
            self._sync_from_items()

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
            audio = [p for p in paths if p.is_file() and p.suffix.lower() in PLAYABLE_EXTS]
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
            # internal drag-reorder: keep _tracks in step with the visible rows,
            # otherwise double-click plays the pre-reorder track at that row
            self._snapshot()
            super().dropEvent(e)
            self._sync_from_items()

    def _snapshot(self):
        self._history.append(list(self._tracks))
        if len(self._history) > _MAX_UNDO:
            self._history.pop(0)

    def undo(self):
        if not self._history:
            return
        prev = self._history.pop()
        self.clear()
        self._tracks = prev
        for p in self._tracks:
            self.addItem(self._make_item(p))

    def add_paths(self, paths: list[Path]):
        added = [p for p in paths if p not in self._tracks]
        if added:
            self._snapshot()
        for p in added:
            self._tracks.append(p)
            self.addItem(self._make_item(p))

    def remove_selected(self):
        rows = sorted([self.row(i) for i in self.selectedItems()], reverse=True)
        if rows:
            self._snapshot()
        for r in rows:
            self.takeItem(r)
            self._tracks.pop(r)

    def clear_all(self):
        if self._tracks:
            self._snapshot()
        self.clear()
        self._tracks.clear()

    def highlight(self, idx: int):
        for i in range(self.count()):
            item = self.item(i)
            txt  = self._tracks[i].stem if i < len(self._tracks) else ""
            item.setText((">> " + txt) if i == idx else txt)
            f = item.font()
            f.setBold(i == idx)
            item.setFont(f)

    def paths(self) -> list[Path]:
        return list(self._tracks)

    def to_m3u(self) -> str:
        lines = ["#EXTM3U"]
        for p in self._tracks:
            lines.append(f"#EXTINF:-1,{p.stem}")
            lines.append(str(p))
        return "\n".join(lines)

    def load_m3u(self, text: str):
        # Missing files are kept (greyed) rather than dropped — dropping them and
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
        return json.dumps([str(p) for p in self._tracks], indent=2)

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
                text = lst._tracks[i].name.lower()
                item.setHidden(bool(q) and q not in text)

    def _current_list(self) -> _PlaylistTab | None:
        w = self._tabs.currentWidget()
        return w if isinstance(w, _PlaylistTab) else None

    def _make_tab(self) -> _PlaylistTab:
        lst = _PlaylistTab()
        lst.play_requested.connect(self._on_play_from_tab)
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
                    "paths": [str(p) for p in lst.paths()],
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
            lst.add_paths(paths)
            self._player.add_many(paths)

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
        lst.takeItem(idx)
        lst._tracks.pop(idx)

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
        lst._tracks[idx] = new_path
        lst.item(idx).setText(new_path.stem)
        lst.item(idx).setData(Qt.UserRole, new_path)
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
