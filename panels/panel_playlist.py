from __future__ import annotations
import json
import safe_write
import os
import sys
import subprocess
import time
from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTabWidget, QLabel, QSizePolicy,
    QHeaderView, QFileDialog, QInputDialog, QMenu, QDialog, QFormLayout, QLineEdit,
    QDialogButtonBox, QMessageBox, QApplication,
)
from PySide6.QtCore import Qt, Signal, QTimer, QByteArray
from PySide6.QtGui import QShortcut, QKeySequence, QActionGroup, QStandardItemModel
from player import Player, PLAYABLE_EXTS
import threading
import library_cache
import logs
import playlist_tags
from playlist_model import (
    GROUPS, NEEDS_EXTRA, PRESETS, SORT_FIELDS, Row, describe_sort, fmt_total, plural,
)
from playlist_view import COLUMNS, FIELD_COL, PlaylistView

_log = logs.get("tags")

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
        ("albumartist", "Album artist"),
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
        self.setWindowTitle(f"Edit tags: {path.name}")
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
            QMessageBox.warning(self, "Save error", str(e))

_SESSION = Path.home() / ".config" / "audiocrate" / "playlists.json"


_MAX_UNDO = 20


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


class _PlaylistTab(PlaylistView):
    """One playlist tab: the list and everything that edits it.

    Two orders are kept. `m.rows` is the order the rows were arranged into and is
    what gets saved, and `m.order` is what is on screen, which is the same thing
    until a column header, the Sort menu or Group by is used. Playback follows the visible
    order, so every index that comes out of a click means a place in `paths()`.
    Dragging or moving rows while a sort is showing keeps that sorted order as the
    new arranged one first, so what you see is what you rearrange.
    """

    play_requested = Signal(int)
    notify         = Signal(str)         # one-liners for the status bar
    order_changed  = Signal()            # the visible order changed
    removed        = Signal(object)      # [Path] that left this playlist
    drop_ready     = Signal(object, object, bool)   # [Path], the row they go next to, after it
    status_changed = Signal()
    scanned        = Signal()            # the reader ran out of work
    _chunk         = Signal(str, object)           # from the reader thread
    _walked        = Signal(object, object, bool)  # from the folder scan thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history: list[tuple[list[Row], list[tuple[str, bool]]]] = []
        self._jobs: list[tuple[bool, list[Row]]] = []
        self._jobs_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._asked_extra: set[Row] = set()
        self._extra_all = False
        self._refresh_ms = 150
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self._refresh_now)
        self._chunk.connect(self._apply_chunk)
        self._walked.connect(self.drop_ready)
        self.activated.connect(self.play_requested)
        self.sort_clicked.connect(self._on_sort_clicked)
        self.reorder_requested.connect(self._on_reorder)
        self.files_dropped.connect(self._on_files_dropped)
        self.selection_changed.connect(self.status_changed)
        self.columns_changed.connect(self._ensure_extra)

    # rows

    def count(self) -> int:
        return len(self.m.rows)

    @property
    def _tracks(self) -> list[Path]:
        return self.m.paths()

    @property
    def _manual(self) -> list[Path]:
        return self.m.manual_paths()

    def paths(self) -> list[Path]:
        """What is on screen, which is what plays."""
        return self.m.paths()

    def manual_paths(self) -> list[Path]:
        """The arranged order, which is what gets written to disk."""
        return self.m.manual_paths()

    def selected_paths(self) -> list[Path]:
        return [r.path for r in self.selected_rows()]

    def has_path(self, path: Path) -> bool:
        return path in self.m.by_path

    def manual_mode(self) -> bool:
        """True while the view shows the arranged order and no sort is applied."""
        return not self.m.sort

    def missing_count(self) -> int:
        return sum(1 for r in self.m.rows if r.missing)

    def _snapshot(self):
        self._history.append((list(self.m.rows), list(self.m.sort)))
        if len(self._history) > _MAX_UNDO:
            self._history.pop(0)

    def _changed(self, order: bool = False):
        self.status_changed.emit()
        if order:
            self.order_changed.emit()

    def _arrange(self, op) -> bool:
        """Run an edit that rearranges rows, as one undo step. False when nothing moved."""
        before, sort = list(self.m.order), list(self.m.sort)
        self._snapshot()
        with self.keep_place():
            op()
        if self.m.order == before and self.m.sort == sort:
            self._history.pop()
            return False
        self._changed(order=True)
        return True

    def undo(self):
        if not self._history:
            return
        rows, sort = self._history.pop()
        old = set(self.m.rows)
        with self.keep_place():
            self.m.replace(rows, sort)
        self.forget(old - set(rows))
        self._extra_all = False
        self._ensure_extra()
        self._changed(order=True)

    def add_paths(self, paths: list[Path], at: Row | None = None, after: bool = False) -> list[Path]:
        """Add what can be played and say so when something cannot.

        A file whose suffix is not one we know is reported, not dropped silently, so a
        name like "track.ogg Variation 2", whose suffix is the whole of ".ogg Variation 2",
        is not mistaken for an unsupported format.

        The new rows go next to `at` when one is given, else at the end. Files
        already here are skipped, and so is a path that appears twice in the list.
        """
        playable, refused = [], []
        for p in paths:
            (playable if p.suffix.lower() in PLAYABLE_EXTS else refused).append(p)
        if refused:
            self.notify.emit(_refused_message(refused))
        have = self.m.by_path
        fresh = [p for p in dict.fromkeys(playable) if p not in have]
        if not fresh:
            return []
        self._snapshot()
        with self.keep_place():
            rows = self.m.add(fresh, at, after)
        self._request(rows)
        self._extra_all = False
        self._ensure_extra()
        self._changed(order=at is not None)
        return [r.path for r in rows]

    def _remove_rows(self, rows) -> list[Path]:
        rows = list(rows)
        if not rows:
            return []
        self._snapshot()
        first = min((r.pos for r in rows if r.pos >= 0), default=-1)
        paths = [r.path for r in rows]
        with self.keep_place():
            self.m.remove(rows)
        self.forget(rows)
        self._select_near(first)
        self._changed(order=True)
        self.removed.emit(paths)
        return paths

    def _select_near(self, pos: int):
        """After a removal, select the row that took the first removed row's place, so
        pressing Delete again carries on down the list."""
        disp = self.m.display
        if pos < 0 or not disp or self._sel:
            return
        i = min(pos, len(disp) - 1)
        for step in (1, -1):
            j = i
            while 0 <= j < len(disp):
                if type(disp[j]) is Row:
                    self.select_rows({disp[j]}, disp[j])
                    return
                j += step

    def remove_selected(self) -> list[Path]:
        return self._remove_rows(self.selected_rows())

    def remove_at(self, idx: int):
        """Drop one visible row, keeping the arranged order in step."""
        order = self.m.order
        if 0 <= idx < len(order):
            self._remove_rows([order[idx]])

    def remove_missing(self) -> list[Path]:
        """Explicitly prune rows whose files are gone. Returns the removed paths."""
        return self._remove_rows([r for r in self.m.rows if r.missing])

    def remove_duplicates(self) -> int:
        gone = self.m.duplicates()
        self._remove_rows(gone)
        return len(gone)

    def rename_at(self, idx: int, new_path: Path):
        """Point one visible row at a file that has just been renamed on disk."""
        order = self.m.order
        if not 0 <= idx < len(order):
            return
        with self.keep_place():
            self.m.rename(order[idx], new_path)

    def clear_all(self):
        if self.m.rows:
            self._snapshot()
        gone = list(self.m.rows)
        with self.keep_place():
            self.m.replace([], self.m.sort)
        self.forget(gone)
        self.verticalScrollBar().setValue(0)
        self._changed()

    # now playing

    def highlight(self, idx: int):
        """Mark the row at a place in the visible order as the playing one."""
        order = self.m.order
        self.set_playing(order[idx] if 0 <= idx < len(order) else None)

    def set_playing_path(self, path: Path | None):
        """The playing row is found by path, so sorting never leaves the marker on
        whatever moved into its place."""
        self.set_playing(self.m.by_path.get(path) if path is not None else None)

    def locate_playing(self):
        row = self.playing()
        if row is not None:
            self.select_rows({row}, row)
            self.scroll_to(row, center=True)

    def note_duration(self, path: Path, seconds: float) -> None:
        """Fill in a Time the player worked out and nothing else could.

        Chiptunes carry no length, and ffmpeg cannot read one out of them
        either, so the column is empty until something plays the file and
        measures it. That answer is written into the row and into the tag cache,
        so it is there next time without playing anything.
        """
        row = self.m.by_path.get(path)
        if row is None or seconds <= 0.0 or row.dur > 0:
            return
        row.dur = seconds
        try:
            library_cache.remember_duration(path, seconds)
        except Exception:
            pass
        self._schedule_refresh()

    # sorting, grouping, filtering

    def set_sort_keys(self, keys):
        keys = [(f, bool(d)) for f, d in keys if f in SORT_FIELDS]
        with self.keep_place():
            self.m.set_sort(keys)
        self._ensure_extra()
        self._hdr.viewport().update()
        self._changed(order=True)

    def set_preset(self, name: str):
        self.set_sort_keys(PRESETS[name][1])

    def clear_sort(self):
        if self.m.sort:
            self.set_sort_keys([])

    def reverse_order(self):
        """Flip the sort when one is showing, else reverse the arranged order."""
        if self.m.sort:
            self.set_sort_keys([(f, not d) for f, d in self.m.sort])
        else:
            self._arrange(self.m.reverse)

    def randomize(self):
        self._arrange(self.m.shuffle)

    def commit_order(self):
        """Keep the sorted order as the arranged one."""
        self._arrange(lambda: (self.m.commit(), self.m.finish()))

    def _on_sort_clicked(self, field: str, shift: bool):
        keys = list(self.m.sort)
        if field == "num":
            self.clear_sort()
            return
        idx = next((i for i, (f, _d) in enumerate(keys) if FIELD_COL.get(f) == FIELD_COL.get(field)), -1)
        if idx >= 0 and (shift or idx == 0):
            keys[idx] = (keys[idx][0], not keys[idx][1])
        elif shift and keys:
            keys.append((field, False))
        else:
            keys = [(field, False)]
        self.set_sort_keys(keys)

    def set_group_mode(self, mode: str):
        before = list(self.m.order)
        with self.keep_place():
            self.m.set_group(mode)
        self._ensure_extra()
        self._changed(order=self.m.order != before)

    def set_all_collapsed(self, on: bool):
        with self.keep_place():
            self.m.set_collapsed(None, on)

    def set_filter(self, text: str):
        if (text or "").strip() == self.m.query:
            return
        with self.keep_place():
            self.m.set_query(text)
        # a selection that the filter now hides must not be one Delete can reach
        self.select_rows([r for r in self._sel if r.pos >= 0])
        self.verticalScrollBar().setValue(0)
        self._changed()

    def sort_selection(self, name: str):
        rows = self.selected_rows()
        if len(rows) > 1:
            keys = PRESETS[name][1]
            self._arrange(lambda: self.m.sort_among(rows, keys))

    def move_selected(self, how: str):
        rows = self.selected_rows()
        if not rows:
            return
        m = self.m
        ops = {"move_up":     lambda: m.move_by(rows, -1),
               "move_down":   lambda: m.move_by(rows, 1),
               "move_top":    lambda: m.move_to_edge(rows, True),
               "move_bottom": lambda: m.move_to_edge(rows, False)}
        if self._arrange(ops[how]):
            self.scroll_to(rows[0] if how in ("move_up", "move_top") else rows[-1])

    def _on_reorder(self, rows, target, after: bool):
        if rows and self._arrange(lambda: self.m.move(rows, target, after)):
            self.select_rows(rows)

    def select_same(self, row: Row, what: str):
        """Select every track that shares the album, or the artist, of a row."""
        key = GROUPS["artist_album" if what == "album" else "artist"][1]
        want = key(row)
        self.select_rows([r for r in self.m.order if r.pos >= 0 and key(r) == want], row)

    def status_text(self) -> str:
        m = self.m
        n = len(self._sel)
        if n:
            dur = sum(r.dur for r in self._sel)
            return f"{n:,} selected" + (f", {fmt_total(dur)}" if dur else "")
        total = plural(len(m.rows), "track")
        if m.query:
            return f"{m.shown:,} of {total}"
        dur = m.total_dur()
        return f"{total}, {fmt_total(dur)}" if dur else total

    # dropped files

    def _on_files_dropped(self, paths: list[Path], target, after: bool):
        dirs = [p for p in paths if p.is_dir()]
        if not dirs:
            self.drop_ready.emit(paths, target, after)
            return
        files = [p for p in paths if not p.is_dir()]

        def _walk():
            found: list[Path] = []
            for d in dirs:
                try:
                    found += sorted((f for f in d.rglob("*")
                                     if f.is_file() and f.suffix.lower() in PLAYABLE_EXTS),
                                    key=lambda x: x.name.lower())
                except Exception:
                    pass
            self._walked.emit(files + found, target, after)

        threading.Thread(target=_walk, daemon=True).start()

    # tags, read off the GUI thread

    def _request(self, rows: list[Row], extra: bool = False):
        """Queue rows for the reader. Title, artist, album and length come from the tag
        cache; year, track, disc, genre and album artist from playlist_tags. A cold
        cache means one mutagen read per file, which would freeze the window when a large
        folder is added without the reader thread."""
        if extra:
            rows = [r for r in rows if not r.missing and r not in self._asked_extra]
            self._asked_extra.update(rows)
        if not rows:
            return
        with self._jobs_lock:
            self._jobs.append((extra, list(rows)))
            if self._reader is None:
                self._reader = threading.Thread(target=self._read_loop, daemon=True, name="playlist-tags")
                self._reader.start()

    def _read_loop(self):
        try:
            while True:
                with self._jobs_lock:
                    if not self._jobs:
                        self._reader = None
                        break
                    extra, rows = self._jobs.pop(0)
                for i in range(0, len(rows), 200):
                    chunk = rows[i:i + 200]
                    try:
                        self._chunk.emit("extra" if extra else "tags", self._read_chunk(extra, chunk))
                    except RuntimeError:
                        return               # the tab was closed while this ran
                    except Exception:
                        # logged, because otherwise an empty column looks like a file with
                        # no tags rather than a read that failed
                        _log.exception("tag read failed for %d file(s)", len(chunk))
            self._chunk.emit("done", None)
        except RuntimeError:
            self._reader = None

    @staticmethod
    def _read_chunk(extra: bool, rows: list[Row]):
        here, gone = [], []
        for r in rows:
            (here if os.path.exists(r.path) else gone).append(r)
        paths = [r.path for r in here]
        if extra:
            got = playlist_tags.get_many(paths)
            return [(r, got.get(r.path, playlist_tags.NONE)) for r in here]
        got = library_cache.get_many(paths)
        out = []
        for r in here:
            t = got.get(r.path) or {}
            out.append((r, (t.get("title") or "", t.get("artist") or "", t.get("album") or "",
                            float(t.get("duration") or 0.0), t.get("artists") or ())))
        out.extend((r, None) for r in gone)
        return out

    def _apply_chunk(self, kind: str, payload):
        if kind == "done":
            self.scanned.emit()
            return
        if kind == "tags":
            for row, vals in payload:
                if vals is None:
                    row.missing = True
                else:
                    row.missing = False
                    row.set_tags(*vals)
        else:
            for row, vals in payload:
                row.set_extra(*vals)
        self._schedule_refresh()

    def _schedule_refresh(self):
        # Tags land a chunk at a time, and rebuilding the sorted and grouped view for
        # each would cost more than the reading, so they are folded together.
        if not self._refresh.isActive():
            self._refresh.start(self._refresh_ms)

    def _refresh_now(self):
        t0 = time.perf_counter()
        m = self.m
        if m.sort or m.group != "none" or m.query:
            with self.keep_place():
                m.rebuild()
        else:
            self.viewport().update()
        self._refresh_ms = max(120, int((time.perf_counter() - t0) * 8000))
        self.status_changed.emit()

    def _ensure_extra(self):
        """Read year, track, disc, genre and album artist once something shows them."""
        m = self.m
        if self._extra_all or not m.rows:
            return
        if (m.group in ("album", "artist_album")
                or any(f in NEEDS_EXTRA for f, _d in m.sort)
                or any(self.column_visible(i) for i in (5, 6, 7))):
            self._extra_all = True
            self._request([r for r in m.rows if not r.extra], extra=True)

    # column layout

    def header_state(self) -> str:
        """Widths, order and the layout choice, as text a JSON file can hold.

        While a layout is still waiting to go on, that layout is the answer
        rather than whatever the header currently holds. Reading the header
        before the widget has a width gives back what Qt clamped the columns to
        in order to fit nothing, and writing that to the session would make
        them come back smaller every run.
        """
        if self.has_pending_columns() or self.viewport().width() <= 1:
            state = self._pending_columns
        else:
            state = self.columns_state()
        if not state:
            return ""
        return json.dumps({"v": 2, **state, "layout": self.layout_pref()}, separators=(",", ":"))

    def restore_header_state(self, blob: str) -> bool:
        """Hold a saved layout, and put it on once there is room for it."""
        if not blob:
            self.set_columns_state(None)
            return False
        if blob.lstrip().startswith("{"):
            try:
                d = json.loads(blob)
            except ValueError:
                return False
            self.set_layout_pref(d.get("layout", "auto"))
            self.set_columns_state({"qt": d["qt"]} if d.get("qt") else None)
            return not self.has_pending_columns()
        return self._restore_legacy(blob)

    def _restore_legacy(self, blob: str) -> bool:
        """The first layouts were a QHeaderView state for five columns: #, Title, Artist,
        Album, Time. Keep their widths and the column they were sorted by."""
        h = QHeaderView(Qt.Orientation.Horizontal)
        h.setModel(QStandardItemModel(0, 5, h))
        try:
            ok = bool(h.restoreState(QByteArray.fromBase64(blob.encode("ascii"))))
        except Exception:
            ok = False
        if not ok:
            return False
        for i in (0, 2, 3, 4):
            if h.sectionSize(i) >= 30:
                self.set_column_width(i, h.sectionSize(i))
        field = {1: "title", 2: "artist", 3: "album", 4: "dur"}.get(h.sortIndicatorSection())
        if field and not self.m.sort:
            self.set_sort_keys([(field, h.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder)])
        return True

    def view_state(self) -> dict:
        out = {}
        if self.m.sort:
            out["sort"] = [[f, d] for f, d in self.m.sort]
        if self.m.group != "none":
            out["group"] = self.m.group
        return out

    def restore_view(self, entry: dict):
        try:
            keys = [(str(f), bool(d)) for f, d in (entry.get("sort") or [])]
        except (TypeError, ValueError):
            keys = []
        group = entry.get("group")
        if group in GROUPS:
            self.set_group_mode(group)
        if keys:
            self.set_sort_keys(keys)

    # import and export

    def to_m3u(self) -> str:
        lines = ["#EXTM3U"]
        for p in self.manual_paths():
            lines.append(f"#EXTINF:-1,{p.stem}")
            lines.append(str(p))
        return "\n".join(lines)

    def load_m3u(self, text: str):
        # Missing files are kept (greyed) rather than dropped: dropping them and
        # re-saving would permanently shrink a playlist when a drive is unplugged.
        paths = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                p = Path(line)
                if p.suffix.lower() in PLAYABLE_EXTS:
                    paths.append(p)
        self.add_paths(paths)

    def to_json(self) -> str:
        return json.dumps([str(p) for p in self.manual_paths()], indent=2)

    def load_json(self, text: str):
        self.add_paths([Path(s) for s in json.loads(text)])


class PlaylistPanel(QWidget):
    play_track = Signal(int)
    notify     = Signal(str)   # user-facing one-liners (shown in the status bar)
    source_changed = Signal(str)   # name of the playlist playback now comes from
    send_to_device = Signal(list)  # paths to copy to a USB stick, iPod or other device
    convert_requested = Signal(list)  # paths to convert to another format

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player     = player
        self._active_tab = 0
        self._clipboard: list[Path] = []
        # The tab whose tracks are in the player's queue: what is playing comes from it, the
        # Up Next list is its upcoming tracks, and it is the only one that marks a playing row.
        self._source: _PlaylistTab | None = None
        # The saved column layout, read back before any tab exists to hold it.
        # _build runs _load_session, which fills this in when there is one.
        self._columns: str = ""
        self._scanning: set[_PlaylistTab] = set()   # tabs whose first pass over the files is still running
        self._build()

        player.track_changed.connect(self._on_track_changed)
        player.state_changed.connect(self._on_state_changed)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(4)
        new_btn  = QPushButton("+")
        new_btn.setFixedWidth(28)
        new_btn.setToolTip("New playlist")
        new_btn.clicked.connect(self._new_tab)

        self._sort_menu = QMenu(self)
        self._sort_menu.aboutToShow.connect(self._fill_sort_menu)
        self._group_menu = QMenu(self)
        self._group_menu.aboutToShow.connect(self._fill_group_menu)
        self._more_menu = QMenu(self)
        self._more_menu.aboutToShow.connect(self._fill_more_menu)

        self._sort_btn = QPushButton("Sort")
        self._sort_btn.setToolTip("Sort the playlist")
        self._sort_btn.setMenu(self._sort_menu)
        self._group_btn = QPushButton("Group")
        self._group_btn.setToolTip("Group the tracks under headers")
        self._group_btn.setMenu(self._group_menu)
        self._manual_btn = QPushButton("Manual")
        self._manual_btn.setToolTip("Back to the order you arranged")
        self._manual_btn.clicked.connect(self._to_manual)
        self._manual_btn.hide()
        more_btn = QPushButton("…")
        more_btn.setToolTip("More: load, save, columns, layout")
        more_btn.setMenu(self._more_menu)
        for b in (self._sort_btn, self._group_btn, self._manual_btn, more_btn):
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet("padding: 4px 9px;")

        top.addWidget(new_btn)
        top.addWidget(self._sort_btn)
        top.addWidget(self._group_btn)
        top.addWidget(self._manual_btn)
        top.addStretch()
        top.addWidget(more_btn)
        root.addLayout(top)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        esc = QShortcut(QKeySequence("Esc"), self._filter)
        esc.setContext(Qt.WidgetShortcut)
        esc.activated.connect(self._leave_filter)
        root.addWidget(self._filter)

        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._on_tab_change)
        # keep the typed filter across tab switches; re-apply it to the new tab
        self._tabs.currentChanged.connect(lambda _: self._apply_filter())
        self._tabs.tabBarDoubleClicked.connect(self._rename_tab)
        root.addWidget(self._tabs, 1)

        self._status = QLabel("")
        self._status.setObjectName("track_sub")
        self._status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._status.setContentsMargins(4, 0, 4, 0)
        root.addWidget(self._status)

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
            ("Ctrl+X", self._cut_selected),
            ("Ctrl+V", self._paste),
            ("Ctrl+J", self._locate_playing),
            ("Delete", self._remove_selected),
        ]:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

        if not self._load_session():
            self._new_tab("Queue")
        self._update_status()

    def _apply_filter(self):
        lst = self._current_list()
        if lst:
            lst.set_filter(self._filter.text())

    def _leave_filter(self):
        self._filter.clear()
        lst = self._current_list()
        if lst:
            lst.setFocus()

    def _current_list(self) -> _PlaylistTab | None:
        w = self._tabs.currentWidget()
        return w if isinstance(w, _PlaylistTab) else None

    def _lists(self):
        for i in range(self._tabs.count()):
            w = self._tabs.widget(i)
            if isinstance(w, _PlaylistTab):
                yield w

    def reread_artists(self):
        """Take the artists of every row from the tag cache again, after the artist settings changed."""
        for lst in self._lists():
            lst._request([r for r in lst.m.rows if r.tagged])

    def _make_tab(self) -> _PlaylistTab:
        lst = _PlaylistTab()
        lst.play_requested.connect(self._on_play_from_tab)
        lst.notify.connect(self.notify)
        lst.order_changed.connect(lambda l=lst: self._sync_queue(l))
        lst.removed.connect(lambda gone, l=lst: self._drop_from_player(gone) if l is self._source else None)
        lst.drop_ready.connect(lambda paths, t, a, l=lst: self._on_drop(l, paths, t, a))
        lst.menu_requested.connect(lambda pos, hit, l=lst: self._menu(l, pos, hit))
        lst.action_requested.connect(lambda a, l=lst: self._do_action(l, a))
        lst.status_changed.connect(lambda l=lst: self._update_status(l))
        lst.columns_changed.connect(lambda l=lst: self._on_columns_changed(l))
        lst.scanned.connect(lambda l=lst: self._on_scanned(l))
        # a new tab takes the column layout the others are already using
        cur = self._current_list()
        lst.restore_header_state((cur.header_state() if cur is not None else "")
                                 or self._columns)
        return lst

    def _new_tab(self, name: str = ""):
        if not name:
            name, ok = QInputDialog.getText(self, "New playlist", "Name:")
            if not ok or not name.strip():
                name = f"Playlist {self._tabs.count() + 1}"
        lst = self._make_tab()
        self._tabs.addTab(lst, name)
        self._tabs.setCurrentWidget(lst)

    def _confirm_discard(self, title: str, question: str) -> bool:
        """Ask before throwing tracks away. True means go ahead.

        No is the default button, so a stray Return, or a double click whose
        second press lands where the dialog just appeared, does not confirm the
        thing the dialog exists to prevent.
        """
        ans = QMessageBox.question(
            self, title, question,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return ans == QMessageBox.Yes

    def _close_tab(self, idx: int):
        """Close a playlist tab, asking first if it holds anything.

        Undo lives on the tab: the history is a list on the widget, so closing
        one takes its own undo away with it. That makes this the one action in
        the panel with nothing behind it, and the close button sits a few pixels
        from the tab you were aiming for.

        An empty playlist has nothing to lose, so it closes without a question.
        """
        if self._tabs.count() <= 1:
            return
        lst = self._tabs.widget(idx)
        n = len(lst.manual_paths()) if isinstance(lst, _PlaylistTab) else 0
        if n and not self._confirm_discard(
                "Close playlist",
                f"Close “{self._tabs.tabText(idx)}” and its "
                f"{n} track{'s' if n != 1 else ''}?\n\n"
                f"A closed playlist cannot be brought back."):
            return
        if lst is self._source:
            self._source = None
            self.source_changed.emit("")
        self._tabs.removeTab(idx)

    def _rename_tab(self, idx: int):
        if idx < 0:
            return
        cur = self._tabs.tabText(idx)
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=cur)
        if ok and name.strip():
            self._tabs.setTabText(idx, name.strip())
            if self._tabs.widget(idx) is self._source:
                self.source_changed.emit(name.strip())

    def _load_session(self) -> bool:
        if not _SESSION.exists():
            return False
        try:
            data = json.loads(_SESSION.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not data:
            return False
        # A dict, so the column layout travels with the tabs; an older bare list
        # of tabs still loads.
        if isinstance(data, dict):
            entries = data.get("tabs") or []
            columns = data.get("columns") or ""
        else:
            entries = data
            columns = ""
        if not entries:
            return False
        self._columns = columns if columns.lstrip().startswith("{") else ""
        for entry in entries:
            name  = entry.get("name", "Playlist")
            # keep unavailable tracks (greyed), never silently shrink a saved list
            paths = [Path(s) for s in entry.get("paths", [])]
            lst   = self._make_tab()
            lst.add_paths(paths)
            # after the rows, so a saved sort applies to a populated tab
            lst.restore_header_state(columns)
            lst.restore_view(entry)
            self._tabs.addTab(lst, name)
            if paths:
                self._scanning.add(lst)
        self._tabs.setCurrentIndex(0)
        first = self._tabs.widget(0)
        if isinstance(first, _PlaylistTab) and first.paths():
            self._player.add_many(first.paths())
            self._make_source(first)
        return True

    def _on_scanned(self, lst: _PlaylistTab):
        """The first pass over every tab's files is done, so say how many are missing.
        The files are looked at off the GUI thread, which is why this waits."""
        if lst not in self._scanning:
            return
        self._scanning.discard(lst)
        if self._scanning:
            return
        missing = sum(l.missing_count() for l in self._lists())
        total = sum(l.count() for l in self._lists())
        if missing:
            self.notify.emit(f"{missing} of {total} playlist track{'s' if total != 1 else ''} "
                             f"currently unavailable (kept, shown greyed)")

    def save_session(self):
        _SESSION.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for i in range(self._tabs.count()):
            lst = self._tabs.widget(i)
            if isinstance(lst, _PlaylistTab):
                data.append({
                    "name":  self._tabs.tabText(i),
                    "paths": [str(p) for p in lst.manual_paths()],
                    **lst.view_state(),
                })
        # One layout for all the tabs, since they all show the same columns.
        # Whichever is in front is the one whose widths were last touched.
        cur = self._current_list()
        if cur is None:
            cur = next(self._lists(), None)
        columns = (cur.header_state() if cur is not None else "") or self._columns
        safe_write.write_text(_SESSION, json.dumps({"tabs": data, "columns": columns}, indent=2))

    def _on_tab_change(self, idx: int):
        self._active_tab = idx
        lst = self._current_list()
        if (lst is not None and self._columns and not lst.has_pending_columns()
                and lst.header_state() != self._columns):
            lst.restore_header_state(self._columns)
        self._update_status()

    def _on_columns_changed(self, lst: _PlaylistTab):
        if lst is self._current_list():
            state = lst.header_state()
            if state:
                self._columns = state

    # keeping the player's queue in step

    def _queue_paths(self) -> list[Path]:
        return [t.path for t in self._player.queue]

    def _make_source(self, lst: _PlaylistTab):
        """Playback comes from this tab now (its tracks are, or are about to be, the queue)."""
        if lst is not self._source:
            self._source = lst
            self.source_changed.emit(self._tabs.tabText(self._tabs.indexOf(lst)))

    def source_name(self) -> str:
        return self._tabs.tabText(self._tabs.indexOf(self._source)) if self._source is not None else ""

    def adopt_current(self):
        """Make the playlist in front the one playback comes from."""
        lst = self._current_list()
        if lst is not None and lst is not self._source:
            self._player.clear()
            self._player.add_many(lst.paths())
            self._make_source(lst)

    def _feed_queue(self, lst: _PlaylistTab, added):
        """New tracks join the queue only when their playlist is the one playing, so adding to
        another playlist does not put its tracks in Up Next. An empty queue takes the playlist."""
        if not self._player.queue:
            self._make_source(lst)
            self._player.add_many(lst.paths())
        elif lst is self._source:
            self._player.add_many(added)

    def _on_play_from_tab(self, idx: int):
        lst = self._current_list()
        if not lst:
            return
        if self._queue_paths() != lst.paths():
            self._player.clear()
            self._player.add_many(lst.paths())
        self._make_source(lst)
        self._player.play_track(idx)

    def _sync_queue(self, lst: _PlaylistTab):
        """Put the player's queue in the order of the playlist it came from, without
        interrupting what is playing. Only done when the queue holds exactly this
        playlist; otherwise the next play from the list builds it again."""
        if lst is not self._source:
            return
        paths = lst.paths()
        have = self._queue_paths()
        if have == paths or len(have) != len(paths) or set(have) != set(paths):
            return
        reorder = getattr(self._player, "reorder", None)
        if reorder is not None:
            reorder(paths)

    def _drop_from_player(self, gone):
        """Take removed tracks out of the queue by path, so the order of the two never
        has to agree. Signals are held back for a long run and sent once, because
        every removal tells the Up Next list to rebuild."""
        gone = set(gone)
        q = self._player.queue
        idxs = [i for i, t in enumerate(q) if t.path in gone]
        if not idxs:
            return
        cur = self._player.index
        quiet = len(idxs) > 8
        if quiet:
            self._player.blockSignals(True)
        try:
            for i in reversed(idxs):
                if i == cur and quiet:
                    self._player.blockSignals(False)     # stopping the playing track must be heard
                    self._player.remove(i)
                    self._player.blockSignals(True)
                else:
                    self._player.remove(i)
        finally:
            if quiet:
                self._player.blockSignals(False)
                self._player.queue_changed.emit()

    def add_paths(self, paths: list[Path]):
        lst = self._current_list()
        if lst:
            added = lst.add_paths(paths)
            if added:
                self._feed_queue(lst, added)

    def _on_drop(self, lst: _PlaylistTab, paths, target, after: bool):
        added = lst.add_paths(paths, target, after)
        if added:
            self._feed_queue(lst, added)
            self.notify.emit(f"Added {plural(len(added), 'track')}")

    def _undo(self):
        lst = self._current_list()
        if lst:
            lst.undo()

    def _copy_selected(self):
        lst = self._current_list()
        if not lst:
            return
        sel = lst.selected_paths()
        if sel:
            self._clipboard = sel
            QApplication.clipboard().setText("\n".join(str(p) for p in sel))

    def _cut_selected(self):
        self._copy_selected()
        if self._clipboard:
            self._remove_selected()

    def _paste(self):
        lst = self._current_list()
        if not lst or not self._clipboard:
            return
        added = lst.add_paths(self._clipboard)
        if added:
            self._feed_queue(lst, added)

    def _remove_selected(self):
        lst = self._current_list()
        if lst:
            lst.remove_selected()

    def _locate_playing(self):
        lst = self._current_list()
        if lst:
            lst.locate_playing()

    def _clear(self):
        """Empty the current playlist, asking first if it holds anything.

        Ctrl+Z puts the rows back, since clear_all takes a snapshot, but the
        button is one click from Remove and empties the whole tab rather than
        the selection, so it says what it is about to do.
        """
        lst = self._current_list()
        if not lst:
            return
        n = len(lst.manual_paths())
        if n and not self._confirm_discard(
                "Clear playlist",
                f"Remove all {n} track{'s' if n != 1 else ''} from "
                f"“{self._tabs.tabText(self._tabs.currentIndex())}”?"):
            return
        lst.clear_all()
        if lst is self._source:
            self._player.clear()

    def restore_selection(self, path: Path):
        for ti in range(self._tabs.count()):
            lst = self._tabs.widget(ti)
            if not isinstance(lst, _PlaylistTab):
                continue
            if lst.has_path(path):
                self._make_source(lst)
                lst.set_playing_path(path)
                self._tabs.setCurrentIndex(ti)
                return

    # toolbar

    def _update_status(self, lst: _PlaylistTab | None = None):
        cur = self._current_list()
        if cur is None or (lst is not None and lst is not cur):
            return
        self._status.setText(cur.status_text())
        self._manual_btn.setVisible(not cur.manual_mode())
        if cur.manual_mode():
            self._sort_btn.setToolTip("Sort the playlist")
        else:
            by = describe_sort(cur.m.sort)
            self._sort_btn.setToolTip(f"Sorted by {by}")
            self._manual_btn.setToolTip(f"Back to the order you arranged. Sorted by {by} now.")

    def _to_manual(self):
        lst = self._current_list()
        if lst:
            lst.clear_sort()

    def _fill_sort_menu(self):
        m = self._sort_menu
        m.clear()
        lst = self._current_list()
        if lst is None:
            return
        grp = QActionGroup(m)
        a = m.addAction("Manual order", lst.clear_sort)
        a.setCheckable(True)
        a.setChecked(lst.manual_mode())
        grp.addAction(a)
        m.addSeparator()
        for name, (label, keys) in PRESETS.items():
            a = m.addAction(label, lambda _c=False, n=name: lst.set_preset(n))
            a.setCheckable(True)
            a.setChecked(lst.m.sort == keys)
            grp.addAction(a)
        m.addSeparator()
        m.addAction("Reverse", lst.reverse_order)
        m.addAction("Randomize", lst.randomize)
        if not lst.manual_mode():
            m.addSeparator()
            keep = m.addAction("Keep this order", lst.commit_order)
            keep.setToolTip("Make the sorted order the one you arranged, and the one that is saved")

    def _fill_group_menu(self):
        m = self._group_menu
        m.clear()
        lst = self._current_list()
        if lst is None:
            return
        grp = QActionGroup(m)
        for mode, (label, _fn) in GROUPS.items():
            a = m.addAction(label, lambda _c=False, md=mode: lst.set_group_mode(md))
            a.setCheckable(True)
            a.setChecked(lst.m.group == mode)
            grp.addAction(a)
        if lst.m.group != "none":
            m.addSeparator()
            m.addAction("Collapse all", lambda: lst.set_all_collapsed(True))
            m.addAction("Expand all", lambda: lst.set_all_collapsed(False))

    def _fill_more_menu(self):
        m = self._more_menu
        m.clear()
        lst = self._current_list()
        m.addAction("Load playlist…", self._load)
        m.addAction("Save playlist…", self._save)
        m.addSeparator()
        if lst is not None:
            cols = m.addMenu("Columns")
            self._fill_columns_menu(cols, lst)
            lay = m.addMenu("Row layout")
            grp = QActionGroup(lay)
            for pref, label in (("auto", "Automatic"), ("columns", "Columns"), ("lines", "Two lines")):
                a = lay.addAction(label, lambda _c=False, p=pref: self._set_layout(p))
                a.setCheckable(True)
                a.setChecked(lst.layout_pref() == pref)
                grp.addAction(a)
            m.addSeparator()
            m.addAction("Locate playing track\tCtrl+J", self._locate_playing)
            if lst.missing_count():
                m.addAction("Remove missing tracks", lambda: self._remove_missing(lst))
            m.addAction("Remove duplicates", lambda: self._remove_duplicates(lst))

    def _fill_columns_menu(self, menu: QMenu, lst: _PlaylistTab):
        for i, (_k, label, _f, _r, _w, _s) in enumerate(COLUMNS):
            a = menu.addAction({"File": "File name"}.get(label, label))
            a.setCheckable(True)
            a.setChecked(lst.column_visible(i))
            a.setEnabled(i != 1)
            a.triggered.connect(lambda on, ix=i: self._set_column(ix, on))

    def _set_column(self, i: int, on: bool):
        for lst in self._lists():
            lst.set_column_visible(i, on)

    def _set_layout(self, pref: str):
        for lst in self._lists():
            lst.set_layout_pref(pref)
        cur = self._current_list()
        if cur is not None:
            self._on_columns_changed(cur)

    def _on_state_changed(self, playing: bool):
        for lst in self._lists():
            lst.set_paused(not playing)

    def _on_track_changed(self, track):
        """Mark the playing row in the playlist playback comes from, and in no other tab that
        happens to hold the same track. It is found by path: the player's own index means a
        place in its queue, which is only the same as a place in a tab while the two have
        the same order."""
        paused = not self._player.is_playing
        for lst in self._lists():
            lst.set_playing_path(track.path if lst is self._source else None)
            lst.set_paused(paused)
        lst = self._current_list()
        if lst:
            lst.note_duration(track.path, self._player.duration_ms / 1000.0)

    # context menu

    def _menu(self, lst: _PlaylistTab, pos, hit):
        self._build_menu(lst, hit).exec(pos)

    def _build_menu(self, lst: _PlaylistTab, hit) -> QMenu:
        if hit == "header":
            m = QMenu(self)
            self._fill_columns_menu(m, lst)
            return m
        sel = lst.selected_rows()
        row = hit if isinstance(hit, Row) else (hit.first if hit is not None else None)
        path = row.path if row is not None else None
        idx = lst.m.order.index(row) if row is not None else -1
        many = len(sel) > 1

        m = QMenu(self)

        if path:
            m.addAction("\U000f040a  Play now",       lambda: self._play_ctx(idx, lst))
            m.addSeparator()
            m.addAction("\U000f018f  Copy  Ctrl+C",   lambda: self._copy_selected())
            m.addAction("\U000f0190  Cut  Ctrl+X",    lambda: self._cut_selected())
        paste_act = m.addAction("\U000f0192  Paste  Ctrl+V", lambda: self._paste())
        paste_act.setEnabled(bool(self._clipboard))
        if path:
            m.addSeparator()
            up = m.addAction("\U000f0795  Move to top",    lambda: lst.move_selected("move_top"))
            dn = m.addAction("\U000f0792  Move to bottom", lambda: lst.move_selected("move_bottom"))
            if many:
                sortsel = m.addMenu("\U000f05bd  Sort selection")
                for name, (label, _keys) in PRESETS.items():
                    sortsel.addAction(label, lambda _c=False, n=name: lst.sort_selection(n))
            if isinstance(hit, Row):
                if hit.album:
                    m.addAction("\U000f0486  Select all in album", lambda: lst.select_same(hit, "album"))
                if hit.artist:
                    m.addAction("\U000f0486  Select all by artist", lambda: lst.select_same(hit, "artist"))
            if hit is not None and not isinstance(hit, Row):
                m.addAction("\U000f0792  " + ("Expand group" if hit.collapsed else "Collapse group"),
                            lambda: lst.toggle_group(hit))
            m.addSeparator()
            m.addAction("\U000f0770  Locate file",    lambda: _open_folder(path))
            m.addAction("\U000f0552  Send to device", lambda: self.send_to_device.emit(lst.selected_paths() or [path]))
            m.addAction("\U000f0fb4  Convert…", lambda: self.convert_requested.emit(lst.selected_paths() or [path]))
            m.addAction("\U000f0339  Copy path",      lambda: QApplication.clipboard().setText(
                "\n".join(str(p) for p in lst.selected_paths()) if many else str(path)))
            if not many and isinstance(hit, Row):
                m.addSeparator()
                m.addAction("\U000f1224  Edit tags…",     lambda: self._edit_tags(path))
                m.addAction("\U000f0455  Rename file…",   lambda: self._rename_on_disk(path, lst, idx))
                m.addAction("\U000f01b4  Delete file…",   lambda: self._delete_on_disk(path, lst, idx))
            m.addSeparator()
            m.addAction("\U000f0410  Remove from playlist", lambda: self._remove_selected())

        m.addSeparator()
        if lst.missing_count():
            m.addAction("\U000f0413  Remove missing tracks", lambda: self._remove_missing(lst))
        m.addAction("\U000f0191  Remove duplicates", lambda: self._remove_duplicates(lst))
        m.addAction("\U000f05e8  Clear playlist", lambda: self._clear())
        return m

    def _do_action(self, lst: _PlaylistTab, action: str):
        lst.move_selected(action)

    def _remove_duplicates(self, lst: _PlaylistTab):
        n = lst.remove_duplicates()
        self.notify.emit(f"Removed {plural(n, 'duplicate')}" if n else "No duplicates found")

    def _remove_missing(self, lst: _PlaylistTab):
        """Explicit prune of unavailable tracks: the only way they leave a list."""
        gone = lst.remove_missing()
        if gone:
            self.notify.emit(f"Removed {plural(len(gone), 'missing track')}")

    def _play_ctx(self, idx: int, lst: _PlaylistTab):
        self._player.clear()
        self._player.add_many(lst.paths())
        self._make_source(lst)
        self._player.play_track(idx)

    def _edit_tags(self, path: Path):
        _TagEditDialog(path, self).exec()

    def _rename_on_disk(self, path: Path, lst: _PlaylistTab, idx: int):
        new_name, ok = QInputDialog.getText(
            self, "Rename file", "New filename (without extension):",
            text=path.stem,
        )
        if not ok or not new_name.strip():
            return
        new_path = path.with_name(new_name.strip() + path.suffix)
        try:
            path.rename(new_path)
        except Exception as e:
            QMessageBox.warning(self, "Rename failed", str(e))
            return
        lst.rename_at(idx, new_path)
        # the player's queue follows too, which keeps its dedupe set in sync
        for i, t in enumerate(self._player.queue):
            if t.path == path:
                self._player.rename_track(i, new_path)
                break

    def _delete_on_disk(self, path: Path, lst: _PlaylistTab, idx: int):
        ans = QMessageBox.question(
            self, "Delete file",
            f"Permanently delete\n{path.name}\nfrom disk?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        # if it's the playing track, move off it first, otherwise the engine
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
            QMessageBox.warning(self, "Delete failed", str(e))
            return
        lst.remove_at(idx)

    def _save(self):
        global _last_dir
        lst = self._current_list()
        if not lst or not lst.paths():
            return
        name = self._tabs.tabText(self._tabs.currentIndex())
        path, _ = QFileDialog.getSaveFileName(
            self, "Save playlist", str(Path(_last_dir) / f"{name}.m3u"),
            "M3U playlist (*.m3u);;JSON playlist (*.json)"
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
            self, "Load playlist", _last_dir,
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
