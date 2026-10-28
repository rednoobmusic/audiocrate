"""Channel and playlist pages of the Online panel, without Qt: which links open one, how a tab is asked for,
what yt-dlp's answer turns into, and how a page is fetched in pieces.

The Online service runs yt-dlp (OnlineService.listing, one run per page of PAGE items, flat). This module
builds the links it is given, decodes what comes back and drops answers that belong to a page the user has
already left. The service is only used through listing() and cancel(), and its answers are handed in with
deliver() and failed(), so a stand-in with those calls is enough to run all of it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote, urlsplit

from online_providers import is_url, normalize_url, provider_for_url
from online_service import item_from_info

PAGE = 30                  # items asked for in one run
MAX_ITEMS = 600            # most items one tab keeps loading: a run starts from the top, so page k costs k requests

# id, label and path of the real YouTube channel tabs, in the order YouTube shows them
TABS = (("videos", "Videos", "/videos"), ("shorts", "Shorts", "/shorts"),
        ("live", "Live", "/streams"), ("playlists", "Playlists", "/playlists"))
TAB_IDS = tuple(t[0] for t in TABS)
TAB_LABEL = {t[0]: t[1] for t in TABS}
TAB_PATH = {t[0]: t[2] for t in TABS}

# Sorting the Videos tab. yt-dlp's tab extractor reads no sort of its own: the sort=p and sort=da of the example
# links in its source are only handed on to YouTube, one of them is marked "Query for sorting no longer works", and
# YouTube does ignore them, the same first five videos come back (tools/fixtures/channel/sort_probe.json). So the
# sorts here are the two orders that exist as lists: what the Videos tab gives by itself (newest first) and the
# channel's "Popular videos" list, UULP followed by the channel id without its UC, which yt-dlp reads like any other
# playlist and which is in order of views (popular.json). Nothing lists a channel oldest first, so there is no Oldest.
SORTS = {"latest": "Latest", "popular": "Popular"}

# codes that say the site had a bad moment, not that the tab is missing
TRANSIENT = ("rate_limited", "network", "timeout", "cancelled", "no_ytdlp")

_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_\-]{22}$")
_HANDLE = re.compile(r"^@[\w.\-]{3,30}$")
_CHANNEL_PATH = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)"
                           r"(?:/(?:videos|shorts|streams|live|playlists|featured|search|community|releases|podcasts|about))?/?$")
_PLAYLISTISH = ("album", "playlist", "set")        # extractor names of other sites whose entries are lists
_TAB_SUFFIX = re.compile(r"\s+-\s+(?:Videos|Shorts|Live|Playlists|Search\b.*)$")
_AVATAR_SIZE = re.compile(r"=s\d+-")
_YT_HOSTS = ("youtube.com", "www.youtube.com")


@dataclass(frozen=True)
class Target:
    """A page the panel can open. url is the channel without any tab, or the playlist."""
    kind: str                  # "channel" or "playlist"
    url: str
    provider: str
    tab: str = field(default="", compare=False)      # the tab a pasted link named (/shorts, /streams ...)

    @property
    def tabs(self) -> bool:
        return self.kind == "channel" and self.provider == "youtube"


@dataclass
class Header:
    kind: str
    title: str = ""
    handle: str = ""
    subscribers: int | None = None
    videos: int | None = None
    description: str = ""
    avatar: str = ""
    owner: str = ""            # a playlist's channel
    owner_url: str = ""
    updated: str = ""          # YYYYMMDD
    cid: str = ""              # the id of a channel, which the Popular list is made from


@dataclass
class Page:
    items: list = field(default_factory=list)
    start: int = 1
    more: bool = False
    total: int | None = None
    size: int = 0              # entries yt-dlp gave, the next page starts at start + size


@dataclass
class Result:
    """What one request came to. token says which navigation asked."""
    token: int
    what: str                  # "header" or "page"
    target: Target
    tab: str = ""
    query: str = ""
    start: int = 1
    ok: bool = False
    header: Header | None = None
    page: Page | None = None
    code: str = ""
    message: str = ""


# links

def link_target(text) -> Target | None:
    """The YouTube channel or playlist a pasted link or @handle names. None for anything else, a video included."""
    s = str(text or "").strip()
    if _HANDLE.match(s):
        return Target("channel", f"https://www.youtube.com/{s}", "youtube")
    if not is_url(s):
        return None
    try:
        u = urlsplit(normalize_url(s))
    except ValueError:
        return None
    if (u.hostname or "") not in _YT_HOSTS:
        return None
    if u.path == "/playlist" or (u.path == "/watch" and not re.search(r"(?:^|&)v=", u.query)):
        m = re.search(r"(?:^|&)list=([\w\-]+)", u.query)
        return Target("playlist", f"https://www.youtube.com/playlist?list={m.group(1)}", "youtube") if m else None
    m = _CHANNEL_PATH.match(u.path)
    if m:
        tab = {"videos": "videos", "shorts": "shorts", "streams": "live", "live": "live",
               "playlists": "playlists"}.get(u.path[m.end(1):].strip("/"), "")
        return Target("channel", f"https://www.youtube.com/{m.group(1)}", "youtube", tab)
    return None


def item_target(item: dict, url: str = "") -> Target | None:
    """The page for a result row: the row itself when it is a playlist or a channel, or the link given (a row's
    uploader). None when yt-dlp has nothing to list there, and always for X, which has no channel pages."""
    link = str(url or item.get("url") or "")
    if not is_url(link):
        return None
    try:
        link = normalize_url(link)
    except ValueError:
        return None
    prov = provider_for_url(link)
    if prov.id == "x":
        return None
    if prov.id == "youtube":
        return link_target(link)
    kind = "channel" if url else item.get("kind")
    return Target(kind, link, prov.id) if kind in ("channel", "playlist") else None


@lru_cache(maxsize=512)
def _opens(url: str) -> bool:
    return item_target({}, url) is not None


def can_open_channel(item: dict) -> bool:
    """True when the uploader of a row can be clicked."""
    return bool(item.get("uploader") and item.get("uploader_url") and _opens(str(item["uploader_url"])))


def tab_url(target: Target, tab: str = "videos", sort: str = "latest", query: str = "", cid: str = "") -> str:
    """The link yt-dlp is given. A search is a question to the channel, whatever tab was open. Only a YouTube channel
    has tabs and sorts, the other pages are listed from their own link. Popular needs the id of the channel (cid),
    and without one the Videos tab is listed as it comes."""
    if not target.tabs:
        return target.url
    q = " ".join(str(query or "").split())[:100]
    if q:
        return f"{target.url}/search?query={quote(q, safe='')}"
    if tab == "videos" and sort == "popular" and _CHANNEL_ID.match(str(cid or "")):
        return f"https://www.youtube.com/playlist?list=UULP{cid[2:]}"
    return target.url + TAB_PATH.get(tab, "/videos")


# numbers

def format_count(n) -> str:
    """87, 532K, 1.2M: three digits at most, the way YouTube writes counts."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        return ""
    n = int(n)
    if n < 1000:
        return str(n)
    for unit, size in (("K", 1e3), ("M", 1e6), ("B", 1e9)):
        s = f"{n / size:.1f}" if n / size < 100 else f"{n / size:.0f}"
        if float(s) < 1000 or unit == "B":
            return s.removesuffix(".0") + unit
    return ""


# what yt-dlp says

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def pick_avatar(thumbs) -> str:
    """The channel picture or playlist cover: the smallest square one of at least 176 px, else the biggest below that.
    A channel's list holds banners as well, and the original without a size (id avatar_uncropped), which comes last."""
    squares, plain = [], ""
    for t in thumbs if isinstance(thumbs, list) else []:
        if not isinstance(t, dict) or not str(t.get("url") or "").startswith(("http://", "https://")):
            continue
        w, h = _num(t.get("width")), _num(t.get("height"))
        if w and h and 0.9 <= w / h <= 1.1:
            squares.append((w < 176, abs(w - 176), t["url"]))
        elif not (w and h) and "banner" not in str(t.get("id") or "") and not plain:
            plain = t["url"]
    url = min(squares)[2] if squares else plain
    return _AVATAR_SIZE.sub("=s176-", url, count=1) if "googleusercontent.com" in url or "ggpht.com" in url else url


def parse_header(data, target: Target) -> Header:
    """The name, handle, subscribers and the rest from the top of one yt-dlp answer. Never raises."""
    d = data if isinstance(data, dict) else {}
    if target.kind == "playlist":
        owner = _text(d.get("uploader")) or _text(d.get("channel"))
        return Header("playlist", _text(d.get("title")), "", None, _num(d.get("playlist_count")),
                      _text(d.get("description"))[:2000], pick_avatar(d.get("thumbnails")), owner,
                      _text(d.get("uploader_url")) or _text(d.get("channel_url")), _text(d.get("modified_date")))
    title = _text(d.get("channel")) or _text(d.get("uploader")) or _TAB_SUFFIX.sub("", _text(d.get("title")))
    handle = _text(d.get("uploader_id"))
    if not handle.startswith("@"):
        m = re.search(r"/(@[^/?#]+)", _text(d.get("uploader_url")) or target.url)
        handle = m.group(1) if m else ""
    return Header("channel", title, handle, _num(d.get("channel_follower_count")), _num(d.get("playlist_count")),
                  _text(d.get("description"))[:2000], pick_avatar(d.get("thumbnails")), cid=_text(d.get("channel_id")))


def parse_page(data, start: int, count: int) -> Page:
    """The items of one yt-dlp answer. A full page may be followed by another, unless the total says it was the last."""
    d = data if isinstance(data, dict) else {}
    entries = [e for e in (d.get("entries") or []) if isinstance(e, dict)]
    items = []
    for e in entries:
        if not (e.get("id") or e.get("url") or e.get("webpage_url")):
            continue
        it = item_from_info(e, d)
        if it["kind"] not in ("playlist", "channel") and str(e.get("ie_key") or "").lower().endswith(_PLAYLISTISH):
            it["kind"] = "playlist"
        items.append(it)
    total = _num(d.get("playlist_count"))
    total = int(total) if total is not None else None
    more = len(entries) >= count and not (total is not None and start - 1 + len(entries) >= total)
    return Page(items, start, more, total, len(entries))


def merge(have: list, new: list) -> tuple[list, int]:
    """have followed by the items of new that are not in it yet, and how many that is."""
    seen = {(it.get("provider"), it.get("id")) for it in have}
    out = list(have)
    for it in new:
        key = (it.get("provider"), it.get("id"))
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out, len(out) - len(have)


# asking

class ChannelFetcher:
    """Asks the service for pages and passes on only the answers that are still wanted.

    begin() starts a navigation: what is in flight is cancelled and answers that still arrive for it are dropped,
    and the token it returns is on every Result. No threads or timers here. The answers of the service come in
    through deliver() and failed(), which the owner connects to its signals.
    """

    def __init__(self, service):
        self.service = service
        self.token = 0
        self._wait: dict[int, tuple] = {}          # request id -> (token, what, target, tab, query, start, count, done)

    def begin(self) -> int:
        self.cancel_all()
        self.token += 1
        return self.token

    def cancel_all(self) -> None:
        wait, self._wait = self._wait, {}
        for rid in wait:
            self.service.cancel(rid)

    def pending(self) -> int:
        return len(self._wait)

    def header(self, target: Target, done, tab: str = "videos") -> int:
        """The header and the first page, from one run. done(Result)."""
        return self._ask("header", target, tab, "latest", "", 1, PAGE, done)

    def page(self, target: Target, tab: str, sort: str, start: int, count: int, done, query: str = "", cid: str = "") -> int:
        return self._ask("page", target, tab, sort, query, start, count, done, cid)

    def playlists(self, target: Target, start: int, count: int, done) -> int:
        return self.page(target, "playlists", "latest", start, count, done)

    def _ask(self, what, target, tab, sort, query, start, count, done, cid: str = "") -> int:
        rid = self.service.listing(tab_url(target, tab, sort, query, cid), start, count)
        self._wait[rid] = (self.token, what, target, tab, query, start, count, done)
        return rid

    def deliver(self, rid: int, data: dict) -> None:
        got = self._wait.pop(rid, None)
        if got is None:
            return
        token, what, target, tab, query, start, count, done = got
        res = Result(token, what, target, tab, query, start)
        try:
            res.page = parse_page(data, start, count)
            if what == "header":
                res.header = parse_header(data, target)
            res.ok = True
        except Exception:
            res.code, res.message = "failed", "yt-dlp answered with something AudioCrate could not read."
        done(res)

    def failed(self, rid: int, code: str, message: str) -> None:
        got = self._wait.pop(rid, None)
        if got is None:
            return
        token, what, target, tab, query, start, _count, done = got
        done(Result(token, what, target, tab, query, start, code=code, message=message))


class History:
    """The channel and playlist pages opened one from another, with a place in them, as a browser keeps it.
    Position -1 is the page the first one was opened from (the results, or the first page)."""
    LIMIT = 30

    def __init__(self):
        self.entries: list[Target] = []
        self.pos = -1

    @property
    def current(self) -> Target | None:
        return self.entries[self.pos] if self.pos >= 0 else None

    @property
    def can_back(self) -> bool:
        return self.pos >= 0

    @property
    def can_forward(self) -> bool:
        return self.pos < len(self.entries) - 1

    def push(self, target: Target) -> None:
        if self.current == target:
            return
        del self.entries[self.pos + 1:]
        self.entries.append(target)
        del self.entries[:-self.LIMIT]
        self.pos = len(self.entries) - 1

    def back(self) -> bool:
        if not self.can_back:
            return False
        self.pos -= 1
        return True

    def forward(self) -> bool:
        if not self.can_forward:
            return False
        self.pos += 1
        return True

    def clear(self) -> None:
        self.entries, self.pos = [], -1
