"""The data behind the playlist: rows, their order, sorting, grouping and the filter.

Nothing in here touches a widget, so it works without a QApplication. The view
paints what `Playlist.display` holds and never walks the whole list while it
paints, which is what keeps a list of tens of thousands of tracks smooth.

Two orders exist. `rows` is the manual order, the one that is saved and the one
drag and drop edits. `order` is what is on screen and what plays: the same list
until a sort is applied, then the sorted copy. Grouping gathers the rows that share
a key next to each other (groups in the order they first appear) but leaves `rows`
alone, so turning it off brings the arranged order back.
"""
from __future__ import annotations

import os
import random
import re
import unicodedata
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path

_SPLIT_DIGITS = re.compile(r"(\d+)").split


def fmt_clock(secs: float) -> str:
    """m:ss, or h:mm:ss from an hour up. Empty when the length is not known."""
    if not secs or secs <= 0:
        return ""
    s = int(round(secs))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_total(secs: float) -> str:
    """The length of a run of tracks: 28:41, then 5h 12m, then 3d 22h."""
    s = int(round(secs or 0))
    if s < 3600:
        return fmt_clock(s) or "0:00"
    h, rem = divmod(s, 3600)
    if h < 24:
        return f"{h}h {rem // 60}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def _nat(s: str) -> list:
    """Natural sort key, so Track 2 comes before Track 10.

    The split always alternates text and digits, starting with text, so two keys
    never compare a string with an int.
    """
    return [int(t) if i & 1 else t for i, t in enumerate(_SPLIT_DIGITS(s.casefold()))]


class Row:
    """One track. Plain attributes in slots, because a playlist can hold tens of
    thousands of these and every one is built on the GUI thread."""

    __slots__ = (
        "path", "name", "title", "artist", "artists", "album", "albumartist", "aa", "genre",
        "year", "track", "disc", "dur", "missing", "tagged", "extra", "num", "pos",
        "grp", "dir", "blob", "gk_artist", "gk_album", "gk_aa",
        "k_title", "k_artist", "k_album", "k_aa", "k_genre", "k_name", "k_path",
    )

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.title = path.stem
        self.artist = self.album = self.albumartist = self.aa = self.genre = ""
        self.artists = ()         # the names in artist, one by one
        self.year = self.track = self.disc = 0
        self.dur = 0.0
        self.missing = self.tagged = self.extra = False
        self.num = 0              # place in the order on screen, 1 based
        self.pos = -1             # place in Playlist.display, -1 while hidden
        self.grp = None
        self.dir = self.blob = None
        self.gk_artist = self.gk_album = self.gk_aa = None
        self.k_title = self.k_artist = self.k_album = self.k_aa = None
        self.k_genre = self.k_name = self.k_path = None

    def set_tags(self, title: str, artist: str, album: str, dur: float, artists=()):
        self.title = title or self.path.stem
        self.artist = artist or ""
        self.artists = tuple(artists)
        self.album = album or ""
        if dur and dur > 0:
            self.dur = float(dur)
        self.aa = self.albumartist or self.lead()
        self.tagged = True
        self._stale()

    def set_extra(self, albumartist: str, year: int, track: int, disc: int, genre: str):
        self.albumartist = albumartist or ""
        self.aa = self.albumartist or self.lead()
        self.year, self.track, self.disc = year or 0, track or 0, disc or 0
        self.genre = genre or ""
        self.extra = True
        self._stale()

    def lead(self) -> str:
        """The first artist, which a track is grouped under when it has no album artist."""
        return self.artists[0] if self.artists else self.artist

    def renamed(self, path: Path):
        self.path = path
        self.name = path.name
        self.title = path.stem
        self.dir = None
        self._stale()
        self.k_name = self.k_path = None

    def _stale(self):
        """Forget what was worked out from the text, since the text changed."""
        self.k_title = self.k_artist = self.k_album = self.k_aa = self.k_genre = None
        self.gk_artist = self.gk_album = self.gk_aa = None
        self.blob = None


def _matches(r: Row, terms: list[str]) -> bool:
    """Every word typed in the filter box has to appear somewhere in the row."""
    b = r.blob
    if b is None:
        b = r.blob = "\n".join((r.title, r.artist, r.album, r.aa, r.genre,
                                str(r.year) if r.year else "", r.name)).lower()
    return all(t in b for t in terms)


# sorting

def _text_key(slot: str, get):
    """Natural-order key for a text field, with blanks after everything else."""
    def key(r: Row):
        k = getattr(r, slot)
        if k is None:
            raw = get(r)
            k = (not raw, _nat(raw))
            setattr(r, slot, k)
        return k
    return key


_KEYS = {
    "title":  _text_key("k_title",  lambda r: r.title),
    "artist": _text_key("k_artist", lambda r: r.artist),
    "album":  _text_key("k_album",  lambda r: r.album),
    "aa":     _text_key("k_aa",     lambda r: r.aa),
    "genre":  _text_key("k_genre",  lambda r: r.genre),
    "name":   _text_key("k_name",   lambda r: r.name),
    "path":   _text_key("k_path",   lambda r: str(r.path)),
    "year":   lambda r: (not r.year, r.year),
    "track":  lambda r: (not r.track, r.track),
    "disc":   lambda r: (not r.disc, r.disc),
    "dur":    lambda r: (not r.dur, r.dur),
    "num":    lambda r: r.num,
}

SORT_FIELDS = frozenset(f for f in _KEYS if f != "num")

# what counts as "nothing here" for a field; those rows go last in either direction
_BLANK = {
    "artist": lambda r: not r.artist, "album": lambda r: not r.album,
    "aa": lambda r: not r.aa, "genre": lambda r: not r.genre,
    "year": lambda r: not r.year, "track": lambda r: not r.track,
    "disc": lambda r: not r.disc, "dur": lambda r: not r.dur,
}

# What breaks a tie, so rows that are equal on the chosen key still land in the
# order a listener expects.
_TIE = ("aa", "album", "disc", "track", "name")

# fields that need the extra tags (year, track, disc, genre, album artist)
NEEDS_EXTRA = frozenset(("aa", "album", "artist", "disc", "genre", "track", "year"))

# (menu label, keys). Each key is (field, descending).
PRESETS = {
    "default": ("Artist, album, track", [("aa", False), ("album", False), ("disc", False), ("track", False), ("name", False)]),
    "artist":  ("Artist", [("artist", False), ("album", False), ("disc", False), ("track", False)]),
    "album":   ("Album", [("album", False), ("aa", False), ("disc", False), ("track", False)]),
    "year":    ("Year", [("year", False), ("aa", False), ("album", False), ("disc", False), ("track", False)]),
    "track":   ("Track number", [("track", False), ("aa", False), ("album", False), ("disc", False)]),
    "title":   ("Title", [("title", False), ("aa", False), ("album", False)]),
    "dur":     ("Duration", [("dur", False), ("aa", False), ("album", False), ("track", False)]),
    "name":    ("File name", [("name", False)]),
    "path":    ("Path", [("path", False)]),
}

FIELD_LABEL = {"num": "Manual", "title": "Title", "artist": "Artist", "album": "Album",
               "aa": "Artist", "year": "Year", "track": "Track", "disc": "Disc",
               "genre": "Genre", "name": "File name", "path": "Path", "dur": "Duration"}


def _stage(rows: list[Row], field: str, desc: bool):
    """One stable sort pass. Rows with nothing in the field stay at the end, so
    flipping the direction never drags the untagged ones to the top."""
    rows.sort(key=_KEYS[field], reverse=desc)
    blank = _BLANK.get(field)
    if desc and blank is not None:
        rows[:] = [r for r in rows if not blank(r)] + [r for r in rows if blank(r)]


def sort_rows(rows: list[Row], keys: list[tuple[str, bool]]) -> list[Row]:
    """A sorted copy. Equal rows keep the order they had, because every pass is
    stable and the least important key runs first."""
    out = list(rows)
    used = {f for f, _ in keys}
    chain = list(keys) + [(f, False) for f in _TIE if f not in used]
    for field, desc in reversed(chain):
        _stage(out, field, desc)
    return out


def describe_sort(keys: list[tuple[str, bool]]) -> str:
    """Words for a status line or tooltip: Artist, album, track."""
    for _label, preset in PRESETS.values():
        if preset == keys:
            return _label
    return ", ".join(FIELD_LABEL.get(f, f) + (" (descending)" if d else "") for f, d in keys)


# grouping

def _dir_of(r: Row) -> str:
    d = r.dir
    if d is None:
        d = r.dir = os.path.dirname(str(r.path))
    return d


def fold(s: str) -> str:
    """A tag as one spelling for grouping: width and style variants folded (full width,
    maths bold), invisible characters dropped, spacing and case ignored."""
    s = unicodedata.normalize("NFKC", s or "")
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Cf").split()).casefold()


def _folded(slot: str, get):
    def key(r: Row):
        k = getattr(r, slot)
        if k is None:
            k = fold(get(r))
            setattr(r, slot, k)
        return k
    return key


_artist_key = _folded("gk_artist", lambda r: r.lead())
_album_key = _folded("gk_album", lambda r: r.album)
_aa_key = _folded("gk_aa", lambda r: r.aa)

GROUPS = {
    "none":         ("None",           None),
    "album":        ("Album",          _album_key),
    "artist":       ("Artist",         _artist_key),
    "folder":       ("Folder",         _dir_of),
    "artist_album": ("Artist - album", lambda r: (_aa_key(r), _album_key(r))),
}


def gather(rows: list[Row], keyfn) -> list[Row]:
    """Rows with the same key next to each other, groups in the order they first appear."""
    runs: dict = {}
    for r in rows:
        runs.setdefault(keyfn(r), []).append(r)
    return [r for run in runs.values() for r in run]


class Group:
    """A run of neighbouring rows that share a key, drawn as one header."""

    __slots__ = ("key", "mode", "first", "rows", "dur", "year", "artist", "collapsed", "pos", "_text")

    def __init__(self, key, first: Row, mode: str):
        self.key = key
        self.mode = mode
        self.first = first
        self.rows: list[Row] = []
        self.dur = 0.0
        self.year = 0
        self.artist = first.aa      # None once the rows disagree
        self.collapsed = False
        self.pos = -1
        self._text = None

    def add(self, r: Row):
        self.rows.append(r)
        self.dur += r.dur
        if r.year and (not self.year or r.year < self.year):
            self.year = r.year
        if self.artist is not None and r.aa != self.artist:
            self.artist = None

    def text(self) -> tuple[str, str]:
        """The title and the dim line under it, worked out when first drawn."""
        if self._text is None:
            first, mode, n = self.first, self.mode, len(self.rows)
            if mode == "folder":
                title = os.path.basename(self.key) or self.key or "Unknown Folder"
                lead = os.path.basename(os.path.dirname(self.key))
            elif mode == "artist":
                title, lead = first.lead() or "Unknown Artist", ""
            else:
                artist = self.artist if self.artist is not None else "Various Artists"
                album = first.album or "Singles"
                title = f"{artist} - {album}" if artist else album
                lead = str(self.year) if self.year else ""
            sub = ", ".join(x for x in (lead, plural(n, "track"), fmt_total(self.dur)) if x)
            self._text = (title, sub)
        return self._text


class Playlist:
    """Rows plus the view settings that decide how they are laid out."""

    def __init__(self):
        self.rows: list[Row] = []
        self.order: list[Row] = []
        self.by_path: dict[Path, Row] = {}
        self.sort: list[tuple[str, bool]] = []    # empty means the manual order
        self.group = "none"
        self.query = ""
        self.collapsed: set = set()
        self.display: list = []                   # Group and Row objects, top to bottom
        self.groups: list[Group] = []
        self.ys: list[int] = [0]                  # top of each display entry, then the total
        self.th = 28                              # track row height
        self.gh = 56                              # group header height
        self.shown = 0                            # tracks that pass the filter
        self.shown_dur = 0.0

    # reading

    def paths(self) -> list[Path]:
        return [r.path for r in self.order]

    def manual_paths(self) -> list[Path]:
        return [r.path for r in self.rows]

    def total_dur(self) -> float:
        return sum(r.dur for r in self.rows)

    def at_y(self, y: int):
        """The display entry under a y coordinate, or None below the last one."""
        i = bisect_right(self.ys, y) - 1
        return self.display[i] if 0 <= i < len(self.display) else None

    def height(self) -> int:
        return self.ys[-1]

    # building the display

    def finish(self):
        """Everything after the row list changed."""
        self.rebuild()

    def rebuild(self):
        order = sort_rows(self.rows, self.sort) if self.sort else list(self.rows)
        keyfn = GROUPS[self.group][1]
        self.order = gather(order, keyfn) if keyfn is not None else order
        for i, r in enumerate(self.order, 1):
            r.num = i
        self.regroup()

    def regroup(self):
        """Walk the order once and decide what is drawn: headers, rows, and what
        the filter and collapsed groups leave out."""
        terms = self.query.lower().split()
        keyfn = GROUPS[self.group][1]
        display: list = []
        groups: list[Group] = []
        collapsed = self.collapsed
        g = None
        cur = None
        shown = 0
        shown_dur = 0.0
        for r in self.order:
            if terms and not _matches(r, terms):
                r.pos = -1
                r.grp = None
                continue
            shown += 1
            shown_dur += r.dur
            if keyfn is not None:
                k = keyfn(r)
                if g is None or k != cur:
                    cur = k
                    g = Group(k, r, self.group)
                    g.collapsed = k in collapsed
                    g.pos = len(display)
                    display.append(g)
                    groups.append(g)
                g.add(r)
                r.grp = g
                if g.collapsed:
                    r.pos = -1
                    continue
            else:
                r.grp = None
            r.pos = len(display)
            display.append(r)
        self.display, self.groups = display, groups
        self.shown, self.shown_dur = shown, shown_dur
        self.layout()

    def layout(self):
        th, gh = self.th, self.gh
        self.ys = list(accumulate((gh if type(x) is Group else th for x in self.display), initial=0))

    def set_heights(self, th: int, gh: int):
        if (th, gh) != (self.th, self.gh):
            self.th, self.gh = th, gh
            self.layout()

    # view settings

    def set_sort(self, keys: list[tuple[str, bool]]):
        self.sort = list(keys)
        self.rebuild()

    def set_group(self, mode: str):
        self.group = mode if mode in GROUPS else "none"
        self.collapsed.clear()
        self.rebuild()

    def set_query(self, text: str):
        text = (text or "").strip()
        if text != self.query:
            self.query = text
            self.regroup()

    def set_collapsed(self, g: Group | None, on: bool):
        """Collapse or expand one group, or all of them when g is None."""
        keys = [x.key for x in self.groups] if g is None else [g.key]
        for k in keys:
            (self.collapsed.add if on else self.collapsed.discard)(k)
        self.regroup()

    # editing

    def add(self, paths: list[Path], at: Row | None = None, after: bool = False) -> list[Row]:
        """Add rows for paths not already here. They go before or after `at` in the
        manual order, or at the end."""
        new = []
        for p in paths:
            if p in self.by_path:
                continue
            r = Row(p)
            self.by_path[p] = r
            new.append(r)
        if not new:
            return new
        if at is not None and self.by_path.get(at.path) is at:
            i = self.rows.index(at) + (1 if after else 0)
            self.rows[i:i] = new
        else:
            self.rows.extend(new)
        self.finish()
        return new

    def remove(self, gone) -> list[Row]:
        gone = set(gone)
        if not gone:
            return []
        self.rows = [r for r in self.rows if r not in gone]
        for r in gone:
            self.by_path.pop(r.path, None)
            r.pos = -1
        self.finish()
        return list(gone)

    def rename(self, r: Row, path: Path):
        """Point a row at a file that was renamed on disk."""
        self.by_path.pop(r.path, None)
        r.renamed(path)
        self.by_path[path] = r
        self.rebuild()

    def replace(self, rows: list[Row], sort: list[tuple[str, bool]]):
        """Put back an earlier state (undo)."""
        self.rows = list(rows)
        self.sort = list(sort)
        self.by_path = {r.path: r for r in self.rows}
        self.finish()

    def commit(self) -> bool:
        """Make the order on screen the manual one, so it can be rearranged and is
        what gets saved. True when that changed anything."""
        if not self.sort and self.order == self.rows:
            return False
        self.rows = list(self.order)
        self.sort = []
        return True

    def move(self, moving, target: Row | None, after: bool = False):
        """Move rows next to `target`, or to the end when it is None. The moved rows
        keep their order among themselves."""
        mset = set(moving)
        if not mset or target in mset:
            return
        self.commit()
        picked = [r for r in self.rows if r in mset]
        rest = [r for r in self.rows if r not in mset]
        i = len(rest) if target is None else rest.index(target) + (1 if after else 0)
        rest[i:i] = picked
        self.rows = rest
        self.finish()

    def move_by(self, moving, step: int):
        """Nudge rows one place up (step -1) or down (1). A row that would pass
        another selected row, or the end of the list, stays put."""
        self.commit()
        mset = set(moving)
        rows = self.rows
        n = len(rows)
        if step < 0:
            for i in range(1, n):
                if rows[i] in mset and rows[i - 1] not in mset:
                    rows[i - 1], rows[i] = rows[i], rows[i - 1]
        else:
            for i in range(n - 2, -1, -1):
                if rows[i] in mset and rows[i + 1] not in mset:
                    rows[i + 1], rows[i] = rows[i], rows[i + 1]
        self.finish()

    def move_to_edge(self, moving, top: bool):
        self.commit()
        mset = set(moving)
        picked = [r for r in self.rows if r in mset]
        rest = [r for r in self.rows if r not in mset]
        self.rows = picked + rest if top else rest + picked
        self.finish()

    def reverse(self):
        self.commit()
        self.rows.reverse()
        self.finish()

    def shuffle(self):
        self.commit()
        random.shuffle(self.rows)
        self.finish()

    def sort_among(self, moving, keys: list[tuple[str, bool]]):
        """Sort only the given rows, among the places they already occupy."""
        self.commit()
        mset = set(moving)
        slots = [i for i, r in enumerate(self.rows) if r in mset]
        picked = sort_rows([self.rows[i] for i in slots], keys)
        for i, r in zip(slots, picked):
            self.rows[i] = r
        self.finish()

    def duplicates(self) -> list[Row]:
        """Rows that repeat an earlier one: same artist and title and a length
        within two seconds. The first of each stays."""
        kept: dict[tuple, list[float]] = {}
        extra = []
        for r in self.order:
            if not r.tagged:
                continue
            durs = kept.setdefault((r.artist.casefold(), r.title.casefold()), [])
            if any(abs(d - r.dur) < 2.0 for d in durs):
                extra.append(r)
            else:
                durs.append(r.dur)
        return extra
