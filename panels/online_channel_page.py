"""YouTube style channel and playlist pages for the Online panel.

A page shows one channel or playlist: its picture, name and numbers, the tabs, a search box, and the videos
in pages of 30 that load as the list is scrolled to the end. It is made of the panel's own parts (result rows,
chips, skeleton, state view) and only asks and tells. The panel owns the history and does the playing, saving
and queueing as it does for its own list, and the fetching and decoding are in online_channel.

A page remembers the last few channels it showed, with their tabs, loaded pages and scroll positions, so
Back and Forward come up at once without asking the site again.
"""
from __future__ import annotations

import functools
from collections import OrderedDict

from PySide6.QtCore import QItemSelectionModel, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontMetrics, QGuiApplication, QIcon, QKeySequence, QPainter, QPainterPath, QPixmap, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from library_widgets import PillButton, ProgressLine, Scheme, _Button, scaled_font
from online_channel import (MAX_ITEMS, PAGE, SORTS, TAB_IDS, TAB_LABEL, TRANSIENT, ChannelFetcher, Target, can_open_channel,
                            format_count, merge)
from online_providers import normalize_url
from online_service import ERROR_ACTIONS
from online_widgets import (CONTAINERS, ChipBar, ElidedLabel, IconButton, ResultsModel, ResultsView, Skeleton, StateView,
                            ThumbLoader, WrapLabel, draw_icon, fmt_age, plural, wrap_lines)

STACK_LIST, STACK_LOADING, STACK_STATE = range(3)
AVATAR = 56                 # pixels of the picture at full size, 40 when the page is short
LONG_LINES = 9              # lines of a description that is opened
# page height from which each part shows, so that on a small panel the list keeps the room
SHOW_PICTURE, SHOW_ABOUT, SHOW_SEARCH, SHOW_ACTIONS = 340, 460, 300, 260

_TITLE = {
    "no_ytdlp": "yt-dlp is missing", "network": "No connection", "login_required": "Sign in needed",
    "age_restricted": "Age check", "geo_blocked": "Not available here", "private": "This is private",
    "unavailable": "Not available", "rate_limited": "Too many requests", "unsupported_url": "Cannot open this",
    "no_media": "Nothing to play here", "timeout": "No answer", "failed": "That did not work",
}
_ICON = {"login_required": "lock", "age_restricted": "lock", "private": "lock", "unsupported_url": "link"}
_BUTTON = {"sign_in": "Sign in", "update": "Update", "retry": "Try again"}
_EMPTY = {"videos": ("No videos", "This channel has not posted any."), "shorts": ("No Shorts", "This channel has not posted any."),
          "live": ("No live streams", "This channel has none to show."), "playlists": ("No playlists", "This channel has made none.")}


class LinkButton(_Button):
    """A line of text that is a button: the uploader of a playlist, More and Less."""

    def __init__(self, sc: Scheme, text: str = "", bold: bool = False, parent=None):
        super().__init__(sc, parent)
        self._bold = bold
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _font(self):
        f = self.font()
        f.setBold(self._bold)
        return f

    def sizeHint(self):
        return QSize(QFontMetrics(self._font()).horizontalAdvance(self.text()) + 8, QFontMetrics(self._font()).height() + 6)

    def setText(self, text: str):
        super().setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()

    def paintEvent(self, _):
        p = QPainter(self)
        f = self._font()
        hot = self.underMouse() or self.isDown() or (self.hasFocus() and self._ring)
        f.setUnderline(hot)
        p.setFont(f)
        p.setPen(self.sc.fg if hot or self._bold else self.sc.win_dim)
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft, QFontMetrics(f).elidedText(self.text(), Qt.ElideRight, self.width()))


class Avatar(QWidget):
    """The round picture of a channel, or the square cover of a playlist, from the picture loader."""

    def __init__(self, sc: Scheme, loader: ThumbLoader, round_: bool = True, parent=None):
        super().__init__(parent)
        self.sc, self._loader, self._round, self._url = sc, loader, round_, ""
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_size(AVATAR)
        loader.ready.connect(self.update)

    def set_size(self, px: int):
        self.setFixedSize(px, px)

    def set_url(self, url: str):
        self._url = url or ""
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        if self._round:
            path.addEllipse(r)
        else:
            path.addRoundedRect(r, 6, 6)
        pm = self._loader.get(self._url) if self._url else False
        if pm is None:
            self._loader.request(self._url)
        if isinstance(pm, QPixmap):
            p.setClipPath(path)
            p.drawPixmap(self.rect(), pm)
            return
        p.fillPath(path, self.sc.ph)
        s = self.width() * 0.42
        draw_icon(p, "user" if self._round else "rows", QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s), self.sc.dimmer)


class _Tab:
    """What one tab, or one search, holds so far. state: idle, loading, ready, empty, error."""
    __slots__ = ("items", "more", "next", "total", "state", "code", "message", "scroll")

    def __init__(self):
        self.items, self.more, self.next, self.total = [], False, 1, None
        self.state, self.code, self.message, self.scroll = "idle", "", "", 0


class _View:
    """One channel or playlist as the page last showed it. head: idle, loading, ready, error."""

    def __init__(self, target: Target):
        self.target, self.header = target, None
        self.head, self.code, self.message = "idle", "", ""
        self.tabs: dict[tuple, _Tab] = {}
        self.tab, self.sort, self.query = "videos", "latest", ""
        self.picked = False                 # the user chose a tab, so the page does not pick one for them
        self.hint = ""                      # the tab a pasted link named, asked for first

    def key(self) -> tuple:
        if self.query:
            return "search", "latest", self.query
        return self.tab, self.sort if self.tab == "videos" else "latest", ""

    def cur(self) -> _Tab:
        return self.tabs.setdefault(self.key(), _Tab())

    def hidden(self, tab: str) -> bool:
        """A tab with nothing in it, or that failed for good, is not offered while another one is open."""
        t = self.tabs.get((tab, "latest", ""))
        if t is None or (tab == self.tab and not self.query):
            return False
        return t.state == "empty" or (t.state == "error" and t.code not in TRANSIENT)


class _Page(QWidget):
    play_now = Signal(list)                 # items
    shuffle = Signal(list)
    play_next = Signal(list)
    add_to_playlist = Signal(list)
    save = Signal(list)
    cancel_item = Signal(dict)
    open_channel = Signal(str)              # link
    open_playlist = Signal(str)
    back = Signal()
    forward = Signal()
    sign_in = Signal(str)                   # provider id
    update_tools = Signal()
    shown = Signal(list)                    # items that came onto the list, so the panel marks the ones it has jobs for
    status = Signal(str, int)               # message, milliseconds
    changed = Signal()                      # what the page says about itself (status_text) may be new

    CACHED = 8                              # channels kept for Back and Forward

    def __init__(self, sc: Scheme, thumbs: ThumbLoader, service, round_picture: bool, parent=None):
        super().__init__(parent)
        self.sc, self._thumbs, self._svc = sc, thumbs, service
        self._fetcher = ChannelFetcher(service)
        service.listing_ready.connect(self._fetcher.deliver)
        service.request_failed.connect(self._fetcher.failed)
        self._pics = ThumbLoader(getattr(thumbs, "dir", None), self, workers=1)
        self._pics.size = QSize(round(112 * thumbs.size.width() / 96), round(112 * thumbs.size.width() / 96))
        self._views: OrderedDict[str, _View] = OrderedDict()
        self._view: _View | None = None
        self._token = self._fetcher.begin()
        self._active, self._closed, self._probe_due = True, False, False
        self._can_forward = False
        self._queue: list[str] = []
        self._probing = ""
        self._shown_key: tuple | None = None
        self._shown_n = -1
        self._state_do = None
        self._text_has = self._text_long = self._opened = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 4)
        bar.setSpacing(2)
        self._back = IconButton(sc, "back")
        self._back.setToolTip("Back")
        self._back.setAccessibleName("Back")
        self._back.clicked.connect(self.back)
        self._fwd = IconButton(sc, "right")
        self._fwd.setToolTip("Forward")
        self._fwd.setAccessibleName("Forward")
        self._fwd.clicked.connect(self.forward)
        self._fwd.hide()
        self._title = ElidedLabel(sc, bold=True)
        bar.addWidget(self._back)
        bar.addWidget(self._fwd)
        bar.addWidget(self._title, 1)
        top = QWidget()
        top.setLayout(bar)
        lay.addWidget(top)
        self._line = ProgressLine(sc)
        lay.addWidget(self._line)

        self._ident = QWidget()
        il = QHBoxLayout(self._ident)
        il.setContentsMargins(10, 8, 10, 4)
        il.setSpacing(10)
        self._pic = Avatar(sc, self._pics, round_picture)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._name = WrapLabel(sc, 2, dim=False, bold=True, scale=1.0)
        self._owner = LinkButton(sc)
        self._owner.clicked.connect(self._owner_clicked)
        self._meta = WrapLabel(sc, 2, dim=True)
        col.addWidget(self._name)
        col.addWidget(self._owner)
        col.addWidget(self._meta)
        col.addStretch(1)
        il.addWidget(self._pic, 0, Qt.AlignTop)
        il.addLayout(col, 1)
        lay.addWidget(self._ident)

        self._about = QWidget()
        dl = QVBoxLayout(self._about)
        dl.setContentsMargins(10, 0, 10, 2)
        dl.setSpacing(0)
        self._desc = WrapLabel(sc, 2, dim=True, scale=0.92)
        self._desc_long = WrapLabel(sc, LONG_LINES, dim=True, scale=0.92)
        self._more = LinkButton(sc, "More", bold=True)
        self._more.clicked.connect(self._toggle_about)
        dl.addWidget(self._desc)
        dl.addWidget(self._desc_long)
        dl.addWidget(self._more, 0, Qt.AlignRight)
        self._desc_long.hide()
        lay.addWidget(self._about)

        self._actions = QWidget()
        al = QHBoxLayout(self._actions)
        al.setContentsMargins(10, 4, 10, 6)
        al.setSpacing(6)
        self._b_play = PillButton(sc, "Play all", primary=True)
        self._b_shuffle = PillButton(sc, "Shuffle")
        self._b_save = PillButton(sc, "Save")
        self._b_save.setToolTip("Save to library")
        self._b_play.clicked.connect(lambda: self.play_now.emit(self._playable()))
        self._b_shuffle.clicked.connect(lambda: self.shuffle.emit(self._playable()))
        self._b_save.clicked.connect(lambda: self.save.emit(self._playable()))
        for b in (self._b_play, self._b_shuffle, self._b_save):
            al.addWidget(b, 1)
        lay.addWidget(self._actions)

        self._tabs = ChipBar(sc)
        self._tabs.chosen.connect(self._on_tab)
        self._sorts = ChipBar(sc)
        self._sorts.chosen.connect(self._on_sort)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Search this channel")
        self._edit.setMinimumWidth(60)
        self._act_find = self._edit.addAction(QIcon(), QLineEdit.LeadingPosition)
        self._act_clear = self._edit.addAction(QIcon(), QLineEdit.TrailingPosition)
        self._act_clear.setToolTip("Clear")
        self._act_clear.setVisible(False)
        self._act_clear.triggered.connect(self._edit.clear)
        self._edit.textChanged.connect(self._on_text)
        self._edit.returnPressed.connect(self._search)
        sh = QShortcut(QKeySequence(Qt.Key_Escape), self._edit)
        sh.setContext(Qt.WidgetShortcut)
        sh.activated.connect(self._edit.clear)
        self._find = QWidget()
        fl = QVBoxLayout(self._find)
        fl.setContentsMargins(10, 2, 10, 4)
        fl.setSpacing(4)
        fl.addWidget(self._edit)
        fl.addWidget(self._sorts)
        self._tabs_box = QWidget()
        tl = QVBoxLayout(self._tabs_box)
        tl.setContentsMargins(10, 0, 10, 0)
        tl.addWidget(self._tabs)
        lay.addWidget(self._tabs_box)
        lay.addWidget(self._find)

        self._model = ResultsModel(self)
        self._list = ResultsView(sc, self._model, thumbs)
        self._list.on_enter = self._enter
        self._list.on_enter_add = lambda: self.add_to_playlist.emit(self._picked(True))
        self._list.on_more = self.load_more
        self._list.on_escape = self.back.emit
        self._list.doubleClicked.connect(self._activated)
        self._list.clicked.connect(self._clicked)
        self._list.pressed_button.connect(self._row_button)
        self._list.customContextMenuRequested.connect(self._context)
        self._list.verticalScrollBar().valueChanged.connect(self._scrolled)
        thumbs.ready.connect(self._list.viewport().update)
        self._skel = Skeleton(sc)
        self._note = StateView(sc)
        self._note.action.connect(self._note_clicked)
        self._stack = QStackedWidget()
        for w in (self._list, self._skel, self._note):
            self._stack.addWidget(w)
        lay.addWidget(self._stack, 1)
        self.setMinimumSize(200, 120)
        self._apply_level()
        self.restyle()

    # what the panel reads and sets

    @property
    def model(self) -> ResultsModel:
        return self._model

    def painted(self) -> list:
        return [self._back, self._fwd, self._title, self._line, self._pic, self._name, self._owner, self._meta, self._desc, self._desc_long,
                self._more, self._b_play, self._b_shuffle, self._b_save, self._tabs, self._sorts, self._skel, self._note]

    def restyle(self):
        self._act_find.setIcon(self._icon("search", self.sc.dimmer))
        self._act_clear.setIcon(self._icon("close", self.sc.dim))
        for w in self.painted():
            w.update()
        self._list.viewport().update()

    def _icon(self, name: str, color) -> QIcon:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(14 * dpr), int(14 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        draw_icon(p, name, QRectF(0, 0, 14, 14), color)
        p.end()
        return QIcon(pm)

    def focus_list(self):
        if self._model.rowCount():
            if not self._list.currentIndex().isValid():
                self._list.setCurrentIndex(self._model.index(0))
            self._list.setFocus()

    def set_nav(self, can_forward: bool):
        self._can_forward = bool(can_forward)
        self._fwd.setVisible(self._can_forward)

    def set_active(self, on: bool):
        """The panel says whether it can be seen. Nothing is asked of the site, and nothing animates, while it cannot."""
        self._active = bool(on)
        self._sync_active()

    def status_text(self) -> str:
        v = self._view
        if v is None or v.head != "ready":
            return ""
        t = v.cur()
        if v.query:
            return f"{plural(len(t.items), 'result')} for “{v.query}”" + (", more available" if t.more else "")
        noun = "playlist" if v.tab == "playlists" else "video"
        total = t.total if t.more and t.total and t.total > len(t.items) else None
        text = plural(len(t.items), noun) if total is None else f"{len(t.items):,} of {plural(total, noun)}"
        who = (v.header.owner if v.header and v.header.kind == "playlist" else v.header.title if v.header else "")
        return f"{text}, {who}" if who else text

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._fetcher.cancel_all()
        for sig, fn in ((self._svc.listing_ready, self._fetcher.deliver), (self._svc.request_failed, self._fetcher.failed)):
            try:
                sig.disconnect(fn)
            except (RuntimeError, TypeError):
                pass
        self._skel.run(False)
        self._line.set(None)
        self._pics.close()

    # opening and leaving

    def open(self, target: Target):
        """Show a channel or playlist. One that was shown lately comes back as it was, without asking again."""
        if self._closed:
            return
        if self._view is not None and self._view.target == target:
            self._render()
            self._ensure()
            return
        self.leave()
        view = self._views.pop(target.url, None) or _View(target)
        self._views[target.url] = view
        if target.tabs and target.tab:
            view.tab, view.picked = target.tab, True      # a pasted /shorts link opens on Shorts
            view.hint = target.tab if view.head == "idle" else ""
        while len(self._views) > self.CACHED:
            self._views.popitem(last=False)
        self._view = view
        self._token = self._fetcher.begin()
        self._opened = False
        self._set_search_text(view.query)
        self._render()
        self._ensure()
        self._sync_active()

    def leave(self):
        """Stop asking. What was loaded stays for when the page is opened again."""
        v = self._view
        if v is None:
            return
        self._save_scroll()
        self._token = self._fetcher.begin()
        if v.head == "loading":
            v.head = "idle"
        for t in v.tabs.values():
            if t.state == "loading":
                t.state = "ready" if t.items else "idle"
        self._queue, self._probing = [], ""
        self._view = None
        self._shown_key, self._shown_n = None, -1
        self._sync_active()

    def retry(self):
        v = self._view
        if v is None:
            return
        if v.head == "error":
            v.head = "idle"
        else:
            t = v.cur()
            if t.state == "error":
                t.state = "idle"
        self._render()
        self._ensure()

    def login_changed(self, provider_id: str):
        """A list that failed for want of a sign in runs again once there is one."""
        v = self._view
        if v is not None and v.target.provider == provider_id and ERROR_ACTIONS.get(self._err_code(v), "") == "sign_in":
            self.retry()

    # asking

    def _runs(self) -> bool:
        return self.isVisible() and self._active and not self._closed

    def _ensure(self):
        """Start whatever the page on screen is waiting for."""
        v = self._view
        if v is None or self._closed:
            return
        if v.head == "idle":
            self._load_head(v.hint or "videos")
            return
        if v.head != "ready":
            return
        if v.cur().state == "idle":
            self._fetch(v.key())
        self._start_probes()

    def _load_head(self, tab: str):
        v = self._view
        v.head = "loading"
        self._fetcher.header(v.target, self._on_head, tab)
        self._render()
        self._sync_active()

    def _on_head(self, res):
        v = self._view
        if v is None or self._closed or res.token != self._token or res.target != v.target:
            return
        tab = res.tab if v.target.tabs else "videos"
        if not res.ok:
            if v.hint and tab == v.hint:
                v.hint, v.tab = "", "videos"                # the channel has no such tab: start from Videos
                self._load_head("videos")
                return
            if v.target.tabs and tab == "videos" and res.code in ("failed", "no_media", "unavailable"):
                self._load_head("playlists")                # a channel without a Videos tab still has its playlists
                return
            v.head, v.code, v.message = "error", res.code, res.message
            self._render()
            self._sync_active()
            return
        v.head, v.header = "ready", res.header
        if tab != "videos":
            v.tab = tab
            if tab != v.hint:
                gone = v.tabs.setdefault(("videos", "latest", ""), _Tab())
                gone.state = "empty"
        self._take(v.tabs.setdefault((tab, "latest", ""), _Tab()), res)
        self._afterwards(v.tabs[(tab, "latest", "")])
        self._start_probes()

    def _fetch(self, key: tuple, more: bool = False):
        v = self._view
        t = v.tabs.setdefault(key, _Tab())
        if t.state == "loading" or (more and t.state != "ready"):
            return
        t.state = "loading"
        tab, sort, query = key
        cid = v.header.cid if v.header is not None else ""
        self._fetcher.page(v.target, tab, sort, t.next if more else 1, PAGE, functools.partial(self._on_page, key), query, cid)
        self._render()
        self._sync_active()

    def _on_page(self, key: tuple, res):
        v = self._view
        if v is None or self._closed or res.token != self._token or res.target != v.target:
            return
        t = v.tabs.setdefault(key, _Tab())
        if res.ok:
            self._take(t, res)
        elif res.start > 1:
            t.state = "ready"                               # the pages we have stay
            self.status.emit(f"Could not load more. {res.message}", 6000)
        else:
            t.state, t.code, t.message = "error", res.code, res.message
        self._afterwards(t, key)

    @staticmethod
    def _take(t: _Tab, res):
        t.items, added = merge(t.items, res.page.items)
        t.next = res.start + res.page.size
        if res.page.total is not None:
            t.total = res.page.total
        t.more = res.page.more and added > 0 and len(t.items) < MAX_ITEMS
        t.state, t.code, t.message = ("ready" if t.items else "empty"), "", ""

    def _afterwards(self, t: _Tab, key: tuple | None = None):
        v = self._view
        if key is not None and key[0] == self._probing:
            self._probing = ""
        self._open_first_filled()
        self._render()
        self._sync_active()
        self._next_probe()
        if key is None or key == v.key():
            QTimer.singleShot(0, self._fill_check)

    def _open_first_filled(self):
        """A channel whose Videos tab is empty opens on the first tab that has something, until the user picks one."""
        v = self._view
        gone = v.tabs.get(("videos", "latest", ""))
        if v.picked or v.query or v.tab != "videos" or gone is None or gone.state != "empty":
            return
        for tab in TAB_IDS[1:]:
            t = v.tabs.get((tab, "latest", ""))
            if t is not None and t.state == "ready":
                v.tab = tab
                return

    def _fill_check(self):
        """A page that does not reach the bottom of the list asks for the next one by itself. The rows are counted,
        since the list lays them out a moment after they are set and has no scroll range yet."""
        if self._view is not None and self._can_more() and self._runs() \
                and self._model.rowCount() * self._list.row_h <= self._list.viewport().height():
            self.load_more()

    def _can_more(self) -> bool:
        v = self._view
        if v is None or v.head != "ready":
            return False
        t = v.cur()
        return t.more and t.state == "ready"

    def load_more(self):
        if self._can_more():
            self._fetch(self._view.key(), more=True)

    def _scrolled(self, value: int):
        sb = self._list.verticalScrollBar()
        if self._can_more() and self._runs() and sb.maximum() - value <= 3 * self._list.row_h:
            self.load_more()

    def _start_probes(self):
        """Look at the other tabs one after another, so that a tab with nothing in it is gone before it is clicked."""
        v = self._view
        if v is None or v.head != "ready" or not v.target.tabs:
            return
        if not self._runs():
            self._probe_due = True
            return
        self._probe_due = False
        self._queue = [t for t in TAB_IDS[1:] if (v.tabs.get((t, "latest", "")) or _Tab()).state == "idle"]
        self._next_probe()

    def _next_probe(self):
        v = self._view
        if v is None or self._probing or not self._runs():
            return
        while self._queue:
            tab = self._queue.pop(0)
            key = (tab, "latest", "")
            if v.tabs.setdefault(key, _Tab()).state == "idle":
                self._probing = tab
                self._fetch(key)
                return

    # tabs, sorts, search

    def _on_tab(self, tab: str):
        v = self._view
        if v is None:
            return
        self._save_scroll()
        v.tab, v.query, v.picked = tab, "", True
        self._set_search_text("")
        t = v.cur()
        if t.state == "error" and t.code in TRANSIENT:
            t.state = "idle"
        self._render()
        self._ensure()

    def _on_sort(self, sort: str):
        v = self._view
        if v is None or sort not in SORTS:
            return
        self._save_scroll()
        v.sort = sort
        self._render()
        self._ensure()

    def _on_text(self, text: str):
        self._act_clear.setVisible(bool(text))
        v = self._view
        if not text and v is not None and v.query:
            self._save_scroll()
            v.query = ""
            self._render()
            self._ensure()

    def _set_search_text(self, text: str):
        self._edit.blockSignals(True)
        self._edit.setText(text)
        self._act_clear.setVisible(bool(text))
        self._edit.blockSignals(False)

    def _search(self):
        v = self._view
        q = " ".join(self._edit.text().split())[:100]
        if v is None or not v.target.tabs or not q:
            return
        self._save_scroll()
        v.query = q
        old = [k for k in v.tabs if k[0] == "search"]
        for k in old[:-4]:                                  # the last few searches are kept, no more
            if k != v.key():
                del v.tabs[k]
        self._render()
        self._ensure()

    # drawing the state

    @staticmethod
    def _err_code(v: _View) -> str:
        return v.code if v.head == "error" else v.cur().code

    def _render(self):
        v = self._view
        if v is None:
            return
        h = v.header
        self._title.setText(h.title if h and h.title else self._fallback_title(v.target))
        self._fwd.setVisible(self._can_forward)
        tabs = v.target.tabs and v.head == "ready"
        self._tabs_box.setVisible(tabs)
        if tabs:
            chips = [(t, TAB_LABEL[t], True, "") for t in TAB_IDS if not v.hidden(t)]
            self._tabs.set_chips(chips, "" if v.query else v.tab)
        self._sorts.set_chips([(k, label, True, "") for k, label in SORTS.items()], v.sort)
        self._sorts.setVisible(len(SORTS) > 1 and v.tab == "videos" and not v.query)
        self._about_text()
        self._ident_text()
        self._apply_level()
        self._body()
        can = bool(self._playable()) and v.head == "ready"
        for b in (self._b_play, self._b_shuffle, self._b_save):
            b.setEnabled(can)
        self._sync_active()
        self.update()
        self.changed.emit()

    @staticmethod
    def _fallback_title(t: Target) -> str:
        tail = t.url.rsplit("/", 1)[-1]
        return tail if tail.startswith("@") else ("Playlist" if t.kind == "playlist" else "Channel")

    def _ident_text(self):
        v = self._view
        h = v.header
        if h is None:
            self._pic.set_url("")
            return
        self._pic.set_url(h.avatar)
        self._name.setText(h.title)
        if h.kind == "playlist":
            self._owner.setText(h.owner)
            self._owner.setVisible(bool(h.owner))
            bits = [plural(h.videos, "video") if h.videos is not None else "", ("updated " + fmt_age(h.updated)) if fmt_age(h.updated) else ""]
        else:
            self._owner.hide()
            bits = [h.handle, f"{format_count(h.subscribers)} subscriber{'' if h.subscribers == 1 else 's'}" if h.subscribers is not None else "",
                    plural(h.videos, "video") if h.videos is not None else ""]
        self._meta.setText(", ".join(b for b in bits if b))

    def _about_text(self):
        v = self._view
        text = " ".join((v.header.description if v.header else "").split())
        self._desc.setText(text)
        self._desc_long.setText(text)
        self._text_has = bool(text)
        fm = QFontMetrics(scaled_font(self._desc.font(), 0.92))
        width = max(40, self._desc.width() or self.width() - 20)
        self._text_long = len(wrap_lines(fm, text, width, 3)) > 2
        self._more.setVisible(self._text_long)
        self._more.setText("Less" if self._opened else "More")
        self._desc.setVisible(bool(text) and not self._opened)
        self._desc_long.setVisible(bool(text) and self._opened)

    def _toggle_about(self):
        self._opened = not self._opened
        self._about_text()
        self._apply_level()

    def _apply_level(self):
        """Show what fits. A short page keeps the tabs and the list, and the rest comes back as it gets taller."""
        v = self._view
        h = self.height()
        ready = v is not None and v.header is not None
        tabbed = v is not None and v.target.tabs
        self._ident.setVisible(ready and h >= SHOW_PICTURE)
        self._title.setVisible(not self._ident.isVisibleTo(self))        # the name is in the header when there is room for it
        self._about.setVisible(ready and h >= SHOW_ABOUT and getattr(self, "_text_has", False))
        self._actions.setVisible(ready and h >= SHOW_ACTIONS)
        self._find.setVisible(bool(tabbed) and ready and h >= SHOW_SEARCH)
        self._pic.set_size(AVATAR if h >= 400 else 40)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_level()
        if self._view is not None:
            self._about_text()

    def _body(self):
        v = self._view
        t = v.cur()
        if v.head == "error":
            self._error(v.code, v.message, self.retry)
        elif v.head != "ready":
            self._stack.setCurrentIndex(STACK_LOADING)
        elif t.items:
            self._stack.setCurrentIndex(STACK_LIST)
            self._fill(t, v)
        elif t.state in ("idle", "loading"):
            self._stack.setCurrentIndex(STACK_LOADING)
        elif t.state == "empty":
            if v.query:
                self._say("search", f"No results for “{v.query}”", "Try other words.")
            else:
                self._say("rows" if v.tab == "playlists" else "search", *_EMPTY.get(v.tab, ("Nothing in here", "This list is empty.")))
        else:
            self._error(t.code, t.message, self.retry)

    def _fill(self, t: _Tab, v: _View):
        key = (id(v), v.key())
        sb = self._list.verticalScrollBar()
        same = self._shown_key == key
        if same and self._shown_n == len(t.items):
            self._model.set_more(t.more)
            return
        keep = sb.value() if same else t.scroll
        new = t.items[max(0, self._shown_n):] if same else t.items
        self._shown_key, self._shown_n = key, len(t.items)
        self._model.set_items(t.items, t.more)
        sb.setValue(keep)
        self.shown.emit(list(new))

    def _save_scroll(self):
        v = self._view
        if v is None or self._shown_key is None or self._shown_key[0] != id(v):
            return
        t = v.tabs.get(self._shown_key[1])
        if t is not None:
            t.scroll = self._list.verticalScrollBar().value()

    def _say(self, glyph: str, title: str, hint: str = "", button: str = "", do=None):
        self._state_do = do
        self._note.show_message(glyph, title, hint, button)
        self._stack.setCurrentIndex(STACK_STATE)

    def _error(self, code: str, msg: str, retry):
        act = ERROR_ACTIONS.get(code, "")
        pid = self._view.target.provider
        if act == "sign_in":
            do = lambda: self.sign_in.emit(pid)
        elif act == "update":
            do = self.update_tools.emit
        else:
            do = retry
        label = "Get yt-dlp" if code == "no_ytdlp" else _BUTTON.get(act, "")
        self._say(_ICON.get(code, "warn"), _TITLE.get(code, "That did not work"), msg, label, do)

    def _note_clicked(self):
        if self._state_do is not None:
            self._state_do()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_active()
        if self._probe_due:
            self._start_probes()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_active()

    def _sync_active(self):
        if self._closed:
            return
        on = self._runs()
        self._skel.run(on and self._stack.currentIndex() == STACK_LOADING)
        self._line.set(-1 if on and self._fetcher.pending() else None)
        if not on:
            self._pics.drop_backlog()

    # rows

    def _playable(self) -> list[dict]:
        v = self._view
        if v is None:
            return []
        return [it for it in v.cur().items if it.get("kind") not in CONTAINERS]

    def _rows(self) -> list[int]:
        n = len(self._model.items)
        rows = sorted({i.row() for i in self._list.selectionModel().selectedRows() if i.row() < n})
        cur = self._list.currentIndex()
        if not rows and cur.isValid() and cur.row() < n and self._list.hasFocus():
            rows = [cur.row()]
        return rows

    def _picked(self, playable: bool = False) -> list[dict]:
        items = [self._model.items[r] for r in self._rows()]
        return [it for it in items if it.get("kind") not in CONTAINERS] if playable else items

    def _open_row(self, it: dict):
        if it.get("kind") == "channel":
            self.open_channel.emit(it.get("url") or "")
        elif it.get("kind") == "playlist":
            self.open_playlist.emit(it.get("url") or "")

    def _enter(self, *_):
        sel = self._picked()
        if len(sel) == 1 and sel[0].get("kind") in CONTAINERS:
            self._open_row(sel[0])
        elif sel:
            self.play_now.emit([it for it in sel if it.get("kind") not in CONTAINERS])

    def _activated(self, ix):
        if self._model.is_more(ix.row()):
            return
        it = self._model.items[ix.row()]
        if it.get("kind") in CONTAINERS:
            self._open_row(it)
        else:
            self._enter()

    def _clicked(self, ix):
        if self._model.is_more(ix.row()):
            self.load_more()

    def _row_button(self, name: str, row: int):
        if not 0 <= row < len(self._model.items):
            return
        it = self._model.items[row]
        if name == "play":
            self.play_now.emit([it])
        elif name == "add":
            self.add_to_playlist.emit([it])
        elif name == "save":
            self.save.emit([it])
        elif name == "cancel":
            self.cancel_item.emit(it)
        elif name == "open":
            self._open_row(it)
        elif name == "who":
            self._open_uploader(it)

    def _open_uploader(self, it: dict):
        if can_open_channel(it):
            self.open_channel.emit(it["uploader_url"])

    def _owner_clicked(self):
        v = self._view
        if v is not None and v.header is not None and v.header.owner_url:
            self.open_channel.emit(v.header.owner_url)

    def _context(self, pos):
        ix = self._list.indexAt(pos)
        if not ix.isValid() or self._model.is_more(ix.row()):
            return
        sm = self._list.selectionModel()
        if not sm.isSelected(ix):
            sm.select(ix, QItemSelectionModel.ClearAndSelect)
        menu = self.menu_for(self._rows())
        if menu is not None:
            menu.exec(self._list.viewport().mapToGlobal(pos))

    def menu_for(self, rows: list[int]) -> QMenu | None:
        items = [self._model.items[r] for r in rows if 0 <= r < len(self._model.items)]
        if not items:
            return None
        menu = QMenu(self)
        fetch = [it for it in items if it.get("kind") not in CONTAINERS]
        if fetch:
            menu.addAction("Play now", lambda: self.play_now.emit(fetch))
            menu.addAction("Play next", lambda: self.play_next.emit(fetch))
            menu.addAction("Add to playlist", lambda: self.add_to_playlist.emit(fetch))
            menu.addSeparator()
            menu.addAction("Save to library", lambda: self.save.emit(fetch))
        one = items[0]
        if len(items) == 1:
            if one.get("kind") in CONTAINERS:
                menu.addAction("Open", lambda: self._open_row(one))
            if can_open_channel(one):
                menu.addAction("Open channel", lambda: self._open_uploader(one))
            if one.get("url"):
                menu.addAction("Open in browser", lambda: self._browser(one["url"]))
        links = [it.get("url") for it in items if it.get("url")]
        if links:
            menu.addAction("Copy link" if len(links) == 1 else "Copy links", lambda: self._copy(links))
        if self._playable():
            menu.addSeparator()
            menu.addAction("Play all", lambda: self.play_now.emit(self._playable()))
            menu.addAction("Shuffle", lambda: self.shuffle.emit(self._playable()))
        return menu

    def _copy(self, links: list[str]):
        QGuiApplication.clipboard().setText("\n".join(links))
        self.status.emit("Copied the link" + ("s" if len(links) > 1 else ""), 2000)

    @staticmethod
    def _browser(url: str):
        try:
            QDesktopServices.openUrl(QUrl(normalize_url(url)))
        except ValueError:
            pass


class ChannelPage(_Page):
    """A channel: header, tabs, search and its videos. For a site without tabs it is the one list."""

    def __init__(self, sc: Scheme, thumbs: ThumbLoader, service, parent=None):
        super().__init__(sc, thumbs, service, True, parent)


class PlaylistPage(_Page):
    """A playlist: its cover, who made it and what it holds, with Play all and Shuffle."""

    def __init__(self, sc: Scheme, thumbs: ThumbLoader, service, parent=None):
        super().__init__(sc, thumbs, service, False, parent)
