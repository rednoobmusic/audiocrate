from __future__ import annotations
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QScrollArea, QFrame,
    QLineEdit, QPushButton, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

from player import Player

# Editable tag fields (display label → mutagen "easy" key).
_EDITABLE = [
    ("Title", "title"), ("Artist", "artist"), ("Album Artist", "albumartist"),
    ("Album", "album"), ("Year", "date"), ("Genre", "genre"),
    ("Track", "tracknumber"), ("Disc", "discnumber"), ("Composer", "composer"),
    ("BPM", "bpm"), ("Comment", "comment"),
]
_READONLY = ["Duration", "Bitrate", "Sample Rate", "Channels", "Format", "File Size"]


def _fmt_duration(secs: float) -> str:
    s = int(secs)
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _read_metadata(path: Path) -> dict:
    result: dict[str, str] = {}
    if path.suffix.lower() == ".fur":
        # mutagen can't read Furnace modules — use the libfurnace metadata reader.
        try:
            from fur_player import fur_tags
            t = fur_tags(path)
            for key, label in [("title", "Title"), ("artist", "Artist"), ("system", "System")]:
                if t.get(key):
                    result[label] = t[key]
            if t.get("channel_count"):
                result["Channels"] = str(t["channel_count"])
            if t.get("subsong_count", 0) > 1:
                result["Sub-songs"] = str(t["subsong_count"])
            if t.get("duration_us"):
                result["Duration"] = _fmt_duration(t["duration_us"] / 1_000_000)
            if names := t.get("channel_names"):
                result["Chips / Channels"] = ", ".join(names)
            result["Format"] = "Furnace module (.fur)"
            result["File Size"] = f"{path.stat().st_size // 1024:,} KB"
        except Exception:
            pass
        return result
    if path.suffix.lower() in {".vgm", ".vgz", ".nsf", ".spc", ".gbs", ".ay", ".gym", ".hes", ".kss", ".sap"}:
        # chiptune formats — mutagen can't read them; use libgme.
        try:
            from vgm_player import gme_tags
            t = gme_tags(path)
            for key, label in [("song", "Title"), ("author", "Artist"), ("game", "Game"),
                               ("system", "System"), ("copyright", "Copyright"),
                               ("comment", "Comment"), ("dumper", "Dumper")]:
                if t.get(key):
                    result[label] = t[key]
            if t.get("track_count", 1) > 1:
                result["Tracks"] = str(t["track_count"])
            if t.get("length_ms", 0) > 0:
                result["Duration"] = _fmt_duration(t["length_ms"] / 1000)
            result["Format"] = path.suffix[1:].upper() + " (chiptune)"
            result["File Size"] = f"{path.stat().st_size // 1024:,} KB"
        except Exception:
            pass
        return result
    try:
        from mutagen import File as MFile

        # info
        f = MFile(str(path))
        if f is None:
            return result

        info = f.info
        if getattr(info, "length",      None): result["Duration"]    = _fmt_duration(info.length)
        if getattr(info, "bitrate",     None): result["Bitrate"]     = f"{info.bitrate // 1000} kbps"
        if getattr(info, "sample_rate", None): result["Sample Rate"] = f"{info.sample_rate} Hz"
        if getattr(info, "channels",    None): result["Channels"]    = str(info.channels)

        result["Format"]    = type(f).__name__.replace("Easy", "")
        result["File Size"] = f"{path.stat().st_size // 1024:,} KB"

        # tags
        fe = MFile(str(path), easy=True)
        if fe:
            for key, label in [
                ("title",        "Title"),
                ("artist",       "Artist"),
                ("albumartist",  "Album Artist"),
                ("album",        "Album"),
                ("date",         "Year"),
                ("genre",        "Genre"),
                ("tracknumber",  "Track"),
                ("discnumber",   "Disc"),
                ("composer",     "Composer"),
                ("comment",      "Comment"),
                ("lyrics",       "Lyrics Tag"),
                ("bpm",          "BPM"),
            ]:
                val = fe.get(key)
                if val:
                    result[label] = str(val[0])
    except Exception:
        pass
    return result


class MetadataWidget(QWidget):
    _data_ready = Signal(dict)
    tags_saved  = Signal(object)   # Path whose tags were just written

    def __init__(self, player: Player, parent=None):
        super().__init__(parent)
        self._player = player
        self._path: Path | None = None
        self._edits: dict[str, QLineEdit] = {}
        self._build()
        self._data_ready.connect(self._apply)
        player.track_changed.connect(lambda t: self._load(t.path))

    # layout

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        self._form = QFormLayout(inner)
        self._form.setContentsMargins(8, 8, 8, 8)
        self._form.setSpacing(3)
        self._form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._scroll.setWidget(inner)

        self._placeholder = QLabel("No track loaded")
        self._placeholder.setAlignment(Qt.AlignCenter)

        # Save / Revert row (persistent — lives outside the form that gets rebuilt).
        self._btn_row = QWidget()
        brow = QHBoxLayout(self._btn_row)
        brow.setContentsMargins(8, 4, 8, 8)
        self._save_btn   = QPushButton("Save tags")
        self._revert_btn = QPushButton("Revert")
        self._save_btn.clicked.connect(self._save_tags)
        self._revert_btn.clicked.connect(lambda: self._load(self._path) if self._path else None)
        brow.addWidget(self._save_btn, 1)
        brow.addWidget(self._revert_btn)

        outer.addWidget(self._placeholder)
        outer.addWidget(self._scroll, 1)
        outer.addWidget(self._btn_row)
        self._scroll.hide()
        self._btn_row.hide()

    # data

    def _load(self, path: Path):
        self._path = path
        threading.Thread(target=self._bg, args=(path,), daemon=True).start()

    def _bg(self, path: Path):
        self._data_ready.emit(_read_metadata(path))

    def _apply(self, data: dict):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._edits.clear()

        if not data and self._path is None:
            self._scroll.hide()
            self._btn_row.hide()
            self._placeholder.show()
            return

        self._placeholder.hide()
        self._scroll.show()
        self._btn_row.show()

        # editable tag fields (shown even when empty so missing tags can be added)
        for label, key in _EDITABLE:
            le = QLineEdit(data.get(label, ""))
            le.setPlaceholderText("—")
            self._form.addRow(label + ":", le)
            self._edits[key] = le

        # read-only technical info
        for label in _READONLY:
            if label in data:
                lbl = QLabel(data[label])
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                self._form.addRow(label + ":", lbl)

    def _save_tags(self):
        if not self._path:
            return
        try:
            from mutagen import File as MFile
            fe = MFile(str(self._path), easy=True)
            if fe is None:
                QMessageBox.warning(self, "Can't edit tags",
                                    "This file type doesn't support editable tags.")
                return
            for key, le in self._edits.items():
                v = le.text().strip()
                if v:
                    fe[key] = v
                elif key in fe:
                    del fe[key]
            fe.save()
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return

        # Invalidate the tag cache and update the live queue entry, then reload.
        try:
            import library_cache
            library_cache.forget(self._path)
        except Exception:
            pass
        for t in self._player.queue:
            if t.path == self._path:
                t.title  = self._edits["title"].text().strip() or self._path.stem
                t.artist = self._edits["artist"].text().strip()
                t.album  = self._edits["album"].text().strip()
        self.tags_saved.emit(self._path)
        self._load(self._path)
