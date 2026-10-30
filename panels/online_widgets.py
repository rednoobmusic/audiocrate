"""Painted pieces of the Online panel: result rows, feed chips, thumbnails, banners and the downloads strip.

They follow the library panel. Everything draws itself from a Scheme (the live theme's colours)
and from QPainter paths, so the dark, art-palette, Aero and system looks all work without an
icon font. None of the buttons are checkable QPushButtons, which main_window restyles on a
theme change.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import math
import os
import socket
import textwrap
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import (QAbstractListModel, QBuffer, QByteArray, QEvent, QMimeData, QModelIndex, QObject, QPointF, QRect,
                            QRectF, QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDrag, QFont, QFontMetrics, QGuiApplication, QImage, QImageReader, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QListView, QMenu, QSizePolicy, QStyle, QStyledItemDelegate, QToolTip,
                               QVBoxLayout, QWidget)

import plat_fs
from library_views import PAD, _Base, _fill_row, _Fonts, fmt_duration
from library_widgets import PillButton, Scheme, _Button, draw_glyph, mix, scaled_font
from online_channel import can_open_channel
from online_service import memory_cost_mb

CONTAINERS = ("playlist", "channel")        # kinds that open as a list instead of playing
ITEM_ROLE = Qt.UserRole + 1
THUMB = QSize(96, 54)
ROW_MIN = 58
BTN = 26                                    # hover buttons on a row
STRIP_ROW = 36                              # the least a strip row is high
LONG_S = 90 * 60                            # seconds from which an item is called long
UA = "AudioCrate"


def item_key(item: dict) -> tuple:
    return item.get("provider", ""), item.get("id", "")


def row_height(font: QFont) -> int:
    """A result row: room for a title on two lines and the dim line under it, never less than the thumbnail."""
    return max(ROW_MIN, 2 * QFontMetrics(font).height() + QFontMetrics(scaled_font(font, 0.86)).height() + 10)


# text helpers

def fmt_views(n) -> str:
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        return ""
    n = int(n)
    if n < 1000:
        return f"{n} view" + ("" if n == 1 else "s")
    v, unit = n / 1e3, "K"
    if n >= 999_500:
        v, unit = n / 1e6, "M"
    if n >= 999_500_000:
        v, unit = n / 1e9, "B"
    s = f"{v:.1f}" if v < 10 else f"{v:.0f}"
    return f"{s.removesuffix('.0')}{unit} views"


def fmt_age(ymd, today: date | None = None) -> str:
    if not (isinstance(ymd, str) and len(ymd) == 8 and ymd.isdigit()):
        return ""
    try:
        born = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
    except ValueError:
        return ""
    days = ((today or date.today()) - born).days
    if days < 0:
        return ""
    if days < 1:
        return "today"
    for span, unit in ((365, "year"), (30, "month"), (7, "week"), (1, "day")):
        if days >= span:
            n = days // span
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return ""


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def item_line_parts(it: dict) -> tuple[str, str, str]:
    """The dim second line of a row in three pieces: what comes before the uploader, the uploader, and the rest."""
    kind = it.get("kind")
    bits = []
    if kind == "channel":
        bits.append("Channel")
    elif kind == "playlist":
        bits.append("Playlist")
    elif kind == "space":
        bits.append("Space recording")
    if it.get("is_live"):
        bits.append("Live")
    rest = (fmt_views(it.get("view_count")), fmt_age(it.get("upload_date")))
    return ", ".join(bits), it.get("uploader") or "", ", ".join(b for b in rest if b)


def item_line(it: dict) -> str:
    """The dim second line of a row: who, how many views, how long ago. Commas, no bullets."""
    return ", ".join(b for b in item_line_parts(it) if b)


def item_badge(it: dict) -> str:
    if it.get("is_live"):
        return "LIVE"
    if it.get("kind") in CONTAINERS and it.get("count"):
        return plural(int(it["count"]), "video")
    return fmt_duration(it["duration"]) if it.get("duration") else ""


def row_tip(it: dict, mark: dict) -> str:
    """Rich text for the tooltip of a row. Everything from the site is escaped."""
    parts = [f"<b>{html.escape(it.get('title') or 'Untitled')}</b>"]
    line = item_line(it)
    dur = fmt_duration(it["duration"]) if it.get("duration") else ""
    if line or dur:
        parts.append(html.escape(", ".join(b for b in (line, dur) if b)))
    if mark.get("quality"):
        parts.append(html.escape(mark["quality"]))
    if (it.get("duration") or 0) > LONG_S:
        parts.append(f"Long: playing it keeps about {memory_cost_mb(it['duration']):,} MB in memory.")
    desc = " ".join((it.get("description") or "").split())
    if desc:
        parts.append("<i>" + "<br>".join(html.escape(x) for x in textwrap.wrap(desc[:200], 56)[:4]) + "</i>")
    if mark.get("error"):
        parts.append(html.escape(mark["error"]))
    return "<br>".join(parts)


def wrap_lines(fm: QFontMetrics, text: str, width: int, lines: int = 2) -> list[str]:
    """text cut into at most `lines` lines no wider than width, broken at spaces, the last one elided."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    if lines <= 1 or width < 24:
        return [fm.elidedText(text, Qt.ElideRight, max(width, 1))]
    out: list[str] = []
    while text and len(out) < lines - 1:
        if fm.horizontalAdvance(text) <= width:
            break
        lo, hi = 1, len(text)
        while lo < hi:                                  # the longest prefix that fits
            mid = (lo + hi + 1) // 2
            if fm.horizontalAdvance(text[:mid]) <= width:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        sp = text.rfind(" ", 0, cut + 1)
        if sp > 0:
            cut = sp
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(fm.elidedText(text, Qt.ElideRight, width))
    return out


def warn_color(sc: Scheme) -> QColor:
    """Error text: a red that keeps its contrast on dark and light backgrounds alike."""
    return mix(sc.fg, QColor("#e5534b"), 0.72)


class Background(QObject):
    """Runs a function on a worker thread and hands the result back on the GUI thread."""
    _done = Signal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done.connect(self._finish)

    def run(self, fn, callback=None, *args):
        threading.Thread(target=self._work, args=(fn, callback, args), daemon=True).start()

    def _work(self, fn, callback, args):
        try:
            res, err = fn(*args), None
        except Exception as e:
            res, err = None, e
        try:
            self._done.emit(callback, res, err)
        except RuntimeError:
            pass                                        # the panel closed while this ran

    @staticmethod
    def _finish(callback, res, err):
        if callback is not None:
            callback(res, err)


# icons

_OWN = {"play", "add", "save", "gear", "check", "link", "video", "warn", "lock", "user", "open", "copy",
        "eq", "back", "paste", "stop"}


def draw_icon(p: QPainter, name: str, r: QRectF, color: QColor, fill: QColor | None = None):
    """The line icons of the library panel plus the few this panel needs, on the same 16 unit grid."""
    if name not in _OWN:
        draw_glyph(p, name, r, color, fill)
        return
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(color, max(1.2, r.width() / 11), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(fill if fill is not None else Qt.NoBrush)
    u = min(r.width(), r.height()) / 16.0
    x0, y0 = r.x() + (r.width() - 16 * u) / 2, r.y() + (r.height() - 16 * u) / 2
    P = lambda a, b: QPointF(x0 + a * u, y0 + b * u)
    R = lambda a, b, w, h: QRectF(x0 + a * u, y0 + b * u, w * u, h * u)
    path = QPainterPath()
    if name == "play":
        path.moveTo(P(4.8, 2.8)); path.lineTo(P(13, 8)); path.lineTo(P(4.8, 13.2)); path.closeSubpath()
        p.setBrush(color)
        p.drawPath(path)
    elif name == "stop":
        p.setBrush(color)
        p.drawRoundedRect(R(4, 4, 8, 8), 1.2 * u, 1.2 * u)
    elif name == "add":
        p.drawLine(P(8, 3), P(8, 13)); p.drawLine(P(3, 8), P(13, 8))
    elif name == "save":
        p.drawLine(P(8, 2.2), P(8, 9.6))
        path.moveTo(P(4.8, 6.6)); path.lineTo(P(8, 9.8)); path.lineTo(P(11.2, 6.6))
        p.drawPath(path)
        tray = QPainterPath()
        tray.moveTo(P(2.6, 10.6)); tray.lineTo(P(2.6, 13.6)); tray.lineTo(P(13.4, 13.6)); tray.lineTo(P(13.4, 10.6))
        p.drawPath(tray)
    elif name == "gear":
        p.drawEllipse(R(3.2, 3.2, 9.6, 9.6))
        p.drawEllipse(R(6.2, 6.2, 3.6, 3.6))
        for k in range(8):
            a = k * math.pi / 4
            p.drawLine(P(8 + 4.8 * math.cos(a), 8 + 4.8 * math.sin(a)), P(8 + 6.8 * math.cos(a), 8 + 6.8 * math.sin(a)))
    elif name == "check":
        path.moveTo(P(3, 8.6)); path.lineTo(P(6.5, 12)); path.lineTo(P(13, 4.6))
        p.drawPath(path)
    elif name == "link":
        p.drawRoundedRect(R(1.4, 5.8, 8.2, 4.4), 2.2 * u, 2.2 * u)
        p.drawRoundedRect(R(6.4, 5.8, 8.2, 4.4), 2.2 * u, 2.2 * u)
    elif name == "video":
        p.drawRoundedRect(R(1.4, 4, 9.2, 8), 1.6 * u, 1.6 * u)
        path.moveTo(P(10.8, 7.2)); path.lineTo(P(14.6, 4.8)); path.lineTo(P(14.6, 11.2)); path.closeSubpath()
        p.drawPath(path)
    elif name == "warn":
        path.moveTo(P(8, 2.2)); path.lineTo(P(14.6, 13.6)); path.lineTo(P(1.4, 13.6)); path.closeSubpath()
        p.drawPath(path)
        p.drawLine(P(8, 6.4), P(8, 9.6)); p.drawLine(P(8, 11.6), P(8, 11.7))
    elif name == "lock":
        p.drawRoundedRect(R(3.2, 7.2, 9.6, 6.6), 1.5 * u, 1.5 * u)
        p.drawArc(R(5, 2.4, 6, 6.4), 0, 180 * 16)
        p.drawLine(P(5, 5.6), P(5, 7.2)); p.drawLine(P(11, 5.6), P(11, 7.2))
    elif name == "user":
        p.drawEllipse(R(5.3, 2.4, 5.4, 5.4))
        p.drawArc(R(2.6, 9.4, 10.8, 9.6), 20 * 16, 140 * 16)
    elif name == "open":
        path.moveTo(P(7, 3.4)); path.lineTo(P(3, 3.4)); path.lineTo(P(3, 13)); path.lineTo(P(12.6, 13)); path.lineTo(P(12.6, 9))
        p.drawPath(path)
        arrow = QPainterPath()
        arrow.moveTo(P(9.2, 2.6)); arrow.lineTo(P(13.4, 2.6)); arrow.lineTo(P(13.4, 6.8))
        p.drawPath(arrow)
        p.drawLine(P(13.4, 2.6), P(7.2, 8.8))
    elif name == "copy":
        p.drawRoundedRect(R(5.6, 5.6, 8, 8), 1.3 * u, 1.3 * u)
        path.moveTo(P(3.4, 10.6)); path.lineTo(P(3.4, 3.4)); path.lineTo(P(10.6, 3.4))
        p.drawPath(path)
    elif name == "eq":
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        for a, b, h in ((2.6, 8, 6), (6.6, 3, 11), (10.6, 6, 8)):
            p.drawRoundedRect(R(a, b, 2.8, h), 0.8 * u, 0.8 * u)
    elif name == "back":
        path.moveTo(P(10, 3.8)); path.lineTo(P(5.8, 8)); path.lineTo(P(10, 12.2))
        p.drawPath(path)
    elif name == "paste":
        p.drawRoundedRect(R(3, 3.6, 10, 10.4), 1.6 * u, 1.6 * u)
        p.drawRoundedRect(R(5.8, 2, 4.4, 3), 1 * u, 1 * u)
        p.drawLine(P(5.6, 8.2), P(10.4, 8.2)); p.drawLine(P(5.6, 10.8), P(9, 10.8))
    p.restore()


class IconButton(_Button):
    """A flat icon button like the library's ToolButton, drawing from this panel's icons."""

    def __init__(self, sc: Scheme, icon: str, parent=None):
        super().__init__(sc, parent)
        self.icon = icon
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self):
        return QSize(28, 26)

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse() or self.isDown()
        if hot or (self.hasFocus() and self._ring):
            p.setPen(Qt.NoPen)
            p.setBrush(sc.well_line if self.isDown() else sc.well_hover)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4.5, 4.5)
        draw_icon(p, self.icon, QRectF((self.width() - 16) / 2, (self.height() - 16) / 2, 16, 16), sc.fg if hot else sc.win_dim)


# thumbnails

_CAP = 2 * 1024 * 1024                      # bytes of picture taken from a site
_MAX_PIXELS = 16_000_000                    # a picture bigger than this is not decoded, whatever its file size


def public_host(host: str | None) -> bool:
    """True for a host that is out on the internet: a name or address that is not this machine, a private
    network, a link-local address or anything else that only a neighbour could answer. Names are looked up."""
    host = (host or "").split("%")[0]
    if not host:
        return False
    try:
        found = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            found = [ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return False
    return bool(found) and all(a.is_global and not a.is_multicast for a in found)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects to http and https only, and with public_only only to public hosts."""

    def __init__(self, public_only: bool = False):
        super().__init__()
        self.public_only = public_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith(("http://", "https://")):
            raise urllib.error.HTTPError(newurl, code, "redirected somewhere AudioCrate will not go", headers, fp)
        if self.public_only and not public_host(urlsplit(newurl).hostname):
            raise urllib.error.HTTPError(newurl, code, "redirected to a private address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ThumbLoader(QObject):
    """Row thumbnails by URL: a memory LRU of 200, a disk cache, three worker threads.

    get() returns None for a URL nobody asked for, False for one that failed, else a pixmap.
    request() queues a miss, newest first, so after a fast scroll what is on screen loads before
    what was scrolled past. Fetching, decoding and scaling happen on the workers. The GUI thread
    only looks up and, when a batch lands, repaints.
    """
    ready = Signal()
    _loaded = Signal(str, object)
    ENTRIES, BACKLOG, TIMEOUT = 200, 48, 5
    # The picture addresses come from the sites themselves, so a page could point one at a router or at a service
    # on this machine. Only public hosts are asked, unless a check that serves its pictures locally turns this off.
    public_only = True

    def __init__(self, cache_dir=None, parent=None, workers: int = 3):
        super().__init__(parent)
        scr = QGuiApplication.primaryScreen()
        k = max(1.0, scr.devicePixelRatio() if scr else 1.0)
        self.size = QSize(round(THUMB.width() * k), round(THUMB.height() * k))
        self.dir = Path(cache_dir) if cache_dir else None
        self.fetched = 0                                 # network fetches made so far
        self._lru: OrderedDict = OrderedDict()
        self._cv = threading.Condition()
        self._stack: list[str] = []
        self._pending: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._workers, self._closed = workers, False
        self._loaded.connect(self._on_loaded)
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(40)
        self._flush.timeout.connect(self.ready)

    def get(self, url: str):
        e = self._lru.get(url)
        if e is not None:
            self._lru.move_to_end(url)
        return e

    def request(self, url: str):
        if not (isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) <= 2048):
            self._lru[url] = False
            return
        with self._cv:
            if self._closed or url in self._pending:
                return
            self._pending.add(url)
            self._stack.append(url)
            for old in self._stack[:-self.BACKLOG]:
                self._pending.discard(old)
            del self._stack[:-self.BACKLOG]
            while len(self._threads) < self._workers:
                t = threading.Thread(target=self._run, daemon=True)
                self._threads.append(t)
                t.start()
            self._cv.notify()

    def drop_backlog(self):
        with self._cv:
            self._stack.clear()
            self._pending.clear()

    def close(self):
        with self._cv:
            self._closed = True
            self._stack.clear()
            self._pending.clear()
            self._cv.notify_all()

    def _run(self):
        while True:
            with self._cv:
                while not self._stack and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                url = self._stack.pop()
            try:
                img = self._load(url)
            except Exception:
                img = None
            try:
                self._loaded.emit(url, img)
            except RuntimeError:
                return                                  # the loader was deleted under us
            time.sleep(0.002)                           # a breather for the GUI and audio threads

    def _file(self, url: str) -> Path | None:
        if self.dir is None:
            return None
        return self.dir / (hashlib.sha1(url.encode()).hexdigest() + ".jpg")

    def _load(self, url: str):
        path = self._file(url)
        img = None
        if path is not None and path.is_file():
            img = QImage(str(path))
            if img.isNull():
                img = None
        if img is None:
            raw = self._decode(self._fetch(url))
            if raw.isNull():
                return None
            s = self.size
            img = raw.scaled(s, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            img = img.copy((img.width() - s.width()) // 2, (img.height() - s.height()) // 2, s.width(), s.height())
            if path is not None:
                self._store(path, img)
        return self._rounded(img)

    @staticmethod
    def _decode(data: bytes | None) -> QImage:
        """The picture in data, or a null image. A few kilobytes of JPEG can claim a huge size, so the size is read first."""
        if not data:
            return QImage()
        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QBuffer.ReadOnly)
        reader = QImageReader(buf)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > _MAX_PIXELS:
            return QImage()
        return reader.read()

    def _fetch(self, url: str) -> bytes | None:
        if self.public_only and not public_host(urlsplit(url).hostname):
            return None
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*"})
        try:
            with urllib.request.build_opener(_SafeRedirect(self.public_only)).open(req, timeout=self.TIMEOUT) as r:
                self.fetched += 1
                if not r.headers.get_content_type().startswith("image/"):
                    return None
                data = r.read(_CAP + 1)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return data if len(data) <= _CAP else None

    @staticmethod
    def _store(path: Path, img: QImage):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if img.save(str(tmp), "JPG", 85):
                plat_fs.replace(tmp, path)
        except OSError:
            pass
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _rounded(img: QImage) -> QImage:
        out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(out.rect()), img.width() * 0.045, img.width() * 0.045)
        p.setClipPath(path)
        p.drawImage(0, 0, img)
        p.end()
        return out

    def _on_loaded(self, url: str, img):
        with self._cv:
            self._pending.discard(url)
        self._lru.pop(url, None)
        self._lru[url] = QPixmap.fromImage(img) if img is not None else False
        while len(self._lru) > self.ENTRIES:
            self._lru.popitem(last=False)
        self._flush.start()


# small labels

class ElidedLabel(QWidget):
    """One line of text, cut with an ellipsis, drawn in the scheme's colours."""

    def __init__(self, sc: Scheme, bold: bool = False, dim: bool = False, small: bool = False, parent=None):
        super().__init__(parent)
        self.sc, self._text, self._bold, self._dim, self._small = sc, "", bold, dim, small
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _font(self) -> QFont:
        f = scaled_font(self.font(), 0.88) if self._small else QFont(self.font())
        f.setBold(self._bold)
        return f

    def setText(self, text: str):
        if text != self._text:
            self._text = text
            self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self):
        return QSize(100, QFontMetrics(self._font()).height() + 4)

    def minimumSizeHint(self):
        return QSize(20, self.sizeHint().height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setFont(self._font())
        p.setPen(self.sc.dim if self._dim else self.sc.fg)
        fm = p.fontMetrics()
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(self._text, Qt.ElideRight, self.width()))


class WrapLabel(QWidget):
    """Text that wraps to a few lines and sets its own height to fit."""

    def __init__(self, sc: Scheme, max_lines: int = 2, dim: bool = True, bold: bool = False, scale: float = 0.88,
                 parent=None):
        super().__init__(parent)
        self.sc, self._text, self._max, self._lines = sc, "", max_lines, []
        self._dim, self._bold, self._scale = dim, bold, scale
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def setText(self, text: str):
        self._text = text
        self._relayout()

    def _font(self) -> QFont:
        f = scaled_font(self.font(), self._scale) if self._scale != 1.0 else QFont(self.font())
        f.setBold(self._bold)
        return f

    def _relayout(self):
        fm = QFontMetrics(self._font())
        self._lines = wrap_lines(fm, self._text, max(20, self.width()), self._max)
        h = max(1, len(self._lines)) * fm.height() + 2
        if h != self.height():
            self.setFixedHeight(h)
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setFont(self._font())
        p.setPen(self.sc.dim if self._dim else self.sc.fg)
        lh = p.fontMetrics().height()
        for i, ln in enumerate(self._lines):
            p.drawText(QRect(0, i * lh, self.width(), lh), Qt.AlignVCenter | Qt.AlignLeft, ln)


# the provider button and the feed chips

class ProviderButton(_Button):
    """The site in use: its glyph and name, and a menu of the others."""
    chosen = Signal(str)

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(sc, parent)
        self.providers: list = []
        self.cur, self.compact = None, False
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.clicked.connect(self._menu)

    def set_providers(self, providers, cur):
        self.providers, self.cur = list(providers), cur
        self.setToolTip(f"Site: {cur.name}")
        self.updateGeometry()
        self.update()

    def set_compact(self, on: bool):
        if on != self.compact:
            self.compact = on
            self.updateGeometry()
            self.update()

    def _glyph_font(self) -> QFont:
        return scaled_font(self.font(), 1.15)

    def sizeHint(self):
        w = 8 + 20 + 4 + 12 + 8                              # glyph, arrow, edges
        if not self.compact and self.cur is not None:
            w += 6 + self.fontMetrics().horizontalAdvance(self.cur.name)
        return QSize(w, 28)

    def build_menu(self) -> QMenu:
        menu = QMenu(self)
        for prov in self.providers:
            a = menu.addAction(f"{prov.glyph}   {prov.name}")
            a.setCheckable(True)
            a.setChecked(prov is self.cur)
            a.triggered.connect(lambda _=False, pid=prov.id: self.chosen.emit(pid))
        return menu

    def _menu(self):
        self.build_menu().exec(self.mapToGlobal(self.rect().bottomLeft()))

    def paintEvent(self, _):
        sc, h = self.sc, self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse() or self.isDown()
        edge = sc.accent if (self.hasFocus() and self._ring) else sc.well_line
        p.setPen(QPen(edge, 1))
        p.setBrush(sc.well_hover if hot else sc.well)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        col = sc.fg if hot else sc.win_dim
        p.setPen(col)
        p.setFont(self._glyph_font())
        p.drawText(QRect(8, 0, 20, h), Qt.AlignCenter, self.cur.glyph if self.cur else "")
        p.setFont(self.font())
        if not self.compact and self.cur is not None:
            p.setPen(sc.fg)
            p.drawText(QRect(8 + 20 + 6, 0, self.width() - 8 - 20 - 6 - 22, h), Qt.AlignVCenter | Qt.AlignLeft, self.cur.name)
        draw_icon(p, "down", QRectF(self.width() - 20, (h - 12) / 2, 12, 12), col)


class ChipBar(QWidget):
    """The feeds of a site as pills in one row. The row scrolls sideways when it is too wide.

    A chip that needs a sign in is dimmed with a lock. Clicking it does not select it, it asks
    for the Accounts settings through blocked.
    """
    chosen = Signal(str)
    blocked = Signal(str)
    GAP, PADX = 4, 11

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.chips, self.cur, self._off, self._hot = sc, [], "", 0, -1
        self.setMouseTracking(True)
        self.setFixedHeight(28)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def minimumSizeHint(self):
        return QSize(40, 28)

    def set_chips(self, chips: list, cur: str):
        """chips: (id, label, enabled, tooltip)."""
        self.chips, self.cur = list(chips), cur
        self._clamp()
        self.ensure_visible(cur)
        self.update()

    def set_current(self, cur: str):
        self.cur = cur
        self.ensure_visible(cur)
        self.update()

    def _rects(self) -> list[QRect]:
        fm, x, out = self.fontMetrics(), 0, []
        for _id, label, ok, _tip in self.chips:
            w = fm.horizontalAdvance(label) + 2 * self.PADX + (0 if ok else 14)
            out.append(QRect(x, 2, w, 24))
            x += w + self.GAP
        return out

    def _total(self) -> int:
        r = self._rects()
        return r[-1].right() + 1 if r else 0

    def _clamp(self):
        self._off = max(0, min(self._off, self._total() - self.width()))

    def ensure_visible(self, chip_id: str):
        """Scroll so the chip shows whole, and not under the soft edge."""
        for (cid, *_), r in zip(self.chips, self._rects()):
            if cid == chip_id:
                if r.left() - self._off < 20:
                    self._off = r.left() - 20
                elif r.right() - self._off > self.width() - 20:
                    self._off = r.right() - self.width() + 21
                self._clamp()
                return

    def resizeEvent(self, e):
        self._clamp()
        super().resizeEvent(e)

    def _at(self, x: int) -> int:
        for i, r in enumerate(self._rects()):
            if r.translated(-self._off, 0).contains(x, 14):
                return i
        return -1

    def wheelEvent(self, e):
        d = e.angleDelta()
        self._off += -(d.x() or d.y()) // 2
        self._clamp()
        self.update()
        e.accept()

    def mouseMoveEvent(self, e):
        i = self._at(int(e.position().x()))
        if i != self._hot:
            self._hot = i
            self.setCursor(Qt.PointingHandCursor if i >= 0 else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hot = -1
        self.update()

    def mousePressEvent(self, e):
        i = self._at(int(e.position().x()))
        if e.button() == Qt.LeftButton and i >= 0:
            self._pick(i)

    def _pick(self, i: int):
        cid, _label, ok, _tip = self.chips[i]
        if not ok:
            self.blocked.emit(cid)
        elif cid != self.cur:
            self.cur = cid
            self.update()
            self.chosen.emit(cid)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Left, Qt.Key_Right) and self.chips:
            ids = [c[0] for c in self.chips]
            i = ids.index(self.cur) if self.cur in ids else 0
            step = -1 if e.key() == Qt.Key_Left else 1
            for j in range(i + step, len(ids) if step > 0 else -1, step):
                if self.chips[j][2]:
                    self._pick(j)
                    self.ensure_visible(self.cur)
                    break
        else:
            super().keyPressEvent(e)

    def event(self, e):
        if e.type() == QEvent.ToolTip:
            i = self._at(int(e.pos().x()))
            if i >= 0 and self.chips[i][3]:
                QToolTip.showText(e.globalPos(), self.chips[i][3], self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def paintEvent(self, _):
        sc, h = self.sc, self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        for i, ((cid, label, ok, _tip), r) in enumerate(zip(self.chips, self._rects())):
            r = r.translated(-self._off, 0)
            if r.right() < 0 or r.left() > self.width():
                continue
            on = cid == self.cur
            if on:
                p.setPen(Qt.NoPen)
                p.setBrush(sc.accent)
                fg = sc.on_accent
            else:
                p.setPen(QPen(sc.well_line, 1))
                p.setBrush(sc.well_hover if (i == self._hot and ok) else sc.well)
                fg = sc.fg if (ok and i == self._hot) else sc.win_dim
            if not ok:
                p.setOpacity(0.62)
            p.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), r.height() / 2 - 1, r.height() / 2 - 1)
            if on and self.hasFocus():                      # the keyboard is on the chips
                p.setPen(QPen(sc.on_accent, 1))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(r).adjusted(2.5, 2.5, -2.5, -2.5), r.height() / 2 - 3, r.height() / 2 - 3)
            x = r.x() + self.PADX
            if not ok:
                draw_icon(p, "lock", QRectF(x - 1, r.center().y() - 6, 12, 12), fg)
                x += 14
            p.setPen(fg)
            p.setFont(self.font())
            p.drawText(QRect(x, r.y(), r.right() - x - self.PADX + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft, label)
            p.setOpacity(1)
        # a soft edge where the row runs on past the side
        for side in (0, 1):
            if (side == 0 and self._off > 0) or (side == 1 and self._off < self._total() - self.width()):
                x0 = 0 if side == 0 else self.width() - 18
                g = QLinearGradient(x0, 0, x0 + 18, 0)
                a, b = QColor(sc.win), QColor(sc.win)
                a.setAlpha(255 if side == 0 else 0)
                b.setAlpha(0 if side == 0 else 255)
                g.setColorAt(0, a)
                g.setColorAt(1, b)
                p.fillRect(QRect(x0, 0, 18, h), g)


# the results list

class ResultsModel(QAbstractListModel):
    """Items of the list on screen, plus what is known about each one: its job, whether it is saved.

    With more set there is one more row after the items, a button that asks for a longer list.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[dict] = []
        self.marks: dict[tuple, dict] = {}
        self.playing = ""
        self.more = False
        self._rows: dict[tuple, list[int]] = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items) + (1 if self.more else 0)

    def is_more(self, row: int) -> bool:
        return self.more and row == len(self.items)

    def flags(self, index):
        if self.is_more(index.row()):
            return Qt.ItemIsEnabled
        return super().flags(index) | Qt.ItemIsDragEnabled

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if self.is_more(index.row()):
            return "Show more results" if role == Qt.DisplayRole else None
        if not 0 <= index.row() < len(self.items):
            return None
        it = self.items[index.row()]
        if role == Qt.DisplayRole:
            return it.get("title", "")
        if role == ITEM_ROLE:
            return it
        return None

    def set_items(self, items: list[dict], more: bool = False):
        self.beginResetModel()
        self.items = list(items)
        self.more = bool(more and items)
        self._rows = {}
        for i, it in enumerate(self.items):
            self._rows.setdefault(item_key(it), []).append(i)
        self.endResetModel()

    def set_more(self, on: bool):
        on = bool(on and self.items)
        if on == self.more:
            return
        n = len(self.items)
        if on:
            self.beginInsertRows(QModelIndex(), n, n)
            self.more = True
            self.endInsertRows()
        else:
            self.beginRemoveRows(QModelIndex(), n, n)
            self.more = False
            self.endRemoveRows()

    def state(self, row: int) -> dict:
        return self.marks.get(item_key(self.items[row]), {}) if 0 <= row < len(self.items) else {}

    def mark(self, key: tuple, fields: dict | None):
        """Replace what is known about one item (None forgets it) and repaint its rows."""
        if fields:
            self.marks[key] = fields
        else:
            self.marks.pop(key, None)
        for r in self._rows.get(key, ()):
            ix = self.index(r)
            self.dataChanged.emit(ix, ix)

    def set_playing(self, path: str):
        if path != self.playing:
            self.playing = path
            if self.items:
                self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))


class Geo:
    """Where the parts of one row are. Shared by painting and by the mouse."""
    __slots__ = ("thumb", "text", "buttons")


def row_buttons(it: dict, mark: dict, width: int) -> list[str]:
    """Names of the hover buttons, from the right edge inwards."""
    if it.get("kind") in CONTAINERS:
        return ["open"]
    if mark.get("busy"):
        return ["cancel"]
    return ["add"] if width < 250 else ["save", "add"]


def row_geometry(rc: QRect, it: dict, mark: dict) -> Geo:
    g = Geo()
    g.thumb = QRect(rc.x() + PAD, rc.y() + (rc.height() - THUMB.height()) // 2, THUMB.width(), THUMB.height())
    right = rc.right() - PAD
    g.buttons, x = [], right - BTN + 1
    for name in row_buttons(it, mark, rc.width()):
        g.buttons.append((name, QRect(x, rc.y() + (rc.height() - BTN) // 2, BTN, BTN)))
        x -= BTN + 2
    if it.get("kind") not in CONTAINERS:
        c = g.thumb.center()
        g.buttons.append(("play", QRect(c.x() - 15, c.y() - 15, 30, 30)))
    tx = g.thumb.right() + 1 + 10
    g.text = QRect(tx, rc.y(), max(10, right - tx), rc.height())
    return g


_PLACEHOLDER_ICON = {"audio": "note", "space": "note", "playlist": "rows", "channel": "user"}


class ResultDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view, self._fonts = view, _Fonts()
        self._wrapped: dict = {}

    def sizeHint(self, opt, index):
        return QSize(10, self.view.row_h)

    def _title_lines(self, ft, text: str, w: int) -> list[str]:
        key = (text, w, ft.f1.key())
        got = self._wrapped.get(key)
        if got is None:
            if len(self._wrapped) > 600:
                self._wrapped.clear()
            got = self._wrapped[key] = wrap_lines(ft.m1, text, w, 2)
        return got

    def _paint_more(self, p, opt, sc: Scheme):
        rc = opt.rect
        hot = bool(opt.state & QStyle.State_MouseOver)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(rc.x() + PAD, rc.center().y() - 15, rc.width() - 2 * PAD, 30).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(sc.well_line, 1))
        p.setBrush(sc.well_hover if hot else sc.well)
        p.drawRoundedRect(r, 5, 5)
        p.setPen(sc.fg)
        p.drawText(r, Qt.AlignCenter, "Show more")

    def paint(self, p, opt, index):
        v, m = self.view, self.view.model()
        if m.is_more(index.row()):
            self._paint_more(p, opt, v.sc)
            return
        it, mark = m.items[index.row()], m.state(index.row())
        sc, ft = v.sc, self._fonts.get(opt.font)
        rc = opt.rect
        sel = bool(opt.state & QStyle.State_Selected)
        hot = bool(opt.state & QStyle.State_MouseOver)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        _fill_row(p, sc, rc, sel, hot)
        if not sel and opt.state & QStyle.State_HasFocus:
            p.setPen(QPen(mix(sc.base, sc.accent, 0.55), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rc.adjusted(0, 0, -1, -2))
        p.fillRect(rc.x() + PAD, rc.bottom(), rc.width() - 2 * PAD, 1, sc.line)

        geo = row_geometry(rc, it, mark)
        playing = bool(m.playing) and m.playing in mark.get("paths", ())
        self._thumb(p, v, geo.thumb, it, sc)
        badge = item_badge(it)
        if badge:
            self._badge(p, geo.thumb, badge, ft, sc)
        if playing:
            r = QRect(geo.thumb.x() + 4, geo.thumb.y() + 4, 20, 16)
            p.setPen(Qt.NoPen)
            p.setBrush(sc.accent)
            p.drawRoundedRect(QRectF(r), 3, 3)
            draw_icon(p, "eq", QRectF(r).adjusted(3, 2, -3, -2), sc.on_accent)
        hot_btn = v.hot[0] if v.hot[1] == index.row() else ""
        if hot and it.get("kind") not in CONTAINERS:
            circle = dict(geo.buttons)["play"]
            over = hot_btn == "play"
            p.setPen(Qt.NoPen)
            col = QColor(sc.accent)
            col.setAlpha(255 if over else 215)
            p.setBrush(col)
            p.drawEllipse(QRectF(circle))
            draw_icon(p, "play", QRectF(circle).adjusted(8, 7, -6, -7), sc.on_accent)

        tx, tw = geo.text.x(), geo.text.width()
        h1 = ft.m1.height()
        lines = self._title_lines(ft, it.get("title") or "Untitled", tw)
        sub = self._sub_rect(rc, geo, it, ft)
        y = sub.y() - 1 - len(lines) * h1
        p.setFont(ft.f1)
        p.setPen(mix(sc.fg, sc.accent, 0.5) if playing else sc.fg)
        for ln in lines:
            p.drawText(QRect(tx, y, tw, h1), Qt.AlignVCenter | Qt.AlignLeft, ln)
            y += h1
        self._sub(p, ft, sc, sub, it, mark, hot_btn == "who")

        # progress along the foot of a busy row
        if mark.get("busy"):
            bar = QRect(rc.x(), rc.bottom() - 2, rc.width(), 3)
            p.fillRect(bar, sc.line)
            if mark.get("indeterminate"):
                seg = max(30, rc.width() // 4)
                x = int((time.perf_counter() * 0.8 % 1.0) * (rc.width() + seg)) - seg
                p.fillRect(QRect(rc.x() + max(0, x), bar.y(), min(rc.width(), x + seg) - max(0, x), 3), sc.accent)
            else:
                p.fillRect(QRect(rc.x(), bar.y(), int(rc.width() * min(100.0, mark.get("percent", 0.0)) / 100), 3), sc.accent)

        # hover buttons, over a fade so the text under them stays readable
        if hot and geo.buttons:
            side = [(n, r) for n, r in geo.buttons if n != "play"]
            left = min(r.x() for _n, r in side)
            bg = QColor(sc.sel if sel else sc.hover)
            g = QLinearGradient(left - 26, 0, left - 2, 0)
            clear = QColor(bg)
            clear.setAlpha(0)
            g.setColorAt(0, clear)
            g.setColorAt(1, bg)
            p.fillRect(QRect(left - 26, rc.y() + 1, 24, rc.height() - 3), g)
            p.fillRect(QRect(left - 2, rc.y() + 1, rc.right() - left + 3, rc.height() - 3), bg)
            for name, r in side:
                over = hot_btn == name
                if over:
                    p.setPen(Qt.NoPen)
                    p.setBrush(sc.well_line)
                    p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 5, 5)
                icon = {"add": "add", "save": "save", "cancel": "close", "open": "right"}[name]
                draw_icon(p, icon, QRectF(r).adjusted(6, 6, -6, -6), sc.fg if over else sc.dim)

    def _sub_rect(self, rc: QRect, geo: Geo, it: dict, ft) -> QRect:
        """The dim line, which sits under as many lines of title as the title takes."""
        h1, h2 = ft.m1.height(), ft.m2.height()
        n = len(self._title_lines(ft, it.get("title") or "Untitled", geo.text.width()))
        return QRect(geo.text.x(), rc.y() + (rc.height() - n * h1 - h2 - 1) // 2 + n * h1 + 1, geo.text.width(), h2)

    @staticmethod
    def _pieces(m: QFontMetrics, rc: QRect, it: dict, mark: dict) -> list[tuple[str, str, QRect]]:
        """The dim line as the pieces that are drawn on their own, left to right: (name, text, rect). tag is Saved or
        Ready, who is the uploader when clicking it opens the channel, line is the rest, or all of it."""
        x, w, out = rc.x(), rc.width(), []
        tag = "Saved" if mark.get("saved") else ("Ready" if mark.get("ready") else "")
        if tag:
            adv = m.horizontalAdvance(tag + ", ")
            out.append(("tag", tag + ",", QRect(x, rc.y(), w, rc.height())))
            x, w = x + adv, w - adv
        pre, who, post = item_line_parts(it)
        lead = pre + ", " if pre else ""
        adv = m.horizontalAdvance(lead)
        if not (who and can_open_channel(it)) or adv >= w:
            return out + [("line", item_line(it), QRect(x, rc.y(), max(0, w), rc.height()))]
        ww = min(m.horizontalAdvance(who) + 1, w - adv)         # the advance is rounded, and a name cut short by a pixel would lose its last letter
        if lead:
            out.append(("line", lead, QRect(x, rc.y(), adv, rc.height())))
        out.append(("who", who, QRect(x + adv, rc.y(), ww, rc.height())))
        if post and w - adv - ww > 6:
            out.append(("line", ", " + post, QRect(x + adv + ww, rc.y(), w - adv - ww, rc.height())))
        return out

    def who_at(self, rc: QRect, it: dict, mark: dict) -> QRect | None:
        """Where the uploader's name is on the row at rc, when it can be clicked."""
        if mark.get("busy") or mark.get("error"):
            return None
        ft = self._fonts.get(self.view.font())
        sub = self._sub_rect(rc, row_geometry(rc, it, mark), it, ft)
        return next((r for name, _t, r in self._pieces(ft.m2, sub, it, mark) if name == "who"), None)

    @staticmethod
    def _thumb(p, v, rc: QRect, it: dict, sc: Scheme):
        url = it.get("thumbnail") or ""
        pm = v.thumbs.get(url) if url else False
        if pm is None:
            v.thumbs.request(url)
        if isinstance(pm, QPixmap):
            p.drawPixmap(rc, pm)
            return
        p.setPen(Qt.NoPen)
        p.setBrush(sc.ph)
        p.drawRoundedRect(QRectF(rc), 4, 4)
        name = _PLACEHOLDER_ICON.get(it.get("kind"), "video")
        draw_icon(p, name, QRectF(rc.center().x() - 10, rc.center().y() - 10, 20, 20), sc.dimmer)

    @staticmethod
    def _badge(p, thumb: QRect, text: str, ft, sc: Scheme):
        f = scaled_font(ft.f2, 0.92)
        fm = QFontMetrics(f)
        w, h = fm.horizontalAdvance(text) + 8, fm.height() + 2
        r = QRectF(thumb.right() - w - 3, thumb.bottom() - h - 3, w, h)
        p.setPen(Qt.NoPen)
        back = QColor(sc.win)
        back.setAlpha(205)
        p.setBrush(back)
        p.drawRoundedRect(r, 3, 3)
        p.setFont(f)
        p.setPen(sc.fg)
        p.drawText(r, Qt.AlignCenter, text)

    def _sub(self, p, ft, sc: Scheme, rc: QRect, it: dict, mark: dict, hot_who: bool = False):
        p.setFont(ft.f2)
        m = ft.m2
        if mark.get("busy"):
            p.setPen(mix(sc.dim, sc.accent, 0.6))
            p.drawText(rc, Qt.AlignVCenter | Qt.AlignLeft, m.elidedText(mark.get("text") or "Waiting", Qt.ElideRight, rc.width()))
            return
        if mark.get("error"):
            p.setPen(warn_color(sc))
            p.drawText(rc, Qt.AlignVCenter | Qt.AlignLeft, m.elidedText(mark["error"], Qt.ElideRight, rc.width()))
            return
        for name, text, r in self._pieces(m, rc, it, mark):
            if name == "tag":
                p.setPen(mix(sc.dim, sc.accent, 0.7) if mark.get("saved") else sc.dim)
            elif name == "who":
                f = QFont(ft.f2)
                f.setUnderline(hot_who)
                p.setFont(f)
                p.setPen(sc.fg if hot_who else sc.dim)
            else:
                p.setPen(sc.dim)
            p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, m.elidedText(text, Qt.ElideRight, r.width()))
            p.setFont(ft.f2)


BUTTON_TIPS = {"play": "Play now", "add": "Add to playlist", "save": "Save to library", "cancel": "Cancel",
               "open": "Open this list", "who": "Open the channel"}


class ResultsView(_Base, QListView):
    """The rows. Hover buttons are painted by the delegate and clicked here."""
    pressed_button = Signal(str, int)               # button name, row

    def __init__(self, sc: Scheme, model: ResultsModel, thumbs: ThumbLoader):
        super().__init__()
        self._init_view(sc)
        self.thumbs, self.row_h = thumbs, ROW_MIN
        self.hot, self._down = ("", -1), ("", -1)
        self.on_enter_add = None
        self.on_more = None
        self.on_escape = None
        self.drag_paths = None                  # returns the files of the selected rows that are already fetched
        self.setModel(model)
        self.setUniformItemSizes(True)
        self.setItemDelegate(ResultDelegate(self))
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self._fit()

    def _fit(self):
        h = row_height(self.font())
        if h != self.row_h:
            self.row_h = h
            self.scheduleDelayedItemsLayout()

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange:
            self._fit()
        super().changeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.on_escape:
            self.on_escape()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.model().is_more(self.currentIndex().row()):
                if self.on_more:
                    self.on_more()
            elif e.modifiers() & Qt.ShiftModifier and self.on_enter_add:
                self.on_enter_add()
            elif self.on_enter:
                self.on_enter()
            return
        super().keyPressEvent(e)

    def startDrag(self, actions):
        """A fetched row can be dragged to the playlist as the file it is. One that is not fetched has nothing to drag."""
        paths = self.drag_paths() if self.drag_paths else []
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def _hit(self, pos) -> tuple:
        """(button name or '', row) under a point of the viewport."""
        ix = self.indexAt(pos)
        if not ix.isValid():
            return "", -1
        m = self.model()
        if m.is_more(ix.row()):
            return "", ix.row()
        it = m.items[ix.row()]
        geo = row_geometry(self.visualRect(ix), it, m.state(ix.row()))
        for name, r in geo.buttons:
            if r.contains(pos):
                return name, ix.row()
        who = self.itemDelegate().who_at(self.visualRect(ix), it, m.state(ix.row()))
        if who is not None and who.contains(pos):
            return "who", ix.row()
        return "", ix.row()

    def _repaint_row(self, row: int):
        if row >= 0:
            self.viewport().update(self.visualRect(self.model().index(row)))

    def mouseMoveEvent(self, e):
        hit = self._hit(e.position().toPoint())
        if hit != self.hot:
            self._repaint_row(self.hot[1])
            self.hot = hit
            self._repaint_row(hit[1])
            self.viewport().setCursor(Qt.PointingHandCursor if hit[0] or self.model().is_more(hit[1]) else Qt.ArrowCursor)
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        self._repaint_row(self.hot[1])
        self.hot = ("", -1)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        name, row = self._hit(e.position().toPoint())
        if e.button() == Qt.LeftButton and name:
            self._down = (name, row)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        name, row = self._hit(e.position().toPoint())
        if name:
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def mouseReleaseEvent(self, e):
        if self._down[0]:
            down, self._down = self._down, ("", -1)
            if self._hit(e.position().toPoint()) == down:
                self.pressed_button.emit(*down)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def viewportEvent(self, e):
        if e.type() == QEvent.ToolTip:
            name, row = self._hit(e.pos())
            m = self.model()
            if name:
                QToolTip.showText(e.globalPos(), BUTTON_TIPS.get(name, ""), self.viewport())
            elif 0 <= row < len(m.items):
                QToolTip.showText(e.globalPos(), row_tip(m.items[row], m.state(row)), self.viewport(), self.visualRect(m.index(row)))
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(e)


# loading, nothing to show, and what went wrong

class Skeleton(QWidget):
    """Grey rows that pulse while a list loads. The timer only runs while it is told to."""

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc
        self._tick = QTimer(self)
        self._tick.setInterval(45)
        self._tick.timeout.connect(self.update)

    def run(self, on: bool):
        if on and not self._tick.isActive():
            self._tick.start()
        elif not on:
            self._tick.stop()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), sc.base)
        t = time.perf_counter()
        h = row_height(self.font())
        for i in range(self.height() // h + 1):
            y = i * h
            k = 0.5 + 0.5 * math.sin(t * 3.2 - i * 0.7)
            col = mix(sc.ph, sc.line, 0.2 + 0.6 * k)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(PAD, y + (h - THUMB.height()) / 2, THUMB.width(), THUMB.height()), 4, 4)
            tx = PAD + THUMB.width() + 10
            tw = max(20, self.width() - tx - PAD)
            p.drawRoundedRect(QRectF(tx, y + h / 2 - 15, tw * 0.9, 9), 3, 3)
            p.drawRoundedRect(QRectF(tx, y + h / 2 + 2, tw * 0.55, 8), 3, 3)
            p.fillRect(PAD, y + h - 1, self.width() - 2 * PAD, 1, sc.line)


class StateView(QWidget):
    """A glyph, a line, a hint and one button, for a page with nothing to show or something to fix.

    It drops the glyph, then the hint, as the page gets short, so the button always stays.
    """
    action = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc
        self.glyph, self.title, self.hint = "search", "", ""
        self.button = PillButton(sc, "", primary=True, parent=self)
        self.button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.button.clicked.connect(self.action)
        self.button.hide()
        self._glyph_r, self._title, self._hint = QRectF(), [], []
        self._small = QFont()

    def show_message(self, glyph: str, title: str, hint: str = "", button: str = ""):
        self.glyph, self.title, self.hint = glyph, title, hint
        self.button.setText(button)
        self.button.setVisible(bool(button))
        self.button.setFixedSize(self.button.sizeHint().width() + 12, 30)
        self._layout()
        self.update()

    def _layout(self):
        w = min(300, max(60, self.width() - 28))
        fm, small = self.fontMetrics(), QFontMetrics(scaled_font(self.font(), 0.92))
        title = wrap_lines(fm, self.title, w, 2)
        hint_all = wrap_lines(small, self.hint, w, 5)
        hint_n = len(hint_all)
        glyph = True
        btn = self.button.height() + 14 if self.button.text() else 0

        def need() -> int:
            return ((44 + 12) if glyph else 0) + len(title) * fm.height() + (6 + hint_n * small.height() if hint_n else 0) + btn

        room = self.height() - 12
        while need() > room:
            if glyph:
                glyph = False
            elif hint_n > 1:
                hint_n -= 1
            elif hint_n:
                hint_n = 0
            else:
                break
        hint = wrap_lines(small, self.hint, w, max(1, hint_n)) if hint_n else []
        y = max(6, (self.height() - need()) // 2 - 8)
        cx = self.width() // 2
        self._glyph_r = QRectF(cx - 22, y, 44, 44) if glyph else QRectF()
        if glyph:
            y += 56
        self._title = [(QRect(cx - w // 2, y + i * fm.height(), w, fm.height()), t) for i, t in enumerate(title)]
        y += len(title) * fm.height()
        self._hint = []
        if hint:
            y += 6
            self._hint = [(QRect(cx - w // 2, y + i * small.height(), w, small.height()), t) for i, t in enumerate(hint)]
            y += len(hint) * small.height()
        self._small = scaled_font(self.font(), 0.92)
        self.button.move(cx - self.button.width() // 2, y + 14)

    def resizeEvent(self, e):
        self._layout()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.fillRect(self.rect(), sc.base)
        if not self._glyph_r.isNull():
            draw_icon(p, self.glyph, self._glyph_r, sc.dimmer)
        p.setPen(sc.fg)
        for r, t in self._title:
            p.drawText(r, Qt.AlignHCenter | Qt.AlignVCenter, t)
        p.setFont(self._small)
        p.setPen(sc.dim)
        for r, t in self._hint:
            p.drawText(r, Qt.AlignHCenter | Qt.AlignVCenter, t)


# header of a list, banners, the downloads strip

class ResultHeader(QWidget):
    """Above a playlist, a channel, a feed or a post with several videos: its name, how big it is and what to do with all of it."""
    back = Signal()
    play_all = Signal()
    add_all = Signal()
    save_all = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(2)
        self._back = IconButton(sc, "back")
        self._back.setToolTip("Back to the last list")
        self._back.setAccessibleName("Back")
        self._back.clicked.connect(self.back)
        self._title = ElidedLabel(sc, bold=True)
        top.addWidget(self._back)
        top.addWidget(self._title, 1)
        lay.addLayout(top)
        self._sub = WrapLabel(sc, 2)
        lay.addWidget(self._sub)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._btns = {}
        for key, text, primary in (("play", "Play all", True), ("add", "Add all", False), ("save", "Save all", False)):
            b = PillButton(sc, text, primary=primary)
            self._btns[key] = b
            row.addWidget(b, 1)
        self._btns["play"].clicked.connect(self.play_all)
        self._btns["add"].clicked.connect(self.add_all)
        self._btns["save"].clicked.connect(self.save_all)
        lay.addLayout(row)

    def set_texts(self, title: str, sub: str, can_back: bool, tips: dict | None = None):
        self._title.setText(title)
        self._sub.setText(sub)
        self._back.setVisible(can_back)
        for k, b in self._btns.items():
            b.setToolTip((tips or {}).get(k, ""))

    def set_compact(self, on: bool):
        """On a short panel only the title stays, so the list keeps the room."""
        self._sub.setVisible(not on)
        for b in self._btns.values():
            b.setVisible(not on)

    def set_buttons(self, play: bool = True, save: bool = True):
        self._btns["play"].setEnabled(play)
        self._btns["add"].setEnabled(play)
        self._btns["save"].setEnabled(play and save)

    def painted(self) -> list:
        return [self._back, self._title, self._sub, *self._btns.values()]


class Bar(QWidget):
    """A slim line with a message, up to two buttons and a close button, for tool warnings."""
    button = Signal(int)
    closed = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.icon = sc, "link"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(36, 3, 3, 3)
        lay.setSpacing(4)
        self._label = ElidedLabel(sc)
        lay.addWidget(self._label, 1)
        self._btns = []
        for i in range(2):
            b = PillButton(sc, "", primary=(i == 0))
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            b.setFixedHeight(22)
            b.clicked.connect(lambda _=False, i=i: self.button.emit(i))
            self._btns.append(b)
            lay.addWidget(b)
        self._x = IconButton(sc, "close")
        self._x.setToolTip("Dismiss")
        self._x.setAccessibleName("Dismiss")
        self._x.clicked.connect(self.closed)
        lay.addWidget(self._x)
        self.setFixedHeight(30)

    def show_message(self, icon: str, text: str, buttons: list[str]):
        self.icon = icon
        self._label.setText(text)
        for i, b in enumerate(self._btns):
            b.setVisible(i < len(buttons))
            if i < len(buttons):
                b.setText(buttons[i])
                b.setFixedWidth(b.sizeHint().width() - 4)
        self.update()

    def painted(self) -> list:
        return [self._label, *self._btns, self._x]

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), sc.base)
        r = QRectF(self.rect()).adjusted(6.5, 1.5, -6.5, -1.5)
        p.setPen(QPen(sc.well_line, 1))
        p.setBrush(sc.well)
        p.drawRoundedRect(r, 6, 6)
        draw_icon(p, self.icon, QRectF(14, (self.height() - 14) / 2, 14, 14), sc.dim)


class DownloadStrip(QWidget):
    """One thin row per job, newest at the bottom: title, a progress bar, speed and time left, a cancel button.

    Appears only while there is something to show. A row that failed keeps its message and
    the one action that fixes it until it is dismissed. Row heights follow the font.
    """
    cancel = Signal(str)
    dismiss = Signal(str)
    action = Signal(str, str)               # job id, sign_in | update | retry
    clear = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.jobs, self.max_rows, self.hidden_n, self._hot = sc, [], 3, 0, (-1, "")
        self._src: list[dict] = []
        self._head = False
        self.setMouseTracking(True)
        self.setVisible(False)

    def _small(self) -> QFont:
        return scaled_font(self.font(), 0.88)

    def _title_h(self) -> int:
        return QFontMetrics(self.font()).height() + 3

    def row_h(self) -> int:
        fm, fs = QFontMetrics(self.font()), QFontMetrics(self._small())
        return max(STRIP_ROW, 3 + fm.height() + fs.height() + 3, 3 + self._title_h() + 2 + 4 + 5)

    def head_h(self) -> int:
        return QFontMetrics(self._small()).height() + 8

    def set_jobs(self, jobs: list[dict], max_rows: int):
        """jobs: job states, active ones first. Rows beyond max_rows are counted, not drawn."""
        self._src = jobs
        self.max_rows = max(0, max_rows)
        self.jobs = jobs[:self.max_rows]
        self.hidden_n = max(0, len(jobs) - self.max_rows)
        self._head = bool(self.hidden_n or any(self._finished(j) for j in jobs))
        self.setFixedHeight((self.head_h() if self._head else 0) + len(self.jobs) * self.row_h() + (1 if self.jobs else 0))
        self.setVisible(bool(self.jobs))
        self.update()

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange and self._src:
            self.set_jobs(self._src, self.max_rows)
        super().changeEvent(e)

    @staticmethod
    def _finished(j: dict) -> bool:
        return j.get("state") in ("done", "error", "cancelled")

    def _row_rect(self, i: int) -> QRect:
        return QRect(0, 1 + (self.head_h() if self._head else 0) + i * self.row_h(), self.width(), self.row_h())

    def _x_rect(self, r: QRect) -> QRect:
        return QRect(r.right() - PAD - 18, r.y() + 3 + (self._title_h() - 20) // 2, 20, 20)

    def _act_rect(self, r: QRect, j: dict) -> QRect:
        w = QFontMetrics(self._small()).horizontalAdvance(j.get("action_label", "")) + 10
        x = self._x_rect(r)
        return QRect(x.x() - w - 4, x.y(), w, x.height())

    def _clear_rect(self) -> QRect:
        w = QFontMetrics(self._small()).horizontalAdvance("Clear finished") + 12
        return QRect(self.width() - PAD - w, 1, w, self.head_h() - 2)

    def _at(self, pos) -> tuple:
        if self._head and self._clear_rect().contains(pos):
            return -1, "clear"
        for i, j in enumerate(self.jobs):
            r = self._row_rect(i)
            if self._x_rect(r).contains(pos):
                return i, "x"
            if j.get("action_label") and self._act_rect(r, j).contains(pos):
                return i, "act"
        return -1, ""

    def mouseMoveEvent(self, e):
        hit = self._at(e.position().toPoint())
        if hit != self._hot:
            self._hot = hit
            self.setCursor(Qt.PointingHandCursor if hit[1] else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, e):
        self._hot = (-1, "")
        self.update()

    def mouseReleaseEvent(self, e):
        i, what = self._at(e.position().toPoint())
        if e.button() != Qt.LeftButton or not what:
            return
        if what == "clear":
            self.clear.emit()
        elif what == "x":
            j = self.jobs[i]
            (self.dismiss if self._finished(j) else self.cancel).emit(j["id"])
        elif what == "act":
            self.action.emit(self.jobs[i]["id"], self.jobs[i]["action"])

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), sc.base)
        p.fillRect(0, 0, self.width(), 1, sc.line)
        small = self._small()
        fm, fs = QFontMetrics(self.font()), QFontMetrics(small)
        th = self._title_h()
        if self._head:
            p.setFont(small)
            p.setPen(sc.dimmer)
            more = f"{self.hidden_n} more" if self.hidden_n else ""
            p.drawText(QRect(PAD, 1, self.width() // 2, self.head_h() - 2), Qt.AlignVCenter | Qt.AlignLeft, more)
            p.setPen(sc.fg if self._hot[1] == "clear" else sc.dim)
            p.drawText(self._clear_rect(), Qt.AlignVCenter | Qt.AlignRight, "Clear finished")
        for i, j in enumerate(self.jobs):
            r = self._row_rect(i)
            state = j.get("state", "")
            xr = self._x_rect(r)
            hot_x = self._hot == (i, "x")
            if hot_x:
                p.setPen(Qt.NoPen)
                p.setBrush(sc.well_line)
                p.drawRoundedRect(QRectF(xr), 4, 4)
            draw_icon(p, "close", QRectF(xr).adjusted(5, 5, -5, -5), sc.fg if hot_x else sc.dim)
            right = xr.x() - 4
            if j.get("action_label"):
                ar = self._act_rect(r, j)
                p.setFont(small)
                p.setPen(sc.accent.lighter(125) if self._hot == (i, "act") else mix(sc.fg, sc.accent, 0.6))
                p.drawText(ar, Qt.AlignCenter, j["action_label"])
                right = ar.x() - 4
            status = j.get("text", "")
            if self.width() < 300 and state != "error":
                status = status.split(",")[0]
            elif state == "error":
                status = ""
            sw = min(fs.horizontalAdvance(status) + 2, max(0, (right - PAD) // 2)) if status else 0
            if status:
                p.setFont(small)
                p.setPen(sc.dim)
                p.drawText(QRect(right - sw, r.y() + 3, sw, th), Qt.AlignVCenter | Qt.AlignRight, fs.elidedText(status, Qt.ElideRight, sw))
            title = (j.get("item") or {}).get("title") or "Untitled"
            tw = max(20, right - sw - 8 - PAD)
            p.setFont(self.font())
            p.setPen(sc.fg)
            p.drawText(QRect(PAD, r.y() + 3, tw, th), Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(title, Qt.ElideRight, tw))
            if state == "error":
                p.setFont(small)
                p.setPen(warn_color(sc))
                w = self.width() - 2 * PAD
                p.drawText(QRect(PAD, r.y() + 3 + fm.height(), w, fs.height() + 2), Qt.AlignVCenter | Qt.AlignLeft,
                           fs.elidedText(j.get("error", ""), Qt.ElideRight, w))
            else:
                bar = QRectF(PAD, r.y() + 3 + th + 2, self.width() - 2 * PAD, 4)
                p.setPen(Qt.NoPen)
                p.setBrush(sc.line)
                p.drawRoundedRect(bar, 2, 2)
                if j.get("indeterminate"):
                    seg = max(30.0, bar.width() / 4)
                    x = (time.perf_counter() * 0.8 % 1.0) * (bar.width() + seg) - seg
                    clip = QRectF(bar.x() + max(0.0, x), bar.y(), min(bar.width(), x + seg) - max(0.0, x), bar.height())
                    p.setBrush(sc.accent)
                    p.drawRoundedRect(clip, 2, 2)
                else:
                    frac = 1.0 if state == "done" else min(1.0, j.get("percent", 0.0) / 100.0)
                    if frac > 0:
                        p.setBrush(sc.accent if state != "cancelled" else sc.dimmer)
                        p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(4.0, bar.width() * frac), bar.height()), 2, 2)

