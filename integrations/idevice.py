"""iPhone, iPad and iPod touch through libimobiledevice's command line tools and the GVfs AFC mount.

A phone is only ever asked what it will say without pairing. `idevicepair validate`, a plain `ideviceinfo` and
`gio mount afc://` all start the "Trust This Computer?" question on the phone when no pair record exists. So
looking for phones uses `idevice_id -l`, `idevicepair list` and `ideviceinfo -s`, and pair() runs only when
someone pressed the button. Every call has a timeout and none of them goes through a shell.
"""
from __future__ import annotations

import os
import plistlib
import re
import selectors
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote

GVFS_ROOT = Path('/run/user/%d/gvfs' % os.getuid())
INSTALL_HINT = ("Install libimobiledevice, usbmuxd and gvfs-afc (usually the packages libimobiledevice, usbmuxd "
                "and gvfs) to use an iPhone or iPad.")
IOS_NOTE = ("iOS does not let other programs add music to the Music app. "
            "You can send files to a player app that uses file sharing.")
TRUST_HELP = "Wake the phone and enter its passcode if it asks, then tap Trust when the question comes up."
# folders AFC shows in the media area, and what they hold
FOLDERS = {'iTunes_Control/Music': 'music', 'Recordings': 'voice memos', 'Books': 'books',
           'Podcasts': 'podcasts', 'Downloads': 'downloads', 'DCIM': 'photos and videos',
           'Purchases': 'purchases'}
_UDID = re.compile(r'[0-9A-Fa-f-]{20,48}')


def tools() -> dict:
    """Where each tool is, '' when it is missing: idevice_id, ideviceinfo, idevicepair, gio and ifuse."""
    return {n: shutil.which(n) or '' for n in ('idevice_id', 'ideviceinfo', 'idevicepair', 'gio', 'ifuse')}


def available() -> bool:
    t = tools()
    return bool(t['idevice_id'] and t['ideviceinfo'] and t['idevicepair'])


_live: set = set()                  # the tools that are running, so that quitting can stop them
_live_lock = threading.Lock()
LIMIT = 1 << 20                     # bytes of output a tool may print before it is stopped


def kill_all():
    """Stop every tool that is running now. The program calls this when it quits, so none is left behind."""
    with _live_lock:
        procs = list(_live)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def _drain(p: subprocess.Popen, end: float, limit: int) -> tuple[bytes, bytes, str]:
    """Read stdout and stderr until both close, but no more than `limit` bytes of either and not past `end`.
    The third item says why it stopped early, 'late' or 'big', '' when the tool finished by itself."""
    got = {p.stdout: bytearray(), p.stderr: bytearray()}
    with selectors.DefaultSelector() as sel:
        for f in got:
            sel.register(f, selectors.EVENT_READ)
        while sel.get_map():
            left = end - time.monotonic()
            if left <= 0:
                return bytes(got[p.stdout]), bytes(got[p.stderr]), 'late'
            for key, _ in sel.select(left):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                got[key.fileobj] += chunk
                if len(got[key.fileobj]) > limit:
                    return bytes(got[p.stdout]), bytes(got[p.stderr]), 'big'
    return bytes(got[p.stdout]), bytes(got[p.stderr]), ''


def _run(args: list[str], timeout: float = 10, limit: int = LIMIT) -> tuple[int, str, str]:
    """(exit code, stdout, stderr). -1 and an explanation in stderr when the tool is missing, too slow or prints
    more than `limit` bytes. A tool that is stopped is killed, so it never outlives this call."""
    # gio waits for every volume monitor to start, and the camera monitor (gvfs-gphoto2) hangs for 30 s
    # whenever a phone is plugged in, so a mount took half a minute. The mount itself needs none of them.
    env = dict(os.environ, GVFS_REMOTE_VOLUME_MONITOR_IGNORE='1') if args and args[0] == 'gio' else None
    try:
        p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except FileNotFoundError:
        return -1, '', '%s is not installed' % args[0]
    except OSError as e:
        return -1, '', str(e)
    with _live_lock:
        _live.add(p)
    end = time.monotonic() + timeout
    try:
        out, err, why = _drain(p, end, limit)
        if not why:
            try:
                p.wait(max(end - time.monotonic(), 0.05))
            except subprocess.TimeoutExpired:
                why = 'late'
        if why:
            p.kill()
        p.wait()
    finally:
        with _live_lock:
            _live.discard(p)
        p.stdout.close()
        p.stderr.close()
    if why == 'late':
        return -1, '', '%s did not answer in %d seconds' % (args[0], timeout)
    if why:
        return -1, '', '%s printed too much' % args[0]
    return p.returncode, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


def _udids(args: list[str]) -> list[str]:
    rc, out, _ = _run(args, 8)
    lines = dict.fromkeys(line.strip() for line in out.splitlines())      # a phone listed twice is one phone
    return [u for u in lines if _UDID.fullmatch(u)] if rc == 0 else []


def list_udids() -> list[str]:
    """UDIDs of the phones on USB, from `idevice_id -l`. Empty when none, or when the tool is missing."""
    return _udids(['idevice_id', '-l'])


def paired_udids() -> set[str]:
    """UDIDs that have a pair record on this computer, from `idevicepair list`. Asks no phone anything."""
    return set(_udids(['idevicepair', 'list']))


def _plain(text: str) -> str:
    """text without control characters, line breaks and the invisible marks that reorder or hide text."""
    return ''.join(c for c in text if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp')).strip()


def device_info(udid: str, paired: bool = False) -> dict:
    """What the phone reports: {'name','product','ios','build','serial','udid','family'}. Without a pair record
    only the keys the phone gives to anyone are asked for (`ideviceinfo -s`). {} when it does not answer."""
    rc, out, _ = _run(['ideviceinfo', '-u', udid] + ([] if paired else ['-s']) + ['-x'], 12)
    try:
        d = plistlib.loads(out.encode()) if rc == 0 else {}
    except Exception:
        d = {}
    if not isinstance(d, dict) or not d:
        return {}
    product = str(d.get('ProductType', ''))
    family = 'iPad' if product.startswith('iPad') else 'iPod touch' if product.startswith('iPod') else 'iPhone'
    return {'name': _plain(str(d.get('DeviceName') or '')) or family, 'product': product, 'family': family,
            'udid': udid, 'ios': str(d.get('ProductVersion', '')), 'build': str(d.get('BuildVersion', '')),
            'serial': str(d.get('SerialNumber', ''))}


def pair(udid: str) -> tuple[str, str]:
    """Ask the phone to trust this computer. Returns (state, sentence), state being paired, waiting, locked,
    denied, no_device or failed. While the question is on the screen the answer is 'waiting' and this may be
    called again."""
    rc, out, err = _run(['idevicepair', '-u', udid, 'pair'], 25)
    text = (out + ' ' + err).lower()
    if rc == 0 and 'success' in text:
        return 'paired', "The phone trusts this computer now."
    if 'denied' in text:
        return 'denied', "Trust was declined on the phone. Unplug it, plug it in again and tap Trust."
    if 'passcode' in text or 'locked' in text:
        return 'locked', "The phone is locked. Enter its passcode on the phone, then try again."
    if 'trust dialog' in text or 'accept the trust' in text:
        return 'waiting', "Tap Trust on the phone."
    if 'no device' in text or 'not found' in text:
        return 'no_device', "The phone is not connected."
    return 'failed', "Pairing did not work. Unplug the phone, plug it in again and try once more."


def find_afc(udid: str) -> Path | None:
    """The folder GVfs made for this phone's media area, if it is mounted."""
    try:
        for e in os.listdir(GVFS_ROOT):
            m = re.fullmatch(r'afc:host=([^,]+)(?:,port=1)?', e)
            if m and unquote(m.group(1)).lower() == udid.lower():
                return GVFS_ROOT / e
    except OSError:
        pass
    return None


def mount_afc(udid: str, wait: float = 4.0) -> tuple[Path | None, str]:
    """Mount the media area. The phone must be paired already. Returns (folder, '') or (None, a sentence)."""
    p = find_afc(udid)
    if p:
        return p, ''
    rc, out, err = _run(['gio', 'mount', 'afc://%s/' % udid], 40)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        p = find_afc(udid)
        if p:
            return p, ''
        time.sleep(0.2)
    if rc == -1:
        return None, "The phone could not be mounted: %s." % (err.strip() or 'gio failed')
    return None, "The phone could not be mounted. %s" % (err.strip().splitlines()[-1] if err.strip() else TRUST_HELP)


def unmount_afc(udid: str) -> tuple[bool, str]:
    rc, out, err = _run(['gio', 'mount', '-u', 'afc://%s/' % udid], 20)
    return rc == 0, err.strip()


def capacity(udid: str) -> tuple[int, int]:
    """(total, free) bytes of the media area, from `gio info -f`. (0, 0) when it cannot be told."""
    rc, out, _ = _run(['gio', 'info', '-f', 'afc://%s/' % udid], 10)
    n = {k: int(v) for k, v in re.findall(r'filesystem::(size|free):\s*(\d+)', out)}
    return n.get('size', 0), n.get('free', 0)


def media_folders(root: Path) -> list[dict]:
    """The media folders that exist on the phone: [{'name','path','what','entries'}]. Everything is optional."""
    out = []
    for name, what in FOLDERS.items():
        p = root / name
        try:
            if p.is_dir():
                out.append({'name': name, 'path': str(p), 'what': what, 'entries': len(os.listdir(p))})
        except OSError:
            pass
    return out


_LIBRARY_QUERIES = (
    # iOS 5 and later: title and file name are in item_extra, artist and album in tables of their own
    """SELECT b.path || '/' || e.location, e.title, a.item_artist, al.album, e.track_number, e.total_time_ms
       FROM item i JOIN item_extra e ON e.item_pid = i.pid
       LEFT JOIN item_artist a ON a.item_artist_pid = i.item_artist_pid
       LEFT JOIN album al ON al.album_pid = i.album_pid
       LEFT JOIN base_location b ON b.base_location_id = i.base_location_id WHERE e.location != ''""",
    # the older layout that libgpod documents
    """SELECT b.path || '/' || l.location, i.title, i.artist, i.album, i.track_number, i.total_time_ms
       FROM item i JOIN location l ON l.item_pid = i.pid
       LEFT JOIN base_location b ON b.id = l.base_location_id""")


def read_media_library(root: Path) -> dict[str, dict]:
    """Titles from iTunes_Control/iTunes/MediaLibrary.sqlitedb: {path below root: {'title','artist','album',
    'track','duration'}}. Opened read only and immutable, and only the layouts known to exist are tried. {} when
    the file is not there or is not one of them, and the caller lists files instead."""
    db = root / 'iTunes_Control' / 'iTunes' / 'MediaLibrary.sqlitedb'
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect('file:%s?mode=ro&immutable=1' % quote(str(db)), uri=True, timeout=3)
    except sqlite3.Error:
        return {}
    try:
        for q in _LIBRARY_QUERIES:
            try:
                rows = con.execute(q).fetchall()
            except sqlite3.Error:
                continue
            return {str(r[0]).strip('/'): {'title': r[1] or '', 'artist': r[2] or '', 'album': r[3] or '',
                                           'track': int(r[4] or 0), 'duration': (r[5] or 0) / 1000 or None}
                    for r in rows if r[0]}
    finally:
        con.close()
    return {}

