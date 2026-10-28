"""Painted pieces of the Devices panel: device cards, notices, the confirm bar and the jobs strip.

They follow the library panel. Everything draws itself from a Scheme (the live theme's colours) and from
QPainter paths, so the default, Aero, art palette and system looks all work. The one picture that is not a path
is the glyph on a card: the back end names it as a Nerd Font character. None of the buttons are checkable
QPushButtons, which main_window restyles on a theme change.
"""
from __future__ import annotations

import html
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSizePolicy, QWidget

from library_widgets import PillButton, Scheme, ToolButton, draw_glyph, mix, scaled_font

CARD_W = 236                # the card column when the panel is wide
STRIP_ROW = 36
FS_NAMES = {"vfat": "FAT32", "exfat": "exFAT", "ntfs": "NTFS", "ntfs3": "NTFS", "msdos": "FAT", "fat": "FAT",
            "hfsplus": "HFS+", "hfs": "HFS", "ext4": "ext4", "ext3": "ext3", "ext2": "ext2", "btrfs": "Btrfs",
            "f2fs": "F2FS", "xfs": "XFS", "iso9660": "ISO 9660", "udf": "UDF"}
_NERD = ("JetBrainsMono Nerd Font Propo", "JetBrainsMonoNL Nerd Font Propo", "JetBrainsMono NFP")


# text and numbers

def fmt_size(n: int) -> str:
    """Bytes the way drives are sold, a gigabyte being a billion of them."""
    if n >= 10 ** 12:
        return f"{n / 10 ** 12:.2f} TB"
    if n >= 10 ** 9:
        return f"{n / 10 ** 9:.1f} GB"
    if n >= 10 ** 6:
        return f"{n / 10 ** 6:.0f} MB"
    return f"{n / 1000:.0f} KB"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def tip(text: str) -> str:
    """Text as a tooltip. Qt shows a tooltip that looks like HTML as HTML, and a tag in a song title or a volume
    label can name a file to read (<img src="/dev/zero">), so everything that came from a device is escaped here
    and the line breaks and spaces are kept as they are."""
    return f'<p style="white-space:pre-wrap">{html.escape(text, quote=False)}</p>' if text else ""


def wrap(fm: QFontMetrics, text: str, width: int, lines: int = 3) -> list[str]:
    """Greedy word wrap into at most `lines` lines, the last one elided. A word wider than the line is cut."""
    out: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}" if cur else word
            if fm.horizontalAdvance(trial) <= width:
                cur = trial
                continue
            if cur:
                out.append(cur)
            cur = word
            while len(cur) > 1 and fm.horizontalAdvance(cur) > width:
                k = max(1, int(len(cur) * width / fm.horizontalAdvance(cur)))
                while k > 1 and fm.horizontalAdvance(cur[:k]) > width:
                    k -= 1
                out.append(cur[:k])
                cur = cur[k:]
        out.append(cur)
    if len(out) > lines:
        out = out[:lines - 1] + [fm.elidedText(" ".join(out[lines - 1:]), Qt.ElideRight, width)]
    return out


def glyph_font(base: QFont, px: int) -> QFont:
    """base at a pixel size, with the bundled Nerd Font first so that the back end's device glyphs draw."""
    f = QFont(base)
    f.setFamilies(list(_NERD) + [x for x in base.families() if x not in _NERD])
    f.setPixelSize(px)
    f.setBold(False)
    f.setItalic(False)
    return f


def _signal(sc: Scheme, hue: float, t: float) -> QColor:
    """A warning colour made from the scheme: the accent's saturation at a fixed hue, mixed with the text colour so
    that it keeps its contrast on dark and light backgrounds alike."""
    sat = min(0.8, max(0.6, sc.accent.hslSaturationF()))
    return mix(sc.fg, QColor.fromHslF(hue, sat, 0.58), t)


def warn_color(sc: Scheme) -> QColor:
    """Error text: red."""
    return _signal(sc, 0.0, 0.72)


def caution_color(sc: Scheme) -> QColor:
    """Caution: amber."""
    return _signal(sc, 0.11, 0.75)


def tone_color(sc: Scheme, tone: str) -> QColor:
    return {"ok": sc.accent, "busy": sc.accent, "warn": caution_color(sc), "bad": warn_color(sc)}.get(tone, sc.dimmer)


def card_state(dev, job: str = "") -> tuple[str, str]:
    """The line under a card: what the device is doing or what it can do, and a tone (ok, busy, warn, idle)."""
    d = dev.details
    if job:
        return job, "busy"
    if dev.kind == "iphone":
        return {"mounted": ("Open, read only", "ok"), "paired": ("Paired, not opened", "idle"),
                "waiting": ("Tap Trust on the phone", "warn"), "locked": ("The phone is locked", "warn"),
                "denied": ("Trust was declined", "warn"), "no_device": ("Not connected", "warn"),
                "failed": ("Pairing did not work", "warn")}.get(d.get("state", ""), ("Needs trust", "warn"))
    if d.get("locked"):
        return "Encrypted", "warn"
    if not dev.mounted:
        return "Not mounted", "idle"
    if dev.read_only:
        return "Read only", "warn"
    if dev.kind == "ipod":
        if d.get("rockbox"):
            return "Rockbox, copies files", "ok"
        return ("Library can be updated", "ok") if d.get("can_write") else ("Copies files only", "warn")
    return ("Rockbox player", "ok") if d.get("rockbox") else ("Ready", "ok")


def model_line(dev, with_fs: bool = True) -> str:
    """The dim line under a card's name: the model and, unless left out, the file system."""
    d = dev.details
    if dev.kind == "iphone":
        return ", ".join(x for x in (d.get("family") or "iPhone", ("iOS " + d["ios"]) if d.get("ios") else "") if x)
    model = dev.model if dev.model and dev.model != dev.name else ""
    fsn = FS_NAMES.get(dev.fs, dev.fs if dev.fs not in ("", "afc") else "") if with_fs else ""
    return ", ".join(x for x in (model, fsn) if x)


# icons

_OWN = {"to_device", "from_device", "eject", "warn", "lock", "info", "usb", "ipod", "phone", "back", "more",
        "album"}


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
    if name in ("to_device", "from_device"):
        path.moveTo(P(2.5, 10.5)); path.lineTo(P(2.5, 13.5)); path.lineTo(P(13.5, 13.5)); path.lineTo(P(13.5, 10.5))
        p.drawPath(path)
        up = name == "to_device"
        p.drawLine(P(8, 10 if up else 2.5), P(8, 2.6 if up else 9.9))
        head = QPainterPath()
        ya, yb = (5.6, 2.5) if up else (6.8, 9.9)
        head.moveTo(P(4.9, ya)); head.lineTo(P(8, yb)); head.lineTo(P(11.1, ya))
        p.drawPath(head)
    elif name == "eject":
        path.moveTo(P(2.8, 9.6)); path.lineTo(P(8, 3)); path.lineTo(P(13.2, 9.6)); path.closeSubpath()
        p.drawPath(path)
        p.drawRoundedRect(R(2.8, 11.4, 10.4, 2.2), 0.6 * u, 0.6 * u)
    elif name == "warn":
        path.moveTo(P(8, 2.2)); path.lineTo(P(14.6, 13.6)); path.lineTo(P(1.4, 13.6)); path.closeSubpath()
        p.drawPath(path)
        p.drawLine(P(8, 6.4), P(8, 9.4)); p.drawLine(P(8, 11.5), P(8, 11.6))
    elif name == "lock":
        p.drawRoundedRect(R(3.4, 7, 9.2, 6.8), 1.4 * u, 1.4 * u)
        p.drawArc(R(5.2, 2.6, 5.6, 8), 0, 180 * 16)
        p.drawLine(P(5.2, 6.6), P(5.2, 7)); p.drawLine(P(10.8, 6.6), P(10.8, 7))
    elif name == "info":
        p.drawEllipse(R(1.8, 1.8, 12.4, 12.4))
        p.drawLine(P(8, 7.2), P(8, 11.4)); p.drawLine(P(8, 4.9), P(8, 5.0))
    elif name == "usb":
        p.drawRoundedRect(R(4.2, 6.2, 7.6, 8.6), 1.6 * u, 1.6 * u)
        p.drawRect(R(5.8, 1.8, 4.4, 4.4))
        p.drawLine(P(7, 3.8), P(7, 3.9)); p.drawLine(P(9, 3.8), P(9, 3.9))
    elif name == "ipod":
        p.drawRoundedRect(R(3.6, 1.6, 8.8, 12.8), 2 * u, 2 * u)
        p.drawRoundedRect(R(5.2, 3.2, 5.6, 3.8), 0.8 * u, 0.8 * u)
        p.drawEllipse(R(5.4, 8.2, 5.2, 5.2))
        p.drawEllipse(R(7.3, 10.1, 1.4, 1.4))
    elif name == "phone":
        p.drawRoundedRect(R(4.4, 1.6, 7.2, 12.8), 1.8 * u, 1.8 * u)
        p.drawLine(P(7, 3.4), P(9, 3.4))
        p.drawEllipse(R(7.3, 11.3, 1.4, 1.4))
    elif name == "album":
        p.drawEllipse(R(1.5, 1.5, 13, 13))
        p.drawEllipse(R(5.8, 5.8, 4.4, 4.4))
    elif name == "back":
        path.moveTo(P(10, 4.2)); path.lineTo(P(6.2, 8)); path.lineTo(P(10, 11.8))
        p.drawPath(path)
    elif name == "more":
        p.setPen(QPen(color, max(2.0, r.width() / 7), Qt.SolidLine, Qt.RoundCap))
        for x in (3.2, 8, 12.8):
            p.drawLine(P(x, 8), P(x + 0.05, 8))
    p.restore()


# threads

class Background(QObject):
    """Runs a function on a worker thread and hands the result back on the GUI thread."""
    _done = Signal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done.connect(self._finish)

    def run(self, fn, callback=None, *args):
        threading.Thread(target=self._work, args=(fn, callback, args), daemon=True, name="devices-ui").start()

    def _work(self, fn, callback, args):
        try:
            res, err = fn(*args), None
        except Exception as e:
            res, err = None, e
        try:
            self._done.emit(callback, res, err)
        except RuntimeError:
            pass                                        # the panel was closed while this ran

    @staticmethod
    def _finish(callback, res, err):
        if callback is not None:
            callback(res, err)


# buttons

class IconButton(ToolButton):
    """The library's flat tool button with this panel's icons, dimmed when it cannot be used."""

    def setToolTip(self, text: str):
        super().setToolTip(tip(text))
        self.setAccessibleName(text)

    def paintEvent(self, _):
        sc, h = self.sc, self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isEnabled()
        if not on:
            p.setOpacity(0.4)
        hot = on and (self.underMouse() or self.isDown())
        if hot or (self.hasFocus() and self._ring):
            p.setPen(Qt.NoPen)
            p.setBrush(sc.well_line if self.isDown() else sc.well_hover)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4.5, 4.5)
        col = sc.fg if hot else sc.win_dim
        x = 6
        draw_icon(p, self.glyph, QRectF(x, (h - 16) / 2, 16, 16), col)
        x += 16
        if self.text():
            p.setPen(col)
            p.drawText(QRect(x + 5, 0, self.width() - x - 5 - (14 if self.arrow else 0), h),
                       Qt.AlignVCenter | Qt.AlignLeft, self.fontMetrics().elidedText(
                           self.text(), Qt.ElideRight, self.width() - x - 5 - (14 if self.arrow else 0)))
        if self.arrow:
            draw_icon(p, "down", QRectF(self.width() - 18, (h - 12) / 2, 12, 12), col)


# text blocks, notices, states

class Text(QWidget):
    """Wrapped text that sets its own height from its width."""

    def __init__(self, sc: Scheme, dim: bool = True, lines: int = 4, scale: float = 0.92, parent=None):
        super().__init__(parent)
        self.sc, self.dim, self.max_lines, self.scale, self.text, self.lines = sc, dim, lines, scale, "", []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_text(self, text: str):
        self.text = text
        self._fit()
        self.update()

    def _fm(self) -> QFontMetrics:
        return QFontMetrics(scaled_font(self.font(), self.scale))

    def _fit(self):
        fm = self._fm()
        self.lines = wrap(fm, self.text, max(20, self.width()), self.max_lines) if self.text else []
        h = max(1, len(self.lines)) * fm.height() + 2
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, e):
        if e.size().width() != e.oldSize().width():
            self._fit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setFont(scaled_font(self.font(), self.scale))
        p.setPen(self.sc.dim if self.dim else self.sc.fg)
        fh = p.fontMetrics().height()
        for i, line in enumerate(self.lines):
            p.drawText(QRect(0, 1 + i * fh, self.width(), fh), Qt.AlignVCenter | Qt.AlignLeft, line)


class Banner(QWidget):
    """A notice in a rounded box with an icon: a warning, or something worth knowing. It can be dismissed."""
    closed = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.kind, self.closable = sc, "info", False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(34, 8, 10, 8)
        lay.setSpacing(6)
        self._text = Text(sc, dim=False, lines=5)
        lay.addWidget(self._text, 1)
        self._x = IconButton(sc, "close")
        self._x.setFixedSize(22, 22)
        self._x.setToolTip("Dismiss")
        self._x.clicked.connect(self.closed)
        lay.addWidget(self._x, 0, Qt.AlignTop)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.hide()

    def show_message(self, kind: str, text: str, closable: bool = False):
        """Set what it says. Whether it is shown is the owner's call, as that depends on the room there is."""
        self.kind, self.closable = kind, closable
        self._x.setVisible(closable)
        self._text.set_text(text)
        self.setToolTip(tip(text))
        self._fit()
        self.update()

    def text(self) -> str:
        return self._text.text

    def set_lines(self, n: int):
        if n != self._text.max_lines:
            self._text.max_lines = n
            self._text._fit()
            self._fit()

    def painted(self) -> list:
        return [self._text, self._x]

    def _fit(self):
        self.setFixedHeight(max(self._text.height(), 22 if self.closable else 0) + 16)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._text._fit()
        self._fit()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        warn = self.kind == "warn"
        edge = mix(sc.well_line, caution_color(sc), 0.55) if warn else sc.well_line
        p.setPen(QPen(edge, 1))
        p.setBrush(sc.well)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(8.5, 1.5, -8.5, -1.5), 6, 6)
        draw_icon(p, "warn" if warn else "info", QRectF(17, 9, 14, 14), caution_color(sc) if warn else sc.dim)


class StateView(QWidget):
    """A glyph, a line, a hint and one button, for a page with nothing to show or something to fix.

    A device glyph (one Nerd Font character) is drawn as text, anything longer is the name of an icon. The page
    drops the glyph, then the hint, as it gets short, so the button always stays.
    """
    action = Signal()

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.glyph, self.title, self.hint = sc, "usb", "", ""
        self.button = PillButton(sc, "", primary=True, parent=self)
        self.button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.button.clicked.connect(self.action)
        self.button.hide()
        self._glyph_r, self._title, self._hint, self._small = QRectF(), [], [], QFont()

    def show_message(self, glyph: str, title: str, hint: str = "", button: str = ""):
        self.glyph, self.title, self.hint = glyph, title, hint
        self.button.setText(button)
        self.button.setVisible(bool(button))
        self.button.setFixedSize(self.button.sizeHint().width() + 12, 30)
        self._layout()
        self.update()

    def _layout(self):
        w = min(340, max(60, self.width() - 28))
        fm, small = self.fontMetrics(), QFontMetrics(scaled_font(self.font(), 0.92))
        title = wrap(fm, self.title, w, 2)
        n = len(wrap(small, self.hint, w, 6)) if self.hint else 0
        glyph, btn = True, (self.button.height() + 14 if self.button.text() else 0)

        def need() -> int:
            return (56 if glyph else 0) + len(title) * fm.height() + (6 + n * small.height() if n else 0) + btn

        while need() > self.height() - 12:
            if glyph:
                glyph = False
            elif n > 1:
                n -= 1
            elif n:
                n = 0
            else:
                break
        hint = wrap(small, self.hint, w, max(1, n)) if n else []
        y = max(6, (self.height() - need()) // 2 - 8)
        cx = self.width() // 2
        self._glyph_r = QRectF(cx - 22, y, 44, 44) if glyph else QRectF()
        y += 56 if glyph else 0
        self._title = [(QRect(cx - w // 2, y + i * fm.height(), w, fm.height()), t) for i, t in enumerate(title)]
        y += len(title) * fm.height()
        self._small, self._hint = scaled_font(self.font(), 0.92), []
        if hint:
            y += 6
            self._hint = [(QRect(cx - w // 2, y + i * small.height(), w, small.height()), t)
                          for i, t in enumerate(hint)]
            y += len(hint) * small.height()
        self.button.move(cx - self.button.width() // 2, y + 14)

    def resizeEvent(self, e):
        self._layout()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.fillRect(self.rect(), sc.base)
        if not self._glyph_r.isNull():
            if len(self.glyph) == 1:
                p.setFont(glyph_font(self.font(), 40))
                p.setPen(sc.dimmer)
                p.drawText(self._glyph_r, Qt.AlignCenter, self.glyph)
            else:
                draw_icon(p, self.glyph, self._glyph_r, sc.dimmer)
        p.setFont(self.font())
        p.setPen(sc.fg)
        for r, t in self._title:
            p.drawText(r, Qt.AlignHCenter | Qt.AlignVCenter, t)
        p.setFont(self._small)
        p.setPen(sc.dim)
        for r, t in self._hint:
            p.drawText(r, Qt.AlignHCenter | Qt.AlignVCenter, t)


# the cards

class DeviceCard(QWidget):
    """One device: a glyph, its name and model, how full it is, what state it is in, and an eject button.

    It is also a drop target: files dropped on it are offered for copying to that device.
    """
    clicked = Signal(str)
    activated = Signal(str)             # a double click: open the device when it is not open
    eject = Signal(str)
    menu = Signal(str, QPoint)
    dropped = Signal(str, list)         # device id, local paths

    def __init__(self, sc: Scheme, dev, parent=None):
        super().__init__(parent)
        self.sc, self.dev = sc, dev
        self.selected = self.compact = self.drag_over = self.hot_x = False
        self.job, self.problem, self._geo = "", "", {}
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_device(dev)

    # state

    def set_device(self, dev):
        self.dev = dev
        self.setToolTip(tip(self._tip()))
        self.setAccessibleName(dev.name or "Drive")
        self._fit()
        self.update()

    def set_job(self, text: str):
        if text != self.job:
            self.job = text
            self.update()

    def set_selected(self, on: bool):
        if on != self.selected:
            self.selected = on
            self.update()

    def set_compact(self, on: bool):
        if on != self.compact:
            self.compact = on
            self._fit()
            self.update()

    def can_eject(self) -> bool:
        d = self.dev
        return d.kind != "iphone" or d.mounted

    def _tip(self) -> str:
        d, det = self.dev, self.dev.details
        lines = [d.name or "Drive", model_line(d)]
        if d.capacity:
            lines.append(f"{fmt_size(d.capacity)} in all" + (f", {fmt_size(d.free)} free" if d.mounted else ""))
        if d.kind == "ipod" and det.get("generation"):
            lines.append("Plays " + ", ".join(x.upper() if len(x) <= 4 else x for x in det.get("formats", [])))
        if d.mount:
            lines.append(str(d.mount))
        return "\n".join(x for x in lines if x)

    # geometry

    def _fit(self):
        w, dev = max(self.width(), 120), self.dev
        fm = self.fontMetrics()
        fs = QFontMetrics(scaled_font(self.font(), 0.86))
        pad = 8 if self.compact else 10
        tile = 28 if self.compact else 34
        tx = pad + tile + (8 if self.compact else 10)
        ej = QRect(w - pad - 22, pad - 3, 22, 22) if self.can_eject() else QRect()
        g = {"pad": pad, "tile": QRect(pad, pad, tile, tile), "tx": tx, "eject": ej, "fs": fs}
        name_w = w - tx - pad - (24 if self.can_eject() else 0)
        g["name"] = QRect(tx, pad, name_w, fm.height())
        has_bar = dev.mounted and dev.capacity > 0
        cap = ""
        if dev.capacity:
            cap = f"{fmt_size(dev.free)} free of {fmt_size(dev.capacity)}" if dev.mounted else fmt_size(dev.capacity)
        g["cap"], g["bar"] = cap, has_bar
        g["short"] = f"{fmt_size(dev.free)} free" if dev.mounted and dev.capacity else cap
        if self.compact:
            y = pad + fm.height() + 4
            g["line2"] = QRect(tx, y, w - tx - pad, fs.height())
            h = y + fs.height() + pad
        else:
            model = wrap(fs, model_line(dev, False), w - tx - pad, 2) if model_line(dev, False) else []
            fsn = FS_NAMES.get(dev.fs, "") if dev.kind != "iphone" else ""
            if fsn and (not model or fs.horizontalAdvance(f"{model[-1]}, {fsn}") <= w - tx - pad):
                model = model[:-1] + [f"{model[-1]}, {fsn}" if model else fsn]
            top = pad + fm.height() + 1
            if ej.isValid():
                top = max(top, ej.bottom() + 2)             # a short font must not slide the text under the eject button
            g["model"] = [QRect(tx, top + i * fs.height(), w - tx - pad, fs.height())
                          for i in range(len(model))]
            g["model_text"] = model
            y = max(pad + tile, top + len(model) * fs.height()) + 8
            if has_bar:
                g["barr"] = QRectF(pad, y, w - 2 * pad, 5)
                y += 10
            if cap:
                g["capr"] = QRect(pad, y, w - 2 * pad, fs.height())
                y += fs.height() + 3
            g["stater"] = QRect(pad, y, w - 2 * pad, fs.height())
            h = y + fs.height() + pad
        if h != self.height():
            self.setFixedHeight(h)
        self._geo = g

    def resizeEvent(self, e):
        self._fit()

    def changeEvent(self, e):
        if e.type() == e.Type.FontChange:
            self._fit()
        super().changeEvent(e)

    # painting

    def paintEvent(self, _):
        sc, dev, g = self.sc, self.dev, self._geo
        if not g:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse()
        fill = mix(sc.well, sc.accent, 0.26) if self.selected else (sc.well_hover if hot else sc.well)
        edge = sc.accent if (self.selected or self.drag_over) else sc.well_line
        if self.drag_over:
            fill = mix(sc.well, sc.accent, 0.14 if self.problem else 0.34)
        p.setPen(QPen(edge, 2 if self.drag_over else 1, Qt.DashLine if self.drag_over and self.problem else Qt.SolidLine))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        if self.hasFocus() and not self.selected:
            p.setPen(QPen(sc.accent, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 7, 7)
        # the picture
        tile = g["tile"]
        p.setPen(Qt.NoPen)
        p.setBrush(mix(fill, sc.accent, 0.22))
        p.drawRoundedRect(QRectF(tile), 7, 7)
        gf = glyph_font(self.font(), int(tile.height() * 0.62))
        if dev.glyph and QFontMetrics(gf).inFontUcs4(ord(dev.glyph[0])):
            p.setFont(gf)
            p.setPen(sc.fg)
            p.drawText(tile, Qt.AlignCenter, dev.glyph)
        else:
            draw_icon(p, {"ipod": "ipod", "iphone": "phone"}.get(dev.kind, "usb"), QRectF(tile).adjusted(5, 5, -5, -5), sc.fg)
        # name and eject
        p.setFont(self.font())
        p.setPen(sc.fg)
        p.drawText(g["name"], Qt.AlignVCenter | Qt.AlignLeft, self.fontMetrics().elidedText(
            dev.name or "Drive", Qt.ElideRight, g["name"].width()))
        if self.can_eject():
            r = g["eject"]
            if self.hot_x:
                p.setPen(Qt.NoPen)
                p.setBrush(sc.well_line)
                p.drawRoundedRect(QRectF(r), 4.5, 4.5)
            draw_icon(p, "eject", QRectF(r).adjusted(3, 3, -3, -3), sc.fg if self.hot_x else sc.dim)
        fs = g["fs"]
        small = scaled_font(self.font(), 0.86)
        p.setFont(small)
        text, tone = card_state(dev, self.job)
        if self.drag_over:
            text, tone = (self.problem, "warn") if self.problem else ("Drop to copy here", "ok")
        if self.compact:
            self._paint_compact(p, text, tone, fs)
        else:
            p.setPen(sc.dim)
            for r, t in zip(g["model"], g["model_text"]):
                p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, t)
            if g["bar"]:
                self._bar(p, g["barr"])
            if g["cap"]:
                p.setPen(sc.dim)
                p.drawText(g["capr"], Qt.AlignVCenter | Qt.AlignLeft, fs.elidedText(g["cap"], Qt.ElideRight, g["capr"].width()))
            self._state(p, g["stater"], text, tone, fs)

    def _bar(self, p: QPainter, r: QRectF):
        dev, sc = self.dev, self.sc
        p.setPen(Qt.NoPen)
        p.setBrush(sc.line)
        p.drawRoundedRect(r, 2.5, 2.5)
        used = min(1.0, max(0.0, 1 - dev.free / dev.capacity)) if dev.capacity else 0
        if used > 0:
            p.setBrush(caution_color(sc) if dev.free / dev.capacity < 0.08 else sc.accent)
            p.drawRoundedRect(QRectF(r.x(), r.y(), max(5.0, r.width() * used), r.height()), 2.5, 2.5)

    def _state(self, p: QPainter, r: QRect, text: str, tone: str, fs: QFontMetrics):
        sc = self.sc
        p.setPen(Qt.NoPen)
        p.setBrush(tone_color(sc, tone))
        p.drawEllipse(QPointF(r.x() + 3.5, r.center().y() + 0.5), 3.0, 3.0)
        p.setPen(caution_color(sc) if tone == "warn" else (sc.fg if tone in ("ok", "busy") else sc.dim))
        p.drawText(QRect(r.x() + 12, r.y(), r.width() - 12, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   fs.elidedText(text, Qt.ElideRight, r.width() - 12))

    def _paint_compact(self, p: QPainter, text: str, tone: str, fs: QFontMetrics):
        """A bar and one label under the name. The label gives up words, then the bar, before it is cut short."""
        sc, g = self.sc, self._geo
        r = g["line2"]
        show_state = tone in ("warn", "busy") or self.drag_over or not g["cap"]
        variants = [text] if show_state else [g["cap"], g["short"]]
        label = next((v for v in variants if fs.horizontalAdvance(v) + 58 <= r.width()), variants[-1])
        lw = min(fs.horizontalAdvance(label) + 2, r.width())
        if g["bar"] and lw + 58 <= r.width():
            self._bar(p, QRectF(r.x(), r.center().y() - 2, r.width() - lw - 8, 5))
        p.setPen((caution_color(sc) if tone == "warn" else sc.fg) if show_state else sc.dim)
        p.drawText(QRect(r.right() - lw, r.y(), lw, r.height()), Qt.AlignVCenter | Qt.AlignRight,
                   fs.elidedText(label, Qt.ElideRight, lw))

    # mouse and keys

    def _in_eject(self, pos) -> bool:
        return self.can_eject() and self._geo.get("eject", QRect()).adjusted(-3, -3, 3, 3).contains(pos)

    def mouseMoveEvent(self, e):
        hot = self._in_eject(e.position().toPoint())
        if hot != self.hot_x:
            self.hot_x = hot
            self.setToolTip(tip("Eject " + (self.dev.name or "this device") if hot else self._tip()))
            self.update()

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.hot_x = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self.rect().contains(e.position().toPoint()):
            return
        (self.eject if self._in_eject(e.position().toPoint()) else self.clicked).emit(self.dev.id)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and not self._in_eject(e.position().toPoint()):
            self.activated.emit(self.dev.id)

    def contextMenuEvent(self, e):
        self.menu.emit(self.dev.id, e.globalPos())

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.dev.id)
        else:
            super().keyPressEvent(e)

    # drops

    @staticmethod
    def _local(mime) -> list[Path]:
        return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []

    def dragEnterEvent(self, e):
        if self._local(e.mimeData()):
            self.drag_over = True
            self.update()
            e.setDropAction(Qt.CopyAction)
            e.accept()

    def dragMoveEvent(self, e):
        if self._local(e.mimeData()) and not self.problem:
            e.setDropAction(Qt.CopyAction)          # whatever the source offers, this never moves a file
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.drag_over = False
        self.update()

    def dropEvent(self, e):
        self.drag_over = False
        self.update()
        paths = self._local(e.mimeData())
        if paths and not self.problem:
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self.dropped.emit(self.dev.id, paths)


# the bar that asks, and the strip that reports

class ConfirmBar(QWidget):
    """One question with its answers: a sentence, up to two menu buttons (the format, the target device), Cancel
    and the button that does it. It sits above the jobs strip and goes away when answered. It places its own
    parts: the menu buttons share the row with the answers when they fit and take the row above when they do not."""
    accepted = Signal()
    cancelled = Signal()
    chosen = Signal(str, str)           # which menu ('profile' or 'device'), and the id picked
    ROW = 26

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc = sc
        self._text = Text(sc, dim=False, lines=4, scale=1.0, parent=self)
        self._menus: dict[str, IconButton] = {}
        for key, icon in (("profile", "sort"), ("device", "usb")):
            b = IconButton(sc, icon, "", arrow=True, parent=self)
            b.clicked.connect(lambda _=False, k=key: self._open(k))
            b.hide()
            self._menus[key] = b
        self._no = PillButton(sc, "Cancel", parent=self)
        self._yes = PillButton(sc, "OK", primary=True, parent=self)
        for b in (self._no, self._yes):
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._no.clicked.connect(self.cancelled)
        self._yes.clicked.connect(self.accepted)
        self._options: dict[str, list] = {}
        self._wide: dict[str, int] = {}                 # the width each menu button would like
        self.current: dict[str, str] = {}
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.hide()

    def ask(self, text: str, ok: str, profiles: list | None = None, profile: str = "", devices: list | None = None,
            device: str = ""):
        """profiles is [{'id','label','detail'}] and devices [(id, label)]; each shows as a menu button when given."""
        self._yes.setText(ok)
        self._yes.setFixedWidth(self._yes.sizeHint().width() + 8)
        self._no.setFixedWidth(self._no.sizeHint().width() + 8)
        self._options = {"profile": profiles or [], "device": devices or []}
        self.pick("profile", profile)
        self.pick("device", device)
        self._text.text = text
        self.show()
        self._place()
        self.update()

    def pick(self, which: str, ident: str):
        b, opts = self._menus[which], self._options.get(which) or []
        names = {(o["id"] if isinstance(o, dict) else o[0]): (o["label"] if isinstance(o, dict) else o[1]) for o in opts}
        b.setVisible(bool(names))
        if names:
            ident = ident if ident in names else next(iter(names))
            self.current[which] = ident
            b.setText(("Format: " if which == "profile" else "To: ") + names[ident])
            self._wide[which] = b.sizeHint().width() + 4

    def value(self, which: str) -> str:
        return self.current.get(which, "")

    def _open(self, which: str):
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for o in self._options.get(which) or []:
            ident, label, detail = (o["id"], o["label"], o.get("detail", "")) if isinstance(o, dict) else (o[0], o[1], "")
            a = menu.addAction(label.replace("&", "&&"))            # a device label is not a shortcut
            a.setToolTip(tip(detail or label))                      # an empty tip would fall back to the raw label
            a.setCheckable(True)
            a.setChecked(ident == self.value(which))
            a.triggered.connect(lambda _=False, i=ident: (self.pick(which, i), self._place(), self.chosen.emit(which, i)))
        b = self._menus[which]
        menu.exec(b.mapToGlobal(QPoint(0, -menu.sizeHint().height())))

    def painted(self) -> list:
        return [self._text, *self._menus.values(), self._no, self._yes]

    def _place(self):
        """Sentence, then the menu buttons in as many rows as they need, then the answers: on the last of those
        rows when they fit there, else on a row of their own."""
        w, row = self.width(), self.ROW
        x0, x1 = 18, w - 18
        self._text.setGeometry(x0, 12, max(40, x1 - x0), self._text.height())
        self._text._fit()
        y = 12 + self._text.height() + 8
        x, started = x0, False
        for which, b in self._menus.items():
            if not b.isVisibleTo(self):
                continue
            bw = min(self._wide.get(which, b.width()), x1 - x0)
            if started and x + bw > x1:
                x, y = x0, y + row + 6
            b.setGeometry(x, y, bw, row)
            x, started = x + bw + 6, True
        need = self._no.width() + 6 + self._yes.width()
        if started and x + need > x1 + 6:
            x, y = x0, y + row + 6
        elif not started:
            x = x0
        for b in (self._no, self._yes):
            b.setFixedHeight(row)
        self._yes.move(x1 - self._yes.width(), y)
        self._no.move(self._yes.x() - 6 - self._no.width(), y)
        self.setFixedHeight(y + row + 12)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.size().width() != e.oldSize().width():
            self._place()

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), sc.base)
        p.setPen(QPen(sc.well_line, 1))
        p.setBrush(sc.well)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(8.5, 4.5, -8.5, -4.5), 8, 8)


class JobStrip(QWidget):
    """One thin row per job, newest at the bottom: what it is doing, a progress bar, a cancel or dismiss button.

    It shows only while there is something to show. A job that failed keeps its message until dismissed.
    """
    cancel = Signal(str)
    dismiss = Signal(str)

    def __init__(self, sc: Scheme, parent=None):
        super().__init__(parent)
        self.sc, self.rows, self.hidden_n, self._hot = sc, [], 0, -1
        self._tick = QTimer(self)               # moves the bar of a job whose end is not known, while it shows
        self._tick.setInterval(33)
        self._tick.timeout.connect(self.update)
        self.setMouseTracking(True)
        self.hide()

    def set_rows(self, rows: list[dict], max_rows: int):
        """rows: {'id','title','text','state','frac'} with frac None for a job whose end is not known."""
        self.rows = rows[:max(1, max_rows)]
        self.hidden_n = max(0, len(rows) - len(self.rows))
        self.setFixedHeight(len(self.rows) * STRIP_ROW + 1 if self.rows else 0)
        self._sync_tick()
        self.update()

    def _sync_tick(self):
        if self.isVisible() and any(r.get("frac") is None and not self.finished(r) for r in self.rows):
            self._tick.start()
        else:
            self._tick.stop()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_tick()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._tick.stop()

    @staticmethod
    def finished(r: dict) -> bool:
        return r.get("state") in ("done", "error", "cancelled")

    def _row_rect(self, i: int) -> QRect:
        return QRect(0, 1 + i * STRIP_ROW, self.width(), STRIP_ROW)

    def _x_rect(self, r: QRect) -> QRect:
        return QRect(r.right() - 10 - 18, r.y() + 3, 20, 20)

    def _at(self, pos) -> int:
        return next((i for i in range(len(self.rows)) if self._x_rect(self._row_rect(i)).contains(pos)), -1)

    def mouseMoveEvent(self, e):
        hot = self._at(e.position().toPoint())
        if hot != self._hot:
            self._hot = hot
            self.setCursor(Qt.PointingHandCursor if hot >= 0 else Qt.ArrowCursor)
            self.update()
        row = next((i for i, _ in enumerate(self.rows) if self._row_rect(i).contains(e.position().toPoint())), -1)
        self.setToolTip(tip(self.rows[row].get("text", "")) if row >= 0 else "")

    def leaveEvent(self, e):
        self._hot = -1
        self.update()

    def mouseReleaseEvent(self, e):
        i = self._at(e.position().toPoint())
        if e.button() == Qt.LeftButton and i >= 0:
            r = self.rows[i]
            (self.dismiss if self.finished(r) else self.cancel).emit(r["id"])

    def paintEvent(self, _):
        sc = self.sc
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), sc.base)
        p.fillRect(0, 0, self.width(), 1, sc.line)
        small = scaled_font(self.font(), 0.88)
        fm, fs = QFontMetrics(self.font()), QFontMetrics(small)
        for i, r in enumerate(self.rows):
            rc = self._row_rect(i)
            xr, state = self._x_rect(rc), r.get("state", "")
            hot = self._hot == i
            if hot:
                p.setPen(Qt.NoPen)
                p.setBrush(sc.well_line)
                p.drawRoundedRect(QRectF(xr), 4, 4)
            draw_icon(p, "close", QRectF(xr).adjusted(5, 5, -5, -5), sc.fg if hot else sc.dim)
            right = xr.x() - 4
            status = r.get("text", "") if state != "error" else ""
            sw = min(fs.horizontalAdvance(status), max(0, (right - 10) // 2)) if status else 0
            if sw:
                p.setFont(small)
                p.setPen(sc.dim)
                p.drawText(QRect(right - sw, rc.y() + 3, sw, 20), Qt.AlignVCenter | Qt.AlignRight,
                           fs.elidedText(status, Qt.ElideRight, sw))
            tw = max(20, right - sw - 8 - 10)
            p.setFont(self.font())
            p.setPen(sc.fg)
            p.drawText(QRect(10, rc.y() + 3, tw, 20), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(r.get("title", ""), Qt.ElideRight, tw))
            if state == "error":
                p.setFont(small)
                p.setPen(warn_color(sc))
                p.drawText(QRect(10, rc.y() + 20, self.width() - 20, 14), Qt.AlignVCenter | Qt.AlignLeft,
                           fs.elidedText(r.get("text", ""), Qt.ElideRight, self.width() - 20))
                continue
            bar = QRectF(10, rc.y() + 25, self.width() - 20, 4)
            p.setPen(Qt.NoPen)
            p.setBrush(sc.line)
            p.drawRoundedRect(bar, 2, 2)
            frac = r.get("frac")
            if frac is None:
                seg = max(30.0, bar.width() / 4)
                x = (time.perf_counter() * 0.8 % 1.0) * (bar.width() + seg) - seg
                clip = QRectF(bar.x() + max(0.0, x), bar.y(), min(bar.width(), x + seg) - max(0.0, x), bar.height())
                p.setBrush(sc.accent)
                p.drawRoundedRect(clip, 2, 2)
            elif frac > 0:
                p.setBrush(sc.dimmer if state == "cancelled" else sc.accent)
                p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(4.0, bar.width() * min(1.0, frac)), bar.height()), 2, 2)

