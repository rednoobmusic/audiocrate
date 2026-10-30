"""A drive as a folder browser: the folders and songs of one place, read as you open them, with no scan of the drive first.

QFileSystemModel reads a folder when it is opened, on a thread of its own, so a big drive shows its top folders at once
and nothing is indexed until a person asks for Songs, Albums or Artists. Double click plays a song or opens a folder, and
Play and Add to playlist take a song, a few songs or a whole folder."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QFileSystemModel, QHBoxLayout, QLabel, QMenu, QPushButton, QTreeView, QVBoxLayout,
                               QWidget)

from devices import expand_sources
from player import AUDIO_EXTS

LIMIT = 5000        # songs one Play or Add takes from a folder: a drive's whole library is not one click


class DriveBrowser(QWidget):
    add_to_playlist = Signal(list)       # list[Path]
    play_now = Signal(list)
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root: Path | None = None
        self._model = QFileSystemModel(self)
        self._model.setReadOnly(True)
        self._model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
        self._model.setNameFilters([f"*{e}" for e in sorted(AUDIO_EXTS)])
        self._model.setNameFilterDisables(False)          # other files are left out, folders stay
        self._model.setOption(QFileSystemModel.DontWatchForChanges, True)     # no watch on every folder opened
        self._model.setOption(QFileSystemModel.DontResolveSymlinks, True)
        self._view = QTreeView()
        self._view.setModel(self._model)
        self._view.setHeaderHidden(False)
        self._view.setUniformRowHeights(True)
        self._view.setAnimated(False)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(0, Qt.AscendingOrder)
        self._view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.setFrameShape(QTreeView.NoFrame)
        for col in (2, 3):                                  # type and date say little on a drive of songs
            self._view.setColumnHidden(col, True)
        self._view.header().setStretchLastSection(False)
        self._view.header().setSectionResizeMode(0, self._view.header().ResizeMode.Stretch)
        self._view.doubleClicked.connect(self._open)
        self._view.customContextMenuRequested.connect(self._menu)
        self._view.selectionModel().selectionChanged.connect(self._sync)
        self._where = QLabel()
        self._where.setObjectName("track_sub")
        self._where.setContentsMargins(8, 4, 8, 4)
        self._play = QPushButton("Play")
        self._add = QPushButton("Add to playlist")
        self._play.clicked.connect(lambda: self._emit(self.play_now))
        self._add.clicked.connect(lambda: self._emit(self.add_to_playlist))
        row = QHBoxLayout()
        row.setContentsMargins(6, 4, 6, 6)
        row.addWidget(self._play)
        row.addWidget(self._add)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._view, 1)
        root.addWidget(self._where)
        root.addLayout(row)
        self.setFocusProxy(self._view)
        self._sync()

    # what the panel asks of a view

    def set_root(self, path: Path | None):
        if path is None or path == self._root:
            return
        self._root = path
        index = self._model.setRootPath(str(path))
        self._view.setRootIndex(index)
        self._view.collapseAll()
        self._sync()

    def refresh(self):
        if self._root is not None:
            path, self._root = self._root, None
            self._model.setRootPath("")                     # forgets what was read
            self.set_root(path)

    def busy(self) -> bool:
        return False

    def apps_root(self):
        return None

    def shutdown(self):
        pass

    # what is chosen

    def _paths(self) -> list[Path]:
        seen, out = set(), []
        for ix in self._view.selectionModel().selectedRows(0):
            p = Path(self._model.filePath(ix))
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _songs(self) -> list[Path]:
        songs: list[Path] = []
        for p in self._paths():
            if p.is_dir():
                songs += expand_sources([p])[:LIMIT]
            elif p.suffix.lower() in AUDIO_EXTS:
                songs.append(p)
            if len(songs) >= LIMIT:
                self.status.emit(f"Only the first {LIMIT} songs were taken.")
                return songs[:LIMIT]
        return songs

    def _emit(self, signal):
        songs = self._songs()
        if songs:
            signal.emit(songs)
        else:
            self.status.emit("There are no songs in what is selected.")

    def _sync(self, *_):
        paths = self._paths()
        self._play.setEnabled(bool(paths))
        self._add.setEnabled(bool(paths))
        self._where.setText(str(paths[0]) if len(paths) == 1 else f"{len(paths)} selected" if paths else
                            (f"{self._root}" if self._root else ""))

    def _open(self, ix: QModelIndex):
        p = Path(self._model.filePath(ix))
        if p.is_dir():
            self._view.setExpanded(ix, not self._view.isExpanded(ix))
        elif p.suffix.lower() in AUDIO_EXTS:
            self.play_now.emit([p])

    def _menu(self, pos):
        ix = self._view.indexAt(pos)
        if not ix.isValid():
            return
        if not self._view.selectionModel().isSelected(ix):
            self._view.setCurrentIndex(ix)
        p = Path(self._model.filePath(ix))
        m = QMenu(self)
        m.addAction("Play", lambda: self._emit(self.play_now))
        m.addAction("Add to playlist", lambda: self._emit(self.add_to_playlist))
        m.addSeparator()
        m.addAction("Show in file manager", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(p if p.is_dir() else p.parent))))
        m.exec(self._view.viewport().mapToGlobal(pos))
