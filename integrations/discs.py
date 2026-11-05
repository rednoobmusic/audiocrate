"""Discs: the commands that write, erase and read the table of contents of CDs and DVDs, and the album lookup
that names the tracks of an audio CD when it is ripped.

Nothing here starts a program: the functions build argument lists (never shell strings) and read what the programs
print, and devices.py runs them as jobs. The tools are the ones a Linux desktop has already: xorriso for data
discs, images and erasing, cdrskin for audio CDs, libdiscid for the disc id. A tool that is missing turns its
feature off with a plain sentence instead of an error.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
import shutil
import urllib.parse
import urllib.request

from _version import VERSION

NO_CDRSKIN = "Writing an audio CD needs cdrskin. Install the libburn package (cdrskin on Debian and Ubuntu) and try again."
NO_IMAGE_TOOL = "Writing a disc image needs xorriso. Install the libisoburn package and try again."
CD_SECONDS = 80 * 60
PCM_PER_SECOND = 176400                         # 44.1 kHz, 16 bit, two channels
IMAGE_EXTS = ('.iso', '.img', '.udf')

_WRITTEN = re.compile(r'(\d+)\s+of\s+(\d+)\s+MB written')


def xorriso() -> str | None:
    return shutil.which('xorriso')


def cdrskin() -> str | None:
    return shutil.which('cdrskin')


# what the disc in the drive is

def media_facts(media: str) -> dict:
    """What UDisks says the disc is ('optical_cd_rw', 'optical_dvd_plus_r_dl', ...) as {'kind': 'cd'|'dvd'|'bd'|'',
    'rewritable': bool}."""
    m = (media or '').lower()
    kind = 'cd' if m.startswith('optical_cd') else 'dvd' if m.startswith('optical_dvd') else 'bd' if m.startswith('optical_bd') else ''
    return {'kind': kind, 'rewritable': bool(re.search(r'_rw$|_re$|_ram$|_plus_rw|_rw_', m))}


def audio_room(free_bytes: int) -> float:
    """Seconds of music a blank CD has room for, from the room xorriso counts in data bytes (2048 a sector, where
    an audio sector holds 2352)."""
    return free_bytes / 2048 * 2352 / PCM_PER_SECOND


# commands

def erase_args(exe: str, node: str) -> list[str]:
    """Erase a rewritable disc the quick way, which is enough to write it again."""
    return [exe, '-outdev', node, '-blank', 'as_needed']


def image_args(exe: str, node: str, image: str) -> list[str]:
    """Write a disc image (ISO) as the whole disc, then eject. The emulation of cdrecord is how xorriso takes an
    image that is already finished."""
    return [exe, '-as', 'cdrecord', '-v', 'dev=' + node, 'gracetime=2', '-eject', image]


def audio_cd_args(exe: str, node: str, wavs: list[str]) -> list[str]:
    """Write WAV files (44.1 kHz, 16 bit, stereo) as the tracks of an audio CD, in one pass, then eject."""
    return [exe, '-v', 'dev=' + node, 'gracetime=2', '-eject', '-dao', '-audio', '-pad', *wavs]


def written_fraction(line: str) -> float | None:
    """How far a cdrecord-style program says it has got ("Track 01: 24 of 368 MB written"), 0 to 1, else None."""
    m = _WRITTEN.search(line)
    if not m or not int(m.group(2)):
        return None
    return min(int(m.group(1)) / int(m.group(2)), 1.0)


def failure_line(tail: list[str]) -> str:
    """The line of a program's last output that says what went wrong."""
    hit = next((l for l in reversed(tail) if re.search(r'FAILURE|SORRY|FATAL|ERROR', l) and 'abort_on' not in l),
               tail[-1] if tail else '')
    return re.sub(r'^\S+\s*:\s*(?:(?:FAILURE|SORRY|FATAL|ERROR)\s*:\s*)?', '', hit).strip()


# the disc id and the album lookup

_lib = None


def _discid():
    global _lib
    if _lib is None:
        name = ctypes.util.find_library('discid')
        try:
            lib = ctypes.CDLL(name) if name else None
        except OSError:
            lib = None
        if lib is not None:
            lib.discid_new.restype = ctypes.c_void_p
            for fn in ('discid_get_id', 'discid_get_toc_string', 'discid_get_error_msg'):
                getattr(lib, fn).restype = ctypes.c_char_p
                getattr(lib, fn).argtypes = [ctypes.c_void_p]
            for fn in ('discid_free',):
                getattr(lib, fn).argtypes = [ctypes.c_void_p]
            lib.discid_read.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            lib.discid_put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            lib.discid_get_first_track_num.argtypes = lib.discid_get_last_track_num.argtypes = [ctypes.c_void_p]
        _lib = lib or False
    return _lib or None


def disc_info(node: str | None = None, toc: list[int] | None = None) -> dict | None:
    """{'id', 'toc', 'tracks'} of the audio CD in the drive `node`, or of a table of contents given as the lead out
    sector followed by each track's start sector. None when libdiscid is not installed or the disc cannot be read."""
    lib = _discid()
    if lib is None:
        return None
    d = lib.discid_new()
    try:
        if toc is not None:
            arr = (ctypes.c_int * 100)(*toc, *([0] * (100 - len(toc))))
            ok = lib.discid_put(d, 1, len(toc) - 1, arr)
        else:
            ok = lib.discid_read(d, node.encode() if node else None)
        if not ok:
            return None
        return {'id': lib.discid_get_id(d).decode(), 'toc': lib.discid_get_toc_string(d).decode(),
                'tracks': lib.discid_get_last_track_num(d) - lib.discid_get_first_track_num(d) + 1}
    finally:
        lib.discid_free(d)


def lookup_url(info: dict) -> str:
    q = urllib.parse.urlencode({'fmt': 'json', 'inc': 'recordings artist-credits', 'cdstubs': 'no', 'toc': info['toc']})
    return 'https://musicbrainz.org/ws/2/discid/%s?%s' % (urllib.parse.quote(info['id']), q)


def _credit(parts) -> str:
    return ''.join((p.get('name') or (p.get('artist') or {}).get('name') or '') + (p.get('joinphrase') or '')
                   for p in parts or []).strip()


def parse_release(data: dict, disc_id: str, count: int) -> dict | None:
    """The album a MusicBrainz answer describes, as {'album', 'albumartist', 'year', 'disc', 'discs', 'titles'}
    where titles is [(title, artist)] by track. A release that lists this very disc id wins over one that only has
    the same number of tracks. None when nothing fits."""
    best = None
    for rel in (data or {}).get('releases') or []:
        for med in rel.get('media') or []:
            if len(med.get('tracks') or []) != count:
                continue
            exact = any(d.get('id') == disc_id for d in med.get('discs') or [])
            if best is None or (exact and not best[0]):
                best = (exact, rel, med)
    if best is None:
        return None
    _, rel, med = best
    album_artist = _credit(rel.get('artist-credit'))
    m = re.match(r'\d{4}', rel.get('date') or '')
    return {'album': rel.get('title') or '', 'albumartist': album_artist, 'year': int(m.group()) if m else 0,
            'disc': med.get('position') or 0, 'discs': len(rel.get('media') or []),
            'titles': [(t.get('title') or '', _credit(t.get('artist-credit')) or album_artist) for t in med['tracks']]}


def fetch_album(info: dict, timeout: float = 12.0) -> dict | None:
    """Ask MusicBrainz which album this disc is. Only the disc id and the track offsets leave the computer. None when
    it does not know the disc or cannot be reached."""
    req = urllib.request.Request(lookup_url(info), headers={'User-Agent': 'AudioCrate/%s ( https://audiocrate.net )' % VERSION,
                                                            'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_release(json.loads(r.read(1 << 22).decode('utf-8')), info['id'], info['tracks'])
    except (OSError, ValueError):
        return None


def track_tags(n: int, count: int, album: dict | None) -> dict:
    """The tags of track n of a disc with `count` tracks: the album's when it is known, else just the numbers."""
    t = {'track': n, 'tracks': count}
    if not album:
        return {**t, 'title': 'Track %02d' % n, 'album': 'Audio CD', 'albumartist': '', 'artist': ''}
    title, artist = album['titles'][n - 1] if 0 < n <= len(album['titles']) else ('', '')
    t.update(title=title or 'Track %02d' % n, artist=artist, album=album['album'], albumartist=album['albumartist'])
    if album['year']:
        t['year'] = album['year']
    if album['discs'] > 1 and album['disc']:
        t.update(disc=album['disc'], discs=album['discs'])
    return t
