"""The Devices panel: USB sticks, SD cards, drives, Rockbox players, iPods and iPhones.

Cards for what is plugged in on one side, what is on the selected device on the other. Everything that touches a
device goes through DeviceMonitor, which works in threads and answers with signals, so nothing here waits.
"""
from __future__ import annotations

import time
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QBoxLayout, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMenu, QScrollArea,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

import idevice
import ipod
from devices import DeviceMonitor, expand_sources
from device_views import DeviceData, DeviceView, Head, folder_rows
from device_widgets import (CARD_W, Background, Banner, ConfirmBar, DeviceCard, IconButton, JobStrip, StateView,
                            plural)
from library_widgets import PillButton, ProgressLine, Scheme, Segmented, StatusLine, draw_glyph
from phone_files_view import PhoneFilesView
from player import AUDIO_EXTS

WIDE = 560                          # below this the cards sit above the contents instead of beside them
MODES = {
    "ipod": [("songs", "Songs"), ("albums", "Albums"), ("artists", "Artists"), ("playlists", "Playlists"),
             ("files", "Files")],
    "usb": [("files", "Folders"), ("songs", "Songs"), ("albums", "Albums"), ("artists", "Artists")],
    "iphone": [("songs", "Music"), ("folders", "Folders"), ("files", "Files")],
}
PHONE_MUSIC = "iTunes_Control/Music"
VERBS = {"copy_to": ("Copying to", "Copied to", "Could not copy to", "Stopped copying to"),
         "copy_from": ("Copying from", "Copied from", "Could not copy from", "Stopped copying from"),
         "mount": ("Opening", "Opened", "Could not open", "Stopped opening"),
         "unmount": ("Unmounting", "Unmounted", "Could not unmount", "Stopped unmounting"),
         "eject": ("Ejecting", "Ejected", "Could not eject", "Stopped ejecting"),
         "restore": ("Restoring", "Restored", "Could not restore", "Stopped restoring")}
KEEP = {"copy_to": 20, "copy_from": 20, "eject": 12}        # seconds a finished job stays in the strip
PHONE_TITLES = {"unpaired": "{} does not trust this computer yet", "waiting": "Tap Trust on the {}",
                "locked": "The {} is locked", "denied": "Trust was declined", "failed": "Pairing did not work",
                "no_device": "The {} is not connected"}
RUNNING = ("queued", "running")


def receive_problem(dev) -> str:
    """Why music cannot be copied onto this device, or '' when it can."""
    if dev.kind == "iphone":
        return "iOS does not let other programs add music to the Music app."
    if dev.details.get("locked"):
        return "This drive is encrypted. Open it in your file manager first."
    if dev.mounted and dev.read_only:
        return (getattr(ipod, "_READ_ONLY_HFS", "") if "hfs" in dev.fs else "") \
            or "This device is mounted read only, so nothing can be copied to it."
    return ""


def _count_music(paths: list[Path]) -> int:
    return len(expand_sources(paths))


class _Divider(QWidget):
    """A hairline between the cards and the contents, upright when they sit side by side."""

    def __init__(self, sc: Scheme):
        super().__init__()
        self.sc = sc

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), self.sc.line)


class _Header(QWidget):
    """The mode switch, the search box and the action buttons. It places them itself: the height decides between
    one row and two, the width between a pill and a menu button for the switch, and which buttons stay."""
    ROW = 28

    def __init__(self, seg, drop, search, buttons: dict):
        super().__init__()
        self.seg, self.drop, self.search, self.buttons = seg, drop, search, buttons
        for w in (seg, drop, search, *buttons.values()):
            w.setParent(self)
        self.two_rows = True

    def place(self, w: int, panel_h: int):
        two = panel_h >= 250 and w >= 290
        names = ["to", "from", "refresh", "eject", "more"] if w >= 330 else ["to", "from", "eject"] if w >= 250 \
            else ["to", "eject"]
        bw = 28
        for k, b in self.buttons.items():
            b.setVisible(k in names)
        bar = bw * len(names)
        fm, labels = self.seg.fontMetrics(), self.seg.labels
        need = 4 + len(labels) * (max(fm.horizontalAdvance(t) for t in labels) + 16)    # its segments are alike
        pill = need <= w - 16 - (0 if two else bar + 6)
        self.two_rows = two
        self.setFixedHeight(6 + self.ROW + (5 + self.ROW if two else 0) + 6)
        row2 = 6 + self.ROW + 5
        # the pill takes a row to itself when there are two, else the buttons share the first with the menu button
        by = row2 if (two and pill) else 6
        self.seg.setVisible(pill)
        self.drop.setVisible(not pill)
        if pill:
            self.seg.setGeometry(8, 6, w - 16 - (0 if two else bar + 6), self.ROW)
        else:
            self.drop.setGeometry(8, 6, min(self.drop.sizeHint().width() + 8, w - 16 - bar - 6), self.ROW)
        x = w - 8 - bar
        for k in names:
            self.buttons[k].setGeometry(x, by, bw, self.ROW)
            x += bw
        room = w - 16 - (bar + 6 if (two and pill) else 0)
        self.search.setVisible(two and room >= 80)
        self.search.setGeometry(8, row2, max(room, 10), self.ROW)


class DevicesWidget(QWidget):
    add_to_playlist = Signal(list)      # list[Path]
    play_now = Signal(list)             # list[Path]
    library_changed = Signal(object)    # a folder that gained files
    status = Signal(str, int)           # message, milliseconds

    def __init__(self, player, parent=None, monitor: DeviceMonitor | None = None):
        super().__init__(parent)
        self._player = player
        self._own = monitor is None
        self._mon = monitor if monitor is not None else DeviceMonitor(self)
        self._devs: list = []
        self._cards: dict[str, DeviceCard] = {}
        self._sel: str | None = None
        self._mode_of: dict[str, str] = {}          # device id -> the view chosen for it
        self._sub: dict[str, str] = {}              # iPhone id -> the media folder opened, '' for the top
        self._phone_views: dict[str, PhoneFilesView] = {}   # iPhone id -> its Files view, made when first asked for
        self._sets: dict[tuple, DeviceData] = {}    # (device id, subpath) -> that scan, indexed
        self._asked: dict[int, tuple] = {}          # request id -> its (device id, subpath)
        self._pending: dict[tuple, int] = {}
        self._waiting: dict[tuple, float] = {}      # scope -> when it started to wait for its first rows
        self._errors: dict[tuple, str] = {}
        self._building: set = set()
        self._keys: dict[str, tuple] = {}           # what makes the rows read from a device out of date
        self._jobs: dict[str, dict] = {}
        self._ctx: dict[str, dict] = {}
        self._after_mount: dict[str, tuple] = {}
        self._backups: dict[str, list] = {}
        self._quiet: set[str] = set()               # devices whose note was put away
        self._ask: dict | None = None
        self._source = None
        self._dest = Path.home() / "Music"
        self._started = self._active = self._ready = False
        self._ticks = 0
        self._quiet_until = time.monotonic() + 3
        self._shown: tuple | None = None
        self._page: tuple = ("empty", None)
        self._token = None
        self._bg = Background(self)
        self._restyle_timer = self._timer(0, self._restyle, once=True)
        self._tidy_timer = self._timer(1000, self._tidy)
        self._ready_timer = self._timer(200, self._poll_ready)
        self._filter_timer = self._timer(120, self._apply_filter, once=True)
        self._late_timer = self._timer(250, lambda: self._show_detail(), once=True)
        self._build()
        m = self._mon
        m.devices_changed.connect(self._on_devices)
        m.device_message.connect(self._on_message)
        m.scan_ready.connect(self._on_scan)
        m.scan_failed.connect(self._on_scan_failed)
        m.job_updated.connect(self._on_job)
        self._on_devices(m.devices())

    def _timer(self, ms: int, fn, once: bool = False) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(once)
        t.setInterval(ms)
        t.timeout.connect(fn)
        return t

    # building

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search this device")
        self._search.setToolTip("Matches title, artist, album and file name")
        sc = self._sc = Scheme(self, self._search)

        self._note = Banner(sc)
        self._note.closed.connect(lambda: (self._note.show_message("info", ""), self._chrome()))
        root.addWidget(self._note)
        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)     # it gives way to the question and the jobs
        root.addWidget(self._stack, 1)
        self._empty = StateView(sc)
        self._stack.addWidget(self._empty)
        self._body = QWidget()
        self._stack.addWidget(self._body)
        self._flow = QBoxLayout(QBoxLayout.LeftToRight, self._body)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setSpacing(0)

        self._area = QScrollArea()
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QFrame.NoFrame)
        self._area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        self._cards_lay = QVBoxLayout(host)
        self._cards_lay.setContentsMargins(8, 8, 8, 8)
        self._cards_lay.setSpacing(6)
        self._cards_lay.addStretch(1)
        self._area.setWidget(host)
        self._flow.addWidget(self._area)
        self._rule = _Divider(sc)
        self._flow.addWidget(self._rule)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)
        self._flow.addWidget(detail, 1)
        self._seg = Segmented(sc, [label for _, label in MODES["ipod"]])
        self._seg.changed.connect(self._set_mode)
        self._drop = IconButton(sc, "sort", "Songs", arrow=True)
        self._drop.clicked.connect(self._mode_menu)
        self._btn = {k: IconButton(sc, icon) for k, icon in (("to", "to_device"), ("from", "from_device"),
                                                             ("refresh", "refresh"), ("eject", "eject"),
                                                             ("more", "more"))}
        self._btn["from"].setToolTip("Copy the selected songs to this computer. With nothing selected, copies every song.")
        self._btn["refresh"].setToolTip("Look at the device again")
        self._btn["more"].setToolTip("More for this device")
        self._btn["to"].clicked.connect(self._copy_to_menu)
        self._btn["from"].clicked.connect(self._copy_from)
        self._btn["refresh"].clicked.connect(self.refresh)
        self._btn["eject"].clicked.connect(lambda: self._eject(self._sel))
        self._btn["more"].clicked.connect(lambda: self._device_menu(
            self._sel, self._btn["more"].mapToGlobal(QPoint(0, self._btn["more"].height()))))
        self._head = _Header(self._seg, self._drop, self._search, self._btn)
        dl.addWidget(self._head)
        self._act_find = self._search.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._act_clear = self._search.addAction(QIcon(), QLineEdit.TrailingPosition)
        self._act_clear.setVisible(False)
        self._act_clear.setToolTip("Clear search")
        self._act_clear.triggered.connect(self._search.clear)
        self._search.textChanged.connect(self._on_text)
        for key, fn in ((Qt.Key_Escape, self._search.clear), (Qt.Key_Down, self._focus_list)):
            sh = QShortcut(QKeySequence(key), self._search)
            sh.setContext(Qt.WidgetShortcut)
            sh.activated.connect(fn)
        self._progress = ProgressLine(sc)
        dl.addWidget(self._progress)
        self._crumb = IconButton(sc, "back", "Back to folders")
        self._crumb.clicked.connect(self._phone_back)
        self._crumb.hide()
        dl.addWidget(self._crumb, 0, Qt.AlignLeft)
        self._banner = Banner(sc)
        self._banner.closed.connect(self._put_away)
        dl.addWidget(self._banner)
        self._pages = QStackedWidget()
        dl.addWidget(self._pages, 1)
        self._list = DeviceView(sc)
        self._list.customContextMenuRequested.connect(self._list_menu)
        self._list.play_requested.connect(self._play)
        self._list.add_requested.connect(self._add)
        self._list.copy_requested.connect(self._copy_paths)
        self._list.song_activated.connect(self._play_one)
        self._list.head_activated.connect(self._open_phone_folder)
        self._list.rows_changed.connect(self._sync_footer)
        self._list.selectionModel().selectionChanged.connect(self._sync_footer)
        self._state = StateView(sc)
        self._state.action.connect(self._state_action)
        self._pages.addWidget(self._list)
        self._pages.addWidget(self._state)
        foot = QVBoxLayout()
        foot.setContentsMargins(8, 4, 8, 8)
        foot.setSpacing(5)
        self._status = StatusLine(sc)
        foot.addWidget(self._status)
        row = QHBoxLayout()
        row.setSpacing(6)
        self._b_play = PillButton(sc, "Play", primary=True)
        self._b_add = PillButton(sc, "Add to playlist")
        self._b_play.clicked.connect(self._play)
        self._b_add.clicked.connect(self._add)
        row.addWidget(self._b_play, 1)
        row.addWidget(self._b_add, 1)
        foot.addLayout(row)
        self._foot = QWidget()
        self._foot.setLayout(foot)
        dl.addWidget(self._foot)

        self._confirm = ConfirmBar(sc)
        self._confirm.accepted.connect(self._accepted)
        self._confirm.cancelled.connect(self._cancelled)
        root.addWidget(self._confirm)
        self._strip = JobStrip(sc)
        self._strip.cancel.connect(self._mon.cancel)
        self._strip.dismiss.connect(self._dismiss_job)
        root.addWidget(self._strip)
        self._painted = [self._seg, self._drop, *self._btn.values(), self._progress, self._status, self._b_play,
                         self._b_add, self._crumb, self._empty, self._state, self._note, self._banner,
                         self._confirm, self._strip, self._rule]
        self._restyle_timer.start()

    # theme and size

    def _restyle(self):
        """Read the live theme again and repaint. Runs a tick after a style change, once the children are polished."""
        sc = self._sc
        sc.refresh()
        self._act_find.setIcon(self._glyph_icon("search", sc.dimmer))
        self._act_clear.setIcon(self._glyph_icon("close", sc.dim))
        for w in self._painted:
            w.update()
            for c in (w.painted() if hasattr(w, "painted") else ()):
                c.update()
        for c in self._cards.values():
            c.update()
        self._list.viewport().update()
        self._relayout()                # a new font or size changes what fits

    def _glyph_icon(self, name: str, color) -> QIcon:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(14 * dpr), int(14 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        draw_glyph(p, name, QRectF(0, 0, 14, 14), color)
        p.end()
        return QIcon(pm)

    def changeEvent(self, e):
        if e.type() in (QEvent.PaletteChange, QEvent.StyleChange, QEvent.ApplicationPaletteChange,
                        QEvent.FontChange):
            self._restyle_timer.start()
        super().changeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._restyle_timer.start()
        self.set_active(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.set_active(False)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def minimumSizeHint(self):
        return QSize(220, 160)

    def _relayout(self):
        """Fit the panel to its size: cards beside or above the contents, how much of each card, the header
        and the footer."""
        w, h = self.width(), self.height()
        wide = w >= WIDE
        compact = h < 330 or (not wide and (len(self._devs) > 1 or h < 400))
        for c in self._cards.values():
            c.set_compact(compact)
        self._flow.setDirection(QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom)
        if wide:
            self._area.setFixedWidth(CARD_W)
            self._area.setMinimumHeight(0)
            self._area.setMaximumHeight(16777215)
            self._rule.setFixedSize(1, 16777215)
            self._rule.setMinimumHeight(0)
        else:
            self._area.setMinimumWidth(0)
            self._area.setMaximumWidth(16777215)
            content = sum(c.height() for c in self._cards.values()) + 6 * max(0, len(self._cards) - 1) + 16
            self._area.setFixedHeight(max(60, min(content, 70 if h < 230 else max(90, int(h * 0.38)))))
            self._rule.setFixedSize(16777215, 1)
            self._rule.setMinimumWidth(0)
        dw = w - (CARD_W if wide else 0)
        self._head.place(dw, h)
        self._b_add.setText("Add to playlist" if dw >= 300 else "Add")
        for b in (self._b_play, self._b_add):
            b.setVisible(h >= 250)
        self._foot.setVisible(self._page[0] == "list" and h >= 200)
        self._sync_strip()
        self._chrome()
        QTimer.singleShot(0, self._show_selected_card)

    def _chrome(self):
        """What is worth its room: notices only when the panel is tall enough, and when it is very short the question
        and the jobs have it to themselves."""
        h = self.height()
        asking, quiet = self._ask is not None, h < 300
        self._stack.setVisible(not (h < 230 and asking))
        self._note.setVisible(bool(self._note.text()) and not quiet)
        self._banner.set_lines(2 if h < 500 else 4)
        self._banner.setVisible(bool(self._banner.text()) and not quiet and self._page[0] != "empty")
        self._strip.setVisible(bool(self._strip.rows) and not (h < 230 and asking))

    def _show_selected_card(self):
        card = self._cards.get(self._sel)
        if card is not None:
            self._area.ensureWidgetVisible(card, 0, 4)

    # public

    def device_ids(self) -> list[str]:
        return [d.id for d in self._devs]

    def current_id(self) -> str | None:
        return self._sel

    def watch(self):
        """Start looking for devices without waiting for the panel to be shown, so that plugging one in is noticed."""
        if not self._started:
            self._started = True
            self._quiet_until = time.monotonic() + 3
            self._mon.start()
            if not self._ready:
                self._poll_ready()
                if not self._ready:
                    self._ready_timer.start()

    def set_active(self, on: bool):
        """The panel is visible: look for phones too and keep the clock that tidies the jobs. Hidden: stop both."""
        on = bool(on)
        if on:
            self.watch()
        if on == self._active:
            return
        self._active = on
        self._mon.set_active(on)
        if on:
            if not self._ready and self._started:
                self._ready_timer.start()
            self._sync_tidy()
            self._show_detail()
        else:
            self._ready_timer.stop()
            self._tidy_timer.stop()
            self._progress.set(None)

    def set_playlist_source(self, fn):
        """fn() gives the songs offered under "From the playlist": a list of paths, or (name, list of paths) so that
        the songs are copied as a playlist of that name too. Without it the player's queue is used."""
        self._source = fn

    def send_paths(self, device_id: str | None, paths: list[Path]) -> None:
        """Offer to copy these files and folders to a device. None means the only device that can take music, or
        the selected one when there are several."""
        paths = [Path(p) for p in paths]
        if not paths:
            return
        dev, why = self._target(device_id)
        if dev is None:
            self.status.emit(why, 6000)
            return
        self._offer_copy(dev, paths, choose=device_id is None)

    def refresh(self):
        self._mon.refresh()
        dev = self._cur()
        if dev is not None:
            self._stale(dev.id)
            if dev.id in self._phone_views and self._mode(dev) == "files":
                self._phone_views[dev.id].refresh()
            self._show_detail()

    def shutdown(self):
        for t in (self._restyle_timer, self._tidy_timer, self._ready_timer, self._filter_timer, self._late_timer):
            t.stop()
        for view in self._phone_views.values():
            view.shutdown()
        self._progress.set(None)
        self._token = None
        if self._own:
            self._mon.shutdown()

    # the devices

    def _cur(self):
        return self._by_id(self._sel)

    def _by_id(self, ident):
        return next((d for d in self._devs if d.id == ident), None)

    def _poll_ready(self):
        """The monitor says nothing when its first look finds nothing, so ask whether it has looked."""
        self._ticks += 1
        try:
            looked = bool(self._mon.tools().get("udisks"))
        except Exception:
            looked = True
        if looked or self._ticks > 25:
            self._ready = True
            self._ready_timer.stop()
            if self._active:
                self._show_detail()

    def _on_devices(self, devs):
        devs = list(devs)
        old = {d.id for d in self._devs}
        if time.monotonic() > self._quiet_until and self._mon.listed:      # the first look is what was there already
            for d in devs:
                if d.id not in old:
                    self.status.emit(f"{d.name or 'A device'} connected.", 5000)
        self._devs = devs
        if devs:
            self._ready = True
        ids = [d.id for d in devs]
        if self._sel not in ids:
            first = next((d for d in devs if d.mounted and d.kind != "iphone"), devs[0] if devs else None)
            self._sel = first.id if first else None
        for d in devs:
            key = (d.mounted, str(d.mount), d.details.get("library"), d.details.get("state"), d.details.get("can_write"))
            if self._keys.get(d.id) != key:
                self._keys[d.id] = key
                self._forget(d.id)
        for gone in [k for k in self._keys if k not in ids]:
            del self._keys[gone]
            self._forget(gone)
            self._backups.pop(gone, None)
        self._sync_cards()
        for ident in [i for i in self._after_mount if self._by_id(i) is None or self._by_id(i).mounted]:
            paths, profile, playlist = self._after_mount.pop(ident)
            dev = self._by_id(ident)
            if dev is not None:
                self._start_copy(dev, paths, profile, playlist)
        self._relayout()
        if self._active:                          # set_active(True) shows the detail, so a hidden panel reads nothing
            self._show_detail()

    def _sync_cards(self):
        want = {d.id for d in self._devs}
        for ident in [i for i in self._cards if i not in want]:
            card = self._cards.pop(ident)
            self._cards_lay.removeWidget(card)
            card.deleteLater()
        for i, d in enumerate(self._devs):
            card = self._cards.get(d.id)
            if card is None:
                card = self._cards[d.id] = DeviceCard(self._sc, d)
                card.clicked.connect(self._select)
                card.activated.connect(self._card_opened)
                card.eject.connect(self._eject)
                card.menu.connect(self._device_menu)
                card.dropped.connect(self._dropped)
            else:
                card.set_device(d)
            card.problem = receive_problem(d)
            card.set_job(self._busy_text(d.id))
            card.set_selected(d.id == self._sel)
            self._cards_lay.removeWidget(card)
            self._cards_lay.insertWidget(i, card)

    def _select(self, ident: str):
        if ident == self._sel or self._by_id(ident) is None:
            return
        self._sel = ident
        for c in self._cards.values():
            c.set_selected(c.dev.id == ident)
        self._search.clear()
        self._show_detail()
        QTimer.singleShot(0, self._show_selected_card)

    def _card_opened(self, ident: str):
        dev = self._by_id(ident)
        if dev is not None and not dev.mounted and not dev.details.get("locked"):
            self._mon.mount(ident)

    def _forget(self, ident: str):
        """What was read from a device is no longer true of it: it was plugged in again, or opened or paired."""
        for table in (self._sets, self._errors, self._waiting):
            for scope in [s for s in table if s[0] == ident]:
                del table[scope]
        for scope in [s for s in self._pending if s[0] == ident]:
            self._asked.pop(self._pending.pop(scope), None)
        self._building = {s for s in self._building if s[0] != ident}
        self._shown = None
        view = self._phone_views.pop(ident, None)
        if view is not None:
            view.shutdown()
            self._pages.removeWidget(view)
            view.deleteLater()

    def _stale(self, ident: str):
        """Read a device again but keep what is on screen until the new rows are in, as after a copy."""
        for scope in [s for s in self._sets if s[0] == ident]:
            self._request_scan(scope)
        self._load_backups(ident)

    # what to show

    def _mode(self, dev) -> str:
        keys = [k for k, _ in MODES.get(dev.kind, MODES["usb"])]
        mode = self._mode_of.get(dev.id)
        if mode not in keys:
            blank = dev.kind == "ipod" and (dev.details.get("rockbox") or dev.details.get("blank"))
            mode = "files" if dev.kind == "usb" or blank else "songs"
        return mode

    def _scope(self, dev) -> tuple:
        if dev.kind == "iphone":
            return dev.id, self._sub.get(dev.id, "")
        return dev.id, "." if dev.kind == "ipod" and self._mode(dev) == "files" else ""

    def _busy_text(self, ident: str) -> str:
        for j in reversed(list(self._jobs.values())):
            if j.get("device") == ident and j["state"] in RUNNING:
                return j.get("text") or VERBS.get(j.get("kind"), ("Working",))[0]
        return ""

    def _opening(self, ident: str) -> bool:
        return any(j.get("device") == ident and j["state"] in RUNNING and j.get("kind") == "mount"
                   for j in self._jobs.values())

    def _gate(self, dev):
        """What stands between a device and its contents: (page, glyph, title, hint, button), or None when it is open."""
        d, busy = dev.details, self._opening(dev.id)
        glyph = dev.glyph or ("phone" if dev.kind == "iphone" else "usb")
        if dev.kind == "iphone" and not dev.mounted:
            thing, state = d.get("family") or "iPhone", d.get("state", "unpaired")
            if busy:
                title = PHONE_TITLES["waiting"].format(thing) if state == "waiting" else f"Opening the {thing}"
                return "pair-wait", glyph, title, d.get("message", ""), "Cancel"
            if state == "paired":
                return "open", glyph, f"{dev.name or thing} is paired", "Open it to see its music and folders.", "Open"
            title = PHONE_TITLES.get(state, PHONE_TITLES["unpaired"]).format(dev.name or thing if state == "unpaired" else thing)
            return "pair", glyph, title, d.get("message", ""), "Pair"
        if d.get("locked"):
            return "locked", "lock", "This drive is encrypted", "Open it in your file manager first, then it shows up here.", ""
        if not dev.mounted:
            if busy:
                return "mounting", glyph, f"Opening {dev.name}", "", ""
            return "mount", glyph, f"{dev.name} is not mounted", "Mount it to see what is on it.", "Mount"
        return None

    def _show_detail(self):
        """Decide what the contents side shows for the selected device and put it there."""
        dev = self._cur()
        if dev is None:
            self._stack.setCurrentIndex(0)
            self._empty.show_message(*self._empty_text())
            self._page = ("empty", None)
            self._progress.set(None)
            self._after_page(None)
            return
        self._stack.setCurrentIndex(1)
        busy = False
        gate = self._gate(dev)
        if gate:
            self._page = (gate[0], dev.id)
            self._state.show_message(*gate[1:])
            self._pages.setCurrentIndex(1)
            busy = gate[0] in ("mounting", "pair-wait")
        elif dev.kind == "iphone" and self._mode(dev) == "files":
            self._show_files(dev)
            self._after_page(dev)
            return
        else:
            scope = self._scope(dev)
            data = self._sets.get(scope)
            if data is not None:
                self._page = ("list", scope)
                self._apply_list(dev, data, scope)
                self._pages.setCurrentIndex(0)
                if not self._list.rows:
                    self._state.show_message(*self._nothing(dev, scope, data))
                    self._pages.setCurrentIndex(1)
                busy = scope in self._pending
            elif scope in self._errors:
                self._page = ("error", scope)
                self._state.show_message("warn", "The device could not be read", self._errors[scope], "Try again")
                self._pages.setCurrentIndex(1)
            else:
                self._page = ("loading", scope)
                self._request_scan(scope)
                waited = time.monotonic() - self._waiting.get(scope, time.monotonic())
                if waited < 0.25:                       # a quick scan never shows a page of its own
                    self._state.show_message("", "")
                    self._late_timer.start(int((0.25 - waited) * 1000) + 20)
                else:
                    self._state.show_message(dev.glyph or "disc", f"Reading {dev.name}",
                                             "A big library can take a minute." if dev.kind == "ipod" else "")
                self._pages.setCurrentIndex(1)
                busy = True
        self._progress.set(-1 if busy and self._active else None)
        self._after_page(dev)

    def _show_files(self, dev):
        """The Files view of an open iPhone: its media area and the file sharing folder of every app."""
        view = self._phone_views.get(dev.id)
        if view is None:
            view = self._phone_views[dev.id] = PhoneFilesView(dev.id, dev.name)
            view.add_to_playlist.connect(self.add_to_playlist)
            view.play_now.connect(self.play_now)
            view.library_changed.connect(self.library_changed)
            view.status.connect(lambda text: self.status.emit(text, 6000))
            self._pages.addWidget(view)
        view.set_name(dev.name)
        view.set_mount(dev.mount)
        self._page = ("files", dev.id)
        self._pages.setCurrentWidget(view)
        self._progress.set(None)

    def _after_page(self, dev):
        if dev is not None:
            self._sync_header(dev)
        self._sync_buttons()
        self._sync_footer()
        self._chrome()

    def _nothing(self, dev, scope: tuple, data: DeviceData) -> tuple:
        """The page for a list with no rows in it: nothing matches, or nothing is there."""
        q = self._search.text().strip()
        if q:
            return "search", f"No matches for \u201c{q}\u201d", "Try a title, artist, album or file name.", "Clear search"
        if dev.kind == "iphone":
            where = "this folder" if scope[1] else "the phone's music folder"
            return "note", f"No music in {where}", "Voice memos and other files are under Folders.", ""
        hint = "Drop songs on its card to put some on it." if not receive_problem(dev) else ""
        blank = dev.details.get("blank") or data.info.get("blank")
        return "note", ("This iPod has no songs yet" if dev.kind == "ipod" and blank
                        else f"No music found on {dev.name}"), hint, "Copy music" if hint else ""

    def _empty_text(self) -> tuple:
        if not self._ready:
            return "usb", "Looking for devices", "", ""
        try:
            tools = self._mon.tools()
        except Exception:
            tools = {}
        hint = "Plug in a USB stick, SD card, external drive, iPod or iPhone and it shows up here."
        if tools.get("udisks") == "mounts":
            hint += " Drives are found only when they are already mounted, because UDisks2 is not running."
        if tools and not tools.get("idevice", True):
            hint += " " + tools.get("idevice_hint", idevice.INSTALL_HINT)
        return "usb", "No devices connected", hint, ""

    def _apply_list(self, dev, data: DeviceData, scope: tuple):
        mode = self._mode(dev)
        top = dev.kind == "iphone" and mode == "folders" and not scope[1]
        key = (scope, id(data), mode, self._search.text().strip().lower())
        if key != self._shown:
            self._shown = key
            view = "folders" if mode == "files" else "songs" if dev.kind == "iphone" else mode
            self._list.show_rows(data, view, folder_rows(data.info.get("folders", [])) if top else None,
                                 self._search.text())

    def _sync_header(self, dev):
        listing = self._page[0] in ("list", "loading", "files")
        self._head.setVisible(listing)
        self._foot.setVisible(self._page[0] == "list" and self.height() >= 200)
        modes = MODES.get(dev.kind, MODES["usb"])
        keys = [k for k, _ in modes]
        mode = self._mode(dev)
        self._seg.labels = [label for _, label in modes]
        self._seg.set_current(keys.index(mode))
        self._seg.updateGeometry()
        self._drop.setText(modes[keys.index(mode)][1])
        self._drop.updateGeometry()
        self._crumb.setVisible(dev.kind == "iphone" and bool(self._sub.get(dev.id)) and self._page[0] != "files")
        self._search.setEnabled(self._page[0] == "list")
        self._search.setPlaceholderText("Filter in the list below" if self._page[0] == "files" else "Search this device")
        w = self.width() - (CARD_W if self.width() >= WIDE else 0)
        self._head.place(w, self.height())
        d, text, kind = dev.details, "", "info"
        if dev.kind == "ipod" and dev.mounted:
            if not d.get("can_write"):
                text, kind = d.get("why_not", ""), "warn"
            elif d.get("rockbox"):
                text = "This iPod runs Rockbox, so songs are copied as files into its Music folder."
            else:
                text = d.get("note", "")
        elif dev.kind == "iphone" and dev.mounted and dev.id not in self._quiet:
            text = d.get("note", idevice.IOS_NOTE).split(". ")[0].rstrip(".") + "."
        elif dev.kind == "usb" and dev.mounted and dev.read_only:
            text, kind = receive_problem(dev), "warn"
        self._banner.show_message(kind, text, closable=dev.kind == "iphone")
        self._chrome()

    def _put_away(self):
        if self._sel:
            self._quiet.add(self._sel)
        self._banner.show_message("info", "")
        self._chrome()

    def _sync_buttons(self):
        dev, b = self._cur(), self._btn
        has = dev is not None
        why = receive_problem(dev) if has else ""
        b["to"].setEnabled(has and not why)
        b["to"].setToolTip(why or (f"Copy music to {dev.name}" if has else "Copy music to a device"))
        b["refresh"].setEnabled(has)
        b["more"].setEnabled(has)
        b["eject"].setEnabled(has and (dev.kind != "iphone" or dev.mounted))
        b["eject"].setToolTip(f"Eject {dev.name}" if has else "Eject")
        self._seg.setEnabled(has and dev.mounted)
        self._drop.setEnabled(has and dev.mounted)

    def _sync_footer(self, *_):
        dev, listing = self._cur(), self._page[0] == "list"
        n = len(self._list.songs_of(self._list.selected())) if listing else 0
        self._b_play.setEnabled(n > 0)
        self._b_add.setEnabled(n > 0)
        self._btn["from"].setEnabled(bool(dev and dev.mounted and listing and self._list.rows and
                                          self._list.fixed_rows is None))
        self._status.set_texts(self._counts() if listing else "", f"{n:,} selected" if n else "")

    def _counts(self) -> str:
        view, data = self._list, self._list.data
        if view.fixed_rows is not None:
            return plural(len(view.fixed_rows), "folder")
        if data is None:
            return ""
        total, mode = len(data.items), view.mode
        text = plural(total, "song") if view.hits is None else f"{len(view.hits):,} of {total:,} songs"
        if view.hits is None and mode in ("albums", "artists"):
            text = f"{plural(len(getattr(data, mode)), mode[:-1])}, {text}"
        elif mode == "playlists":
            text = plural(len(data.playlists), "playlist")
        if data.info.get("truncated"):
            text += ", the first 50,000 only"
        if data.info.get("missing"):
            text += f", {plural(data.info['missing'], 'listed song')} missing from disk"
        return text

    # scanning

    def _request_scan(self, scope: tuple):
        if scope in self._pending or scope in self._building:
            return
        self._waiting.setdefault(scope, time.monotonic())
        rid = self._mon.scan(*scope)
        self._pending[scope] = rid
        self._asked[rid] = scope

    def _on_scan(self, rid, items, info):
        scope = self._asked.pop(rid, None)
        if scope is None or self._pending.get(scope) != rid:
            return                              # something that was asked for before the device changed
        del self._pending[scope]
        self._errors.pop(scope, None)
        self._building.add(scope)
        self._bg.run(DeviceData, partial(self._built, scope), items, info)

    def _built(self, scope, data, err):
        if scope not in self._building:
            return                              # the device was forgotten while this was being made
        self._building.discard(scope)
        self._waiting.pop(scope, None)
        if err is not None or data is None:
            self._errors[scope] = "The list of songs could not be read."
        else:
            self._sets[scope] = data
            self._load_backups(scope[0])
        dev = self._cur()
        if dev is not None and self._scope(dev) == scope:
            self._show_detail()

    def _on_scan_failed(self, rid, text):
        scope = self._asked.pop(rid, None)
        if scope is None or self._pending.get(scope) != rid:
            return
        del self._pending[scope]
        self._waiting.pop(scope, None)
        self._errors[scope] = text
        dev = self._cur()
        if dev is not None and self._scope(dev) == scope:
            self.status.emit(f"{dev.name}: {text}", 6000)
            self._show_detail()

    def _load_backups(self, ident: str):
        dev = self._by_id(ident)
        if dev is not None and dev.kind == "ipod" and dev.details.get("can_write") and dev.mounted:
            self._bg.run(self._mon.backups, partial(self._got_backups, ident), ident)

    def _got_backups(self, ident, res, err):
        if err is None and res is not None:
            self._backups[ident] = res

    def _on_text(self, text: str):
        self._act_clear.setVisible(bool(text))
        self._filter_timer.start()

    def _apply_filter(self):
        self._show_detail()

    def _focus_list(self):
        v = self._list
        if v.model().rowCount():
            if not v.currentIndex().isValid():
                v.setCurrentIndex(v.model().index(0, 0))
            v.setFocus()

    # views

    def _set_mode(self, idx: int):
        dev = self._cur()
        if dev is None:
            return
        self._mode_of[dev.id] = MODES.get(dev.kind, MODES["usb"])[idx][0]
        if dev.kind == "iphone":
            self._sub[dev.id] = ""
        self._search.clear()
        self._show_detail()

    def _mode_menu(self):
        dev = self._cur()
        if dev is None:
            return
        menu = QMenu(self)
        for i, (key, label) in enumerate(MODES.get(dev.kind, MODES["usb"])):
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == self._mode(dev))
            a.triggered.connect(lambda _=False, i=i: self._set_mode(i))
        menu.exec(self._drop.mapToGlobal(QPoint(0, self._drop.height())))

    def _open_phone_folder(self, head: Head):
        dev = self._cur()
        if dev is None:
            return
        name = head.key.split(":", 1)[1]
        self._mode_of[dev.id] = "songs" if name == PHONE_MUSIC else "folders"
        self._sub[dev.id] = "" if name == PHONE_MUSIC else name
        self._show_detail()

    def _phone_back(self):
        dev = self._cur()
        if dev is not None:
            self._sub[dev.id] = ""
            self._mode_of[dev.id] = "folders"
            self._show_detail()

    def _state_action(self):
        dev, kind = self._cur(), self._page[0]
        if dev is None:
            return
        if kind in ("mount", "open", "pair"):
            self._mon.mount(dev.id)
        elif kind == "pair-wait":
            for j in self._jobs.values():
                if j.get("device") == dev.id and j["state"] in RUNNING and j.get("kind") == "mount":
                    self._mon.cancel(j["id"])
        elif kind == "error":
            self._forget(dev.id)
            self._show_detail()
        elif kind == "list":
            if self._search.text().strip():
                self._search.clear()
            else:
                self._copy_to_menu()

    # what is chosen

    def _selected_ids(self) -> list[int]:
        return self._list.songs_of(self._list.selected())

    def _paths_of(self, ids: list[int]) -> list[Path]:
        items = self._list.data.items if self._list.data is not None else []
        return [Path(items[i]["path"]) for i in ids]

    def _play(self, *_):
        paths = self._paths_of(self._selected_ids())
        if paths:
            self.play_now.emit(paths)
            self.status.emit("Playing " + (paths[0].name if len(paths) == 1 else plural(len(paths), "song")), 3000)

    def _play_one(self, item: int):
        paths = self._paths_of([item])
        if paths:
            self.play_now.emit(paths)

    def _add(self, *_):
        paths = self._paths_of(self._selected_ids())
        if paths:
            self.add_to_playlist.emit(paths)
            self.status.emit(f"Added {plural(len(paths), 'song')} to the playlist.", 3000)

    def _copy_paths(self):
        paths = self._paths_of(self._selected_ids())
        if paths:
            mime = QMimeData()
            mime.setText("\n".join(str(p) for p in paths))
            mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
            QGuiApplication.clipboard().setMimeData(mime)

    def _list_menu(self, pos):
        v = self._list
        ix = v.indexAt(pos)
        if not ix.isValid():
            return
        sm, flags = v.selectionModel(), v.selectionModel().SelectionFlag
        if not sm.isSelected(ix):
            sm.select(ix, flags.ClearAndSelect | flags.Rows)
        sm.setCurrentIndex(ix, flags.NoUpdate)
        if not self._selected_ids():
            return
        menu = QMenu(self)
        menu.addAction("\U000f040a  Play now", self._play)
        menu.addAction("\U000f0412  Add to playlist", self._add)
        menu.addSeparator()
        menu.addAction("\U000f01da  Copy to this computer", self._copy_from)
        menu.addSeparator()
        menu.addAction("\U000f0770  Show in file manager", self._reveal)
        menu.addAction("\U000f0339  Copy path", self._copy_paths)
        menu.exec(v.viewport().mapToGlobal(pos))

    def _reveal(self):
        paths = self._paths_of(self._selected_ids())
        if paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[0].parent)))

    # copying to a device

    def _target(self, ident: str | None):
        """The device a send goes to: (device, '') or (None, why not)."""
        if ident is not None:
            dev = self._by_id(ident)
            if dev is None:
                return None, "That device is not connected any more."
            return (None, receive_problem(dev)) if receive_problem(dev) else (dev, "")
        ok = [d for d in self._devs if not receive_problem(d)]
        if not ok:
            return None, "There is no device to copy to. Plug one in first."
        sel = self._by_id(self._sel)
        return (sel if sel in ok else ok[0]), ""

    def _playlist(self) -> tuple[str, list[Path]]:
        if self._source is not None:
            try:
                got = self._source()
                name, paths = got if isinstance(got, tuple) else ("", got)
                return name, [Path(p) for p in paths]
            except Exception:
                pass
        return "", [t.path for t in getattr(self._player, "queue", [])]

    def _copy_to_menu(self):
        dev = self._cur()
        if dev is None or receive_problem(dev):
            return
        name, paths = self._playlist()
        menu = QMenu(self)
        a = menu.addAction(f"\U000f0cb8  From the playlist ({plural(len(paths), 'song')})",
                           lambda: self._offer_copy(dev, paths, playlist=name))
        a.setEnabled(bool(paths))
        menu.addAction("\U000f0752  Choose files", lambda: self._pick_files(dev))
        menu.addAction("\U000f024b  Choose a folder", lambda: self._pick_folder(dev))
        menu.exec(self._btn["to"].mapToGlobal(QPoint(0, self._btn["to"].height())))

    def _pick_files(self, dev):
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(self, f"Copy music to {dev.name}", str(self._dest),
                                                f"Audio ({exts});;All files (*)")
        if paths:
            self._dest = Path(paths[0]).parent
            self._offer_copy(dev, [Path(p) for p in paths])

    def _pick_folder(self, dev):
        d = QFileDialog.getExistingDirectory(self, f"Copy a folder to {dev.name}", str(self._dest))
        if d:
            self._dest = Path(d).parent
            self._offer_copy(dev, [Path(d)])

    def _dropped(self, ident: str, paths: list):
        dev = self._by_id(ident)
        if dev is None:
            return
        root = str(dev.mount) + "/" if dev.mount else None
        if root and all(str(p).startswith(root) for p in paths):
            self.status.emit(f"Those files are on {dev.name} already.", 4000)
            return
        self._select(ident)
        self._offer_copy(dev, [Path(p) for p in paths])

    def _offer_copy(self, dev, paths: list[Path], playlist: str = "", choose: bool = False):
        """Count the music in paths off the GUI thread, then ask."""
        self._token = token = object()
        self._bg.run(_count_music, partial(self._counted, token, dev.id, paths, playlist, choose), paths)

    def _counted(self, token, ident, paths, playlist, choose, n, err):
        dev = self._by_id(ident)
        if token is not self._token or dev is None:
            return
        if not n:
            self.status.emit("There is no music in what was chosen.", 5000)
            return
        songs, d = plural(n, "song"), dev.details
        library = dev.kind == "ipod" and d.get("can_write") and not d.get("rockbox")
        if not dev.mounted:
            text = f"{dev.name} is not mounted. Mount it and copy {songs}?"
        elif library:
            text = f"Add {songs} to the library of {dev.name}? A backup of its library is made first."
        elif dev.kind == "ipod" and not d.get("rockbox"):
            text = f"Copy {songs} to the disk of {dev.name}? They will not show in its own menus."
        else:
            text = f"Copy {songs} to {dev.name}?"
            if playlist:
                text += f" They also become the playlist {playlist}."
        try:
            if not self._mon.tools().get("ffmpeg", True):
                text += " ffmpeg is not installed, so songs that need converting are skipped."
            profiles = self._mon.profiles(dev.id)
        except Exception:
            profiles = []
        others = [x for x in self._devs if not receive_problem(x)]
        choices = [(x.id, x.name) for x in others] if choose and len(others) > 1 else []
        self._ask = {"kind": "copy", "device": dev.id, "paths": paths, "playlist": playlist}
        self._confirm.ask(text, "Copy", profiles if len(profiles) > 1 else None, "auto", choices, dev.id)
        self._chrome()

    def _accepted(self):
        ask, self._ask = self._ask, None
        self._confirm.hide()
        self._chrome()
        if not ask:
            return
        if ask["kind"] == "copy":
            dev = self._by_id(self._confirm.value("device") or ask["device"])
            if dev is None:
                self.status.emit("That device is not connected any more.", 5000)
                return
            profile = self._confirm.value("profile") or "auto"
            if not dev.mounted:
                self._after_mount[dev.id] = (ask["paths"], profile, ask["playlist"])
                self._mon.mount(dev.id)
            else:
                self._start_copy(dev, ask["paths"], profile, ask["playlist"])
        elif ask["kind"] == "eject":
            self._mon.eject(ask["device"])
        elif ask["kind"] == "restore":
            self._mon.restore(ask["device"], ask["backup"])

    def _cancelled(self):
        self._ask = None
        self._confirm.hide()
        self._chrome()

    def _start_copy(self, dev, paths: list[Path], profile: str, playlist: str):
        self._mon.copy_to(dev.id, paths, profile=profile, **({"playlist": playlist} if playlist else {}))

    # copying from a device

    def _copy_from(self):
        dev, data = self._cur(), self._list.data
        if dev is None or data is None or not dev.mounted:
            return
        ids = self._selected_ids() or self._list.all_songs()
        if not ids:
            self.status.emit("There are no songs to copy.", 4000)
            return
        d = QFileDialog.getExistingDirectory(self, f"Copy {plural(len(ids), 'song')} from {dev.name} to",
                                             str(self._dest))
        if d:
            self._dest = Path(d)
            jid = self._mon.copy_from(dev.id, [data.items[i] for i in ids], Path(d))
            self._ctx[jid] = {"dest": Path(d)}

    # mount, eject, restore, menus

    def _queued_from(self, dev) -> int:
        if dev.mount is None:
            return 0
        roots = [str(dev.mount) + "/"]
        view = self._phone_views.get(dev.id)
        if view is not None and view.apps_root() is not None:        # songs played from an app's folder
            roots.append(str(view.apps_root()) + "/")
        return sum(1 for t in getattr(self._player, "queue", []) if str(t.path).startswith(tuple(roots)))

    def _eject(self, ident):
        dev = self._by_id(ident)
        if dev is None:
            return
        view = self._phone_views.get(ident)
        if view is not None and view.busy():
            self.status.emit(f"A copy to or from {dev.name} is still running. Wait for it or cancel it first.", 6000)
            return
        n = self._queued_from(dev)
        if not n:
            self._mon.eject(dev.id)
            return
        self._ask = {"kind": "eject", "device": dev.id}
        self._confirm.ask(f"{plural(n, 'song')} from {dev.name} {'is' if n == 1 else 'are'} in the playlist. "
                          "They stop playing once it is ejected.", "Eject anyway")
        self._chrome()

    def _device_menu(self, ident, pos):
        dev = self._by_id(ident)
        if dev is None:
            return
        menu = QMenu(self)
        if not dev.mounted and not dev.details.get("locked"):
            pair = dev.kind == "iphone" and dev.details.get("state") != "paired"
            menu.addAction("\U000f0337  Pair" if pair else "\U000f0770  Open", lambda: self._mon.mount(dev.id))
        if dev.mounted and dev.kind != "iphone":
            menu.addAction("\U000f0338  Unmount", lambda: self._mon.unmount(dev.id)).setEnabled(
                not self._busy_text(dev.id))
        if dev.kind != "iphone" or dev.mounted:
            menu.addAction("\U000f01ea  Eject", lambda: self._eject(dev.id))
        menu.addSeparator()
        if dev.mount:
            menu.addAction("\U000f0770  Show in file manager",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(dev.mount))))
            menu.addAction("\U000f0339  Copy path", lambda: QGuiApplication.clipboard().setText(str(dev.mount)))
        if dev.kind == "ipod" and dev.details.get("can_write"):
            sub = menu.addMenu("\U000f006f  Restore the library")
            backups = self._backups.get(dev.id) or []
            if not backups:
                sub.addAction("No backups yet").setEnabled(False)
            for b in backups:
                label = b["when"] + (f", {plural(b['tracks'], 'song')}" if b.get("tracks") else "")
                sub.addAction(label, lambda _=False, b=b: self._ask_restore(dev, b))
        menu.addSeparator()
        menu.addAction("\U000f0450  Refresh", self.refresh)
        menu.exec(pos)

    def _ask_restore(self, dev, backup: dict):
        self._ask = {"kind": "restore", "device": dev.id, "backup": backup["id"]}
        self._confirm.ask(f"Put the library of {dev.name} back as it was on {backup['when']}? Songs added since "
                          "then stop showing in its menus.", "Restore")
        self._chrome()

    # messages and jobs

    def _on_message(self, ident: str, text: str):
        if text:
            if not ident:
                self._note.show_message("info", text, closable=True)
                self._chrome()
            self.status.emit(text, 6000)

    def _job_row(self, j: dict) -> dict:
        verbs = VERBS.get(j.get("kind"), ("Working on", "Finished", "Could not finish", "Stopped"))
        dev = self._by_id(j.get("device"))
        j["name"] = dev.name if dev else j.get("name", "the device")
        state = j["state"]
        frac = j.get("percent", 0) / 100.0 if j.get("total_files") or state == "cancelled" else \
            (1.0 if state == "done" else None)
        verb = verbs[0] if state in RUNNING else verbs[{"done": 1, "error": 2}.get(state, 3)]
        return {"id": j["id"], "title": f"{verb} {j['name']}",
                "text": j.get("error") or j.get("text", ""), "state": state, "frac": frac}

    def _on_job(self, jid: str, st):
        prev = self._jobs.get(jid)
        j = dict(st)
        j["at"] = prev["at"] if prev else time.monotonic()
        finished = j["state"] in ("done", "error", "cancelled")
        if finished:
            j["done_at"] = (prev or {}).get("done_at") or time.monotonic()
        self._jobs[jid] = j
        ident = j.get("device")
        if ident in self._cards:
            self._cards[ident].set_job(self._busy_text(ident))
        self._sync_strip()
        if finished and not (prev and prev["state"] in ("done", "error", "cancelled")):
            self._job_finished(j)
        elif ident == self._sel and j.get("kind") == "mount":
            self._show_detail()

    def _job_finished(self, j: dict):
        dev = self._by_id(j.get("device"))
        name = dev.name if dev else j.get("name", "The device")
        kind, state = j.get("kind"), j["state"]
        if state == "done":
            if j.get("text"):
                self.status.emit(f"{name}: {j['text']}", 8000)
            if kind in ("copy_to", "restore") and dev is not None:
                self._stale(dev.id)
                self._mon.refresh()
            elif kind == "copy_from":
                ctx = self._ctx.pop(j["id"], None)
                if ctx:
                    self.library_changed.emit(ctx["dest"])
        else:
            self._after_mount.pop(j.get("device"), None)
            self.status.emit(f"{name}: " + (j.get("error") or j.get("text") or "That did not work.") if state == "error"
                             else f"{name}: Cancelled.", 10000 if state == "error" else 3000)
        if self._active:
            self._show_detail()
        self._sync_tidy()

    def _sync_strip(self):
        rows = [self._job_row(j) for j in sorted(self._jobs.values(), key=lambda j: j["at"])]
        self._strip.set_rows(rows, 1 if self.height() < 300 else 2 if self.height() < 700 else 3)
        self._chrome()

    def _sync_tidy(self):
        if self._active and any(j["state"] in ("done", "cancelled") for j in self._jobs.values()):
            self._tidy_timer.start()
        else:
            self._tidy_timer.stop()

    def _tidy(self):
        """Finished jobs leave the strip after a while. A failed one stays until it is dismissed."""
        now = time.monotonic()
        for jid, j in list(self._jobs.items()):
            keep = KEEP.get(j.get("kind"), 6) * (0.4 if j["state"] == "cancelled" else 1)
            if j["state"] in ("done", "cancelled") and now - j["done_at"] > keep:
                del self._jobs[jid]
        self._sync_strip()
        self._sync_tidy()

    def _dismiss_job(self, jid: str):
        self._jobs.pop(jid, None)
        self._ctx.pop(jid, None)
        self._sync_strip()
        self._sync_tidy()

