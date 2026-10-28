from __future__ import annotations
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QScrollArea, QFrame,
    QLineEdit, QPushButton, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

from artists import join_artists, split_artists
from player import Player

# Editable tag fields: display label and mutagen "easy" key.
_EDITABLE = [
    ("Title", "title"), ("Artist", "artist"), ("Album artist", "albumartist"),
    ("Album", "album"), ("Year", "date"), ("Genre", "genre"),
    ("Track", "tracknumber"), ("Disc", "discnumber"), ("Composer", "composer"),
    ("BPM", "bpm"), ("Comment", "comment"),
]
_READONLY = ["Game", "System", "Copyright", "Dumper", "Tracks", "Sub-songs", "Chips / channels",
             "Duration", "Bitrate", "Sample rate", "Channels", "Format", "File size", "Lyrics tag"]


def _fmt_duration(secs: float) -> str:
    s = int(secs)
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _id3_version(fe, path: Path) -> tuple | None:
    """The version of the ID3 tag of an MP3 file, None for any other kind of file."""
    from mutagen.mp3 import EasyMP3
    if not isinstance(fe, EasyMP3):
        return None
    from mutagen.id3 import ID3
    try:
        return ID3(str(path)).version
    except Exception:
        return None


def _read_metadata(path: Path) -> dict:
    result: dict[str, str] = {}
    if path.suffix.lower() == ".fur":
        # mutagen can't read Furnace modules, so use the libfurnace metadata reader.
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
                result["Chips / channels"] = ", ".join(names)
            result["Format"] = "Furnace module (.fur)"
            result["File size"] = f"{path.stat().st_size // 1024:,} KB"
        except Exception:
            pass
        return result
    if path.suffix.lower() in {".vgm", ".vgz", ".nsf", ".spc", ".gbs", ".ay", ".gym", ".hes", ".kss", ".sap"}:
        # chiptune formats: mutagen can't read them, so use libgme.
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
            result["File size"] = f"{path.stat().st_size // 1024:,} KB"
        except Exception:
            pass
        return result
    try:
        from mutagen import File as MFile

        f = MFile(str(path))
        if f is None:
            return result

        info = f.info
        if getattr(info, "length",      None): result["Duration"]    = _fmt_duration(info.length)
        if getattr(info, "bitrate",     None): result["Bitrate"]     = f"{info.bitrate // 1000} kbps"
        if getattr(info, "sample_rate", None): result["Sample rate"] = f"{info.sample_rate} Hz"
        if getattr(info, "channels",    None): result["Channels"]    = str(info.channels)

        result["Format"]    = type(f).__name__.replace("Easy", "")
        result["File size"] = f"{path.stat().st_size // 1024:,} KB"

        fe = MFile(str(path), easy=True)
        if fe:
            for key, label in [
                ("title",        "Title"),
                ("artist",       "Artist"),
                ("albumartist",  "Album artist"),
                ("album",        "Album"),
                ("date",         "Year"),
                ("genre",        "Genre"),
                ("tracknumber",  "Track"),
                ("discnumber",   "Disc"),
                ("composer",     "Composer"),
                ("comment",      "Comment"),
                ("lyrics",       "Lyrics tag"),
                ("bpm",          "BPM"),
            ]:
                val = fe.get(key)
                if key == "artist":       # every artist, however the tag keeps them
                    names = split_artists([str(v) for v in val or ()], str((fe.get("albumartist") or [""])[0]))
                    val = ["; ".join(names)] if names else None
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
        self._artist0 = ""             # the artist line as it was loaded, to see whether it was edited
        self._edits: dict[str, QLineEdit] = {}
        self._build()
        self._data_ready.connect(self._apply)
        player.track_changed.connect(lambda t: self._load(t.path))

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

        # Save and revert row. It sits outside the form, which is rebuilt on every load.
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

        self._artist0 = data.get("Artist", "")
        # editable tag fields (shown even when empty so missing tags can be added)
        for label, key in _EDITABLE:
            le = QLineEdit(data.get(label, ""))
            self._form.addRow(label + ":", le)
            self._edits[key] = le

        for label in _READONLY:
            if label in data:
                lbl = QLabel(data[label])
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                self._form.addRow(label + ":", lbl)

    def _artist_names(self) -> list[str]:
        return [n.strip() for n in self._edits["artist"].text().split(";") if n.strip()]

    @staticmethod
    def _write_artists(fe, names: list[str], several: bool):
        """Several artists go in as separate values where the tag can hold them (several is False for ID3v2.3,
        which has no such thing), otherwise as one line with ";" between the names."""
        if not names:
            if "artist" in fe:
                del fe["artist"]
        elif several and len(names) > 1:
            fe["artist"] = names
        else:
            fe["artist"] = "; ".join(names)

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
            ver = _id3_version(fe, self._path)
            v23 = ver is not None and ver < (2, 4, 0)
            for key, le in self._edits.items():
                v = le.text().strip()
                if key == "artist":
                    if v != self._artist0:       # an untouched line leaves the file's own values alone
                        self._write_artists(fe, self._artist_names(), not v23)
                elif v:
                    fe[key] = v
                elif key in fe:
                    del fe[key]
            if v23:
                fe.save(v2_version=3)       # the default would turn the file's ID3v2.3 tag into v2.4
            else:
                fe.save()
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return

        # the library cache and the live queue entries must not keep the old tags
        try:
            import library_cache
            library_cache.forget(self._path)
        except Exception:
            pass
        for t in self._player.queue:
            if t.path == self._path:
                t.title  = self._edits["title"].text().strip() or self._path.stem
                t.artists = self._artist_names()
                t.artist = join_artists(t.artists)
                t.album  = self._edits["album"].text().strip()
        self.tags_saved.emit(self._path)
        self._load(self._path)
