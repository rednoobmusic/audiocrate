"""What is on an iPhone, as a file manager shows it: the media area and the file sharing folder of every app.

Both areas are folders on the GVfs FUSE mount (afc:host=UDID and afc:host=UDID,port=3), so plain os calls read them.
`gio list -l` is the way in when the mount has not appeared yet. There is no Qt in here. Nothing on the phone is
changed except by copy_in, which writes the files it is given into the folder it is given, and takes back only
the partial file of its own that a cancel or an error left behind.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

import idevice

APPS_PORT = 3
CHUNK = 1 << 20
MAX_TRACKS = 20000
READ_ONLY = "This app does not let files be added here. Some apps only share what they already have."
# what the person cares about comes first in the media area, the technical folders after it, none hidden
MEDIA_FIRST = ('Music', 'iTunes_Control', 'Downloads', 'Books', 'Podcasts', 'Recordings', 'DCIM', 'Photos',
               'Purchases')
# what a folder of the media area holds, for the ones where that is known; the rest get no note
MEDIA_NOTES = {
    'iTunes_Control': "Music synced from a computer, under made up names. Its Music folder holds the songs.",
    'Books': "Books and PDFs",
    'Podcasts': "Podcast episodes",
    'Recordings': "Voice memos",
    'DCIM': "Photos and videos from the camera",
    'Purchases': "Items bought in the stores",
    'PhotoData': "The Photos library database and thumbnails",
    'MediaAnalysis': "Analysis data the Photos app keeps",
    'PublicStaging': "Where apps wait when they are installed from a computer",
}
# bundle id without its team suffix, lower case -> the name the app has on the home screen
APP_NAMES = {
    'com.spotify.client': 'Spotify', 'org.videolan.vlc-ios': 'VLC', 'com.foobar2000.mobile': 'foobar2000',
    'com.yoyofr.modizer': 'Modizer', 'com.yourcompany.isunvox': 'iSunVox', 'com.rileytestut.delta': 'Delta',
    'com.sidestore.sidestore': 'SideStore', 'com.kdt.livecontainer': 'LiveContainer', 'com.vrchat.mobile': 'VRChat',
    'com.mojang.minecraftpe': 'Minecraft', 'ai.polycam.polycam': 'Polycam',
    'com.apple.mobilegarageband': 'GarageBand', 'com.apple.finalcutapp.companion': 'Final Cut Pro',
    'io.tailscale.ipn.ios': 'Tailscale', 'vaida.app.pianotranscription': 'Piano Transcription',
    'com.skyjos.ftpmanagerfree': 'FTP Manager', 'com.apple.imovie': 'iMovie', 'com.apple.keynote': 'Keynote',
    'com.apple.numbers': 'Numbers', 'com.apple.pages': 'Pages', 'com.apple.ibooks': 'Books',
    'com.google.ios.youtube': 'YouTube', 'com.hammerandchisel.discord': 'Discord', 'ph.telegra.telegraph': 'Telegram',
    'net.whatsapp.whatsapp': 'WhatsApp', 'com.burbn.instagram': 'Instagram', 'com.zhiliaoapp.musically': 'TikTok',
}
_GENERIC = {'com', 'org', 'net', 'io', 'ai', 'co', 'app', 'apps', 'ios', 'iphone', 'ipad', 'mobile', 'client',
            'free', 'lite', 'companion'}
_TEAM = re.compile(r'\.[A-Z0-9]{10}$')
# the phone stopped answering, as opposed to a file that cannot be read
_GONE = (errno.ENODEV, errno.ENXIO, errno.ENOTCONN, errno.ESHUTDOWN, errno.ECONNRESET, errno.ECONNABORTED,
         errno.ETIMEDOUT)
_NO_WRITE = (errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP)
_FALLBACK_EXTS = frozenset({'.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.wav', '.aiff', '.wma'})


class PhoneError(Exception):
    """The message is a plain sentence for the person using the player."""


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    path: Path | None = None        # on the FUSE mount
    uri: str = ''                   # afc:// address, known when the entry came from gio
    mtime: float = 0.0
    link: bool = False              # a symbolic link: shown, never followed


@dataclass
class Place:
    kind: str                       # 'media' or 'app'
    title: str
    subtitle: str                   # for an app, the full bundle id
    path: Path
    writable: bool | None = None    # None until a copy into it showed which


@dataclass
class CopyResult:
    files: int = 0
    bytes: int = 0
    errors: list = field(default_factory=list)      # plain sentences, one per thing that did not work
    skipped: list = field(default_factory=list)     # names left alone on purpose
    cancelled: bool = False
    read_only: bool = False         # the phone refused to take a file
    dest: Path | None = None
    made: list = field(default_factory=list)        # what was created, top level only

    def summary(self) -> str:
        out, n = [], len(self.skipped)
        if self.cancelled:
            out.append("Cancelled. %s copied first." % plural(self.files, "file") if self.files
                       else "Cancelled. Nothing was copied.")
        elif self.files:
            out.append("Copied %s, %s." % (plural(self.files, "file"), format_size(self.bytes)))
        elif not (self.errors or n):
            out.append("There was nothing to copy.")
        if n:
            out.append("%s skipped because %s there already." % (plural(n, "file"), "it is" if n == 1 else "they are"))
        if self.errors:
            out.append(self.errors[0] + (" %d more did not work." % (len(self.errors) - 1)
                                         if len(self.errors) > 1 else ""))
        return " ".join(out)


def plural(n: int, word: str) -> str:
    return "%d %s" % (n, word if n == 1 else word + "s")


def format_size(n: int) -> str:
    """1536 -> '1.5 KB'. Binary units under the names people know."""
    if n < 1024:
        return "%d B" % n
    n /= 1024
    for unit in ('KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return "%.1f %s" % (n, unit) if n < 10 else "%d %s" % (round(n), unit)
        n /= 1024


# names

def app_name(bundle_id: str) -> tuple[str, str]:
    """(title, subtitle) for a folder in the apps area. ideviceinstaller would give the real display names but is
    not a dependency, so a table covers the common apps and the rest is read off the bundle id: the team suffix
    goes, then the last part that is not a filler such as mobile, client or ios, split into words. The
    subtitle is always the whole bundle id."""
    base = _TEAM.sub('', bundle_id)
    known = APP_NAMES.get(base.lower())
    if known:
        return known, bundle_id
    for part in reversed(base.split('.')):
        words = _words(part)
        if words:
            return ' '.join(words), bundle_id
    return base or bundle_id, bundle_id


def _words(part: str) -> list[str]:
    if part[:1].isupper():                  # PianoTranscription -> Piano Transcription, iSunVox is left alone
        part = re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', part)
    return [w for w in re.split(r'[\s_-]+', part) if w and w.lower() not in _GENERIC]


def _valid_name(name: str) -> bool:
    return bool(name) and name not in ('.', '..') and '/' not in name and '\x00' not in name


def _natural(name: str):
    return [int(t) if i % 2 else t for i, t in enumerate(re.split(r'(\d+)', name.casefold()))], name


def order(entries: list[Entry], media: bool = False) -> list[Entry]:
    """Folders first, then files, each in natural order (file2 before file10). With media, the folders people
    look for lead and the technical ones follow."""
    rank = {n: i for i, n in enumerate(MEDIA_FIRST)}

    def key(e: Entry):
        if media and e.is_dir:
            return (0, rank.get(e.name, len(rank)), _natural(e.name))
        return (0 if e.is_dir else 1, 0, _natural(e.name))
    return sorted(entries, key=key)


# keeping to the phone's folders

def safe_join(root: Path, sub: str) -> Path:
    """root/sub, refusing '..', absolute names and links that lead out of root."""
    root = Path(root)
    if not sub:
        return root
    sub = os.fspath(sub)
    parts = sub.split('/')
    if sub.startswith('/') or '\x00' in sub or '..' in parts:
        raise PhoneError("That folder is outside the phone's files.")
    p = root.joinpath(*[x for x in parts if x not in ('', '.')])
    inside(root, p)
    return p


def inside(root: Path, path: Path) -> Path:
    """path itself, after checking that it is root or below it, also after links are followed."""
    root, path = Path(root), Path(path)
    norm_root, norm = os.path.normpath(root), os.path.normpath(path)
    if norm != norm_root and not norm.startswith(norm_root + os.sep):
        raise PhoneError("That folder is outside the phone's files.")
    real_root, real = os.path.realpath(root), os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise PhoneError("That folder is outside the phone's files.")
    return path


# gio

def parse_gio_list(text: str, gvfs: Path | None = None) -> list[Entry]:
    """The lines of `gio list -l -u uri`: uri, size and type separated by tabs, the name percent encoded. A name
    that would leave its folder (an encoded slash, dots) is dropped."""
    out = []
    for line in text.splitlines():
        cols = line.split('\t')
        uri = cols[0].strip()
        if not uri:
            continue
        name = unquote(uri.rstrip('/').rsplit('/', 1)[-1] if '://' in uri else uri)
        if not _valid_name(name):
            continue
        kind = cols[2].strip(' ()').lower() if len(cols) > 2 else ''
        try:
            size = int(cols[1])
        except (IndexError, ValueError):
            size = 0
        is_dir = kind == 'directory'
        out.append(Entry(name, is_dir, 0 if is_dir else size, path_of(uri, gvfs) if '://' in uri else None, uri,
                         link=kind in ('symlink', 'symbolic link')))
    return out


def _gvfs(gvfs) -> Path:
    return Path(gvfs) if gvfs else idevice.GVFS_ROOT


def path_of(uri: str, gvfs: Path | None = None) -> Path | None:
    """afc://UDID:3/app/folder -> where the FUSE mount shows it."""
    m = re.fullmatch(r'afc://([^/:]+)(?::(\d+))?(?:/(.*))?', uri)
    if not m:
        return None
    host, port, rest = unquote(m.group(1)), m.group(2), m.group(3) or ''
    top = 'afc:host=%s' % host + (',port=%s' % port if port and port != '1' else '')
    return _gvfs(gvfs).joinpath(top, *[unquote(x) for x in rest.split('/') if x])


def uri_of(path: Path, gvfs: Path | None = None) -> str:
    """The reverse, '' for a path that is not on an AFC mount."""
    try:
        rel = Path(path).relative_to(_gvfs(gvfs))
    except ValueError:
        return ''
    m = re.fullmatch(r'afc:host=([^,]+)(?:,port=(\d+))?', rel.parts[0]) if rel.parts else None
    if not m:
        return ''
    port = m.group(2)
    return 'afc://%s%s/' % (unquote(m.group(1)), ':' + port if port and port != '1' else '') \
        + '/'.join(quote(x) for x in rel.parts[1:])


def _gio_list(uri: str, run, gvfs) -> list[Entry]:
    rc, out, err = (run or idevice._run)(['gio', 'list', '-l', '-u', uri], 20)
    if rc != 0:
        last = err.strip().splitlines()[-1] if err.strip() else ''
        raise PhoneError("The phone could not be read. %s" % (last or idevice.TRUST_HELP))
    return parse_gio_list(out, gvfs)


# finding and opening the two areas

def _find(udid: str, port: int, gvfs=None) -> Path | None:
    pat = re.compile(r'afc:host=([^,]+)' + (r'(?:,port=1)?' if port == 1 else r',port=%d' % port))
    root = _gvfs(gvfs)
    try:
        for e in os.listdir(root):
            m = pat.fullmatch(e)
            if m and unquote(m.group(1)).lower() == udid.lower():
                return root / e
    except OSError:
        pass
    return None


def find_media(udid: str, gvfs=None) -> Path | None:
    return _find(udid, 1, gvfs)


def find_apps(udid: str, gvfs=None) -> Path | None:
    return _find(udid, APPS_PORT, gvfs)


def _mount(udid: str, port: int, what: str, wait: float, run, gvfs) -> tuple[Path | None, str]:
    p = _find(udid, port, gvfs)
    if p:
        return p, ''
    uri = 'afc://%s/' % udid if port == 1 else 'afc://%s:%d/' % (udid, port)
    rc, out, err = (run or idevice._run)(['gio', 'mount', uri], 40)
    end = time.monotonic() + (wait if rc == 0 else 0)       # the FUSE folder takes a moment after a good mount only
    while True:
        p = _find(udid, port, gvfs)
        if p:
            return p, ''
        if time.monotonic() >= end:
            break
        time.sleep(0.2)
    if rc == 0:                                 # mounted for gio, the FUSE folder is late: where it will be
        return path_of(uri, gvfs), ''
    if rc == -1:
        return None, "The %s could not be opened: %s." % (what, err.strip() or 'gio failed')
    last = err.strip().splitlines()[-1] if err.strip() else ''
    return None, "The %s could not be opened. %s" % (what, last or idevice.TRUST_HELP)


def mount_apps(udid: str, wait: float = 4.0, run=None, gvfs=None) -> tuple[Path | None, str]:
    """Open the file sharing area of the apps. The phone must be paired already. (folder, '') or (None, a sentence)."""
    return _mount(udid, APPS_PORT, "phone's apps", wait, run, gvfs)


def unmount_apps(udid: str, run=None, gvfs=None) -> tuple[bool, str]:
    """Close the apps area when it is open. Nothing to close counts as done."""
    if _find(udid, APPS_PORT, gvfs) is None:
        return True, ''
    rc, out, err = (run or idevice._run)(['gio', 'mount', '-u', 'afc://%s:%d/' % (udid, APPS_PORT)], 20)
    return rc == 0, err.strip()


def media_place(path: Path) -> Place:
    return Place('media', 'Media', "Music, books, photos and more", Path(path))


def list_places(udid: str, run=None, gvfs=None, media: Path | None = None, apps: Path | None = None,
                wait: float = 4.0) -> tuple[list[Place], list[str]]:
    """The media area first, then every app that shares files, as (places, notes). A note is a sentence about
    an area that could not be opened: the other one is still listed. Raises PhoneError when neither opens."""
    places, notes = [], []
    path, why = (Path(media), '') if media else _mount(udid, 1, "phone", wait, run, gvfs)
    if path:
        places.append(media_place(path))
    else:
        notes.append(why)
    root, why = (Path(apps), '') if apps else _mount(udid, APPS_PORT, "phone's apps", wait, run, gvfs)
    if root:
        try:
            for e in list_dir(root, run=run, gvfs=gvfs):
                if e.is_dir:
                    title, sub = app_name(e.name)
                    places.append(Place('app', title, sub, e.path or root / e.name))
        except PhoneError as err:
            notes.append(str(err))
    else:
        notes.append(why)
    if not places:
        raise PhoneError(notes[0] if len(notes) == 1 else " ".join(notes))
    apps_part = sorted((p for p in places if p.kind == 'app'), key=lambda p: _natural(p.title))
    return [p for p in places if p.kind == 'media'] + apps_part, notes


# folders

def os_message(e: OSError, side: str = 'phone_read') -> str:
    """A sentence for an OSError. side: phone_read, pc_write, pc_read or phone_write."""
    n = e.errno
    if n in _GONE:
        return "The phone stopped answering. Was it unplugged?"
    if side == 'phone_read':
        if n == errno.ENOENT:
            return "It is not on the phone any more."
        if n in (errno.EACCES, errno.EPERM):
            return "The phone does not let it be read."
    elif side == 'pc_write':
        text = {errno.ENOSPC: "The disk on this computer is full.", errno.EDQUOT: "The disk quota is used up.",
                errno.EROFS: "That folder is read only.", errno.EACCES: "You may not write in that folder.",
                errno.EPERM: "You may not write in that folder.",
                errno.ENAMETOOLONG: "The name is too long for that folder.",
                errno.EINVAL: "That folder's file system does not accept this name."}.get(n)
        if text:
            return text
    elif side == 'pc_read':
        if n == errno.ENOENT:
            return "It is not there any more."
        if n in (errno.EACCES, errno.EPERM):
            return "You may not read it."
    elif side == 'phone_write':
        if n in _NO_WRITE:
            return READ_ONLY
        if n in (errno.ENOSPC, errno.EDQUOT):
            return "The phone is full."
    return "%s." % (e.strerror or e)


def _entry(e: os.DirEntry) -> Entry | None:
    if not _valid_name(e.name):
        return None
    link = e.is_symlink()
    try:
        is_dir = not link and e.is_dir(follow_symlinks=False)
    except OSError:
        is_dir = False
    size, mtime = 0, 0.0
    try:
        st = e.stat(follow_symlinks=False)
        size, mtime = (0 if is_dir else st.st_size), st.st_mtime
    except OSError:
        pass
    return Entry(e.name, is_dir, size, Path(e.path), '', mtime, link)


def list_dir(path: Path, root: Path | None = None, media: bool = False, run=None, gvfs=None) -> list[Entry]:
    """What is in a folder on the phone: folders first, then files, natural order, dot files included. Links are
    listed but not followed. With root, a path that leads out of it is refused. Raises PhoneError."""
    path = Path(path)
    if root is not None:
        inside(root, path)
    try:
        with os.scandir(path) as it:
            out = [x for x in map(_entry, it) if x is not None]
    except FileNotFoundError:
        uri = uri_of(path, gvfs)
        if not uri or _top_exists(path, gvfs):      # not an AFC path, or the mount is there and the folder is not
            raise PhoneError("That folder is not on the phone any more.")
        out = _gio_list(uri, run, gvfs)             # the FUSE folder has not appeared yet
    except NotADirectoryError:
        raise PhoneError("That is a file, not a folder.")
    except OSError as e:
        raise PhoneError("The phone could not be read. %s" % os_message(e))
    return order(out, media)


def _top_exists(path: Path, gvfs) -> bool:
    try:
        return (_gvfs(gvfs) / Path(path).relative_to(_gvfs(gvfs)).parts[0]).is_dir()
    except (ValueError, IndexError, OSError):
        return False


# which files are songs

class PhoneTracks:
    """Which files on the phone the player can play, and the songs below a folder, in the order of the folder."""

    def __init__(self, exts=None):
        self._exts = frozenset(exts) if exts is not None else None

    @property
    def exts(self) -> frozenset:
        if self._exts is None:
            try:
                from player import AUDIO_EXTS
                self._exts = frozenset(AUDIO_EXTS)
            except Exception:
                self._exts = _FALLBACK_EXTS
        return self._exts

    def is_audio(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in self.exts

    def collect(self, entries, cancel=None, limit: int = MAX_TRACKS) -> tuple[list[Path], bool]:
        """The songs among entries, searching folders below them. Returns (paths, cut short). Hidden files and
        folders below a folder are skipped, as the other device views do. Links are not followed."""
        stop, out = _Stop(cancel), []
        for e in map(_as_entry, entries):
            if stop() or len(out) > limit:
                break
            if e.link or e.path is None:
                continue
            if e.is_dir:
                self._walk(e.path, out, stop, limit)
            elif self.is_audio(e.name):
                out.append(e.path)
        return out[:limit], len(out) > limit

    def _walk(self, folder: Path, out: list, stop, limit: int):
        try:
            with os.scandir(folder) as it:
                items = sorted(((_natural(x.name), x) for x in it if _valid_name(x.name)), key=lambda t: t[0])
        except OSError:
            return
        for _, x in items:
            if stop() or len(out) > limit:
                return
            try:
                if x.is_symlink() or x.name.startswith('.'):
                    continue
                if x.is_dir(follow_symlinks=False):
                    self._walk(Path(x.path), out, stop, limit)
                elif self.is_audio(x.name):
                    out.append(Path(x.path))
            except OSError:
                continue


# copying

class _Stop:
    def __init__(self, cancel):
        self.cancel = cancel

    def __call__(self) -> bool:
        c = self.cancel
        if c is None:
            return False
        return bool(c.is_set() if hasattr(c, 'is_set') else c())


class _Cancel(Exception):
    pass


class _Short(Exception):
    pass


class _Abort(Exception):
    pass


class _Fail(Exception):
    def __init__(self, side: str, err: OSError):
        super().__init__(side)
        self.side, self.err = side, err

    @property
    def fatal(self) -> bool:
        """A failure that every other file would meet too, so the copy ends instead of listing it again and again."""
        n, full = self.err.errno, (errno.ENOSPC, errno.EDQUOT)
        if n in _GONE:
            return True
        if self.side == 'pc_write':
            return n in full + (errno.EROFS, errno.EACCES, errno.EPERM)
        return self.side == 'phone_write' and n in full + _NO_WRITE

    @property
    def text(self) -> str:
        return os_message(self.err, self.side)


def _as_entry(x) -> Entry:
    if isinstance(x, Entry):
        return x
    p = Path(x)
    return Entry(p.name, p.is_dir() and not p.is_symlink(), path=p)


def _numbered(name: str, n: int, is_dir: bool) -> str:
    stem, ext = (name, '') if is_dir else os.path.splitext(name)
    return '%s (%d)%s' % (stem, n, ext)


def _create_file(folder: Path, name: str):
    """Open folder/name for writing without ever replacing a file: on a clash it becomes 'name (2).ext'."""
    for n in range(1, 1000):
        p = folder / (name if n == 1 else _numbered(name, n, False))
        try:
            return open(p, 'xb'), p
        except FileExistsError:
            continue
        except OSError as e:
            raise _Fail('pc_write', e)
    raise _Fail('pc_write', OSError(errno.EEXIST, 'too many files with that name'))


def _make_dir(folder: Path, name: str) -> Path:
    for n in range(1, 1000):
        p = folder / (name if n == 1 else _numbered(name, n, True))
        try:
            os.mkdir(p)
            return p
        except FileExistsError:
            continue
        except OSError as e:
            raise _Fail('pc_write', e)
    raise _Fail('pc_write', OSError(errno.EEXIST, 'too many folders with that name'))


def _plan_out(entries, only, stop, res: CopyResult):
    """What to copy: a list of (entry, items), items being (is_dir, parts below the folder, source, size, mtime).
    Folders are walked here, links are not followed. Returns (plan, total bytes)."""
    plan, total = [], 0
    for e in map(_as_entry, entries):
        if stop():
            break
        if not _valid_name(e.name) or e.path is None:
            res.errors.append("%s cannot be copied." % (e.name or 'That'))
            continue
        try:
            st = os.lstat(e.path)
        except OSError as err:
            res.errors.append("%s was not copied. %s" % (e.name, os_message(err)))
            continue
        if stat.S_ISLNK(st.st_mode):
            res.errors.append("%s is a link and was not copied." % e.name)
        elif stat.S_ISDIR(st.st_mode):
            items = []
            _walk_out(Path(e.path), (), items, only, stop, res)
            if items or only is None:
                plan.append((e, items))
                total += sum(i[3] for i in items if not i[0])
        elif stat.S_ISREG(st.st_mode):
            if only is None or only(e.name):
                plan.append((e, [(False, (), Path(e.path), st.st_size, st.st_mtime)]))
                total += st.st_size
        else:
            res.errors.append("%s is not a file and was not copied." % e.name)
    return plan, total


def _walk_out(folder: Path, parts: tuple, items: list, only, stop, res: CopyResult):
    try:
        with os.scandir(folder) as it:
            found = sorted(((_natural(x.name), x) for x in it if _valid_name(x.name)), key=lambda t: t[0])
    except OSError as err:
        res.errors.append("%s was not read. %s" % (folder.name, os_message(err)))
        return
    if only is None and not found:
        items.append((True, parts, folder, 0, 0.0))
    for _, x in found:
        if stop():
            return
        try:
            if x.is_symlink():
                if only is None or only(x.name):
                    res.errors.append("%s is a link and was not copied." % x.name)
            elif x.is_dir(follow_symlinks=False):
                _walk_out(Path(x.path), parts + (x.name,), items, only, stop, res)
            elif only is None or only(x.name):
                st = x.stat(follow_symlinks=False)
                items.append((False, parts + (x.name,), Path(x.path), st.st_size, st.st_mtime))
        except OSError as err:
            res.errors.append("%s was not copied. %s" % (x.name, os_message(err)))


def _copy_chunks(src: Path, make_dst, size: int, mtime: float, stop, add, src_side: str, dst_side: str):
    """Copy one file in 1 MiB steps. The copy is removed again when it is cancelled, fails, or ends short."""
    try:
        a = open(src, 'rb')
    except OSError as e:
        raise _Fail(src_side, e)
    with a:
        b, dst = make_dst()
        ok = False
        try:
            n = 0
            try:
                with b:
                    while True:
                        if stop():
                            raise _Cancel()
                        try:
                            chunk = a.read(CHUNK)
                        except OSError as e:
                            raise _Fail(src_side, e)
                        if not chunk:
                            break
                        try:
                            b.write(chunk)
                        except OSError as e:
                            raise _Fail(dst_side, e)
                        n += len(chunk)
                        add(len(chunk))
            except OSError as e:                    # the file did not close cleanly, which is where a full disk shows
                raise _Fail(dst_side, e)
            if size and n < size and src_side == 'phone_read':
                raise _Short()
            ok = True
        finally:
            if not ok:
                try:
                    os.unlink(dst)
                except OSError:
                    pass
    if mtime and dst_side == 'pc_write':
        try:
            os.utime(dst, (mtime, mtime))
        except OSError:
            pass
    return dst, n


def copy_out(entries, dest: Path, progress=None, cancel=None, root: Path | None = None, only=None) -> CopyResult:
    """Copy files and folders off the phone into a folder on this computer. Nothing is replaced: a name that is taken
    becomes 'name (2).ext'. progress(done bytes, total bytes, name) after every chunk. cancel is a threading.Event
    or a function, heard between files and inside big ones, and what was half copied is removed. With only, a
    function of a file name, folders are copied but only the files it accepts. With root, entries must lie below it.
    Links are skipped, never followed. Problems are collected as sentences in the result and the copy goes on,
    unless the phone is gone or the folder cannot be written, which ends it."""
    dest = Path(dest)
    res = CopyResult(dest=dest)
    stop = _Stop(cancel)
    if not dest.is_dir():
        res.errors.append("The folder %s is not there any more." % (dest.name or dest))
        return res
    if not os.access(dest, os.W_OK | os.X_OK):
        res.errors.append('"%s" is read only. Choose another folder.' % (dest.name or dest))
        return res
    entries = [_as_entry(e) for e in entries]
    if root is not None:
        for e in entries:
            try:
                inside(root, e.path or Path('/'))
            except PhoneError as err:
                res.errors.append("%s was not copied. %s" % (e.name, err))
                return res
    plan, total = _plan_out(entries, only, stop, res)
    if stop():
        res.cancelled = True
        return res
    done, label, misses = 0, [''], 0

    def add(n: int):
        nonlocal done
        done += n
        if progress:
            progress(done, total, label[0])
    if progress:
        progress(0, total, '')
    try:
        for e, items in plan:
            top = None
            for is_dir, parts, src, size, mtime in items:
                if stop():
                    raise _Cancel()
                try:
                    if e.is_dir:
                        if top is None:
                            top = _make_dir(dest, e.name)
                            res.made.append(top)
                        folder = top.joinpath(*(parts if is_dir else parts[:-1]))
                        folder.mkdir(parents=True, exist_ok=True)
                        if is_dir:
                            continue
                        name = parts[-1]
                    else:
                        folder, name = dest, e.name
                    label[0] = name
                    got, n = _copy_chunks(src, lambda: _create_file(folder, name), size, mtime, stop, add,
                                          'phone_read', 'pc_write')
                    if not e.is_dir:
                        res.made.append(got)
                    res.files += 1
                    res.bytes += n
                    misses = 0
                except _Short:
                    res.errors.append("%s came out shorter than the original. The phone may have stopped answering."
                                      % src.name)
                except _Fail as f:
                    res.errors.append("%s was not copied. %s" % (src.name, f.text))
                    misses += 1 if f.side == 'phone_read' else 0
                    if f.fatal or misses >= 5:          # five files in a row that the phone will not give
                        raise _Abort()
                except OSError as err:                  # making a folder
                    res.errors.append("%s was not copied. %s" % (src.name, os_message(err, 'pc_write')))
    except _Cancel:
        res.cancelled = True
    except _Abort:
        pass
    if progress:
        progress(done, total, '')
    return res


def copy_in(paths, app_folder: Path, progress=None, cancel=None, overwrite: bool = False,
            root: Path | None = None) -> CopyResult:
    """Copy files from this computer into a folder on the phone, which is the one thing this module writes there.
    A name that is on the phone already is skipped, unless overwrite. Folders are not copied: nothing creates a
    folder on the phone. With root, app_folder must lie below it. A cancel or an error removes the half written
    file of this copy. Where the app refuses the write, the result says read_only and the copy ends."""
    folder = Path(app_folder)
    res = CopyResult(dest=folder)
    stop = _Stop(cancel)
    if root is not None:
        try:
            inside(root, folder)
        except PhoneError as err:
            res.errors.append(str(err))
            return res
    if not folder.is_dir():
        res.errors.append("That folder is not on the phone any more.")
        return res
    plan, total = [], 0
    for p in map(Path, paths):
        target = folder / p.name
        try:
            st = os.lstat(p)
        except OSError as err:
            res.errors.append("%s was not copied. %s" % (p.name, os_message(err, 'pc_read')))
            continue
        if not _valid_name(p.name):
            res.errors.append("%s cannot be copied." % (p.name or 'That'))
        elif stat.S_ISDIR(st.st_mode):
            res.errors.append("%s is a folder. Only files are copied to the phone." % p.name)
        elif not stat.S_ISREG(st.st_mode):
            res.errors.append("%s is not a plain file and was not copied." % p.name)
        elif os.path.lexists(target) and not (overwrite and os.path.isfile(target) and not os.path.islink(target)):
            res.skipped.append(p.name)
        else:
            plan.append((p, target, st.st_size))
            total += st.st_size
    done, label = 0, ['']

    def add(n: int):
        nonlocal done
        done += n
        if progress:
            progress(done, total, label[0])

    def opener(target):
        def make():
            try:
                return open(target, 'wb'), target
            except OSError as e:
                raise _Fail('phone_write', e)
        return make
    if progress:
        progress(0, total, '')
    try:
        for p, target, size in plan:
            if stop():
                raise _Cancel()
            label[0] = p.name
            try:
                _, n = _copy_chunks(p, opener(target), size, 0, stop, add, 'pc_read', 'phone_write')
                res.files += 1
                res.bytes += n
                res.made.append(target)
            except _Fail as f:
                res.errors.append("%s was not copied. %s" % (p.name, f.text))
                res.read_only = res.read_only or (f.side == 'phone_write' and f.err.errno in _NO_WRITE)
                if f.fatal:
                    raise _Abort()
    except _Cancel:
        res.cancelled = True
    except _Abort:
        pass
    if progress:
        progress(done, total, '')
    return res
