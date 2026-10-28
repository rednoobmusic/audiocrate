"""Library data for the panel: parallel lists built once per scan, and flat models over them."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

UNKNOWN_ARTIST = "Unknown Artist"


class LibData:
    """One scan as parallel lists, indexed by track row (the scan's own name order).

    Built on the worker thread and read-only afterwards. Albums are the
    artist + album pairs, stored in artist order, each with its track rows in
    title order. The artist of an album is its album artist, else the first artist of the track.
    Artists are every individual name in the artist tags, stored in name order, each with its track rows.
    """

    def __init__(self, files: list[Path], meta: dict, mtimes: dict):
        titles, artists, albums, durs, hay, mt = [], [], [], [], [], []
        groups: dict[tuple[str, str], list[int]] = {}
        who: dict[str, list] = {}
        same: dict[str, str] = {}
        for i, p in enumerate(files):
            m = meta.get(p) or {}
            title  = m.get("title") or p.stem
            artist = same.setdefault(m.get("artist") or "", m.get("artist") or "")     # one string per artist and album
            album  = same.setdefault(m.get("album") or "", m.get("album") or "")
            titles.append(title)
            artists.append(artist)
            albums.append(album)
            durs.append(m.get("duration") or 0.0)
            hay.append(f"{title}\n{artist}\n{album}\n{p.name}".lower())
            mt.append(mtimes.get(p, 0.0))
            owner = (m.get("albumartist") or "").strip() or (m.get("artists") or [artist])[0]     # one album under one name
            key = ((owner or "Unknown Artist").strip() or "Unknown Artist",
                   (album or "Unknown Album").strip() or "Unknown Album")
            groups.setdefault(key, []).append(i)
            for name in m.get("artists") or (UNKNOWN_ARTIST,):
                e = who.get(low := name.lower())
                if e is None:
                    who[low] = [name, [i]]
                elif e[1][-1] != i:
                    e[1].append(i)

        self.paths, self.titles, self.artists, self.albums = files, titles, artists, albums
        self.durs, self.hay, self.mtimes = durs, hay, mt
        self.all_rows = list(range(len(files)))

        order = sorted(groups, key=lambda k: (k[0].lower(), k[1].lower()))
        self.a_artist = [k[0] for k in order]
        self.a_album  = [k[1] for k in order]
        self.a_rows   = [sorted(groups[k], key=lambda i: titles[i].lower()) for k in order]
        self.a_first  = [files[r[0]] for r in self.a_rows]
        self.a_key    = [(str(p), mt[r[0]]) for p, r in zip(self.a_first, self.a_rows)]
        self.a_mtime  = [max(mt[i] for i in r) for r in self.a_rows]
        self.alb_of   = [0] * len(files)
        for a, rows in enumerate(self.a_rows):
            for i in rows:
                self.alb_of[i] = a
        names = sorted(who)
        self.r_key  = names
        self.r_id   = {k: n for n, k in enumerate(names)}
        self.r_name = [who[k][0] for k in names]
        self.r_rows = [who[k][1] for k in names]
        self.r_albums = [len({self.alb_of[i] for i in rows}) for rows in self.r_rows]
        self._rank: dict[str, list[int]] = {}

    # sorting: a rank list (row -> position) per order, made on first use

    def _ranked(self, name: str, n: int, key) -> list[int]:
        r = self._rank.get(name)
        if r is None:
            r = [0] * n
            for pos, i in enumerate(sorted(range(n), key=key)):
                r[i] = pos
            self._rank[name] = r
        return r

    def track_rank(self, sort: str):
        """Row -> position for a track sort, None for the scan's own (file name) order."""
        t, ar, al, mt = self.titles, self.artists, self.albums, self.mtimes
        keys = {
            "artist": lambda i: (ar[i].lower(), al[i].lower(), i),
            "album":  lambda i: (al[i].lower(), ar[i].lower(), i),
            "title":  lambda i: t[i].lower(),
            "newest": lambda i: -mt[i],
        }
        return self._ranked("t" + sort, len(t), keys[sort]) if sort in keys else None

    def album_rank(self, sort: str):
        """Album -> position, None for artist order."""
        al, ar, mt = self.a_album, self.a_artist, self.a_mtime
        keys = {
            "album":  lambda i: (al[i].lower(), ar[i].lower()),
            "newest": lambda i: -mt[i],
        }
        return self._ranked("a" + sort, len(al), keys[sort]) if sort in keys else None

    # filtering: one pass over the haystacks, shared by both views

    def match(self, q: str):
        """Track rows whose title, artist, album or file name contain q (None for all)."""
        return [i for i, h in enumerate(self.hay) if q in h] if q else None

    def track_rows(self, hits, sort: str) -> list[int]:
        rows = self.all_rows if hits is None else hits
        r = self.track_rank(sort)
        return list(rows) if r is None else sorted(rows, key=r.__getitem__)

    def album_rows(self, hits, sort: str) -> list[int]:
        ids = range(len(self.a_rows)) if hits is None else {self.alb_of[i] for i in hits}
        r = self.album_rank(sort)
        return sorted(ids) if r is None else sorted(ids, key=r.__getitem__)

    def album_paths(self, a: int) -> list[Path]:
        return [self.paths[i] for i in self.a_rows[a]]

    def artist_hits(self, hits) -> dict[int, list[int]] | None:
        """For a search: each artist that has a matching track, with those tracks (None for no search)."""
        if hits is None:
            return None
        hit = set(hits)
        out = {}
        for k, rows in enumerate(self.r_rows):
            m = [i for i in rows if i in hit]
            if m:
                out[k] = m
        return out

    def artist_albums(self, rows: list[int]) -> list[tuple[int, list[int]]]:
        """An artist's tracks by album, as (album, tracks): albums by name, tracks by title."""
        by: dict[int, list[int]] = {}
        for i in rows:
            by.setdefault(self.alb_of[i], []).append(i)
        t = self.titles
        return [(a, sorted(by[a], key=lambda i: t[i].lower()))
                for a in sorted(by, key=lambda a: (self.a_album[a].lower(), self.a_artist[a].lower()))]


class _Model(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.d: LibData | None = None
        self.rows: list[int] = []          # what is shown: track rows or album ids

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def reset(self, d: LibData | None, rows: list[int]):
        self.beginResetModel()
        self.d, self.rows = d, rows
        self.endResetModel()


class TrackModel(_Model):
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        if role == Qt.DisplayRole:
            return self.d.titles[r]
        if role == Qt.ToolTipRole:
            return str(self.d.paths[r])
        return None


class AlbumModel(_Model):
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        a, d = self.rows[index.row()], self.d
        if role == Qt.DisplayRole:
            return d.a_album[a]
        if role == Qt.ToolTipRole:
            n = len(d.a_rows[a])
            return (f"{d.a_album[a]}\n{d.a_artist[a]}\n{n} track{'s' if n != 1 else ''}\n"
                    f"{d.a_first[a].parent}")
        return None


class ArtistModel(_Model):
    """The artist list as one flat list. An int is an artist; below an open artist come its albums as
    (1, artist, album) and below an open album its tracks as (2, artist, album, track). With a search
    only the matching tracks count. Open rows are remembered by name, so a new scan keeps them open."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ids: list[int] = []                    # the artists listed, in name order
        self.hit: dict[int, list[int]] | None = None
        self.opened: set = set()

    def reset(self, d: LibData | None, ids: list[int], hit: dict[int, list[int]] | None = None):
        self.ids, self.hit = ids, hit
        rows: list = []
        for k in ids:
            rows.append(k)
            if d.r_key[k] in self.opened:
                rows.extend(self._kids(d, k))
        super().reset(d, rows)

    def tracks_of(self, k: int, d: LibData | None = None) -> list[int]:
        return (d or self.d).r_rows[k] if self.hit is None else self.hit[k]

    def _kids(self, d: LibData, k: int) -> list:
        out: list = []
        for a, trs in d.artist_albums(self.tracks_of(k, d)):
            out.append((1, k, a))
            if self.key(out[-1], d) in self.opened:
                out.extend((2, k, a, r) for r in trs)
        return out

    def key(self, r, d: LibData | None = None) -> tuple | str:
        """What names a row across scans: the artist, the album under it, the track under that."""
        d = d or self.d
        if type(r) is int:
            return d.r_key[r]
        base = (d.r_key[r[1]], d.a_artist[r[2]].lower(), d.a_album[r[2]].lower())
        return base if r[0] == 1 else (*base, d.paths[r[3]])

    @staticmethod
    def depth(r) -> int:
        return 0 if type(r) is int else r[0]

    def can_open(self, r) -> bool:
        return self.depth(r) < 2

    def is_open(self, r) -> bool:
        return self.can_open(r) and self.key(r) in self.opened

    def toggle(self, row: int, state: bool | None = None) -> bool:
        """Open or close a row (state None flips it). Returns whether anything changed."""
        r = self.rows[row]
        if not self.can_open(r) or state == self.is_open(r):
            return False
        d, key = self.d, self.key(r)
        if key in self.opened:
            end = row + 1
            while end < len(self.rows) and self.depth(self.rows[end]) > self.depth(r):
                end += 1
            self.beginRemoveRows(QModelIndex(), row + 1, end - 1)
            del self.rows[row + 1:end]
            self.endRemoveRows()
            self.opened.discard(key)
            return True
        self.opened.add(key)
        if type(r) is int:
            kids = self._kids(d, r)
        else:
            kids = [(2, r[1], r[2], i) for i in dict(d.artist_albums(self.tracks_of(r[1])))[r[2]]]
        if kids:
            self.beginInsertRows(QModelIndex(), row + 1, row + len(kids))
            self.rows[row + 1:row + 1] = kids
            self.endInsertRows()
        return True

    def row_tracks(self, r) -> list[int]:
        """The track rows a row stands for, albums by name and tracks by title."""
        if type(r) is int:
            return [i for _, trs in self.d.artist_albums(self.tracks_of(r)) for i in trs]
        if r[0] == 1:
            return dict(self.d.artist_albums(self.tracks_of(r[1])))[r[2]]
        return [r[3]]

    def counts(self, k: int) -> tuple[int, int]:
        """Tracks and albums of an artist as listed."""
        if self.hit is None:
            return len(self.d.r_rows[k]), self.d.r_albums[k]
        m = self.hit[k]
        return len(m), len({self.d.alb_of[i] for i in m})

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, d = self.rows[index.row()], self.d
        if role == Qt.DisplayRole:
            return d.r_name[r] if type(r) is int else d.a_album[r[2]] if r[0] == 1 else d.titles[r[3]]
        if role == Qt.ToolTipRole:
            if type(r) is int:
                nt, na = self.counts(r)
                return f"{d.r_name[r]}\n{na} album{'s' if na != 1 else ''}, {nt} track{'s' if nt != 1 else ''}"
            if r[0] == 1:
                return f"{d.a_album[r[2]]}\n{d.a_artist[r[2]]}\n{d.a_first[r[2]].parent}"
            return str(d.paths[r[3]])
        return None
