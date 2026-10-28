"""Album art thumbnails for the library panel.

Covers are read and decoded on one worker thread through player.extract_art and
player.decode_art, scaled to a square there, and kept as pixmaps in a small LRU.
The GUI thread only ever looks a key up (cheap enough for paint) and, when a batch
lands, repaints.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath, QPixmap

from player import decode_art, extract_art

_BUDGET  = 24 << 20     # bytes of decoded thumbnails to keep
_ENTRIES = 4000         # and entries, since "no art" costs no bytes
_BACKLOG = 48           # requests kept waiting, oldest dropped first


def _year(path: Path) -> str:
    try:
        from mutagen import File
        f = File(str(path), easy=True)
        d = str(f["date"][0])[:4] if f is not None and f.get("date") else ""
        return d if d.isdigit() else ""
    except Exception:
        return ""


class CoverCache(QObject):
    """Square, rounded thumbnails by key, filled in off the GUI thread.

    get() returns None for a key nobody has asked for, else (pixmap or None for
    "no art", year). request() queues a miss: newest first, so after a fast
    scroll what is on screen loads before what was scrolled past.
    """
    ready   = Signal()          # a batch landed, repaint
    _loaded = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        scr = QGuiApplication.primaryScreen()
        self.size = int(160 * max(1.0, scr.devicePixelRatio() if scr else 1.0))
        self._lru: OrderedDict = OrderedDict()
        self._bytes = 0
        self._cv = threading.Condition()
        self._stack: list = []
        self._pending: set = set()
        self._thread: threading.Thread | None = None
        self._loaded.connect(self._on_loaded)
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(40)
        self._flush.timeout.connect(self.ready)

    def get(self, key):
        e = self._lru.get(key)
        if e is not None:
            self._lru.move_to_end(key)
        return e

    def request(self, key, path: Path):
        with self._cv:
            if key in self._pending:
                return
            self._pending.add(key)
            self._stack.append((key, path))
            for k, _ in self._stack[:-_BACKLOG]:
                self._pending.discard(k)
            del self._stack[:-_BACKLOG]
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                while not self._stack:
                    self._cv.wait()
                key, path = self._stack.pop()
            try:
                self._loaded.emit((key,) + self._load(path))
            except RuntimeError:
                return              # the cache was deleted under us
            time.sleep(0.002)       # a breather for the GUI and audio threads between covers

    def _load(self, path: Path):
        img = None
        try:
            data = extract_art(path)
            img = decode_art(data) if data else None
            if img is not None:
                s = self.size
                img = img.scaled(s, s, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                img = img.copy((img.width() - s) // 2, (img.height() - s) // 2, s, s)
                img = self._rounded(img)
        except Exception:
            img = None
        return img, _year(path)

    def _rounded(self, img: QImage) -> QImage:
        s = img.width()
        out = QImage(s, s, QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, s, s, s * 0.05, s * 0.05)
        p.setClipPath(path)
        p.drawImage(0, 0, img)
        p.end()
        return out

    def _on_loaded(self, item):
        key, img, year = item
        with self._cv:
            self._pending.discard(key)
        pm = QPixmap.fromImage(img) if img is not None else None
        old = self._lru.pop(key, None)
        if old is not None and old[0] is not None:
            self._bytes -= old[0].width() * old[0].height() * 4
        self._lru[key] = (pm, year)
        if pm is not None:
            self._bytes += pm.width() * pm.height() * 4
        while (self._bytes > _BUDGET or len(self._lru) > _ENTRIES) and len(self._lru) > 1:
            _, (gone, _y) = self._lru.popitem(last=False)
            if gone is not None:
                self._bytes -= gone.width() * gone.height() * 4
        self._flush.start()
