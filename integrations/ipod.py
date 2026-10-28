"""iPods: which models exist and what can be done with each, a reader for the iTunesDB that needs no
library, backups with restore, and a writer that adds songs through libgpod.

Reading is plain Python. libgpod is only called from a helper process (`python ipod.py serve`), so a
crash inside the C library cannot take the player down, and a write is always the last step of a session
that has already made a backup.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from ctypes import (POINTER, byref, c_char_p, c_double, c_float, c_int, c_int16, c_int32, c_long, c_uint,
                    c_uint8, c_uint16, c_uint32, c_uint64, c_void_p)
from dataclasses import dataclass, field
from pathlib import Path

BACKUP_ROOT = Path.home() / ".config" / "audiocrate" / "devices" / "backups"
KEEP_BACKUPS = 3
MAX_DB = 160 << 20          # bytes: the biggest iTunesDB read, compressed or not (100,000 songs need about 100 MB)
MAX_SYSINFO = 1 << 20       # bytes: SysInfo and SysInfoExtended are a few KB, a bigger one is damaged
_KILL_GRACE = 5.0           # seconds a helper gets to end after SIGTERM before it is killed
FROZEN_HELPER = False       # main.py sets this when it answers --ipod-helper, see helper_cmd

F_ALL = 'mp3 aac alac wav aiff'
# key: (name, checksum, write, stock formats, artwork, video, Rockbox port)
# write: yes, nano5 (only after iTunes has signed a database once), no
# fmt: off
_GEN = {
    'first':     ('iPod 1st generation', 'none', 'yes', 'mp3 wav aiff', 0, 0, 'stable'),
    'second':    ('iPod 2nd generation', 'none', 'yes', 'mp3 wav aiff', 0, 0, 'stable'),
    'third':     ('iPod 3rd generation', 'none', 'yes', 'mp3 aac wav aiff', 0, 0, 'stable'),
    'fourth':    ('iPod 4th generation', 'none', 'yes', F_ALL, 0, 0, 'stable'),
    'photo':     ('iPod photo', 'none', 'yes', F_ALL, 1, 0, 'stable'),
    'video_1':   ('iPod video 5th generation', 'none', 'yes', F_ALL, 1, 1, 'stable'),
    'video_2':   ('iPod video 5.5th generation', 'none', 'yes', F_ALL, 1, 1, 'stable'),
    'mini_1':    ('iPod mini 1st generation', 'none', 'yes', 'mp3 aac wav aiff', 0, 0, 'stable'),
    'mini_2':    ('iPod mini 2nd generation', 'none', 'yes', F_ALL, 0, 0, 'stable'),
    'nano_1':    ('iPod nano 1st generation', 'none', 'yes', F_ALL, 1, 0, 'stable'),
    'nano_2':    ('iPod nano 2nd generation', 'none', 'yes', F_ALL, 1, 0, 'stable'),
    'nano_3':    ('iPod nano 3rd generation', 'hash58', 'yes', F_ALL, 1, 1, 'in development'),
    'nano_4':    ('iPod nano 4th generation', 'hash58', 'yes', F_ALL, 1, 1, 'no'),
    'nano_5':    ('iPod nano 5th generation', 'hash72', 'nano5', F_ALL, 1, 1, 'no'),
    'nano_6':    ('iPod nano 6th generation', 'hashAB', 'no', F_ALL, 0, 0, 'no'),
    'classic_1': ('iPod classic 6th generation', 'hash58', 'yes', F_ALL, 1, 1, 'stable'),
    'classic_2': ('iPod classic 6.5th generation', 'hash58', 'yes', F_ALL, 1, 1, 'stable'),
    'classic_3': ('iPod classic 7th generation', 'hash58', 'yes', F_ALL, 1, 1, 'stable'),
    'shuffle_1': ('iPod shuffle 1st generation', 'none', 'yes', 'mp3 aac wav', 0, 0, 'no'),
    'shuffle_2': ('iPod shuffle 2nd generation', 'none', 'yes', 'mp3 aac wav aiff', 0, 0, 'no'),
    'shuffle_3': ('iPod shuffle 3rd generation', 'none', 'yes', 'mp3 aac wav aiff', 0, 0, 'no'),
    'shuffle_4': ('iPod shuffle 4th generation', 'none', 'yes', F_ALL, 0, 0, 'no'),
    'touch_1':   ('iPod touch 1st generation', 'hash72', 'no', '', 1, 1, 'no'),
    'touch_2':   ('iPod touch 2nd generation', 'hash72', 'no', '', 1, 1, 'no'),
    'touch_3':   ('iPod touch 3rd generation', 'hash72', 'no', '', 1, 1, 'no'),
    'touch_4':   ('iPod touch 4th generation', 'hashAB', 'no', '', 0, 1, 'no'),
    'iphone_1':  ('iPhone', 'hash72', 'no', '', 1, 1, 'no'),
    'iphone_2':  ('iPhone 3G', 'hash72', 'no', '', 1, 1, 'no'),
    'iphone_3':  ('iPhone 3GS', 'hash72', 'no', '', 1, 1, 'no'),
    'iphone_4':  ('iPhone 4', 'hashAB', 'no', '', 0, 1, 'no'),
    'ipad_1':    ('iPad', 'hashAB', 'no', '', 0, 1, 'no'),
    'mobile':    ('Motorola phone', 'none', 'no', '', 1, 0, 'no'),
}
# fmt: on
_IOS = "iOS keeps its music library in a database that only Apple's own sync service can update."
_WHY = {
    'nano_5': "The database needs a device specific signature that can only be copied from a database iTunes "
              "wrote, so this iPod has to be synced with iTunes once first.",
    'nano_6': "The database signature of this model needs an Apple library that AudioCrate cannot use.",
    'mobile': "This is a phone, not an iPod.",
    **dict.fromkeys(('touch_1', 'touch_2', 'touch_3', 'touch_4', 'iphone_1', 'iphone_2', 'iphone_3', 'iphone_4',
                     'ipad_1'), _IOS),
}
_NOTE = {'shuffle_3': "New songs get no spoken track names on this model.",
         'shuffle_4': "New songs get no spoken track names on this model."}

# model number as libgpod writes it (the first letter of ModelNumStr dropped): generation and marketed gigabytes
_MODELS = """
first 5: 8513 8541 8697
first 10: 8709
second 10: 8737 8740
second 20: 8738 8741
third 10: 8976
third 15: 8946 9460
third 20: 9244
third 30: 8948
third 40: 9245
fourth 20: 9282
fourth 25: 9787
fourth 40: 9268 E436
mini_1 4: 9160 9436 9435 9434 9437
mini_2 4: 9800 9802 9804 9806
mini_2 6: 9801 9803 9805 9807
photo 20: A079 A127
photo 30: 9829 S492
photo 40: 9585
photo 60: 9830 9586
shuffle_1 0.5: 9724
shuffle_1 1: 9725
shuffle_2 1: A546 A947 A949 A951 A953 C167 B225 B233 B231 B227 B228 B229
shuffle_2 2: B518 B520 B522 B524 B526
shuffle_3 2: C306 C323 C381 C384 C387
shuffle_3 4: B867 C164 C303 C307 C328 C331
shuffle_4 2: C584 C585 C749 C750 C751
nano_1 1: A350 A352
nano_1 2: A004 A099
nano_1 4: A005 A107
video_1 30: A002 A146 A452
video_1 60: A003 A147
video_2 30: A444 A446 A664
video_2 80: A448 A450
nano_2 2: A477
nano_2 4: A426 A428 A487 A489 A725
nano_2 8: A726 A497
classic_1 80: B029 B147
classic_1 160: B145 B150
classic_2 120: B562 B565
classic_3 160: C293 C297
nano_3 4: A978
nano_3 8: A980 B261 B249 B253 B257
nano_4 4: B480 B651 B654 B657 B660 B663 B666
nano_4 8: B598 B732 B735 B739 B742 B745 B748 B751 B754
nano_4 16: B903 B905 B907 B909 B911 B913 B915 B917 B918
nano_5 8: C027 C031 C034 C037 C040 C043 C046 C049 C050
nano_5 16: C060 C062 C064 C066 C068 C070 C072 C074 C075
nano_6 8: C525 C688 C689 C690 C691 C692 C693
nano_6 16: C526 C694 C695 C696 C697 C698 C699
touch_1 8: A623
touch_1 16: A627
touch_1 32: B376
touch_2 8: B528 C086
touch_2 16: B531
touch_2 32: B533
touch_3 32: C008
touch_3 64: C011
touch_4 8: C540
touch_4 32: C544
touch_4 64: C547
iphone_1 4: A501
iphone_1 8: A712
iphone_1 16: B384
iphone_2 8: B046
iphone_2 16: B500 B048 B496
iphone_3 16: C131
iphone_3 32: C133 C134
iphone_4 16: C603
iphone_4 32: C605
ipad_1 16: B292 C349
ipad_1 32: B293 C496
ipad_1 64: B294 C497
mobile -1: mobile1
"""
# the last three characters of the serial number point at a model number too
_SERIALS = """
LG6=8541 NAM=8541 MJ2=8541 ML1=8709 MME=8709 MMB=8737 MMC=8738 NGE=8740 NGH=8740 MMF=8741
NLW=8946 NRH=8976 QQF=9460 PQ5=9244 PNT=9244 NLY=8948 NM7=8948 PNU=9245 PS9=9282 Q8U=9282
V9V=9787 S2X=9787 PQ7=9268 TDU=A079 TDS=A079 TM2=A127 SAZ=9830 SB1=9830 SAY=9829 R5Q=9585
R5R=9586 R5T=9586 PFW=9160 PRC=9160 QKL=9436 QKQ=9436 QKK=9435 QKP=9435 QKJ=9434 QKN=9434
QKM=9437 QKR=9437 S41=9800 S4C=9800 S43=9802 S45=9804 S47=9806 S4J=9806 S42=9801 S44=9803
S48=9807 RS9=9724 QGV=9724 TSX=9724 PFV=9724 R80=9724 RSA=9725 TSY=9725 C60=9725 VTE=A546
VTF=A546 XQ5=A947 XQS=A947 XQV=A949 XQX=A949 YX7=B228 XQY=A951 YX8=A951 XR1=A953 YXA=B233
YX6=B225 YX9=B225 8CQ=C167 1ZH=B518 UNA=A350 UNB=A350 UPR=A352 UPS=A352 SZB=A004 SZV=A004
SZW=A004 SZC=A005 SZT=A005 TJT=A099 TJU=A099 TK2=A107 TK3=A107 VQ5=A477 VQ6=A477 V8T=A426
V8U=A426 V8W=A428 V8X=A428 VQH=A487 VQJ=A487 VQK=A489 VKL=A489 WL2=A725 WL3=A725 X9A=A726
X9B=A726 VQT=A497 VQU=A497 Y0P=A978 Y0R=A980 YXR=B249 YXV=B257 YXT=B253 YXX=B261 SZ9=A002
WEC=A002 WED=A002 WEG=A002 WEH=A002 WEL=A002 TXK=A146 TXM=A146 WEE=A446 WEF=A146 WEJ=A146
WEK=A146 SZA=A003 SZU=A003 TXL=A147 TXN=A147 V9K=A444 V9L=A444 WU9=A444 VQM=A446 V9M=A446
V9N=A446 V9P=A448 V9Q=A448 V9R=A450 V9S=A450 V95=A450 V96=A450 WUC=A450 W9G=A664 Y5N=B029
YMV=B147 YMU=B145 YMX=B150 2C5=B562 2C7=B565 9ZS=C293 9ZU=C297 37P=B663 37Q=B666 37H=B654
1P1=B480 37K=B657 37L=B660 2ME=B598 3QS=B732 3QT=B735 3QU=B739 3QW=B742 3QX=B745 3QY=B748
3R0=B754 3QZ=B751 5B7=B903 5B8=B905 5B9=B907 5BA=B909 5BB=B911 5BC=B913 5BD=B915 5BE=B917
5BF=B918 71V=C027 71Y=C031 721=C034 726=C037 72A=C040 72F=C046 72K=C049 72L=C050 72Q=C060
72R=C062 72S=C064 72X=C066 734=C068 738=C070 739=C072 73A=C074 73B=C075 CMN=C525 DVX=C688
DVY=C689 DW0=C690 DW1=C691 DW2=C692 DW3=C693 CMP=C526 DW4=C694 DW5=C695 DW6=C696 DW7=C697
DW8=C698 DW9=C699 A1S=C306 A78=C323 ALB=C381 ALD=C384 ALG=C387 4NZ=B867 891=C164 A1L=C303
A1U=C307 A7B=C328 A7D=C331 CMJ=C584 CMK=C585 FDM=C749 FDN=C750 FDP=C751 W4N=A623 W4T=A627
0JW=B376 201=B528 203=B531 75J=C086 6K2=C008 6K4=C011 CP7=C540 CP9=C544 CPC=C547 VR0=A501
WH8=A712 0KH=B384 Y7H=B046 Y7K=B496 3NP=C131 3NR=C133 3NS=C134 A4S=C603 A4T=C605 Z38=B292
Z39=B293 Z3A=B294
"""
# libgpod's Itdb_IpodGeneration in declaration order, to read its table through the helper
_GPOD_GENS = ('unknown first second third fourth photo mobile mini_1 mini_2 shuffle_1 shuffle_2 shuffle_3 nano_1 '
              'nano_2 nano_3 nano_4 video_1 video_2 classic_1 classic_2 touch_1 iphone_1 shuffle_4 touch_2 '
              'iphone_2 iphone_3 classic_3 nano_5 touch_3 ipad_1 iphone_4 touch_4 nano_6').split()


def _build():
    gens = {k: dict(key=k, name=v[0], checksum=v[1], write=v[2], formats=v[3].split(), artwork=bool(v[4]),
                    video=bool(v[5]), rockbox=v[6], why=_WHY.get(k, ''), note=_NOTE.get(k, ''))
            for k, v in _GEN.items()}
    models = {}
    for line in _MODELS.strip().splitlines():
        head, _, rest = line.partition(':')
        gen, cap = head.split()
        models.update({m.upper(): (gen, float(cap)) for m in rest.split()})
    return gens, models, dict(p.split('=') for p in _SERIALS.split())


GENERATIONS, MODELS, SERIAL = _build()
_UNKNOWN_MODEL = ("This iPod is newer than the model list AudioCrate knows, or it does not report a model "
                  "number, so its library is left alone.")
_READ_ONLY = "This iPod is mounted read only, so nothing can be written to it."
_READ_ONLY_HFS = ("This iPod is formatted as Mac OS Extended with journaling, which Linux can only read. "
                  "Restore it as FAT32 from Windows or iTunes to put music on it.")
FILES_ONLY = ("Songs copied as files are stored on the iPod's disk but do not appear in its own menus. "
              "They play with firmware that reads files, such as Rockbox.")


class IpodError(Exception):
    """The message is a plain sentence for the person using the player."""
    cancelled = False


@dataclass
class IpodInfo:
    mount: Path
    control: Path | None = None
    model: str = ''                 # table model number, e.g. 'B565'
    model_str: str = ''             # ModelNumStr as the iPod reports it, e.g. 'MB565'
    generation: str = 'unknown'     # key of GENERATIONS
    name: str = 'iPod'              # 'iPod classic 6.5th generation 120 GB'
    capacity_gb: float = 0.0
    serial: str = ''
    guid: str = ''                  # FireWire id, 16 hex digits
    guid_from: str = ''             # SysInfoExtended, SysInfo or usb serial
    db: str = ''                    # iTunesDB, iTunesCDB or iTunesSD, whichever the iPod has
    rockbox: bool = False
    hashinfo: bool = False
    fs: str = ''                    # file system type when the caller knows it, e.g. 'vfat' or 'hfsplus'
    read_only: bool | None = None   # None: find out by asking for write access
    sysinfo: dict = field(default_factory=dict)


def lookup_model(model: str = '', serial: str = '') -> tuple[str, str]:
    """(table model number, generation key) in libgpod's order: the serial's last three characters first, then
    ModelNumStr without its first letter. ('', 'unknown') when neither is in the table."""
    m = SERIAL.get(serial[-3:].upper()) if len(serial) >= 3 else None
    if not m and model:
        m = (model[1:] if model[:1].isalpha() else model).upper()
    return (m, MODELS[m][0]) if m in MODELS else ('', 'unknown')


def _ci(parent: Path | None, name: str) -> Path | None:
    """parent/name with the case it has on disk. FAT and HFS+ do not care about case, Linux listings do."""
    if parent is None:
        return None
    p = parent / name
    if p.exists():
        return p
    try:
        want = name.casefold()
        return next((parent / e.name for e in os.scandir(parent) if e.name.casefold() == want), None)
    except OSError:
        return None


def _read_small(path: Path | None) -> bytes | None:
    """The bytes of a small file, None when it is missing or bigger than MAX_SYSINFO. It reads at most one byte
    more than the limit, so a huge file on a stick costs nothing."""
    if path is None:
        return None
    try:
        with open(path, 'rb') as f:
            raw = f.read(MAX_SYSINFO + 1)
    except OSError:
        return None
    return raw if len(raw) <= MAX_SYSINFO else None


def _sysinfo(dev: Path | None) -> tuple[dict, dict]:
    """The Key: value lines of SysInfo and the plist of SysInfoExtended. Either is {} when it is missing."""
    plain, ext = {}, {}
    raw = _read_small(_ci(dev, 'SysInfo'))
    for line in (raw or b'').decode('utf-8', 'replace').splitlines():
        k, sep, v = line.partition(':')
        if sep and k.strip():
            plain[k.strip()] = v.strip()
    try:
        ext = plistlib.loads(_read_small(_ci(dev, 'SysInfoExtended')) or b'')
    except Exception:
        pass
    return plain, ext if isinstance(ext, dict) else {}


def _guid(v) -> str:
    if isinstance(v, bytes):
        v = v.hex()
    elif isinstance(v, int):
        v = '%016X' % v
    v = str(v or '').strip()
    v = v[2:] if v[:2].lower() == '0x' else v
    return v.upper() if len(v) == 16 and all(c in '0123456789abcdefABCDEF' for c in v) else ''


def db_name(control: Path | None) -> str:
    """The database file the iPod uses. iTunesCDB wins over iTunesDB because libgpod empties the old one when it
    writes a CDB. iTunesSD is the shuffle's own."""
    it = _ci(control, 'iTunes')
    for name in ('iTunesCDB', 'iTunesDB', 'iTunesSD'):
        f = _ci(it, name)
        if f is not None and f.stat().st_size > 0:
            return name
    return ''


def identify_ipod(mount, usb_serial: str = '', fs: str = '', read_only: bool | None = None) -> IpodInfo | None:
    """What this volume is, from files only. None when it has no iPod_Control folder. usb_serial is the serial the
    USB descriptor reports, which for an iPod is its FireWire id. fs and read_only are what the caller knows
    about the mount."""
    mount = Path(mount)
    ctl = _ci(mount, 'iPod_Control')
    if ctl is None:
        return None
    dev = _ci(ctl, 'Device')
    plain, ext = _sysinfo(dev)
    ext_serial = str(ext.get('SerialNumber') or '')
    model, gen = lookup_model(plain.get('ModelNumStr', ''), ext_serial)
    if gen == 'unknown' and plain.get('pszSerialNumber'):
        model, gen = lookup_model('', plain['pszSerialNumber'])
    guid = src = ''
    for value, where in ((ext.get('FireWireGUID'), 'SysInfoExtended'), (plain.get('FirewireGuid'), 'SysInfo'),
                         (usb_serial, 'usb serial')):
        guid = _guid(value)
        if guid:
            src = where
            break
    info = IpodInfo(mount=mount, control=ctl, model=model, model_str=plain.get('ModelNumStr', ''), generation=gen,
                    serial=ext_serial or plain.get('pszSerialNumber', ''), guid=guid, guid_from=src,
                    rockbox=_ci(mount, '.rockbox') is not None, hashinfo=_ci(dev, 'HashInfo') is not None,
                    fs=fs, read_only=read_only, sysinfo={k: v[:200] for k, v in plain.items()}, db=db_name(ctl))
    if gen != 'unknown':
        info.capacity_gb = MODELS[model][1]
        info.name = GENERATIONS[gen]['name']
        if info.capacity_gb > 0:
            info.name += ' %g GB' % info.capacity_gb
    return info


def ipod_capabilities(x) -> dict:
    """What can be done with an iPod. x is an IpodInfo (the volume is checked too), a model number such as 'MB565'
    or a key of GENERATIONS (then only what the model allows is answered).
    {'generation','name','can_read','can_write','why_not','needs','formats','artwork','video','checksum',
    'rockbox','note'}"""
    info = x if isinstance(x, IpodInfo) else None
    gen = info.generation if info else (x if x in GENERATIONS else lookup_model(str(x))[1])
    g = GENERATIONS.get(gen)
    caps = {'generation': gen, 'name': info.name if info else g['name'] if g else 'iPod', 'can_read': True,
            'can_write': False, 'why_not': '', 'needs': [], 'formats': list(g['formats']) if g else ['mp3', 'aac'],
            'artwork': bool(g and g['artwork']), 'video': bool(g and g['video']),
            'checksum': g['checksum'] if g else '', 'rockbox': g['rockbox'] if g else 'no',
            'note': g['note'] if g else ''}
    if g is None:
        caps['why_not'] = _UNKNOWN_MODEL
        return caps
    if g['checksum'] == 'hash58':
        caps['needs'] = ['firewire_guid']
    if g['write'] == 'nano5':
        caps['needs'] = ['SysInfoExtended', 'HashInfo']
    if g['write'] == 'no':
        caps['why_not'] = g['why'] or _IOS
    elif info is None:
        caps['why_not'] = '' if g['write'] == 'yes' else g['why']
    else:
        caps['why_not'] = _blocker(info, g, caps['needs'])
    caps['can_write'] = not caps['why_not']
    return caps


def _blocker(info: IpodInfo, g: dict, needs: list) -> str:
    """The first reason this volume cannot be written, or ''."""
    status = gpod_status()
    if g['write'] == 'nano5' and not (info.hashinfo and _ci(_ci(info.control, 'Device'), 'SysInfoExtended')):
        return g['why']
    if not status['ok']:
        return status['message']
    if info.read_only or (info.read_only is None and not os.access(info.mount, os.W_OK)):
        return _READ_ONLY_HFS if 'hfs' in info.fs else _READ_ONLY
    if _ci(info.control, 'iTunes') is None or _ci(info.control, 'Music') is None:
        return "This iPod has no iTunes and Music folders yet. Restore it with Apple's software first."
    if 'firewire_guid' in needs and not info.guid:
        return ("This model signs its library with the iPod's FireWire id, and AudioCrate could not read it from "
                "the iPod or from its USB serial number.")
    return ''


# reading the library

_MHOD = {1: 'title', 2: 'path', 3: 'album', 4: 'artist', 5: 'genre', 6: 'filetype', 8: 'comment', 12: 'composer',
         13: 'grouping', 22: 'albumartist'}


def _u(b, o, n=4):
    if o < 0 or o + n > len(b):
        raise ValueError('truncated at %d' % o)
    return int.from_bytes(b[o:o + n], 'little')


def _chunk(d, p, magic, min_hlen):
    """Header length and total length of the record at p."""
    if d[p:p + 4] != magic:
        raise ValueError('%s expected at %d' % (magic.decode(), p))
    hlen, tlen = _u(d, p + 4), _u(d, p + 8)
    if hlen < min_hlen or tlen < hlen or p + hlen > len(d):
        raise ValueError('bad %s lengths at %d' % (magic.decode(), p))
    return hlen, min(tlen, len(d) - p)


def _mhod(d, p):
    hlen, tlen = _chunk(d, p, b'mhod', 16)
    typ, s = _u(d, p + 12), None
    if typ in _MHOD:
        raw = d[p + hlen + 16:p + hlen + 16 + _u(d, p + hlen + 4)]
        s = raw.decode('utf-8' if _u(d, p + hlen) == 2 else 'utf-16-le', 'replace')
    return typ, s, tlen


def _mhit(d, p):
    hlen, tlen = _chunk(d, p, b'mhit', 0x9c)
    t = {'id': _u(d, p + 16), 'compilation': d[p + 30], 'rating': d[p + 31], 'size': _u(d, p + 36),
         'ms': _u(d, p + 40), 'track': _u(d, p + 44), 'tracks': _u(d, p + 48), 'year': _u(d, p + 52),
         'bitrate': _u(d, p + 56), 'rate': _u(d, p + 60) >> 16, 'plays': _u(d, p + 80), 'disc': _u(d, p + 92),
         'discs': _u(d, p + 96), 'dbid': _u(d, p + 112, 8), 'mediatype': _u(d, p + 208) if hlen >= 0xF4 else 0}
    q = p + hlen
    for _ in range(min(_u(d, p + 12), 64)):
        if d[q:q + 4] != b'mhod':
            break
        typ, s, ml = _mhod(d, q)
        if s is not None:
            t[_MHOD[typ]] = s
        q += ml
    return t, p + tlen


def _mhyp(d, p, budget):
    """One playlist. budget is [entries still allowed in the whole file]: records cannot overlap in an honest
    database, so there are never more entries than the file has room for."""
    hlen, tlen = _chunk(d, p, b'mhyp', 48)
    nmhod, nmhip = _u(d, p + 12), _u(d, p + 16)
    pl = {'master': d[p + 20] == 1, 'id': _u(d, p + 28, 8), 'podcast': _u(d, p + 42, 2), 'name': '', 'ids': []}
    q = p + hlen
    for _ in range(min(nmhod, 64)):
        typ, s, ml = _mhod(d, q)
        if typ == 1 and s:
            pl['name'] = s
        q += ml
    if nmhip > budget[0]:
        raise ValueError('playlist entries overlap or their count is too large')
    budget[0] -= nmhip
    for _ in range(nmhip):
        ihlen, itlen = _chunk(d, q, b'mhip', 36)
        pl['ids'].append(_u(d, q + 24))
        nxt = q + itlen
        if itlen <= ihlen:         # old databases give the header length as the total, so walk the child records
            nxt = q + ihlen
            for _ in range(min(_u(d, q + 12), 8)):
                nxt += _chunk(d, nxt, b'mhod', 16)[1]
        q = nxt
    return pl, p + tlen


def read_db(raw: bytes) -> tuple[dict, dict, list]:
    """Parse the bytes of an iTunesDB or iTunesCDB: (info, {track id: track}, [(section type, playlist)]).
    Unknown records are skipped. Raises ValueError for anything that is not a usable database."""
    if raw[:4] != b'mhbd':
        raise ValueError('not an iTunesDB')
    hlen = _u(raw, 4)
    d = raw
    if len(raw) > MAX_DB:
        raise ValueError('the database is too large')
    if len(raw) > 0xA9 and hlen >= 0xA9 and raw[0xA8] == 1:        # iTunesCDB: plain header, then one zlib stream
        try:
            z = zlib.decompressobj()
            body = z.decompress(raw[hlen:], max(MAX_DB - hlen, 1))
            if z.unconsumed_tail:
                raise ValueError('the database is too large once expanded')
            d = raw[:hlen] + body
        except zlib.error as e:
            raise ValueError('bad compressed database: %s' % e)
    if hlen < 0x20 or hlen > len(d):
        raise ValueError('bad mhbd header')
    info = {'version': _u(d, 0x10), 'id': _u(d, 0x18, 8)}
    tracks, playlists, pos, budget = {}, [], hlen, [len(d) // 36]
    for _ in range(_u(d, 20)):
        shlen, slen = _chunk(d, pos, b'mhsd', 16)
        typ, body = _u(d, pos + 12), pos + shlen
        if typ == 1 and d[body:body + 4] == b'mhlt':
            n, p = _u(d, body + 8), body + _u(d, body + 4)
            if n > len(d) // 100:
                raise ValueError('track count too large')
            for _i in range(n):
                t, p = _mhit(d, p)
                tracks[t['id']] = t
        elif typ in (2, 3) and d[body:body + 4] == b'mhlp':
            n, p = _u(d, body + 8), body + _u(d, body + 4)
            if n > len(d) // 40:
                raise ValueError('playlist count too large')
            for _i in range(n):
                pl, p = _mhyp(d, p, budget)
                playlists.append((typ, pl))
        pos += slen
    return info, tracks, playlists


def read_shuffle_db(raw: bytes) -> list[str]:
    """Paths (with '/' separators) from an iTunesSD, version 1 (shuffle 1G and 2G) or 2 (3G and 4G)."""
    out = []
    if raw[:4] == b'bdhs':
        pos = _u(raw, 36)
        if raw[pos:pos + 4] != b'hths':
            raise ValueError('no track list')
        for i in range(min(_u(raw, pos + 8), 20000)):
            o = _u(raw, pos + 20 + 4 * i)
            if raw[o:o + 4] == b'rths':
                out.append(raw[o + 24:o + 280].split(b'\0')[0].decode('utf-8', 'replace'))
        return out
    n, start = int.from_bytes(raw[0:3], 'big'), int.from_bytes(raw[6:9], 'big')
    if start < 12 or n > 20000:
        raise ValueError('not an iTunesSD')
    for i in range(n):
        e = raw[start + i * 558:start + (i + 1) * 558]
        if len(e) < 558:
            raise ValueError('truncated iTunesSD')
        out.append(e[33:555].decode('utf-16-le', 'replace').split('\0')[0])
    return out


@dataclass
class IpodLibrary:
    mount: Path = Path('.')
    items: list = field(default_factory=list)       # one scan item per song whose file exists
    playlists: list = field(default_factory=list)   # {'name','id','master','ids'}, ids are the dbid of each song
    name: str = ''                                   # what the iPod is called: the master playlist's name
    source: str = ''                                 # iTunesDB, iTunesCDB or iTunesSD
    version: int = 0
    missing: int = 0                                 # songs the database lists whose file is gone
    raw: dict = field(default_factory=dict)          # track_key -> the track as parsed, ratings and counts too


def resolve_ipod_path(mount: Path, ipod_path: str, cache: dict | None = None) -> Path | None:
    """':iPod_Control:Music:F04:ABCD.mp3' to the file on the mount, or None when it is not there."""
    cur, cache = Path(mount), cache if cache is not None else {}
    parts = [p for p in ipod_path.replace('/', ':').split(':') if p]
    if '..' in parts or '.' in parts:
        return None                     # a database that leads out of the iPod is not believed
    for part in parts:
        nxt = cur / part
        if not nxt.exists():
            if cur not in cache:
                try:
                    cache[cur] = {e.name.casefold(): e.name for e in os.scandir(cur)}
                except OSError:
                    cache[cur] = {}
            hit = cache[cur].get(part.casefold())
            if hit is None:
                return None
            nxt = cur / hit
        cur = nxt
    return cur if cur.is_file() else None


def track_key(t: dict):
    """What names a song across two writes: its 64 bit dbid, or its path in a very old database that has none.
    libgpod gives every song a new plain id on each write, so the id will not do."""
    return t['dbid'] or t.get('path') or t['id']


def _item(t: dict, path: Path, mount: Path, playlists: list) -> dict:
    return {'path': str(path), 'rel': str(path.relative_to(mount)), 'title': t.get('title') or path.stem,
            'artist': t.get('artist', ''), 'artists': [],   # the Artists list splits artist itself
            'album': t.get('album', ''), 'track': t['track'],
            'duration': t['ms'] / 1000 if t['ms'] else None, 'size': t['size'],
            'format': path.suffix.lower().lstrip('.'), 'ipod_id': t['dbid'] or t['id'], 'playlists': playlists,
            'albumartist': t.get('albumartist', ''), 'genre': t.get('genre', ''), 'year': t['year'],
            'disc': t['disc'], 'rating': t['rating'] // 20, 'plays': t['plays']}


def read_library(mount) -> IpodLibrary:
    """The songs and playlists of the iPod mounted at mount. Raises IpodError with a plain sentence."""
    mount = Path(mount)
    ctl = _ci(mount, 'iPod_Control')
    chosen = _ci(_ci(ctl, 'iTunes'), db_name(ctl)) if db_name(ctl) else None
    if chosen is None:
        raise IpodError("This iPod has no music database yet.")
    lib, cache = IpodLibrary(mount=mount, source=chosen.name), {}
    try:
        if chosen.stat().st_size > MAX_DB:
            raise ValueError('the file is too large')
        raw = chosen.read_bytes()
        if chosen.name == 'iTunesSD':
            return _read_shuffle(lib, read_shuffle_db(raw), cache)
        info, tracks, pls = read_db(raw)
    except (ValueError, OSError, UnicodeError) as e:
        raise IpodError("The iPod's music database is damaged (%s)." % e)
    lib.version = info['version']
    wanted = 3 if any(kind == 3 for kind, _ in pls) else 2     # libgpod writes both lists, so use one
    by_id = {t['id']: t['dbid'] or t['id'] for t in tracks.values()}
    names: dict = {}
    for kind, pl in pls:
        if kind != wanted:
            continue
        ids = [by_id[i] for i in pl['ids'] if i in by_id]
        lib.playlists.append({'name': pl['name'], 'id': pl['id'], 'master': pl['master'], 'ids': ids})
        if pl['master']:
            lib.name = pl['name']
        elif not pl['podcast']:
            for i in ids:
                names.setdefault(i, []).append(pl['name'])
    for t in tracks.values():
        lib.raw[track_key(t)] = t
        f = resolve_ipod_path(mount, t['path'], cache) if t.get('path') else None
        if f is None:
            lib.missing += 1
        else:
            lib.items.append(_item(t, f, mount, names.get(t['dbid'] or t['id'], [])))
    return lib


def _read_shuffle(lib: IpodLibrary, paths: list, cache: dict) -> IpodLibrary:
    """A shuffle's iTunesSD only lists files, so the titles are the file names."""
    for i, p in enumerate(paths, 1):
        f = resolve_ipod_path(lib.mount, p, cache)
        if f is None:
            lib.missing += 1
            continue
        t = {'id': i, 'dbid': i, 'track': 0, 'ms': 0, 'size': f.stat().st_size, 'year': 0, 'disc': 0, 'rating': 0,
             'plays': 0, 'title': f.stem}
        lib.raw[i] = t
        lib.items.append(_item(t, f, lib.mount, []))
    return lib


def verify(before: IpodLibrary | None, after: IpodLibrary, added: list, pending_counts: bool = False) -> list[str]:
    """What is wrong with the library a write produced. Every song that was there is still there with its title,
    artist and album, no play count went down, no playlist lost songs, and every added song is listed with a file.
    pending_counts says a Play Counts file was waiting: libgpod folds it in, so ratings may change."""
    bad = []
    for a in added:
        t = after.raw.get(a['dbid'])
        f = resolve_ipod_path(after.mount, t['path']) if t and t.get('path') else None
        if t is None or f is None or f.stat().st_size == 0:
            bad.append("%s did not make it into the library" % (a.get('title') or 'a song'))
    if before is not None:
        if len(after.raw) != len(before.raw) + len(added):
            bad.append("the library has %d songs, expected %d" % (len(after.raw), len(before.raw) + len(added)))
        for k, t in before.raw.items():
            n, title = after.raw.get(k), t.get('title') or 'a song'
            if n is None:
                bad.append("%s is missing" % title)
            elif any(n.get(f, '') != t.get(f, '') for f in ('title', 'artist', 'album')):
                bad.append("%s changed" % title)
            elif n['plays'] < t['plays'] or (n['rating'] != t['rating'] and not pending_counts):
                bad.append("the rating or play count of %s changed" % title)
        now = {p['name']: len(p['ids']) for p in after.playlists}
        bad += ["playlist %s lost songs" % p['name'] for p in before.playlists
                if now.get(p['name'], 0) < len(p['ids'])]
    return bad[:8]


# backups

_DB_FILES = ('iTunesDB', 'iTunesCDB', 'iTunesSD')
_ARTWORK_LIMIT = 300 * 1024 * 1024


def _stamp(taken: Path | None = None) -> str:
    """The UTC time as a name. When a backup of that second exists, the next free second."""
    t = time.time()
    while True:
        name = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(t))
        if taken is None or not (taken / name).exists():
            return name
        t += 1


def _key(info: IpodInfo, uuid: str = '') -> str:
    k = info.serial or info.guid or uuid or 'unknown'
    return ''.join(c if c.isalnum() or c in '-_.' else '_' for c in k)[:80].lstrip('.') or 'unknown'   # '..' is no name


def _backup_files(info: IpodInfo) -> list[Path]:
    """Everything a write can replace, delete or rename: the database files, the identity files, the artwork
    (unless that is huge, then it cannot be restored)."""
    out, art = [], []
    for sub, deep in (('iTunes', True), ('Device', False), ('Artwork', False)):
        d = _ci(info.control, sub)
        for root, dirs, files in os.walk(d) if d else ():
            if not deep:
                dirs.clear()
            (art if sub == 'Artwork' else out).extend(Path(root) / f for f in files if '.audiocrate-' not in f)
    if sum(p.stat().st_size for p in art) <= _ARTWORK_LIMIT:
        out += art
    return out


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _copy_checked(src: Path, dst: Path):
    """Copy, then read the copy back: the same size always, and for a database file the same bytes, written out
    to the device first. A backup that is not whole is worse than none, because the write would go ahead."""
    shutil.copyfile(src, dst)
    db = src.name in _DB_FILES
    if db:
        fd = os.open(dst, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if dst.stat().st_size != src.stat().st_size or (db and _digest(dst) != _digest(src)):
        raise OSError(errno.EIO, "the copy of %s did not match the original" % src.name)


def make_backup(info: IpodInfo, uuid: str = '', root: Path | None = None, tracks: int | None = None) -> dict:
    """Copy what a write can change to this computer and, for the database files, next to the originals on the
    iPod. {'id': stamp, 'host': folder, 'files': count}. tracks is the number of songs when the caller knows it.
    Raises IpodError: with no backup there is no write."""
    host = Path(root or BACKUP_ROOT) / _key(info, uuid)
    stamp = _stamp(host)
    dest = host / stamp
    files, on_ipod = _backup_files(info), []
    try:
        for p in files:
            rel = p.relative_to(info.mount)
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            _copy_checked(p, dest / rel)
            if p.name in _DB_FILES:
                on_ipod.append(p.with_name('%s.audiocrate-backup-%s' % (p.name, stamp)))
                _copy_checked(p, on_ipod[-1])
        if tracks is None:
            try:
                tracks = len(read_library(info.mount).raw) if info.db else 0
            except IpodError:
                tracks = 0
        manifest = {'id': stamp, 'tracks': tracks, 'model': info.name,
                    'files': [str(p.relative_to(info.mount)) for p in files]}
        (dest / 'backup.json').write_text(json.dumps(manifest))
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)
        for f in on_ipod:
            f.unlink(missing_ok=True)
        raise IpodError("AudioCrate could not save a backup of the iPod's library (%s), so it did not change "
                        "anything." % (e.strerror or e))
    _prune(info, host)
    return {'id': stamp, 'host': dest, 'files': len(files)}


def _prune(info: IpodInfo, host: Path):
    for d in (sorted(p for p in host.iterdir() if p.is_dir())[:-KEEP_BACKUPS] if host.is_dir() else ()):
        shutil.rmtree(d, ignore_errors=True)
    it = _ci(info.control, 'iTunes')
    for name in _DB_FILES:
        for p in (sorted(it.glob(name + '.audiocrate-backup-*'))[:-KEEP_BACKUPS] if it else ()):
            p.unlink(missing_ok=True)


def list_backups(info: IpodInfo, uuid: str = '', root: Path | None = None) -> list[dict]:
    """Backups of this iPod, newest first: {'id','when','host','ipod','files','tracks'}. host says the full copy is
    on this computer, ipod says the database files are on the iPod itself."""
    found: dict = {}
    host = Path(root or BACKUP_ROOT) / _key(info, uuid)
    for f in (host.glob('*/backup.json') if host.is_dir() else ()):
        try:
            m = json.loads(f.read_text())
            found[m['id']] = {'id': m['id'], 'host': True, 'ipod': False, 'files': len(m['files']),
                              'tracks': m.get('tracks', 0)}
        except (OSError, ValueError, KeyError):
            pass
    it = _ci(info.control, 'iTunes')
    for name in _DB_FILES:
        for p in (it.glob(name + '.audiocrate-backup-*') if it else ()):
            stamp = p.name.rsplit('-', 1)[1]
            found.setdefault(stamp, {'id': stamp, 'host': False, 'files': 0, 'tracks': 0})['ipod'] = True
    for key, b in list(found.items()):
        b.setdefault('ipod', False)
        try:
            b['when'] = time.strftime('%Y-%m-%d %H:%M UTC', time.strptime(b['id'], '%Y%m%dT%H%M%SZ'))
        except (ValueError, TypeError):
            del found[key]                  # a file that only looks like a backup
    return sorted(found.values(), key=lambda b: b['id'], reverse=True)


def restore_backup(info: IpodInfo, stamp: str, uuid: str = '', root: Path | None = None) -> int:
    """Put a backup back, file by file through a temporary name, then check that the database reads. Returns the
    number of files restored. The copy on this computer is used when there is one, else the database copies on
    the iPod."""
    host = Path(root or BACKUP_ROOT) / _key(info, uuid) / stamp
    it = _ci(info.control, 'iTunes')
    try:
        if (host / 'backup.json').is_file():
            files = json.loads((host / 'backup.json').read_text())['files']
            pairs = [(host / rel, info.mount / rel) for rel in files]
        else:
            copies = [(it / ('%s.audiocrate-backup-%s' % (n, stamp)), it / n) for n in _DB_FILES] if it else []
            pairs = [(src, dst) for src, dst in copies if src.is_file()]
        if not pairs:
            raise IpodError("There is no backup %s for this iPod." % stamp)
        for src, dst in pairs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + '.audiocrate-tmp')
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
        os.sync()
        info.db = db_name(info.control)
        if info.db:
            read_library(info.mount)
    except (OSError, ValueError, KeyError) as e:
        raise IpodError("The backup could not be restored (%s)." % e)
    return len(pairs)


# writing: the session that talks to the helper process

_NO_HELPER = ("This build of AudioCrate cannot update an iPod's own library, because the helper that does it is "
              "not part of it.")


def helper_cmd(mode: str) -> list[str] | None:
    """How to start the helper, None when it cannot be. A frozen build has no script to run, so the app itself has
    to answer --ipod-helper (main.py calls ipod.main for it and sets FROZEN_HELPER). Until it does, starting
    sys.executable would open a second AudioCrate window, so there is no helper."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--ipod-helper', mode] if FROZEN_HELPER else None
    return [sys.executable, str(Path(__file__).resolve()), mode]


_status: dict | None = None


def gpod_status() -> dict:
    """{'ok', 'message'}: does libgpod load here and have the layout this code expects. The helper process finds
    out once and the answer is kept. Starting it takes a moment, so call this from a worker thread."""
    global _status
    if _status is None:
        try:
            cmd = helper_cmd('probe')
            if cmd is None:
                _status = {'ok': False, 'message': _NO_HELPER}
                return _status
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            _status = json.loads(r.stdout.strip().splitlines()[-1])
            _merge_table(_status.pop('table', []))
        except Exception as e:
            _status = {'ok': False, 'message': "The iPod helper could not start (%s)." % (str(e) or type(e).__name__)}
    return _status


def _merge_table(rows: list):
    """Add models libgpod knows and the built-in table lacks. Nothing is removed, and nothing is added when
    libgpod numbers its generations differently from what this file expects."""
    keys = [_GPOD_GENS[g] if 0 <= g < len(_GPOD_GENS) else '' for _, g, _ in rows]
    if any(MODELS.get(m.upper(), (k,))[0] != k for (m, _, _), k in zip(rows, keys)):
        return
    for (m, _, cap), k in zip(rows, keys):
        if k in GENERATIONS:
            MODELS.setdefault(m.upper(), (k, cap))


def _db_signature(info: IpodInfo):
    name = db_name(info.control)
    f = _ci(_ci(info.control, 'iTunes'), name) if name else None
    return (f.stat().st_mtime_ns, f.stat().st_size) if f else None


class IpodSession:
    """Adds songs to an iPod's own library. Steps: start (checks, backup, helper), add for each song (copy), commit
    (write, flush, read the database back and compare). If anything fails after the write the backup is put back
    and the new files are deleted. If the session is cancelled before the write the database was never touched.

        s = IpodSession(info, uuid=device_id)
        s.start(need_bytes)
        s.add(path, meta, cover, playlist)   # meta: title artist album albumartist genre composer track tracks
        s.commit()                           #       disc discs year ms kbps hz compilation

    stop() may be called from any thread, abort() only from the thread that runs the session.
    """

    def __init__(self, info: IpodInfo, uuid: str = '', backup_root: Path | None = None, helper: list | None = None):
        self.info, self.uuid, self.root, self.helper = info, uuid, backup_root, helper
        self.added: list[dict] = []
        self.backup: dict = {}
        self.before: IpodLibrary | None = None
        self.pending = False
        self.aborted = False
        self._dests: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._log = None
        self._wrote = self._committing = False

    def start(self, need_bytes: int = 0, name: str = 'iPod'):
        """Checks, backup, helper. name is only used when the iPod has no database yet and a new one is made."""
        caps = ipod_capabilities(self.info)
        if not caps['can_write']:
            raise IpodError(caps['why_not'])
        mount = self.info.mount
        self.before = read_library(mount) if self.info.db else None
        free = shutil.disk_usage(mount).free
        copies = sum(p.stat().st_size for p in _backup_files(self.info) if p.name in _DB_FILES)
        if free < need_bytes * 1.02 + copies + (16 << 20):
            raise IpodError("There is not enough room on the iPod: %d MB needed, %d MB free."
                            % (need_bytes >> 20, free >> 20))
        self._sig = _db_signature(self.info)
        self.pending = _ci(_ci(self.info.control, 'iTunes'), 'Play Counts') is not None
        self.backup = make_backup(self.info, self.uuid, self.root, len(self.before.raw) if self.before else 0)
        self._log = tempfile.TemporaryFile()
        cmd = self.helper or helper_cmd('serve')
        if cmd is None:
            raise IpodError(_NO_HELPER)
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=self._log, text=True, bufsize=1,
                                          start_new_session=True)
        except OSError as e:
            raise IpodError("The iPod helper could not start (%s)." % e.strerror)
        guid = self.info.guid if self.info.guid_from == 'usb serial' else ''
        self._call({'cmd': 'open', 'mount': str(mount), 'guid': guid, 'name': name})

    def add(self, src, meta: dict, cover=None, playlist: str = '', on_bytes=None) -> dict:
        """Copy one song onto the iPod and list it in memory. on_bytes(n) hears how much of the file is copied."""
        ev = self._call({'cmd': 'add', 'src': str(src), 'meta': meta, 'cover': str(cover or ''),
                         'playlist': playlist}, on_bytes)
        rec = {'dbid': ev['dbid'], 'path': ev['path'], 'title': meta.get('title', '')}
        self.added.append(rec)
        return rec

    def commit(self) -> dict:
        """Write the database and check it. Returns {'added', 'backup'}. Raises IpodError after putting
        everything back."""
        if not self.added:
            self.abort()
            return {'added': 0, 'backup': self.backup.get('id', '')}
        self._committing = True
        try:
            if _db_signature(self.info) != self._sig:
                raise IpodError("The iPod's library changed while AudioCrate was working, so nothing was written.")
            self._wrote = True
            self._call({'cmd': 'commit'})
            self._quit()
            bad = verify(self.before, read_library(self.info.mount), self.added, self.pending)
            if bad:
                raise IpodError("The new library did not check out (%s)." % '; '.join(bad))
        except IpodError as e:
            self._kill()
            raise IpodError(str(e) + self._rollback())
        return {'added': len(self.added), 'backup': self.backup.get('id', '')}

    def stop(self):
        """Ask the helper to end, from any thread and without waiting: SIGTERM now and SIGKILL after _KILL_GRACE if
        it is still there. The thread that runs the session sees the helper go and finishes with abort(). Does
        nothing once commit began."""
        self.aborted = True
        p = self._proc
        if self._committing or p is None or p.poll() is not None:
            return
        try:
            p.terminate()
        except OSError:
            return
        timer = threading.Timer(_KILL_GRACE, lambda: p.poll() is None and p.kill())
        timer.daemon = True
        timer.start()

    def abort(self):
        """Stop before the write: kill the helper and delete what was copied. Does nothing once commit began."""
        self.aborted = True
        if not self._committing:
            self._kill()
            self._remove_new_files()

    def _call(self, cmd: dict, on_bytes=None) -> dict:
        p = self._proc
        try:
            p.stdin.write(json.dumps(cmd) + '\n')
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    raise BrokenPipeError
                ev = json.loads(line)
                if ev['ev'] == 'dest':
                    self._dests.append(ev['path'])
                elif ev['ev'] == 'bytes' and on_bytes:
                    on_bytes(ev['n'])
                elif ev['ev'] == 'ok':
                    return ev
                elif ev['ev'] == 'error':
                    raise IpodError(ev['message'])
        except (BrokenPipeError, ValueError, OSError):
            if self.aborted:
                e = IpodError("Cancelled.")
                e.cancelled = True
                raise e
            rc = self._kill()
            self._log.seek(0)
            tail = self._log.read()[-300:].decode('utf-8', 'replace').strip()
            raise IpodError("The iPod library code stopped unexpectedly (code %s)%s. %s" % (
                rc, ': ' + tail if tail else '',
                'The library may need restoring from the backup.' if self._wrote
                else 'Nothing on the iPod was changed.'))

    def _quit(self):
        try:
            self._proc.stdin.write('{"cmd": "quit"}\n')
            self._proc.stdin.close()
            self._proc.wait(15)
        except (OSError, subprocess.TimeoutExpired):
            self._kill()

    def _kill(self):
        p = self._proc
        if p is None:
            return None
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(_KILL_GRACE)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(_KILL_GRACE)
                except subprocess.TimeoutExpired:
                    pass                    # stuck in the kernel on a stalled device, nothing more can be done
        for f in (p.stdin, p.stdout):      # closed now: a half written command in the buffer would fail again later
            try:
                f.close()
            except (OSError, ValueError):
                pass
        return p.returncode

    def _remove_new_files(self):
        for d in self._dests:
            try:
                Path(d).unlink(missing_ok=True)
            except OSError:
                pass
        self._dests = []

    def _rollback(self) -> str:
        """After a failed write: the old library back, the copied songs deleted. A sentence for the error."""
        msg = ''
        if self._wrote:
            try:
                restore_backup(self.info, self.backup['id'], self.uuid, self.root)
                msg = " The previous library was put back."
            except IpodError as e:
                msg = " The previous library could not be put back (%s). The backup is in %s." % (
                    e, self.backup.get('host', ''))
        self._remove_new_files()
        return msg


# libgpod, used only inside the helper process

class GError(ctypes.Structure):
    _fields_ = [('domain', c_uint32), ('code', c_int), ('message', c_char_p)]


class IpodRow(ctypes.Structure):
    _fields_ = [('model_number', c_char_p), ('capacity', c_double), ('ipod_model', c_int),
                ('ipod_generation', c_int), ('musicdirs', c_uint), ('reserved_int1', c_int32),
                ('reserved_int2', c_int32), ('reserved1', c_void_p), ('reserved2', c_void_p)]


class Itdb_Track(ctypes.Structure):
    # field order and sizes checked against gcc's offsetof for libgpod 0.8.3 on x86-64
    _fields_ = [
        ('itdb', c_void_p), ('title', c_void_p), ('ipod_path', c_void_p), ('album', c_void_p),
        ('artist', c_void_p), ('genre', c_void_p), ('filetype', c_void_p), ('comment', c_void_p),
        ('category', c_void_p), ('composer', c_void_p), ('grouping', c_void_p), ('description', c_void_p),
        ('podcasturl', c_void_p), ('podcastrss', c_void_p), ('chapterdata', c_void_p), ('subtitle', c_void_p),
        ('tvshow', c_void_p), ('tvepisode', c_void_p), ('tvnetwork', c_void_p), ('albumartist', c_void_p),
        ('keywords', c_void_p), ('sort_artist', c_void_p), ('sort_title', c_void_p), ('sort_album', c_void_p),
        ('sort_albumartist', c_void_p), ('sort_composer', c_void_p), ('sort_tvshow', c_void_p),
        ('id', c_uint32), ('size', c_uint32), ('tracklen', c_int32), ('cd_nr', c_int32), ('cds', c_int32),
        ('track_nr', c_int32), ('tracks', c_int32), ('bitrate', c_int32), ('samplerate', c_uint16),
        ('samplerate_low', c_uint16), ('year', c_int32), ('volume', c_int32), ('soundcheck', c_uint32),
        ('time_added', c_long), ('time_modified', c_long), ('time_played', c_long),
        ('bookmark_time', c_uint32), ('rating', c_uint32), ('playcount', c_uint32), ('playcount2', c_uint32),
        ('recent_playcount', c_uint32), ('transferred', c_int), ('BPM', c_int16), ('app_rating', c_uint8),
        ('type1', c_uint8), ('type2', c_uint8), ('compilation', c_uint8), ('starttime', c_uint32),
        ('stoptime', c_uint32), ('checked', c_uint8), ('dbid', c_uint64), ('drm_userid', c_uint32),
        ('visible', c_uint32), ('filetype_marker', c_uint32), ('artwork_count', c_uint16),
        ('artwork_size', c_uint32), ('samplerate2', c_float), ('unk126', c_uint16), ('unk132', c_uint32),
        ('time_released', c_long), ('unk144', c_uint16), ('explicit_flag', c_uint16), ('unk148', c_uint32),
        ('unk152', c_uint32), ('skipcount', c_uint32), ('recent_skipcount', c_uint32),
        ('last_skipped', c_uint32), ('has_artwork', c_uint8), ('skip_when_shuffling', c_uint8),
        ('remember_playback_position', c_uint8), ('flag4', c_uint8), ('dbid2', c_uint64),
        ('lyrics_flag', c_uint8), ('movie_flag', c_uint8), ('mark_unplayed', c_uint8), ('unk179', c_uint8),
        ('unk180', c_uint32), ('pregap', c_uint32), ('samplecount', c_uint64), ('unk196', c_uint32),
        ('postgap', c_uint32), ('unk204', c_uint32), ('mediatype', c_uint32), ('season_nr', c_uint32),
        ('episode_nr', c_uint32), ('unk220', c_uint32), ('unk224', c_uint32), ('unk228', c_uint32),
        ('unk232', c_uint32), ('unk236', c_uint32), ('unk240', c_uint32), ('unk244', c_uint32),
        ('gapless_data', c_uint32), ('unk252', c_uint32), ('gapless_track_flag', c_uint16),
        ('gapless_album_flag', c_uint16), ('obsolete', c_uint16), ('artwork', c_void_p),
        ('mhii_link', c_uint32), ('reserved_int1', c_int32), ('reserved_int2', c_int32),
        ('reserved_int3', c_int32), ('reserved_int4', c_int32), ('reserved_int5', c_int32),
        ('reserved_int6', c_int32), ('priv', c_void_p), ('reserved2', c_void_p), ('reserved3', c_void_p),
        ('reserved4', c_void_p), ('reserved5', c_void_p), ('reserved6', c_void_p), ('usertype', c_uint64),
        ('userdata', c_void_p), ('userdata_duplicate', c_void_p), ('userdata_destroy', c_void_p)]


class Itdb_iTunesDB(ctypes.Structure):
    _fields_ = [('tracks', c_void_p), ('playlists', c_void_p), ('filename', c_void_p), ('device', c_void_p),
                ('version', c_uint32), ('id', c_uint64), ('tzoffset', c_int32)]


class GPodError(Exception):
    def __init__(self, message, notfound=False):
        super().__init__(message)
        self.notfound = notfound


class GPod:
    """The libgpod calls the writer needs. Strings put into a track must come from g_strdup, which s() does,
    because libgpod frees them with g_free."""

    def __init__(self):
        name, gname = ctypes.util.find_library('gpod'), ctypes.util.find_library('glib-2.0')
        if not name or not gname:
            raise OSError("libgpod is not installed")
        L, G = self.lib, self.glib = ctypes.CDLL(name), ctypes.CDLL(gname)
        P, E = c_void_p, POINTER(POINTER(GError))
        for n, res, args in (
                ('itdb_parse', P, [c_char_p, E]), ('itdb_new', P, []), ('itdb_free', None, [P]),
                ('itdb_set_mountpoint', None, [P, c_char_p]), ('itdb_write', c_int, [P, E]),
                ('itdb_track_new', P, []), ('itdb_track_add', None, [P, P, c_int32]),
                ('itdb_track_remove', None, [P]), ('itdb_track_free', None, [P]),
                ('itdb_track_duplicate', P, [P]), ('itdb_cp_get_dest_filename', P, [P, c_char_p, c_char_p, E]),
                ('itdb_cp_finalize', P, [P, c_char_p, c_char_p, E]), ('itdb_playlist_new', P, [c_char_p, c_int]),
                ('itdb_playlist_add', None, [P, P, c_int32]), ('itdb_playlist_add_track', None, [P, P, c_int32]),
                ('itdb_playlist_mpl', P, [P]), ('itdb_playlist_set_mpl', None, [P]),
                ('itdb_playlist_by_name', P, [P, c_char_p]),
                ('itdb_device_set_sysinfo', None, [P, c_char_p, c_char_p]),
                ('itdb_device_supports_artwork', c_int, [P]), ('itdb_info_get_ipod_info_table', P, []),
                ('itdb_track_set_thumbnails_from_data', c_int, [P, c_char_p, ctypes.c_size_t]),
                ('itdb_file_error_quark', c_uint32, [])):
            f = getattr(L, n)
            f.restype, f.argtypes = res, args
        G.g_free.argtypes, G.g_free.restype = [c_void_p], None
        G.g_strdup.argtypes, G.g_strdup.restype = [c_char_p], c_void_p
        G.g_error_free.argtypes, G.g_error_free.restype = [POINTER(GError)], None
        self.file_domain = L.itdb_file_error_quark()

    def raise_(self, err, fallback='libgpod failed'):
        msg, notfound = fallback, False
        if err:
            msg = (err.contents.message or b'').decode('utf-8', 'replace') or fallback
            notfound = err.contents.domain == self.file_domain and err.contents.code == 2
            self.glib.g_error_free(err)
        raise GPodError(msg, notfound)

    def s(self, struct, field, text):
        if getattr(struct, field):
            self.glib.g_free(getattr(struct, field))
        setattr(struct, field, self.glib.g_strdup(text.encode('utf-8')) if text else None)

    def gets(self, struct, field) -> str:
        v = getattr(struct, field)
        return ctypes.string_at(v).decode('utf-8', 'replace') if v else ''

    def take(self, ptr) -> str | None:
        if not ptr:
            return None
        v = ctypes.string_at(ptr).decode('utf-8', 'replace')
        self.glib.g_free(ptr)
        return v

    def parse(self, mount):
        err = POINTER(GError)()
        db = self.lib.itdb_parse(os.fsencode(mount), byref(err))
        if not db:
            self.raise_(err, "The iPod's database could not be read.")
        return ctypes.cast(db, POINTER(Itdb_iTunesDB))

    def write(self, db):
        err = POINTER(GError)()
        if not self.lib.itdb_write(db, byref(err)):
            self.raise_(err, 'libgpod could not write the database.')

    def model_table(self) -> list:
        t = ctypes.cast(self.lib.itdb_info_get_ipod_info_table(), POINTER(IpodRow))
        rows, i = [], 0
        while t[i].model_number:
            rows.append([t[i].model_number.decode(), t[i].ipod_generation, t[i].capacity])
            i += 1
        return rows

    def guard(self) -> tuple[bool, str]:
        """Does libgpod's track struct have the layout ours assumes? libgpod's own copy of a track carries every
        field, so a wrong offset anywhere shows up as a value that did not arrive."""
        tr = ctypes.cast(self.lib.itdb_track_new(), POINTER(Itdb_Track))
        t = tr.contents
        self.s(t, 'title', 'guard')
        t.tracklen, t.dbid, t.mediatype, t.gapless_data, t.mhii_link, t.usertype = (
            1234, 0x1122334455667788, 1, 77, 55, 99)
        dup = ctypes.cast(self.lib.itdb_track_duplicate(tr), POINTER(Itdb_Track))
        d = dup.contents
        ok = self.gets(d, 'title') == 'guard' and (
            d.tracklen, d.dbid, d.mediatype, d.gapless_data, d.mhii_link, d.usertype) == (
            1234, 0x1122334455667788, 1, 77, 55, 99)
        self.lib.itdb_track_free(dup)
        self.lib.itdb_track_free(tr)
        return ok, '' if ok else ("The installed libgpod is not the version this code was written for, so "
                                  "AudioCrate will not write to iPods with it.")


_FILETYPE = {'.mp3': 'MPEG audio file', '.m4a': 'AAC audio file', '.m4b': 'AAC audio file',
             '.mp4': 'AAC audio file', '.aac': 'AAC audio file', '.wav': 'WAV audio file',
             '.aif': 'AIFF audio file', '.aiff': 'AIFF audio file'}
_FRIENDLY = (
    ('firewire', "This model signs its library with the iPod's FireWire id and AudioCrate could not find it."),
    ('HashInfo', _WHY['nano_5']),
    ('Unsupported checksum', _WHY['nano_6']),
    ('No space left', "The iPod is full."),
    ('No such file', "A file could not be found while copying."),
    ("No 'F", "This iPod has no Music folders. Restore it with Apple's software first."),
    ('ead-only', "The iPod is mounted read only."),
)


def _friendly(msg: str) -> str:
    return next((v for k, v in _FRIENDLY if k in msg), msg)


class _Helper:
    """One editing session on one iPod. Songs are copied and listed in memory. Nothing in the database file
    changes until commit."""

    def __init__(self, send):
        self.send, self.g, self.db, self.mpl, self.mount = send, None, None, None, ''
        self.copied: list[str] = []

    def open(self, m):
        g = self.g = GPod()
        ok, msg = g.guard()
        if not ok:
            raise IpodError(msg)
        self.mount = m['mount']
        try:
            self.db = g.parse(self.mount)
        except GPodError as e:
            if not e.notfound:
                raise
            # no database yet: make one, with the master playlist every iPod needs
            self.db = ctypes.cast(g.lib.itdb_new(), POINTER(Itdb_iTunesDB))
            g.lib.itdb_set_mountpoint(self.db, os.fsencode(self.mount))
            mpl = g.lib.itdb_playlist_new((m.get('name') or 'iPod').encode(), 0)
            g.lib.itdb_playlist_set_mpl(mpl)
            g.lib.itdb_playlist_add(self.db, mpl, -1)
        if m.get('guid'):
            g.lib.itdb_device_set_sysinfo(self.db.contents.device, b'FirewireGuid', m['guid'].encode())
        self.mpl = g.lib.itdb_playlist_mpl(self.db)

    def add(self, m):
        g, db, meta, src = self.g, self.db, m['meta'], m['src']
        ext = Path(src).suffix.lower()
        tr = ctypes.cast(g.lib.itdb_track_new(), POINTER(Itdb_Track))
        t = tr.contents
        for k in ('title', 'artist', 'album', 'albumartist', 'genre', 'composer'):
            g.s(t, k, meta.get(k, ''))
        g.s(t, 'filetype', _FILETYPE.get(ext, 'MPEG audio file'))
        n = lambda k: int(meta.get(k) or 0)
        t.track_nr, t.tracks, t.cd_nr, t.cds, t.year = n('track'), n('tracks'), n('disc'), n('discs'), n('year')
        t.tracklen, t.bitrate, t.samplerate = n('ms'), n('kbps'), min(n('hz'), 65535)
        t.mediatype, t.compilation = 1, 1 if meta.get('compilation') else 0
        t.type1, t.type2 = (0, 1) if ext == '.mp3' else (0, 0)
        t.time_added = t.time_modified = int(time.time())
        g.lib.itdb_track_add(db, tr, -1)
        err, dest = POINTER(GError)(), None
        try:
            dest = g.take(g.lib.itdb_cp_get_dest_filename(None, os.fsencode(self.mount), os.fsencode(src), byref(err)))
            if not dest:
                g.raise_(err, 'libgpod found no place for the file')
            self.send(ev='dest', path=dest)
            self._copy(src, dest)
            if not g.lib.itdb_cp_finalize(tr, os.fsencode(self.mount), dest.encode(), byref(err)):
                g.raise_(err, 'libgpod could not finish the copy')
        except BaseException:
            g.lib.itdb_track_remove(tr)
            if dest:
                Path(dest).unlink(missing_ok=True)
            raise
        if m.get('cover') and g.lib.itdb_device_supports_artwork(db.contents.device):
            data = Path(m['cover']).read_bytes()
            g.lib.itdb_track_set_thumbnails_from_data(tr, data, len(data))
        g.lib.itdb_playlist_add_track(self.mpl, tr, -1)       # every song must be in the master playlist
        if m.get('playlist'):
            pl = g.lib.itdb_playlist_by_name(db, m['playlist'].encode())
            if not pl:
                pl = g.lib.itdb_playlist_new(m['playlist'].encode(), 0)
                g.lib.itdb_playlist_add(db, pl, -1)
            g.lib.itdb_playlist_add_track(pl, tr, -1)
        self.copied.append(dest)
        return {'dbid': t.dbid, 'path': g.gets(t, 'ipod_path'), 'dest': dest}

    def _copy(self, src, dest):
        done = 0
        with open(src, 'rb') as a, open(dest, 'wb') as b:
            while chunk := a.read(1 << 20):
                b.write(chunk)
                done += len(chunk)
                self.send(ev='bytes', n=done)
            b.flush()
            os.fsync(b.fileno())

    def commit(self, m):
        self.g.write(self.db)
        os.sync()
        self.g.lib.itdb_free(self.db)
        self.db = None


def main(argv=None) -> int:
    """Entry point of the helper process. 'probe' tells whether libgpod can be used. 'serve' runs an editing
    session, driven by one JSON command per line on stdin and answering with one JSON event per line on stdout.
    libgpod prints to stdout now and then, so the real stdout is moved aside and fd 1 points at stderr."""
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else ''
    out = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)

    def send(**ev):
        out.write(json.dumps(ev) + '\n')
        out.flush()

    if mode == 'probe':
        try:
            g = GPod()
            ok, msg = g.guard()
            send(ok=ok, message=msg, table=g.model_table() if ok else [])
        except OSError as e:
            send(ok=False, table=[], message="%s. Install the libgpod package to add songs to an iPod's own "
                                             "library." % e)
        return 0
    if mode != 'serve':
        print('usage: ipod.py probe|serve', file=sys.stderr)
        return 2
    h = _Helper(send)
    for line in sys.stdin:
        try:
            m = json.loads(line)
            cmd = m['cmd']
            if cmd == 'quit':
                return 0
            if cmd not in ('open', 'add', 'commit'):
                raise ValueError('unknown command %s' % cmd)
            if cmd == 'commit':
                h.copied = []                       # from here the songs belong to the library
            send(ev='ok', **(getattr(h, cmd)(m) or {}))
        except BaseException as e:
            send(ev='error', message=_friendly(str(e)) if str(e) else type(e).__name__)
    for f in h.copied:                              # the player went away before the write: nothing lists these
        Path(f).unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

