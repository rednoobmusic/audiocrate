"""External devices: USB sticks, SD cards, drives, Rockbox players, iPods and iPhones.

DeviceMonitor lists the drives UDisks2 knows (or the mount table when UDisks2 is not there) and looks again every
few seconds, mounts, unmounts and ejects, lists the music on a device, and copies music to and from it in the
background with progress and cancel. Files a device cannot play are converted with ffmpeg, an iPod's own library is
updated through ipod.IpodSession, an iPhone is read through idevice.py. Nothing here blocks the GUI thread: every call
that touches a device runs in a worker thread and reports back with a signal.
"""
from __future__ import annotations

import ctypes
import errno
import io
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cli_render
import idevice
import ipod
import phone_files
import sid_player
from applog import log
from artists import join_artists, split_artists
from audio_files import (CHIP_EXTS, FAT_32_FS, Cancelled as _Cancelled, _norm, _numbered, base_renderers,  # noqa: F401
                         blank_tags, expand_sources as _expand, fat_name, free_path, printable, put_artists,
                         read_mounts, read_tags, run_ffmpeg, run_until_cancel, unique_name)
from ffmpeg_util import ffmpeg_path
from player import AUDIO_EXTS, extract_art
from PySide6.QtCore import QObject, QTimer, Signal

GLYPH = {'usb': '\U000f129e', 'sd': '\U000f0479', 'disk': '\U000f02ca', 'ipod': '\U000f0c91',
         'iphone': '\U000f011c', 'mtp': '\U000f011c'}
MUSIC_DIR = 'Music'
MAX_SCAN = 50000
FAT_FILE_LIMIT = 4 * 1024 ** 3 - 1
PART_AGE = 600                              # seconds: an older hidden .part was left by a copy that died
JOB_AGE = 6 * 3600                          # seconds: an older job-* folder in the cache was left by a crash
# what Rockbox plays, chip formats included. Its manual's table leaves Opus out, but the codec is in the source
# (lib/rbcodec/codecs/opus.c). The slowest players may stutter on it.
ROCKBOX_EXTS = frozenset({'.mp1', '.mp2', '.mp3', '.aac', '.m4a', '.m4b', '.mp4', '.ogg', '.oga', '.opus', '.mpc',
                          '.spx', '.wma', '.flac', '.wav', '.w64', '.wv', '.ape', '.aif', '.aiff', '.au', '.tta',
                          '.shn', '.nsf', '.nsfe', '.sid', '.spc', '.gbs', '.ay', '.hes', '.kss', '.sgc', '.vgm',
                          '.vgz', '.sap', '.mod'})
_BUSY = "A copy is still running on this device. Wait for it to finish or cancel it first."
_GONE = "That device is not connected any more."


@dataclass
class Device:
    id: str                 # stable across plugging: the file system UUID, a serial or a UDID, never a /dev name
    kind: str               # 'usb' | 'ipod' | 'iphone' | 'mtp'
    name: str               # volume label or device name
    model: str = ''
    vendor: str = ''
    mount: Path | None = None
    block: str = ''         # '/dev/sdb1', informational only
    capacity: int = 0
    free: int = 0
    fs: str = ''
    removable: bool = True
    read_only: bool = False
    mounted: bool = False
    details: dict = field(default_factory=dict)
    glyph: str = ''


# what is in a file (the name and tag helpers live in audio_files, shared with the converter)

def scan_item(path: Path, root: Path, tags: dict | None = None) -> dict:
    """The dict the UI gets for one song, built from the file."""
    t = tags or read_tags(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    return {'path': str(path), 'rel': printable(rel),
            'title': t['title'], 'artist': t['artist'], 'artists': t.get('artists', []), 'album': t['album'], 'track': t['track'],
            'duration': t['duration'], 'size': size, 'format': path.suffix.lower().lstrip('.'), 'ipod_id': None,
            'playlists': [], 'albumartist': t['albumartist'], 'genre': t['genre'], 'year': t['year'],
            'disc': t['disc'], 'rating': 0, 'plays': 0}


# what a device plays and what copying to it does

@dataclass
class Target:
    formats: frozenset | None = None    # codec families it plays: mp3 aac alac wav aiff. None: it is a plain disk
    exts: frozenset | None = None       # for Rockbox, the file types it plays
    best: str = 'aac'                   # what Automatic converts to
    db: bool = False                    # songs go into an iPod's library, so only what it plays may be added


PROFILES = {
    'auto': ('Automatic', 'Copies what the device plays as it is and converts the rest to its best format.'),
    'original': ('Original files', 'Copies every file as it is. Nothing is converted.'),
    'mp3_v2': ('MP3', 'Converts to MP3 with variable bitrate, about 190 kbps. Plays almost everywhere.'),
    'aac_256': ('AAC 256 kbps', 'Converts to AAC in an M4A file. Good quality at a small size.'),
    'alac': ('Apple Lossless', 'Converts to lossless M4A, 16 bit, at most 48 kHz. Large files.'),
}


def profiles_for(target: Target) -> list[dict]:
    """The profiles that make sense for a device: [{'id','label','detail'}]."""
    ok = {'aac_256': target.formats is None or 'aac' in target.formats,
          'alac': target.formats is None or 'alac' in target.formats}
    return [{'id': k, 'label': v[0], 'detail': v[1] + (' ' + _auto_detail(target) if k == 'auto' else '')}
            for k, v in PROFILES.items() if ok.get(k, True)]


def _auto_detail(t: Target) -> str:
    if t.db:
        return 'This one converts to %s.' % ('AAC 256 kbps' if t.best == 'aac' else 'MP3')
    if t.exts is not None:
        return 'Rockbox plays most formats, so only formats it does not know are converted.'
    return 'This device is a plain disk, so everything is copied as it is.'


def unknown_ipod_details(why: str) -> dict:
    """The details of an iPod nothing is known about yet, so that every key is always there."""
    return {'generation': '', 'generation_key': 'unknown', 'can_write': False, 'why_not': why, 'formats': [],
            'sysinfo': {}, 'needs': [], 'note': '', 'checksum': '', 'artwork': False, 'video': False,
            'rockbox_port': 'no', 'library': '', 'blank': False, 'files_note': '', 'guid_from': ''}


def _song_key(t: dict) -> tuple:
    """What makes a song the same song when it is copied again: title, artist and album. The artist is
    taken as the names it holds, so "A & B" on a device matches "A, B" in the file."""
    artist = join_artists(split_artists([t.get('artist') or '']))
    return tuple(_norm(v).strip() for v in (t.get('title') or '', artist, t.get('album') or ''))


def kind_of(path: Path, tags: dict) -> str:
    """mp3, aac, alac, wav, aiff, chip, protected, or other (anything else ffmpeg can decode)."""
    ext = path.suffix.lower()
    if ext in CHIP_EXTS:
        return 'chip'
    if ext == '.m4p' or tags['codec'].startswith('drm'):
        return 'protected'
    if ext == '.mp3':
        return 'mp3'
    if ext in ('.m4a', '.m4b', '.mp4'):
        return 'alac' if tags['codec'] == 'alac' else 'aac'
    if ext in ('.wav', '.wave'):
        return 'wav'
    if ext in ('.aif', '.aiff', '.aifc'):
        return 'aiff'
    return 'other'


def _fits_stock(kind: str, tags: dict) -> bool:
    """Stock iPod firmware plays 16 bit and up to 48 kHz for PCM and lossless files, and up to 48 kHz for AAC."""
    if kind in ('wav', 'aiff', 'alac') and tags['bits'] > 16:
        return False
    return not tags['rate'] or tags['rate'] <= 48000


def plan_item(src: Path, tags: dict, target: Target, profile: str, render_ok: bool = True) -> dict:
    """What copying one file does: {'action': 'copy'|'convert'|'skip', 'fmt': mp3, aac, alac or None, 'ext',
    'why', 'render'}. 'render' is True when a chiptune has to be rendered to audio before it is converted."""
    ext, kind = src.suffix.lower(), kind_of(src, tags)
    copy = {'action': 'copy', 'fmt': None, 'ext': ext, 'why': '', 'render': False}

    def skip(why):
        return {'action': 'skip', 'fmt': None, 'ext': ext, 'why': why, 'render': False}

    def convert(fmt, render=False):
        return {'action': 'convert', 'fmt': fmt, 'ext': '.mp3' if fmt == 'mp3' else '.m4a', 'why': '',
                'render': render}

    if kind == 'protected':
        return skip('is copy protected and cannot be played anywhere but where it was bought')
    if target.db:
        plays = kind in target.formats and (kind == 'mp3' or _fits_stock(kind, tags))
    elif target.exts is not None:
        plays = ext in target.exts
    else:
        plays = True
    if profile == 'original':
        return copy if plays else skip('is not a format this device plays')
    fmt = target.best if profile == 'auto' else {'mp3_v2': 'mp3', 'aac_256': 'aac', 'alac': 'alac'}[profile]
    if kind == 'chip':
        if profile == 'auto' and plays:
            return copy
        return convert(fmt, True) if render_ok else skip('cannot be turned into audio here')
    if profile == 'auto':
        return copy if plays else convert(fmt)
    same = {'mp3_v2': kind == 'mp3', 'aac_256': kind == 'aac' and _fits_stock(kind, tags),
            'alac': kind == 'alac' and _fits_stock(kind, tags)}[profile]
    return copy if same else convert(fmt)


def ffmpeg_args(exe: str, src: str, out: str, fmt: str, tags: dict, cover: bool = True,
                title: str = '') -> list[str]:
    """The ffmpeg command for one conversion, as an argument list and never a shell string. Tags come along (Ogg
    and Opus keep theirs on the stream, hence the second map_metadata), a cover is kept as an attached picture of
    at most 600 pixels, and anything above 48 kHz or two channels is brought down."""
    a = [exe, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', src, '-map', '0:a:0',
         '-map_metadata', '0', '-map_metadata:g', '0:s:a:0', '-sn', '-dn']
    if cover:
        a += ['-map', '0:v:0?', '-c:v', 'mjpeg', '-vf', "scale='min(600,iw)':-2", '-q:v', '4',
              '-disposition:v:0', 'attached_pic']
    else:
        a += ['-vn']
    if fmt == 'mp3':
        a += ['-c:a', 'libmp3lame', '-q:a', '2', '-id3v2_version', '3', '-write_id3v1', '1']
    elif fmt == 'aac':
        a += ['-c:a', 'aac', '-b:a', '256k', '-f', 'ipod']
    else:
        a += ['-c:a', 'alac', '-sample_fmt', 's16p', '-af', 'aresample=dither_method=triangular', '-f', 'ipod']
    rate = tags.get('rate') or 0
    if rate > 48000:
        a += ['-ar', '44100' if rate % 44100 == 0 else '48000']
    elif fmt == 'alac' and rate == 0:
        a += ['-ar', '44100']
    if (tags.get('channels') or 0) > 2:
        a += ['-ac', '2']
    if title:
        a += ['-metadata', 'title=' + title]
    if len(tags.get('artist_values') or ()) > 1:     # ffmpeg keeps the first of several values, or joins them with a bare ";"
        a += ['-metadata', 'artist=' + '; '.join(tags['artist_values'])]
    return a + ['-progress', 'pipe:1', '-nostats', out]


def estimate_size(plan: dict, src_size: int, duration: float | None) -> int:
    """Rough size of the file a plan item will make, for the room check before a copy starts."""
    if plan['action'] == 'copy':
        return src_size
    bytes_per_second = {'mp3': 24_000, 'aac': 32_000, 'alac': 100_000}.get(plan['fmt'], 32_000)
    return int((duration or 240.0) * bytes_per_second) + 200_000


# UDisks2, and the mount table for when it is not there

UD_SERVICE = 'org.freedesktop.UDisks2'
UD_ROOT = '/org/freedesktop/UDisks2'
_I = {'block': UD_SERVICE + '.Block', 'drive': UD_SERVICE + '.Drive', 'fs': UD_SERVICE + '.Filesystem',
      'om': 'org.freedesktop.DBus.ObjectManager', 'ptable': UD_SERVICE + '.PartitionTable'}
_BLOCK_PROPS = ('Device', 'IdUsage', 'IdType', 'IdLabel', 'IdUUID', 'Size', 'ReadOnly', 'HintSystem', 'HintIgnore',
                'HintName', 'Drive')
_DRIVE_PROPS = ('Vendor', 'Model', 'Serial', 'Id', 'Size', 'Removable', 'MediaRemovable', 'ConnectionBus',
                'CanPowerOff', 'SiblingId', 'Optical', 'Media', 'RotationRate')
# keys are patterns: the error names that udisksctl prints, and the plain messages UDisks2 sends (busctl shows no name)
_UD_ERRORS = (
    ('DeviceBusy|is busy', "Something is still using the device. Close the programs that use it and try again."),
    ('NotAuthorized|Not authorized', "This computer did not allow that. Try it from your file manager."),
    ('AlreadyMounted|already mounted', "It is already mounted."), ('NotMounted|is not mounted', "It is not mounted."),
    ('UnknownFilesystem|nknown filesystem|wrong fs type', "The file system on this device is not one this "
                                                          "computer can open."),
    ('NotSupported', "This computer cannot open that file system."))


def classify(block: dict, drive: dict) -> str:
    """'' for a block device that is not a person's removable storage, else 'usb', 'ipod' or 'locked'
    (encrypted). Pure, so it can be tested with canned property dicts."""
    if block.get('HintIgnore') or not drive or drive.get('Optical'):
        return ''
    if not (drive.get('Removable') or drive.get('MediaRemovable')
            or drive.get('ConnectionBus') in ('usb', 'sdio', 'ieee1394')):
        return ''
    usage = block.get('IdUsage')
    if usage == 'crypto':
        return 'locked'
    if usage != 'filesystem':
        return ''
    vendor, model = (drive.get('Vendor') or '').strip().lower(), (drive.get('Model') or '').lower()
    return 'ipod' if vendor == 'apple' and 'ipod' in model else 'usb'


def parent_disk(dev: str) -> str:
    """'/dev/sdb1' to '/dev/sdb', '/dev/mmcblk0p1' to '/dev/mmcblk0'."""
    m = re.fullmatch(r'(/dev/(?:nvme\d+n\d+|mmcblk\d+|loop\d+))p\d+', dev)
    return m.group(1) if m else re.sub(r'\d+$', '', dev)


def ud_error(text: str) -> str:
    return next((v for k, v in _UD_ERRORS if re.search(k, text)), text.split(': ', 1)[-1] or "The operation failed.")


class UdisksBackend:
    """UDisks2 over the system bus, through busctl. Every call is a separate process that can be killed, so a
    daemon that answers late never holds this program up. (QtDBus calls hold the GIL for as long as they wait.)
    snapshot(), mount(), unmount() and power_off() block, so call them from a worker."""
    name = 'udisks2'

    @staticmethod
    def _busctl(args: list[str], timeout: float, limit: int = idevice.LIMIT) -> tuple[int, str, str]:
        return idevice._run(['busctl', '--system', '--json=short'] + args, timeout, limit)

    @staticmethod
    def _props(raw: dict, names) -> dict:
        """The wanted properties of one interface as plain values. busctl wraps each in {'type', 'data'}."""
        out = {}
        for n in names:
            v = raw.get(n)
            v = v.get('data') if isinstance(v, dict) else None
            if n == 'Device':
                v = bytes(v).split(b'\0', 1)[0].decode('utf-8', 'replace') if isinstance(v, list) else ''
            out[n] = v
        return out

    def snapshot(self) -> list[dict]:
        rc, out, err = self._busctl(['call', UD_SERVICE, UD_ROOT, _I['om'], 'GetManagedObjects'], 8, 16 << 20)
        if rc != 0:
            raise OSError(err.strip() or 'UDisks2 did not answer')
        try:
            objs = json.loads(out)['data'][0]
        except (ValueError, LookupError, TypeError):
            raise OSError('UDisks2 gave an answer that could not be read') from None
        entries = []
        for path in sorted(objs):
            ifaces = objs[path]
            if '/block_devices/' not in path or _I['block'] not in ifaces:
                continue
            blk = self._props(ifaces[_I['block']], _BLOCK_PROPS)
            dp = blk['Drive']
            if blk['IdUsage'] not in ('filesystem', 'crypto') or blk['HintIgnore'] or dp in ('', '/', None):
                continue
            drive = self._props(objs.get(dp, {}).get(_I['drive'], {}), _DRIVE_PROPS)
            kind = classify(blk, drive)
            # a whole disk that has a partition table is not a volume, its partitions are
            if kind and (kind == 'locked' or _I['ptable'] not in ifaces):
                entries.append({'kind': kind, 'path': path, 'drive_path': dp, 'block': blk, 'drive': drive})
        return entries

    def _call(self, path: str, iface: str, member: str, options: dict | None = None) -> tuple[bool, str]:
        opts = [x for kv in (options or {}).items() for x in (kv[0], 's', kv[1])]
        # interactive authorization lets a polkit agent ask for a password, and a person needs time to type it
        rc, out, err = self._busctl(['--allow-interactive-authorization=true', '--timeout=90', 'call', UD_SERVICE, path,
                                     iface, member, 'a{sv}', str(len(options or {}))] + opts, 95)
        if rc == -1:
            return False, "The operation took too long." if 'did not answer' in err else err
        if rc != 0:
            return False, ud_error(err.strip())
        try:
            return True, str(json.loads(out)['data'][0]) if out.strip() else ''
        except (ValueError, LookupError, TypeError):
            return True, ''

    def mount(self, e: dict):
        return self._call(e['path'], _I['fs'], 'Mount', {'options': 'ro'} if e['block'].get('ReadOnly') else {})

    def unmount(self, e: dict):
        return self._call(e['path'], _I['fs'], 'Unmount')

    def power_off(self, e: dict):
        return self._call(e['drive_path'], _I['drive'], 'PowerOff')


class MountBackend:
    """Without UDisks2: volumes already mounted under /run/media or /media, and udisksctl for mounting,
    unmounting and powering off when it exists. A device nobody mounted cannot be seen."""
    name = 'mounts'

    def __init__(self, mountinfo: str | Path = '/proc/self/mountinfo', roots: tuple = ('/run/media/', '/media/')):
        self.mountinfo, self.roots = mountinfo, roots

    def snapshot(self) -> list[dict]:
        out = []
        for dev, m in read_mounts(self.mountinfo).items():
            if dev.startswith('/dev/') and any(m['mount'].startswith(r) for r in self.roots):
                blk = {'Device': dev, 'IdUsage': 'filesystem', 'IdType': m['fs'], 'IdLabel': Path(m['mount']).name,
                       'IdUUID': '', 'Size': 0, 'ReadOnly': m['ro'], 'HintSystem': False, 'HintIgnore': False,
                       'HintName': '', 'Drive': ''}
                out.append({'kind': 'usb', 'path': '', 'drive_path': '', 'block': blk, 'drive': {},
                            'mount': m['mount']})
        return out

    @staticmethod
    def _ctl(args: list[str]) -> tuple[bool, str]:
        rc, out, err = idevice._run(['udisksctl'] + args + ['--no-user-interaction'], 60)
        if rc == -1 and 'not installed' in err:
            return False, ("udisks2 is not installed, so AudioCrate cannot mount or eject drives. "
                           "Use your file manager.")
        if rc == -1:
            return False, "The operation took too long." if 'did not answer' in err else err
        return (True, out.strip()) if rc == 0 else (False, ud_error(err.strip()))

    def mount(self, e: dict):
        ok, text = self._ctl(['mount', '-b', e['block']['Device']])
        m = re.search(r' at (.+?)\.?$', text)
        return ok, m.group(1) if ok and m else text

    def unmount(self, e: dict):
        return self._ctl(['unmount', '-b', e['block']['Device']])

    def power_off(self, e: dict):
        return self._ctl(['power-off', '-b', parent_disk(e['block']['Device'])])


# jobs and the file work they do

class DeviceError(Exception):
    """The message is a plain sentence for the person using the player."""


class ConvertError(DeviceError):
    """One file could not be converted. The job goes on with the next one."""


class _Job:
    def __init__(self, jid: str, kind: str, device_id: str):
        self.id, self.kind, self.device_id = jid, kind, device_id
        self.state, self.text, self.error = 'queued', '', ''
        self.done_files = self.total_files = self.bytes = 0
        self.sub = 0.0                      # how far the file in hand is, 0 to 1
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.session: ipod.IpodSession | None = None
        self.last = 0.0
        self.publish = lambda force=False: None     # set by the monitor

    @property
    def percent(self) -> int:
        if self.state == 'done':
            return 100
        done = min(self.done_files + self.sub, self.total_files)
        return int(100 * done / self.total_files) if self.total_files else 0

    def dict(self) -> dict:
        return {'id': self.id, 'state': self.state, 'percent': self.percent, 'text': self.text,
                'done_files': self.done_files, 'total_files': self.total_files, 'bytes': self.bytes,
                'error': self.error, 'kind': self.kind, 'device': self.device_id}


def _os_message(e: OSError) -> str:
    stopped = "The device stopped answering. Was it unplugged?"
    return {errno.ENOSPC: "The device is full.", errno.EROFS: "The device is read only.",
            errno.EFBIG: "A file is too big for this device's file system. FAT32 stops at 4 GB.",
            errno.EIO: stopped, errno.ENODEV: stopped, errno.ENXIO: stopped}.get(
        e.errno, "The device refused a write: %s." % (e.strerror or e))


def put_file(src: Path, dest: Path, job: _Job):
    """Copy src to dest in chunks, through a hidden partial file, so that cancel is heard, progress is known and
    a cancel or an error never leaves half a song behind. The mtime comes along where the file system allows."""
    part = dest.with_name('.' + dest.name + '.part')
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(src, 'rb') as a, open(part, 'wb') as b:
            while chunk := a.read(1 << 20):
                if job.cancel.is_set():
                    raise _Cancelled
                b.write(chunk)
                job.bytes += len(chunk)
                job.publish()
        if part.stat().st_size != src.stat().st_size:
            raise DeviceError("%s came out shorter than the original. The device may be failing." % src.name)
        try:
            os.utime(part, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns))
        except OSError:
            pass
        os.replace(part, dest)
    except OSError as e:
        raise DeviceError(_os_message(e))
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass                    # the device is probably gone, and that is what the error already says


_FFMPEG = threading.Lock()


def convert_audio(job: _Job, src: Path, out: Path, fmt: str, tags: dict, title: str = ''):
    """One ffmpeg run, never two at once whatever the number of jobs, at low priority so that playback is not
    disturbed, killed by cancel. The result is written to `out`."""
    exe = ffmpeg_path()
    if not exe:
        raise DeviceError("ffmpeg is not installed, so files cannot be converted.")
    args = ffmpeg_args(exe, str(src), str(out), fmt, tags, True, title)
    if shutil.which('nice'):
        args = ['nice', '-n', '10'] + args
    while not _FFMPEG.acquire(timeout=0.2):
        if job.cancel.is_set():
            raise _Cancelled
    try:
        _run_ffmpeg(job, args, src, tags)
    finally:
        _FFMPEG.release()


def _run_ffmpeg(job: _Job, args: list, src: Path, tags: dict):
    def progress(seconds):
        job.sub = min(seconds / tags['duration'], 0.99)
        job.publish()

    def own(p):
        job.proc = p
    rc, tail = run_ffmpeg(args, job.cancel, progress if tags.get('duration') else None, own)
    if rc != 0:
        raise ConvertError("%s could not be converted: %s" % (src.name, tail[-200:] or 'ffmpeg failed'))
    job.sub = 0.0


def cover_jpeg(src: Path, folder: Path, size: int = 600) -> Path | None:
    """The song's cover as a baseline JPEG of at most `size` pixels, in folder. Rockbox and old iPods cannot
    read progressive ones."""
    try:
        from PIL import Image
        data = extract_art(src)
        if not data:
            return None
        im = Image.open(io.BytesIO(data)).convert('RGB')
        im.thumbnail((size, size))
        out = folder / ('cover-%d.jpg' % abs(hash(str(src))))
        im.save(out, 'JPEG', quality=88, progressive=False)
        return out
    except Exception:
        return None


def expand_sources(paths: list[Path]) -> list[Path]:
    """Files as given, folders replaced by the music below them, anything that is not audio left out."""
    return _expand(paths, AUDIO_EXTS)


def dest_path(base: Path, tags: dict, src: Path, ext: str, layout: str) -> Path:
    """Where a song goes below base: Artist/Album/NN - Title.ext, or just the file name in a flat layout. Every
    part is safe for FAT, which makes it safe for everything, and the whole path stays short enough for the
    Windows tools that stop at 260 characters."""
    if layout == 'flat':
        return base / fat_name(src.stem + ext)
    num = '%02d' % tags['track'] if tags['track'] else ''
    if tags['discs'] > 1 and tags['disc']:
        num = '%d-%s' % (tags['disc'], num or '00')
    title = tags['title'] or src.stem
    name = fat_name(('%s - %s' % (num, title) if num else title) + ext)
    lead = (tags.get('artist_values') or [tags['artist']])[0]     # named after the first value, as earlier copies were
    d = (base / fat_name(tags['albumartist'] or lead or 'Unknown artist', 96)
         / fat_name(tags['album'] or 'Unknown album', 96))
    over = len(str(d / name)) - 240
    if over > 0:
        name = fat_name(os.path.splitext(name)[0][:max(len(name) - over - len(ext), 8)] + ext)
    return d / name


PLAYLIST_MARK = '#AudioCrate playlist'
_LINE_BREAKS = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029]+')


def _playlist_file(folder: Path, name: str) -> Path:
    """Playlists/<name>.m3u8, or the first 'name (2).m3u8' and so on that is free or was written by AudioCrate. A
    playlist somebody else made is never replaced."""
    base = fat_name(name + '.m3u8')
    stem, ext = os.path.splitext(base)
    out, n = folder / base, 1
    while out.exists():
        try:
            with open(out, 'rb') as f:
                if PLAYLIST_MARK.encode() in f.read(400):
                    break
        except OSError:
            pass
        n += 1
        out = folder / fat_name(_numbered(stem, ext, n))
    return out


def write_playlist(root: Path, name: str, entries: list) -> Path:
    """Playlists/<name>.m3u8 below root, listing songs in order with paths relative to the playlist, which Rockbox
    and most players read. entries is [(file, tags)]. Files that are not there are left out."""
    folder = root / 'Playlists'
    folder.mkdir(exist_ok=True)
    sweep_parts(folder)
    out = _playlist_file(folder, name)
    lines = ['#EXTM3U', PLAYLIST_MARK]
    for dest, t in entries:
        if dest.exists():
            label = _LINE_BREAKS.sub(' ', ' - '.join(x for x in (t['artist'], t['title']) if x)).strip()
            lines += ['#EXTINF:%d,%s' % (round(t['duration'] or -1), label),
                      os.path.relpath(dest, folder).replace(os.sep, '/')]
    part = out.with_name('.' + out.name + '.part')
    try:
        part.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    return out


def write_tags(path: Path, item: dict):
    """Fill in title, artist and album from the library where a file has none. Only MP3 and M4A, which is what
    iPods hold."""
    try:
        import mutagen
        f = mutagen.File(str(path), easy=True)
        if f is None or (f.tags and f.tags.get('title')):
            return
        if f.tags is None:
            f.add_tags()
        for k in ('title', 'artist', 'album', 'albumartist', 'genre'):
            if item.get(k):
                f.tags[k] = item[k]
        if item.get('track'):
            f.tags['tracknumber'] = str(item['track'])
        if item.get('year'):
            f.tags['date'] = str(item['year'])
        f.save()
    except Exception:
        pass


def default_renderers() -> dict:
    """File extension to a function (path, cancel event) giving the path of a WAV, for chiptunes that have to
    become audio before they can be converted. Only what can be rendered here is listed. The main window adds
    .fur from the engine it already has."""
    return base_renderers()


def _syncfs(fd: int) -> bool:
    """syncfs(2) from libc, which Python has no call for. False when this libc has none or the file system takes
    no sync, then the caller syncs everything."""
    try:
        fn = ctypes.CDLL(None, use_errno=True).syncfs
    except (OSError, AttributeError):
        return False
    if fn(fd) == 0:
        return True
    e = ctypes.get_errno()
    if e in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        return False
    raise OSError(e, os.strerror(e))


def flush(path: Path):
    """Write out everything pending for the file system `path` is on, and only that one. Raises DeviceError when
    the device could not take it, which a plain sync would not say."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            if not _syncfs(fd):
                os.sync()
        finally:
            os.close(fd)
    except OSError as e:
        raise DeviceError(_os_message(e))


def sweep_parts(folder: Path):
    """Remove the partial files a copy that died left in folder: hidden, named .<song>.part and older than
    PART_AGE, so one that another copy is writing now stays."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    now, exts = time.time(), AUDIO_EXTS | {'.m3u8'}
    for n in names:
        if n.startswith('.') and n.endswith('.part') and Path(n[1:-5]).suffix.lower() in exts:
            try:
                if now - (folder / n).stat().st_mtime > PART_AGE:
                    (folder / n).unlink()
            except OSError:
                pass


def safe_join(root: Path, sub: str) -> Path:
    """root/sub, refusing anything that leads out of root."""
    p = (root / sub.lstrip('/')).resolve() if sub else root.resolve()
    if p != root.resolve() and not p.is_relative_to(root.resolve()):
        raise DeviceError("That folder is outside the device.")
    return p


class DeviceMonitor(QObject):
    # list and dict payloads are declared as object. PySide6 would turn a dict into a QVariantMap, which copies a
    # library of thousands of songs under the GIL and fails on the 64 bit ids an iPod uses.
    devices_changed = Signal(object)            # list[Device]
    device_message = Signal(str, str)           # device id ('' for no particular device), plain language text
    scan_ready = Signal(int, object, object)    # request id, items (list), info (dict)
    scan_failed = Signal(int, str)
    job_updated = Signal(str, object)           # job id, {'id','state','percent','text','done_files', ...}
    _snapshot = Signal(object)
    _wake = Signal()

    def __init__(self, parent=None, backend=None, mountinfo: str | Path = '/proc/self/mountinfo',
                 cache_dir: Path | None = None):
        super().__init__(parent)
        self._backend, self._fixed = backend, backend is not None
        self.mountinfo = mountinfo
        self.cache_dir = Path(cache_dir or Path.home() / '.cache' / 'audiocrate' / 'devices')
        self.renderers = default_renderers()      # extension -> function(path, cancel) -> WAV path
        self._devices: list[Device] = []
        self._entries: dict = {}
        self._jobs: dict[str, _Job] = {}
        self._busy: dict[str, int] = {}
        self._threads: set = set()
        self._ids = itertools.count(1)
        self._refreshing = self._again = self._active = self._started = self._closed = False
        self._refresh_t0 = 0.0
        self._lock = threading.Lock()
        self._pairing: dict = {}
        self._phones: dict = {}
        self._table: dict | None = None           # the mount table as last seen, for a hidden panel's tick
        self.listed = False                       # the first look has been delivered
        self._snapshot.connect(self._on_snapshot)
        self._wake.connect(self.refresh)
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._tick)

    # watching
    def start(self):
        """Begin watching. Nothing here talks to the bus: a look is a worker thread, and the mount table is read
        every 3 s to tell when to take one."""
        if self._started:
            return
        self._started = True
        self._table = read_mounts(self.mountinfo)
        self._poll.start()
        self._thread(self._sweep_cache, wait=False)
        self.refresh()

    def stop(self):
        self._started = False
        self._poll.stop()

    def set_active(self, on: bool):
        """The panel is visible. Phones are only looked for then, because each look runs a few commands."""
        self._active = bool(on)
        if on:
            self.refresh()

    def devices(self) -> list[Device]:
        return list(self._devices)

    def refresh(self):
        if self._closed:
            return
        if self._refreshing and time.monotonic() - self._refresh_t0 < 20:    # then a worker stuck on a dead device
            self._again = True                                               # is given up on
            return
        self._refreshing, self._refresh_t0 = True, time.monotonic()
        self._thread(self._refresh_worker)

    def _tick(self):
        """Every 3 seconds. Nothing reports a device that comes or goes, so a visible panel looks at everything,
        and a hidden one only when the mount table changed, so that an idle disk is left alone."""
        table = read_mounts(self.mountinfo)
        changed, self._table = table != self._table, table
        if self._active or changed:
            self.refresh()

    def _sweep_cache(self):
        """Converted files of a copy that died stay in job-* folders: remove the ones untouched for JOB_AGE."""
        try:
            old = [p for p in self.cache_dir.glob('job-*') if p.is_dir() and time.time() - p.stat().st_mtime > JOB_AGE]
        except OSError:
            return
        for p in old:
            shutil.rmtree(p, ignore_errors=True)

    def _thread(self, fn, *args, wait: bool = True) -> threading.Thread:
        """fn(*args) in a daemon thread. Quitting waits a moment for the ones that start tools or clean up after a
        job; wait=False is for a thread that only reads, which can be left to die with the program."""
        def run():
            try:
                fn(*args)
            finally:
                self._threads.discard(threading.current_thread())
        t = threading.Thread(target=run, daemon=True, name='devices-%s' % fn.__name__)
        if wait:
            self._threads.add(t)
        t.start()
        return t

    def _refresh_worker(self):
        note, entries = '', None
        try:
            if self._backend is None:
                self._backend, note, entries = self._pick_backend()
            if entries is None:
                try:
                    entries = self._backend.snapshot()
                except Exception as e:
                    if self._fixed or not isinstance(self._backend, UdisksBackend):
                        raise
                    log.warning("UDisks2 stopped answering (%s), using the mount table", e)
                    self._backend = MountBackend(self.mountinfo)
                    note = "UDisks2 is not answering, so only drives that are already mounted are listed."
                    entries = self._backend.snapshot()
            devices, by_id = self._build(entries)
            # phones are looked for while the panel is shown, and kept as they were while it is hidden
            devices += self._phone_devices() if self._active else [d for d in self._devices if d.kind == 'iphone']
            devices.sort(key=lambda d: (d.kind, d.name.lower(), d.id))
            self._deliver((devices, by_id, note))
        except Exception:
            log.exception("looking for devices")
            self._deliver(None)

    def _deliver(self, result):
        try:
            self._snapshot.emit(result)
        except RuntimeError:                      # the monitor was deleted while the worker was finishing
            pass

    def _pick_backend(self):
        """(backend, note, entries): UDisks2 with its first listing when it answers, else the mount table."""
        try:
            b = UdisksBackend()
            return b, '', b.snapshot()
        except Exception as e:
            log.info("UDisks2 is not available: %s", e)
        return MountBackend(self.mountinfo), ("UDisks2 is not running, so only drives that are already mounted "
                                              "are listed and they cannot be ejected from here."), None

    def _on_snapshot(self, res):
        self._refreshing = False
        if res is not None and not self._closed:
            devices, by_id, note = res
            if note:
                self.device_message.emit('', note)
            old = {d.id: d for d in self._devices}
            self._entries = by_id
            if devices != self._devices:
                self._devices = devices
                log.info("devices: %s", ', '.join('%s (%s%s)' % (d.name, d.kind, ', mounted' if d.mounted else '')
                                                  for d in devices) or 'none')
                self.devices_changed.emit(list(devices))
            for gone in set(old) - {d.id for d in devices}:
                self.device_message.emit(gone, "%s was removed." % old[gone].name)
            self.listed = True
        if self._again:
            self._again = False
            self.refresh()

    # building the device list (worker thread)
    def _build(self, entries: list) -> tuple[list[Device], dict]:
        mounts = read_mounts(self.mountinfo)
        out, by_id = [], {}
        for e in entries:
            d = self._device(e, mounts)
            while d.id in by_id:
                d.id += '-2'
            by_id[d.id] = e
            out.append(d)
        return out, by_id

    def _device(self, e: dict, mounts: dict) -> Device:
        b, dr = e['block'], e['drive']
        m = mounts.get(b.get('Device', ''))
        mnt = e.get('mount') or (m['mount'] if m else '')
        mount = Path(mnt) if mnt else None
        vendor, model = (dr.get('Vendor') or '').strip(), (dr.get('Model') or '').strip()
        ident = (b.get('IdUUID') or dr.get('Id') or dr.get('Serial') or ('mnt:%s' % mnt if mnt else '')
                 or '%s-%s' % (b.get('IdLabel') or 'volume', b.get('Size') or 0))
        locked = e['kind'] == 'locked'
        dev = Device(id=ident, kind='ipod' if e['kind'] == 'ipod' else 'usb',
                     name=b.get('IdLabel') or b.get('HintName') or model or 'Drive',
                     model=(vendor + ' ' + model).strip(), vendor=vendor, mount=mount, block=b.get('Device', ''),
                     capacity=int(b.get('Size') or 0), fs=b.get('IdType') or '',
                     read_only=bool(b.get('ReadOnly')) or bool(m and m['ro']),
                     mounted=mount is not None and not locked)
        media = str(dr.get('Media') or '')
        if dev.kind == 'ipod':
            dev.glyph = GLYPH['ipod']
        elif dr.get('ConnectionBus') == 'sdio' or 'sd' in media or 'mmc' in media:
            dev.glyph = GLYPH['sd']
        elif (dr.get('RotationRate') or 0) > 0 or int(dr.get('Size') or 0) > 10 ** 12:
            dev.glyph = GLYPH['disk']
        else:
            dev.glyph = GLYPH['usb']
        dev.details = {'rockbox': False, 'locked': locked, 'serial': dr.get('Serial') or '',
                       'bus': dr.get('ConnectionBus') or '', 'can_power_off': bool(dr.get('CanPowerOff')),
                       'drive': e.get('drive_path', ''), 'sibling': dr.get('SiblingId') or ''}
        if dev.kind == 'ipod':
            dev.details.update(unknown_ipod_details("Mount it to see what this iPod can do."))
        if dev.mounted:
            self._look_inside(dev, dr.get('Serial') or '')
        return dev

    @staticmethod
    def _look_inside(dev: Device, usb_serial: str):
        """Free space, Rockbox, and whether the volume is an iPod and what can be done with it."""
        try:
            du = shutil.disk_usage(dev.mount)
            dev.capacity, dev.free = du.total, du.free >> 20 << 20    # whole megabytes: a few kilobytes are no news
        except OSError:
            pass
        dev.details['rockbox'] = ipod._ci(dev.mount, '.rockbox') is not None
        info = ipod.identify_ipod(dev.mount, usb_serial, dev.fs, dev.read_only)
        if info is None:
            if dev.kind == 'ipod':
                dev.details['why_not'] = "This iPod has no iPod_Control folder, so it has no library to update."
            return
        caps = ipod.ipod_capabilities(info)
        dev.kind, dev.glyph, dev.model = 'ipod', GLYPH['ipod'], info.name
        dev.details.update({
            'generation': ipod.GENERATIONS.get(info.generation, {}).get('name', 'iPod'),
            'generation_key': info.generation, 'can_write': caps['can_write'], 'why_not': caps['why_not'],
            'formats': caps['formats'], 'sysinfo': info.sysinfo, 'needs': caps['needs'], 'note': caps['note'],
            'checksum': caps['checksum'], 'artwork': caps['artwork'], 'video': caps['video'],
            'rockbox_port': caps['rockbox'], 'library': info.db, 'serial': info.serial or dev.details['serial'],
            'blank': not info.db, 'guid_from': info.guid_from,
            'files_note': '' if caps['can_write'] or dev.details['rockbox'] else ipod.FILES_ONLY})

    def _phone_devices(self) -> list[Device]:
        if not idevice.available():
            return []
        paired, out, now = idevice.paired_udids(), [], time.monotonic()
        for udid in idevice.list_udids():
            is_paired = udid in paired
            c = self._phones.get(udid)
            if not c or c['paired'] != is_paired or now - c['at'] > 30:     # the phone is asked again every 30 s
                info = idevice.device_info(udid, is_paired)
                c = self._phones[udid] = {'paired': is_paired, 'at': now, 'info': info, 'cap': None}
            afc = idevice.find_afc(udid)
            if afc and c['cap'] is None:
                c['cap'] = idevice.capacity(udid)
            total, free = c['cap'] or (0, 0)
            info, (pair_state, pair_text) = c['info'], self._pairing.get(udid, ('', ''))
            state = 'mounted' if afc else 'paired' if is_paired else pair_state or 'unpaired'
            out.append(Device(
                id=udid, kind='iphone', name=info.get('name') or 'iPhone', vendor='Apple', mount=afc, fs='afc',
                model=info.get('product') or info.get('family', 'iPhone'), capacity=total, free=free,
                mounted=afc is not None, glyph=GLYPH['iphone'],
                details={'udid': udid, 'paired': is_paired, 'ios': info.get('ios', ''), 'state': state,
                         'product': info.get('product', ''), 'family': info.get('family', 'iPhone'),
                         'message': pair_text or ('' if is_paired else idevice.TRUST_HELP),
                         'note': idevice.IOS_NOTE, 'ifuse': bool(idevice.tools()['ifuse'])}))
        return out

    # jobs
    def _find(self, device_id: str) -> Device | None:
        return next((d for d in self._devices if d.id == device_id), None)

    def _publish(self, job: _Job, force: bool = False):
        now = time.monotonic()
        if self._closed or (not force and now - job.last < 0.125):
            return
        job.last = now
        try:
            self.job_updated.emit(job.id, job.dict())
        except RuntimeError:                      # the monitor was deleted while a worker was finishing
            pass

    def _new_job(self, kind: str, device_id: str) -> _Job:
        job = _Job('job-%d' % next(self._ids), kind, device_id)
        job.publish = lambda force=False: self._publish(job, force)
        self._jobs[job.id] = job
        return job

    def _start(self, kind: str, device_id: str, fn, *args) -> str:
        job = self._new_job(kind, device_id)
        with self._lock:
            self._busy[device_id] = self._busy.get(device_id, 0) + 1
        job.publish(True)
        self._thread(self._run, job, fn, args)
        return job.id

    def _finished(self, kind: str, device_id: str, state: str, text: str) -> str:
        """A job that is over before it started: a refusal, or nothing to do."""
        job = self._new_job(kind, device_id)
        job.state, job.text, job.error = state, text, text if state == 'error' else ''
        QTimer.singleShot(0, lambda: job.publish(True))
        return job.id

    def _run(self, job: _Job, fn, args):
        job.state = 'running'
        job.publish(True)
        try:
            fn(job, *args)
            job.state = 'done'
        except _Cancelled:
            job.state, job.text = 'cancelled', 'Cancelled.'
        except ipod.IpodError as e:
            if e.cancelled:
                job.state, job.text = 'cancelled', 'Cancelled.'
            else:
                job.state, job.error, job.text = 'error', str(e), str(e)
        except DeviceError as e:
            job.state, job.error, job.text = 'error', str(e), str(e)
        except Exception as e:
            log.exception("%s on %s", job.kind, job.device_id)
            job.state = 'error'
            job.error = job.text = "Something went wrong: %s" % (str(e) or type(e).__name__)
        finally:
            with self._lock:
                self._busy[job.device_id] -= 1
            job.sub = 0.0
            log.info("device job %s %s: %s", job.kind, job.state, job.error or job.text)
            job.publish(True)
            if not self._closed:
                self._wake.emit()

    def _drive_busy(self, dev: Device) -> bool:
        """Is a job running on this device, or on another volume of the same drive?"""
        ids = {dev.id} | {d.id for d in self._devices
                          if dev.details.get('drive') and d.details.get('drive') == dev.details['drive']}
        return any(self._busy.get(i, 0) for i in ids)

    def cancel(self, job_id: str):
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.cancel.set()
        p, s = job.proc, job.session
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
        if s is not None:
            s.stop()                    # the job's own thread sees the helper go and tidies up in abort()

    def jobs(self) -> list[dict]:
        return [j.dict() for j in self._jobs.values()]

    def tools(self) -> dict:
        """What this machine has, for an empty state that says what to install: 'udisks' is the way drives are
        found ('udisks2', 'mounts' or '' before the first look), 'ffmpeg' and 'idevice' say whether converting and
        iPhones work, 'libgpod' is {'ok','message'} or None until something needed to know."""
        return {'udisks': getattr(self._backend, 'name', ''), 'ffmpeg': bool(ffmpeg_path()),
                'idevice': idevice.available(), 'ifuse': bool(idevice.tools()['ifuse']),
                'libgpod': ipod._status, 'idevice_hint': idevice.INSTALL_HINT}

    # what a person can ask for
    def mount(self, device_id: str) -> str:
        """Mount a drive. For an iPhone this is also where pairing happens: it asks the phone to trust this
        computer, waits for the tap, then opens the phone's media area."""
        dev = self._find(device_id)
        if dev is None:
            return self._finished('mount', device_id, 'error', _GONE)
        if dev.kind == 'iphone':
            if dev.mounted:
                return self._finished('mount', device_id, 'done', "The phone is open.")
            return self._start('mount', device_id, self._do_phone, dev)
        if dev.details.get('locked'):
            return self._finished('mount', device_id, 'error', "This drive is encrypted. Open it in your file "
                                                               "manager first, then it shows up here.")
        if dev.mounted:
            return self._finished('mount', device_id, 'done', "It is already mounted.")
        return self._start('mount', device_id, self._do_mount, self._entries.get(device_id))

    pair = mount

    def unmount(self, device_id: str) -> str:
        dev = self._find(device_id)
        if dev is None or not dev.mounted:
            return self._finished('unmount', device_id, 'done', "It is not mounted.")
        if self._drive_busy(dev):
            return self._finished('unmount', device_id, 'error', _BUSY)
        return self._start('unmount', device_id, self._do_unmount, dev)

    def eject(self, device_id: str) -> str:
        """Save what is pending, unmount, power the drive off. Refuses while a copy is running."""
        dev = self._find(device_id)
        if dev is None:
            return self._finished('eject', device_id, 'error', _GONE)
        if self._drive_busy(dev):
            return self._finished('eject', device_id, 'error', _BUSY)
        return self._start('eject', device_id, self._do_eject, dev)

    def profiles(self, device_id: str) -> list[dict]:
        dev = self._find(device_id)
        return profiles_for(self._target(dev)[0]) if dev is not None and dev.kind != 'iphone' else []

    def backups(self, device_id: str) -> list[dict]:
        """The backups of an iPod's library that AudioCrate made, newest first. Reads a few small files."""
        dev = self._find(device_id)
        info = self._ipod_info(dev) if dev is not None and dev.mounted else None
        return ipod.list_backups(info, dev.id) if info is not None else []

    def restore(self, device_id: str, backup_id: str) -> str:
        """Put an iPod's library back as a backup holds it."""
        dev = self._find(device_id)
        if dev is None or not dev.mounted or dev.kind != 'ipod':
            return self._finished('restore', device_id, 'error', "That iPod is not mounted.")
        if self._drive_busy(dev):
            return self._finished('restore', device_id, 'error', _BUSY)
        return self._start('restore', device_id, self._do_restore, dev, backup_id)

    def scan(self, device_id: str, subpath: str = '') -> int:
        """List the music on a device in a worker thread: scan_ready(id, items, info) or scan_failed(id, text).
        An iPod lists its library when subpath is empty and the files below subpath otherwise."""
        rid = next(self._ids)
        dev = self._find(device_id)
        if dev is None or not dev.mounted:
            text = "Mount the device first." if dev else _GONE
            QTimer.singleShot(0, lambda: self.scan_failed.emit(rid, text))
        else:
            self._thread(self._scan_worker, rid, dev, subpath, wait=False)
        return rid

    def copy_to(self, device_id: str, paths: list[Path], profile: str = 'auto', subdir: str = '',
                layout: str = 'tree', library: bool | None = None, playlist: str = '') -> str:
        """Copy music onto a device. profile is one of profiles(). subdir is the folder below the device's root
        (Music when empty, '.' for the top). layout is 'tree' (Artist/Album/NN - Title) or 'flat'. library forces
        (True) or forbids (False) updating an iPod's own library instead of copying files. playlist, when given,
        makes the songs a playlist of that name: in an iPod's library, or Playlists/<name>.m3u8 on a drive."""
        dev = self._find(device_id)
        if dev is not None and dev.kind == 'iphone':
            return self._finished('copy_to', device_id, 'error', idevice.IOS_NOTE)
        if dev is None or not dev.mounted:
            return self._finished('copy_to', device_id, 'error', "Mount the device first.")
        if dev.read_only:
            why = (ipod._READ_ONLY_HFS if 'hfs' in dev.fs
                   else "This device is mounted read only, so nothing can be copied to it.")
            return self._finished('copy_to', device_id, 'error', why)
        if self._busy.get(device_id):
            return self._finished('copy_to', device_id, 'error',
                                  "Another job is running on this device. Wait for it to finish first.")
        return self._start('copy_to', device_id, self._do_copy_to, dev, [Path(p) for p in paths], profile, subdir,
                           layout, library, playlist)

    def copy_from(self, device_id: str, items: list[dict], dest_dir: Path) -> str:
        """Copy songs off a device into dest_dir. Songs from an iPod or iPhone get real names (Artist/Album/
        NN - Title) and tags, files from a drive keep their folders. Nothing is overwritten."""
        dev = self._find(device_id)
        if dev is None or not dev.mounted:
            return self._finished('copy_from', device_id, 'error', "Mount the device first.")
        return self._start('copy_from', device_id, self._do_copy_from, dev, list(items), Path(dest_dir))

    def shutdown(self):
        """Cancel every job, kill every child process, stop watching."""
        self._closed = True
        self.stop()
        for j in self._jobs.values():
            if j.state in ('queued', 'running'):
                self.cancel(j.id)
        end = time.monotonic() + 0.3                  # the threads are daemons: what is still running goes with us
        while self._threads and time.monotonic() < end:
            idevice.kill_all()                        # a worker starts the next tool as soon as one is killed
            time.sleep(0.01)
        idevice.kill_all()

    # the work
    def _target(self, dev: Device, library: bool | None = None) -> tuple[Target, str]:
        """What copying to this device does: (Target, 'db' to update an iPod's library or 'files')."""
        d = dev.details
        writable = dev.kind == 'ipod' and d.get('can_write')
        if writable and (library or (library is None and not d.get('rockbox'))):
            fm = d['formats']
            return Target(frozenset(fm), None, 'aac' if 'aac' in fm else 'mp3', True), 'db'
        if d.get('rockbox'):
            return Target(None, ROCKBOX_EXTS, 'mp3'), 'files'
        return Target(), 'files'

    def _library_keys(self, dev: Device) -> set:
        """The songs an iPod's library lists already, as _song_key makes them."""
        try:
            lib = ipod.read_library(dev.mount)
        except ipod.IpodError:
            return set()
        return {_song_key(i) for i in lib.items}

    def _ipod_info(self, dev: Device):
        serial = ((self._entries.get(dev.id) or {}).get('drive') or {}).get('Serial') or ''
        return ipod.identify_ipod(dev.mount, serial, dev.fs, dev.read_only) if dev.mount else None

    def _do_mount(self, job, e):
        if e is None:
            raise DeviceError(_GONE)
        ok, text = self._backend.mount(e)
        if not ok:
            raise DeviceError(text)
        job.text = "Mounted."

    def _do_unmount(self, job, dev):
        job.text = "Saving files"
        job.publish()
        flush(dev.mount)
        if dev.kind == 'iphone':
            ok, text = idevice.unmount_afc(dev.id)
        else:
            ok, text = self._backend.unmount(self._entries[dev.id])
        if not ok:
            raise DeviceError(text or "It could not be unmounted.")
        job.text = "Unmounted."

    def _do_eject(self, job, dev):
        job.text = "Saving files"
        job.publish()
        if dev.mount:
            flush(dev.mount)
        if dev.kind == 'iphone':
            if dev.mounted:
                phone_files.unmount_apps(dev.id)        # the apps area is only open when the Files view asked for it
                ok, text = idevice.unmount_afc(dev.id)
                if not ok:
                    raise DeviceError("The phone could not be closed: %s" % text)
                self._phones.pop(dev.id, None)
            job.text = "Done. You can unplug the phone."
            return
        e = self._entries.get(dev.id)
        if e is None:
            raise DeviceError(_GONE)
        drive = e.get('drive_path')
        disk = parent_disk(dev.block) if not drive and dev.block.startswith('/dev/') else ''
        job.text = "Unmounting"
        job.publish()
        for d in self._devices:       # every volume of the drive, or the power off would pull one from under a user
            if d.mounted and (d.id == dev.id or (drive and d.details.get('drive') == drive)
                              or (disk and d.block.startswith('/dev/') and parent_disk(d.block) == disk)):
                ok, text = self._backend.unmount(self._entries[d.id])
                if not ok and 'not mounted' not in text.lower():
                    raise DeviceError("%s could not be unmounted. %s" % (d.name, text))
        sib = e['drive'].get('SiblingId')
        if sib and any(x.get('drive_path') != drive and x['drive'].get('SiblingId') == sib
                       for x in self._entries.values()):
            job.text = ("Unmounted. It shares its connection with another drive, so it was not powered off. "
                        "Unplug it after ejecting the other one too.")
        elif drive and e['drive'].get('CanPowerOff'):
            job.text = "Powering off"
            job.publish()
            ok, text = self._backend.power_off(e)
            if not ok:
                raise DeviceError("Unmounted, but the drive could not be powered off: %s" % text)
            job.text = "Safe to remove."
        else:
            job.text = "Unmounted. It is safe to remove."

    def _do_phone(self, job, dev):
        udid = dev.id
        end = time.monotonic() + 60
        while not (dev.details.get('paired') or udid in idevice.paired_udids()):
            state, text = idevice.pair(udid)
            self._pairing[udid] = (state, text)
            job.text = text
            job.publish(True)
            self._wake.emit()
            if state == 'paired':
                break
            if state != 'waiting' or time.monotonic() > end:
                raise DeviceError(text)
            if job.cancel.wait(2):
                raise _Cancelled
        self._pairing.pop(udid, None)
        job.text = "Opening the phone"
        job.publish(True)
        path, err = idevice.mount_afc(udid)
        if path is None:
            raise DeviceError(err)
        self._phones.pop(udid, None)
        job.text = "The phone is open."

    def _do_restore(self, job, dev, backup_id):
        job.text = "Putting the backup back"
        job.publish(True)
        info = self._ipod_info(dev)
        if info is None:
            raise DeviceError("That iPod is not mounted.")
        n = ipod.restore_backup(info, backup_id, dev.id)
        job.text = "Restored %d files. The iPod's library is back as it was." % n

    # scanning
    def _scan_worker(self, rid: int, dev: Device, subpath: str):
        try:
            if dev.kind == 'ipod' and not subpath:
                items, info = self._scan_ipod(dev)
            elif dev.kind == 'iphone':
                items, info = self._scan_phone(dev, subpath)
            else:
                items, info = self._scan_files(dev, subpath)
            self.scan_ready.emit(rid, items, info)
        except (DeviceError, ipod.IpodError) as e:
            self.scan_failed.emit(rid, str(e))
        except OSError as e:
            self.scan_failed.emit(rid, "The device could not be read: %s." % (e.strerror or e))
        except Exception:
            log.exception("scanning %s", dev.id)
            self.scan_failed.emit(rid, "Something went wrong while reading the device.")

    @staticmethod
    def _music_below(base: Path, root: Path, tags: dict | None = None, skip_top: tuple = ()) -> tuple[list[dict], bool]:
        """Every song below base as a scan item, and whether the list was cut short. tags maps a path below
        root to tags already known, so the file need not be opened. Only regular files count: a pipe would block
        the scan and a link may lead out of the device. skip_top names folders left out at the top of base."""
        items, skip_dirs, first = [], ('System Volume Information', 'lost+found', '$RECYCLE.BIN'), True
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in skip_dirs
                             and not (first and d.casefold() in skip_top))
            first = False
            for f in sorted(files):
                if Path(f).suffix.lower() in AUDIO_EXTS and not f.startswith('.'):
                    p = Path(dirpath) / f
                    try:
                        if not stat.S_ISREG(os.lstat(p).st_mode):
                            continue
                    except OSError:
                        continue
                    if len(items) >= MAX_SCAN:
                        return items, True
                    items.append(scan_item(p, root, (tags or {}).get(str(p.relative_to(root)))))
        return items, False

    def _scan_files(self, dev: Device, subpath: str):
        # an iPod's own music is under names like F07/ABCD.mp3, which its library view lists properly
        skip = ('ipod_control',) if dev.kind == 'ipod' and not subpath.strip('./') else ()
        items, truncated = self._music_below(safe_join(dev.mount, subpath), dev.mount, skip_top=skip)
        return items, {'kind': dev.kind, 'device': dev.id, 'subpath': subpath, 'count': len(items),
                       'truncated': truncated, 'rockbox': dev.details.get('rockbox', False)}

    def _scan_ipod(self, dev: Device):
        info = {'kind': 'ipod', 'device': dev.id, 'name': '', 'source': '', 'count': 0, 'missing': 0, 'playlists': [],
                'truncated': False, 'blank': not dev.details.get('library')}
        if info['blank']:
            return [], info                                 # nothing to read yet, which is not an error
        lib = ipod.read_library(dev.mount)
        info.update(name=lib.name, source=lib.source, count=len(lib.items), missing=lib.missing,
                    playlists=[{'name': p['name'], 'ids': p['ids']} for p in lib.playlists if not p['master']])
        return lib.items, info

    def _scan_phone(self, dev: Device, subpath: str):
        root = dev.mount
        info = {'kind': 'iphone', 'device': dev.id, 'subpath': subpath, 'folders': []}
        if subpath:
            items, truncated = self._music_below(safe_join(root, subpath), root)
        else:
            titles = idevice.read_media_library(root)
            known = {k: {**blank_tags(Path(k)), **v} for k, v in titles.items()}
            items, truncated = self._music_below(root / 'iTunes_Control' / 'Music', root, known)
            info.update(folders=idevice.media_folders(root), library=bool(titles))
        info.update(count=len(items), truncated=truncated)
        return items, info

    # copying
    def _prepare(self, job: _Job, it: dict, tmp: Path) -> tuple[Path, dict]:
        """The file to put on the device for one item: the source as it is, or a converted copy in tmp. Returns
        it with its tags."""
        plan, src, tags = it['plan'], it['src'], it['tags']
        if plan['action'] == 'copy':
            return src, tags
        out = tmp / ('%d%s' % (job.done_files, plan['ext']))
        source, conv_tags, title = src, tags, ''
        if plan['render']:
            job.text = "Rendering %s" % src.name
            job.publish()
            wav = run_until_cancel(self.renderers[src.suffix.lower()], job.cancel, src, job.cancel)
            if job.cancel.is_set():
                raise _Cancelled
            if not wav:
                raise ConvertError("%s could not be turned into audio." % src.name)
            source, conv_tags, title = Path(wav), read_tags(Path(wav)), src.stem
        convert_audio(job, source, out, plan['fmt'], conv_tags, title)
        if plan['fmt'] != 'mp3' and len(conv_tags.get('artist_values') or ()) > 1:
            put_artists(out, conv_tags['artist_values'])        # M4A keeps several artists as separate values
        result = read_tags(out)
        if title:
            result['title'] = title
        return out, result

    def _plan_copy(self, job, dev, sources, target, profile, base, layout):
        """Decide for every file what happens to it. Returns (items to do, why files are skipped, how many are
        on the device already, the files in order for a playlist)."""
        fat = dev.fs in FAT_32_FS
        todo, skipped, taken, already, entries = [], [], {}, 0, []
        listed = self._library_keys(dev) if base is None else set()
        for src in sources:
            if job.cancel.is_set():
                raise _Cancelled
            tags = read_tags(src)
            plan = plan_item(src, tags, target, profile, src.suffix.lower() in self.renderers)
            size = src.stat().st_size
            if plan['action'] == 'copy' and fat and size > FAT_FILE_LIMIT:
                plan = dict(plan, action='skip', why='is bigger than the 4 GB a FAT32 drive can hold')
            if plan['action'] == 'skip':
                skipped.append("%s %s" % (src.name, plan['why']))
                continue
            it = {'src': src, 'tags': tags, 'plan': plan, 'dest': None,
                  'size': estimate_size(plan, size, tags['duration'])}
            if base is None and _song_key(tags) in listed:
                already += 1
                continue
            listed.add(_song_key(tags))
            if base is not None:
                dest = dest_path(base, tags, src, plan['ext'], layout)
                if dest.exists() and (plan['action'] == 'convert' or dest.stat().st_size == size):
                    already += 1
                    entries.append((dest, tags))
                    continue
                it['dest'] = free_path(dest, taken)
                entries.append((it['dest'], tags))
            todo.append(it)
        return todo, skipped, already, entries

    def _do_copy_to(self, job, dev, paths, profile, subdir, layout, library, playlist):
        target, mode = self._target(dev, library)
        if profile not in {p['id'] for p in profiles_for(target)}:
            profile = 'auto'
        job.text = "Looking at the files"
        job.publish(True)
        sources = expand_sources(paths)
        if not sources:
            raise DeviceError("There is no music in what was chosen.")
        parts = [p for p in re.split(r'[\\/]+', subdir) if p not in ('', '.')]
        if '..' in parts:
            raise DeviceError("That folder is outside the device.")
        sub = '/'.join(fat_name(p) for p in parts) if parts or subdir == '.' else MUSIC_DIR   # '.': top of the device
        base = safe_join(dev.mount, sub) if mode == 'files' else None
        todo, skipped, already, entries = self._plan_copy(job, dev, sources, target, profile, base, layout)
        job.total_files = len(todo)
        if not todo:
            job.text = ("Everything is already on the device." if already
                        else "Nothing could be copied. %s" % '; '.join(skipped[:2]))
            if already and playlist and base is not None:          # the playlist may still be new
                try:
                    write_playlist(dev.mount, playlist, entries)
                except OSError as e:
                    raise DeviceError(_os_message(e))
                flush(dev.mount)
            return
        need, free = sum(i['size'] for i in todo), shutil.disk_usage(dev.mount).free
        if base is not None and need > free:
            raise DeviceError("There is not enough room on %s: about %d MB needed, %d MB free."
                              % (dev.name, need >> 20, free >> 20))
        for folder in {it['dest'].parent for it in todo if it['dest'] is not None}:
            sweep_parts(folder)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix='job-', dir=self.cache_dir))
        try:
            if mode == 'files':
                counts = self._copy_files(job, dev, todo, tmp, entries, playlist)
            else:
                counts = self._copy_ipod(job, dev, todo, playlist, need, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        job.done_files = job.total_files
        text = "Copied %d" % counts['copied']
        if counts['converted']:
            text += ", converted %d" % counts['converted']
        text += '.' + (" %d already there." % already if already else '')
        if skipped:
            text += " Skipped %d: %s." % (len(skipped), '; '.join(skipped[:2]))
        if counts['failed']:
            text += " %d failed: %s" % (len(counts['failed']), counts['failed'][0])
        if mode == 'files' and dev.kind == 'ipod' and dev.details.get('files_note'):
            text += ' ' + dev.details['files_note']
        if mode == 'db' and dev.details.get('guid_from') == 'usb serial' and dev.details.get('checksum') == 'hash58':
            text += (" The iPod did not report its FireWire id, so its USB serial number was used. If the iPod then "
                     "shows no songs, restore the backup.")
        job.text = text
        if skipped or counts['failed']:
            self.device_message.emit(dev.id, '; '.join(skipped[:3] + counts['failed'][:3]))

    def _copy_files(self, job, dev, todo, tmp, entries, playlist) -> dict:
        counts, albums = {'copied': 0, 'converted': 0, 'failed': []}, {}
        for n, it in enumerate(todo):
            if job.cancel.is_set():
                raise _Cancelled
            job.done_files, job.sub = n, 0.0
            job.text = "Copying %d of %d: %s" % (n + 1, len(todo), it['tags']['title'])
            job.publish()
            try:
                path, _ = self._prepare(job, it, tmp)
            except ConvertError as e:
                counts['failed'].append(str(e))
                continue
            try:
                put_file(path, it['dest'], job)
            finally:
                if path != it['src']:
                    path.unlink(missing_ok=True)
            counts['copied'] += 1
            counts['converted'] += it['plan']['action'] == 'convert'
            albums.setdefault(it['dest'].parent, it['src'])
        job.text = "Saving files"
        job.publish(True)
        if dev.details.get('rockbox'):
            self._album_covers(albums, tmp)
        if playlist:
            try:
                write_playlist(dev.mount, playlist, entries)
            except OSError as e:
                raise DeviceError(_os_message(e))
        flush(dev.mount)
        return counts

    @staticmethod
    def _album_covers(albums: dict, tmp: Path):
        """Rockbox shows a cover.jpg that sits in the album folder."""
        for folder, src in albums.items():
            if not any((folder / n).exists() for n in ('cover.jpg', 'folder.jpg')):
                jpg = cover_jpeg(src, tmp, 500)
                if jpg:
                    try:
                        shutil.copyfile(jpg, folder / 'cover.jpg')
                    except OSError:
                        pass

    def _copy_ipod(self, job, dev, todo, playlist, need, tmp) -> dict:
        info = self._ipod_info(dev)
        session = job.session = ipod.IpodSession(info, uuid=dev.id)
        counts = {'copied': 0, 'converted': 0, 'failed': []}
        art = bool(ipod.ipod_capabilities(info)['artwork'])
        try:
            job.text = "Backing up the iPod's library"
            job.publish(True)
            session.start(need, name=dev.name)
            for n, it in enumerate(todo):
                if job.cancel.is_set():
                    raise _Cancelled
                job.done_files, job.sub = n, 0.0
                job.text = "Copying %d of %d: %s" % (n + 1, len(todo), it['tags']['title'])
                job.publish()
                try:
                    path, t = self._prepare(job, it, tmp)
                except ConvertError as e:
                    counts['failed'].append(str(e))
                    continue
                meta = {k: t[k] for k in ('title', 'artist', 'album', 'albumartist', 'genre', 'track', 'tracks',
                                          'disc', 'discs', 'year')}
                meta.update(ms=int((t['duration'] or 0) * 1000), kbps=t['bitrate'], hz=t['rate'])

                def progress(b, size=it['size']):
                    job.sub = min(b / max(size, 1), 0.99)
                    job.publish()
                try:
                    session.add(path, meta, cover_jpeg(it['src'], tmp) if art else None, playlist, progress)
                finally:
                    if path != it['src']:
                        path.unlink(missing_ok=True)
                counts['copied'] += 1
                counts['converted'] += it['plan']['action'] == 'convert'
            if not counts['copied']:
                session.abort()
                return counts
            job.done_files, job.sub = len(todo), 0.0
            job.text = "Writing the iPod's library"
            job.publish(True)
            session.commit()
        except BaseException:
            session.abort()
            raise
        finally:
            job.session = None
        return counts

    def _do_copy_from(self, job, dev, items, dest_dir):
        root = dev.mount.resolve()
        srcs = []
        for it in items:
            p = Path(it['path'])
            if not p.resolve().is_relative_to(root):
                raise DeviceError("A file is outside the device.")
            srcs.append((p, it))
        if not srcs:
            raise DeviceError("Nothing was chosen.")
        need = sum(p.stat().st_size for p, _ in srcs)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if need > shutil.disk_usage(dest_dir).free:
            raise DeviceError("There is not enough room in that folder: %d MB needed." % (need >> 20))

        def scrambled(it):      # songs an iPod or iPhone keeps under names like F07/ABCD.mp3
            return re.search(r'(iPod|iTunes)_Control/Music/F\d+/', it.get('rel', '').replace('\\', '/')) is not None

        keep = [os.path.dirname(it['rel']) for _, it in srcs if not scrambled(it)]
        common = os.path.commonpath(keep) if keep else ''
        job.total_files = len(srcs)
        taken: dict = {}
        copied = already = 0
        for n, (p, it) in enumerate(srcs):
            if job.cancel.is_set():
                raise _Cancelled
            job.done_files, job.text = n, "Copying %d of %d: %s" % (n + 1, len(srcs), it.get('title') or p.name)
            job.publish()
            if scrambled(it):
                tags = {'title': it.get('title') or p.stem, 'artist': it.get('artist', ''),
                        'album': it.get('album', ''), 'albumartist': it.get('albumartist', ''),
                        'track': it.get('track') or 0, 'disc': it.get('disc') or 0, 'discs': 0}
                dest = dest_path(dest_dir, tags, p, p.suffix.lower(), 'tree')
            else:
                dest = dest_dir / Path(*[fat_name(x) for x in Path(os.path.relpath(it['rel'], common or '.')).parts])
            # a song that had no tags got some written into its copy, so a copy a little larger is the same song
            slack = 65536 if scrambled(it) else 0
            if dest.exists() and 0 <= dest.stat().st_size - p.stat().st_size <= slack:
                already += 1
                continue
            dest = free_path(dest, taken)
            put_file(p, dest, job)
            if scrambled(it):
                write_tags(dest, it)
            copied += 1
        job.text = "Copied %d song%s to %s.%s" % (copied, '' if copied == 1 else 's', dest_dir,
                                                  " %d were already there." % already if already else '')

