"""Convert audio to other formats with ffmpeg: MP3, AAC, Opus, Vorbis, FLAC, ALAC, WAV, AIFF, WavPack and, when the
mac program is installed, Monkey's Audio.

formats() lists what this machine can really write: each format is probed once with a 0.2 s tone. A ConvertJob is one
source file with its Options. A Converter runs a bounded pool of jobs and reports through two plain callbacks, so a
window can adapt it with signals. Sources can be audio, video (the sound is taken) and chip or tracker files, which are
rendered to audio first, one file for each song when the file holds several and all of them are asked for.

Nothing here touches Qt and nothing runs a shell: every program is started with an argument list. The source is
never written. A result is encoded to a hidden temp name in the destination folder, checked with ffprobe and then
renamed, and a cancel or an error removes the temp file.
"""
from __future__ import annotations

import base64
import ctypes
import errno
import functools
import io
import json
import math
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, fields, replace
from pathlib import Path

import audio_files as af
import cli_render
import plat_fs
import plat_paths
import plat_proc
import sid_player
from applog import log
from artists import join_artists, split_artists
from audio_files import Cancelled, _norm, fat_name, printable, put_artists, read_mounts, read_tags, unique_name
from ffmpeg_util import ffmpeg_path

# what ffmpeg can write

@dataclass(frozen=True)
class Quality:
    id: str
    label: str
    args: tuple = ()            # ffmpeg codec arguments for this choice
    kbps: int = 0               # for the size estimate, 0 for lossless
    bits: int = 0               # PCM depth, so that a 16 bit result is dithered


@dataclass(frozen=True)
class Format:
    id: str
    label: str
    ext: str
    encoder: str                # what `ffmpeg -encoders` has to list
    muxer: str
    qualities: tuple
    default: str
    lossless: bool = False
    tags: bool = True           # the container takes text tags
    art: bool = True            # and a picture
    art_by: str = 'ffmpeg'      # 'mutagen': Ogg keeps its picture in a tag that ffmpeg does not write; 'id3' and 'ape':
                                # mutagen puts it in an ID3 or an APEv2 block after the encode
    tags_by: str = 'ffmpeg'     # 'id3' and 'ape': the text tags are written by mutagen as well
    external: str = ''          # a program that does the encoding, fed WAV on its standard input
    rates: tuple = ()           # sample rates the encoder takes, empty for any
    max_channels: int = 0       # 0 for no limit
    flags: tuple = ()           # fixed codec and muxer arguments
    note: str = ''              # one plain sentence for the person choosing


def _q(rows):
    return tuple(Quality(*r) for r in rows)


_RATES_MP3 = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
_RATES_AAC = _RATES_MP3 + (64000, 88200, 96000)
_RATES_OPUS = (8000, 12000, 16000, 24000, 48000)
_FLAC_NAMES = {0: 'Faster, largest files (level 0)', 5: 'Normal (level 5)', 8: 'Smaller file (level 8)',
               12: 'Smallest file, slowest (level 12)'}

_TABLE = [
    Format('mp3', 'MP3', '.mp3', 'libmp3lame', 'mp3', _q([
        ('v0', 'Excellent, about 245 kbps (V0)', ('-q:a', '0'), 245),
        ('v2', 'Very good, about 190 kbps (V2)', ('-q:a', '2'), 190),
        ('320', 'Excellent, 320 kbps', ('-b:a', '320k'), 320),
        ('256', 'Very good, 256 kbps', ('-b:a', '256k'), 256),
        ('192', 'Very good, 192 kbps', ('-b:a', '192k'), 192),
        ('128', 'Good, about 128 kbps', ('-b:a', '128k'), 128)]), 'v2',
        rates=_RATES_MP3, max_channels=2, flags=('-id3v2_version', '3', '-write_id3v1', '1'),
        note='Plays almost everywhere. Sample rates above 48 kHz and more than two channels are reduced.'),
    Format('aac', 'AAC in M4A', '.m4a', 'aac', 'ipod', _q([
        ('128', 'Good, about 128 kbps', ('-b:a', '128k'), 128),
        ('192', 'Very good, 192 kbps', ('-b:a', '192k'), 192),
        ('256', 'Excellent, 256 kbps', ('-b:a', '256k'), 256),
        ('320', 'Excellent, 320 kbps', ('-b:a', '320k', '-aac_ms', '0'), 320)]), '256', rates=_RATES_AAC,
        note='Good quality at a small size. Plays on Apple devices, Android and most players.'),
    Format('opus', 'Opus', '.opus', 'libopus', 'opus', _q([
        ('64', 'Fair, 64 kbps (small files)', ('-b:a', '64k'), 64),
        ('96', 'Good, 96 kbps', ('-b:a', '96k'), 96),
        ('128', 'Very good, 128 kbps', ('-b:a', '128k'), 128),
        ('192', 'Excellent, 192 kbps', ('-b:a', '192k'), 192),
        ('256', 'Excellent, 256 kbps', ('-b:a', '256k'), 256)]), '128', art_by='mutagen', rates=_RATES_OPUS,
        flags=('-application', 'audio'), note='The best quality for its size. Always saved at 48 kHz.'),
    Format('vorbis', 'Ogg Vorbis', '.ogg', 'libvorbis', 'ogg', _q([
        ('q3', 'Fair, about 112 kbps (q3)', ('-q:a', '3'), 112),
        ('q5', 'Good, about 160 kbps (q5)', ('-q:a', '5'), 160),
        ('q7', 'Very good, about 224 kbps (q7)', ('-q:a', '7'), 224),
        ('q9', 'Excellent, about 320 kbps (q9)', ('-q:a', '9'), 320)]), 'q5', art_by='mutagen',
        note='An open format that most players and many games understand.'),
    Format('flac', 'FLAC', '.flac', 'flac', 'flac',
           _q([(str(n), _FLAC_NAMES.get(n, 'Level %d' % n), ('-compression_level', str(n))) for n in range(13)]),
           '5', lossless=True, note='Lossless. Sounds the same as the original, in a smaller file.'),
    Format('alac', 'Apple Lossless in M4A', '.m4a', 'alac', 'ipod', _q([('lossless', 'Lossless')]), 'lossless',
           lossless=True, note='Lossless, for Apple devices and iTunes.'),
    Format('wav', 'WAV', '.wav', 'pcm_s16le', 'wav', _q([
        ('16', '16 bit, CD quality', ('-c:a', 'pcm_s16le'), 0, 16),
        ('24', '24 bit', ('-c:a', 'pcm_s24le')),
        ('32', '32 bit float', ('-c:a', 'pcm_f32le'))]), '16', lossless=True, art_by='id3', tags_by='id3',
        note='Uncompressed and large. Tags and cover art are saved in an ID3 block, which most players read.'),
    Format('aiff', 'AIFF', '.aiff', 'pcm_s16be', 'aiff', _q([
        ('16', '16 bit, CD quality', ('-c:a', 'pcm_s16be'), 0, 16),
        ('24', '24 bit', ('-c:a', 'pcm_s24be'))]), '16', lossless=True,
        flags=('-write_id3v2', '1', '-id3v2_version', '3'), note='Uncompressed and large.'),
    Format('wavpack', 'WavPack', '.wv', 'wavpack', 'wv', _q([
        ('0', 'Fast (level 0)', ('-compression_level', '0')),
        ('1', 'Normal (level 1)', ('-compression_level', '1')),
        ('2', 'High (level 2)', ('-compression_level', '2'))]), '2', lossless=True, art_by='ape', tags_by='ape',
        flags=('-frame_size', '4096'),             # the encoder refuses the 1152 sample frames of an MP3
        note='Lossless. Tags and cover art are saved in an APE block.'),
    Format('ape', "Monkey's Audio", '.ape', '', 'ape', _q([
        ('1000', 'Fast', ('-c1000',)),
        ('2000', 'Normal', ('-c2000',)),
        ('3000', 'High', ('-c3000',)),
        ('4000', 'Extra high', ('-c4000',)),
        ('5000', 'Insane, slow', ('-c5000',))]), '2000', lossless=True, art_by='ape', tags_by='ape', external='mac',
        note="Lossless. Encoded with the mac program."),
]
_FDK = replace(_TABLE[1], encoder='libfdk_aac')

_encoders: dict[str, set] = {}
_enc_lock = threading.Lock()
_works: dict[str, dict] = {}            # build key to {format id: the encoder that really worked}
_probe_lock = threading.Lock()
PROBE_SECONDS = 0.2
PROBE_TIMEOUT = 20.0          # a program that takes longer than this for 0.2 s of sound is not offered


def encoders(refresh: bool = False) -> set:
    """The audio encoders this ffmpeg lists. Asked once and kept."""
    exe = ffmpeg_path() or ''
    with _enc_lock:
        if refresh or exe not in _encoders:
            found: set = set()
            if exe:
                try:
                    out = subprocess.run([exe, '-hide_banner', '-encoders'], capture_output=True, timeout=20,
                                         stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                                         **plat_proc.popen_kwargs(group=False)).stdout
                    found = {m.group(1) for m in re.finditer(r'^\s*A[A-Z.]{5}\s+(\S+)', out, re.M)}
                except (OSError, subprocess.SubprocessError):
                    pass
            _encoders[exe] = found
        return _encoders[exe]


def _stamp(path) -> str:
    try:
        st = os.stat(path)
        return '%s:%d:%d' % (path, st.st_mtime_ns, st.st_size)
    except (OSError, TypeError):
        return str(path or '')


def _build_key(exe: str) -> str:
    """Names the ffmpeg build and the helper programs, so that a new build is probed again."""
    return '|'.join([_stamp(exe)] + [_stamp(shutil.which(n)) for n in sorted({f.external for f in _TABLE if f.external})])


def _candidates(have: set) -> list[Format]:
    """Formats whose encoder or program is installed, with the better AAC encoder first when ffmpeg has it."""
    out = []
    for f in _TABLE:
        if f.external:
            if shutil.which(f.external):
                out.append(f)
        elif f.id == 'aac' and 'libfdk_aac' in have:
            out += [_FDK, f]
        elif f.encoder in have:
            out.append(f)
    return out


def _tone_wav(path: Path):
    rate, n = 44100, int(44100 * PROBE_SECONDS)
    frames = b''.join(struct.pack('<hh', *(int(8000 * math.sin(2 * math.pi * 440 * i / rate)),) * 2) for i in range(n))
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def _probe_encode(exe: str, fmt: Format) -> bool:
    """Whether a 0.2 s tone really comes out in this format, through the same command lines a conversion uses."""
    try:
        with tempfile.TemporaryDirectory(prefix='convert-probe-') as d:
            wav, out = Path(d) / 'tone.wav', Path(d) / ('probe' + fmt.ext)
            _tone_wav(wav)
            opts = Options(format=fmt.id, tags=False, art=False)
            q = quality_of(fmt, '')
            if fmt.external:
                args = encode_args(exe, str(wav), '', fmt, q, opts, pcm=16)
                rc, _ = _run_pair(args, external_args(fmt, q, str(out)), threading.Event(), None, None, PROBE_TIMEOUT)
            else:
                args = encode_args(exe, str(wav), str(out), fmt, q, opts, rate=pick_rate(fmt, 44100))
                rc = subprocess.run(args, capture_output=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL,
                                    **plat_proc.popen_kwargs(group=False)).returncode
            return rc == 0 and out.is_file() and out.stat().st_size > 0
    except Exception:
        return False


def _probe_all(exe: str, have: set) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    cands = _candidates(have)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(cands)))) as pool:
        ok = list(pool.map(lambda f: _probe_encode(exe, f), cands))
    found: dict = {}
    for f, good in zip(cands, ok):
        if good and f.id not in found:
            found[f.id] = f.encoder
    return found


def _cache_file() -> Path:
    return plat_paths.cache_dir() / 'convert-formats.json'


def _remembered(key: str) -> dict | None:
    """What an earlier probe of this build found, from memory or from the cache folder. Probes nothing."""
    with _probe_lock:
        if key in _works:
            return _works[key]
    try:
        d = json.loads(_cache_file().read_text(encoding='utf-8'))
        if d.get('key') == key and isinstance(d.get('ok'), dict):
            return {str(k): str(v) for k, v in d['ok'].items()}
    except (OSError, ValueError):
        pass
    return None


def _usable(exe: str, have: set, refresh: bool) -> dict:
    key = _build_key(exe)
    with _probe_lock:
        if not refresh and key in _works:
            return _works[key]
    found = None if refresh else _remembered(key)
    with _probe_lock:
        if not refresh and key in _works:
            return _works[key]
        if found is None:
            found = _probe_all(exe, have)
            try:
                _cache_file().parent.mkdir(parents=True, exist_ok=True)
                tmp = _cache_file().with_name('.convert-formats.%d.tmp' % os.getpid())
                tmp.write_text(json.dumps({'key': key, 'ok': found}), encoding='utf-8')
                plat_fs.replace(tmp, _cache_file())
            except OSError:
                pass
        _works[key] = found
        return found


def formats(refresh: bool = False) -> list[Format]:
    """The formats this machine can write, best known first. A format is listed only after a 0.2 s probe encode
    worked, once per ffmpeg build (the answer is kept in memory and in the cache folder). AAC uses libfdk_aac when
    ffmpeg has it and it works. Monkey's Audio needs the mac program."""
    exe = ffmpeg_path()
    if not exe:
        return []
    have = encoders(refresh)
    works = _usable(exe, have, refresh)
    out = []
    for f in _TABLE:
        enc = works.get(f.id)
        if enc is not None:
            out.append(f if f.encoder == enc else replace(f, encoder=enc))
    return out


def format_by_id(fid: str) -> Format | None:
    """A format by its id. A conversion looks here, so this never probes: it goes by the probe that was made for this
    ffmpeg build when there is one, else by the encoders ffmpeg lists."""
    exe = ffmpeg_path()
    if not exe:
        return None
    works = _remembered(_build_key(exe))
    if works is not None:
        enc = works.get(fid)
        f = next((x for x in _TABLE if x.id == fid), None)
        return None if enc is None or f is None else (f if f.encoder == enc else replace(f, encoder=enc))
    return next((f for f in _candidates(encoders()) if f.id == fid), None)


def quality_of(fmt: Format, qid: str) -> Quality:
    return next((q for q in fmt.qualities if q.id == qid), None) or next(q for q in fmt.qualities
                                                                         if q.id == fmt.default)


def pick_rate(fmt: Format, want: int) -> int:
    """The sample rate to ask for: `want` when the encoder takes it, else the nearest one it does, keeping to the
    44.1 or the 48 kHz family where it can."""
    if not want or not fmt.rates or want in fmt.rates:
        return want
    if want > max(fmt.rates):
        return max(fmt.rates)
    family = [r for r in fmt.rates if (r % 44100 == 0) == (want % 44100 == 0)] or list(fmt.rates)
    return min(family, key=lambda r: abs(r - want))


# options and the file name template

FIELDS = ('artist', 'album', 'albumartist', 'title', 'track', 'disc', 'year', 'genre', 'filename')
NORMALISE = ('off', 'replaygain', 'loudnorm')
EXISTS = ('number', 'skip', 'overwrite')
RATES = (0, 44100, 48000, 96000)


@dataclass
class Options:
    format: str = 'mp3'
    quality: str = ''               # '' is the format's default
    rate: int = 0                   # 0 keeps the source's rate
    channels: int = 0               # 0 keeps them, else 1 or 2
    normalise: str = 'off'          # 'off', 'replaygain' (from the file's tags) or 'loudnorm' (measured first, then a plain gain)
    trim: bool = False              # silence at the start and the end
    tags: bool = True
    art: bool = True
    template: str = '{filename}'    # folders and name, '/' between the parts
    folder: str = ''                # '' puts each result next to its source
    exists: str = 'number'          # a file with that name is there already: number the new one, skip or replace
    fat: bool | None = None         # names that work on FAT drives, None decides from the destination
    chip_seconds: int = 180         # for a chip file that does not say how long it is
    chip_fade: float = 0.0          # seconds of fade out at the end of a chip file
    chip_loops: int = 1             # times a looping chip tune plays
    subsong: str | int = 'first'    # which song of a file that holds several: 'first', 'all' (one file each) or a number from 1
    dsp: bool = False               # run the sound through the player's equalizer and enhancer first

    @classmethod
    def from_dict(cls, d) -> Options:
        """Options from saved JSON. A value of the wrong kind or outside its range falls back to the default."""
        o = cls()
        if not isinstance(d, dict):
            return o

        def pick(key, ok):
            v = d.get(key)
            if v is not None and ok(v):
                setattr(o, key, v)
        is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)
        is_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        pick('format', lambda v: isinstance(v, str) and v in {f.id for f in _TABLE})
        pick('quality', lambda v: isinstance(v, str) and len(v) < 16)
        pick('rate', lambda v: v in RATES and is_int(v))
        pick('channels', lambda v: v in (0, 1, 2) and is_int(v))
        pick('normalise', lambda v: v in NORMALISE)
        for k in ('trim', 'tags', 'art', 'dsp'):
            pick(k, lambda v: isinstance(v, bool))
        pick('template', lambda v: isinstance(v, str) and 0 < len(v) < 300)
        pick('folder', lambda v: isinstance(v, str) and len(v) < 4096)
        pick('exists', lambda v: v in EXISTS)
        pick('fat', lambda v: isinstance(v, bool))
        pick('chip_seconds', lambda v: is_int(v) and 5 <= v <= 3600)
        pick('chip_fade', lambda v: is_num(v) and 0 <= v <= 60)
        pick('chip_loops', lambda v: is_int(v) and 1 <= v <= 16)
        pick('subsong', lambda v: v in ('first', 'all') if isinstance(v, str) else is_int(v) and 1 <= v <= 999)
        return o

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def settings_path() -> Path:
    return plat_paths.config_dir() / 'convert.json'


def load_options(path: Path | None = None) -> Options:
    """The saved options. A missing, damaged or foreign file gives the defaults."""
    try:
        return Options.from_dict(json.loads((path or settings_path()).read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return Options()


def save_options(opts: Options, path: Path | None = None) -> bool:
    """Write the options through a temp file and a rename, so a crash never leaves half a file."""
    path = path or settings_path()
    tmp = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(opts.to_dict(), indent=2), encoding='utf-8')
        plat_fs.replace(tmp, path)
        return True
    except OSError:
        log.warning('could not save the converter settings to %s', path)
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_FIELD = re.compile(r'\{([^{}]*)\}')


def template_error(template: str) -> str:
    """'' when the template can be used, else one plain sentence about what is wrong with it."""
    if not template.strip():
        return "Type a name for the files."
    for m in _FIELD.finditer(template):
        if m.group(1) not in FIELDS:
            return "{%s} is not a field. Use %s." % (m.group(1), ', '.join('{%s}' % f for f in FIELDS))
    if '{' in _FIELD.sub('', template) or '}' in _FIELD.sub('', template):
        return "A curly bracket is not closed."
    return ''


def _values(tags: dict, src: Path) -> dict:
    artist = (tags.get('artist') or '').strip()
    first = (tags.get('artist_values') or [artist])[0]   # with no album artist a file is filed under the first value
    return {'artist': artist or 'Unknown artist',
            'albumartist': (tags.get('albumartist') or '').strip() or first or 'Unknown artist',
            'album': (tags.get('album') or '').strip() or 'Unknown album',
            'title': (tags.get('title') or '').strip() or printable(src.stem),
            'track': '%02d' % tags['track'] if tags.get('track') else '',
            'disc': str(tags['disc']) if tags.get('disc') else '',
            'year': str(tags['year']) if tags.get('year') else '',
            'genre': (tags.get('genre') or '').strip() or 'Unknown genre',
            'filename': printable(src.stem)}


def render_name(template: str, tags: dict, src: Path, ext: str, fat: bool = True) -> list[str]:
    """The folders and file name a template gives for one source, as safe path parts with the extension on the
    last. Only the template makes folders: a slash in a tag is replaced, never obeyed."""
    vals = _values(tags, src)
    raw = [p for p in re.split(r'[\\/]+', template) if p.strip() not in ('', '.')] or ['{filename}']
    parts = []
    for n, part in enumerate(raw):
        last = n == len(raw) - 1
        hole = [False]

        def fill(m, hole=hole):
            v = vals.get(m.group(1), '')
            hole[0] |= v == ''
            return v
        text = _FIELD.sub(fill, part)
        text = re.sub(r'\s+', ' ', re.sub(r'\(\s*\)|\[\s*\]', '', text)) if hole[0] else re.sub(r'\s+', ' ', text)
        if hole[0]:                             # a missing field leaves a dangling separator behind
            text = re.sub(r'^[\s\-_.,:]+|[\s\-_,:]+$', '', text)
        text = text.strip()
        if not text:
            text = printable(src.stem) if last else 'Unknown'
        parts.append(fat_name(text, 200 - len(ext.encode()), strict=fat) + ext if last else fat_name(text, 200, strict=fat))
    return parts


def example(template: str, tags: dict, src: Path, ext: str, fat: bool = True) -> str:
    return '/'.join(render_name(template, tags, src, ext, fat))


def fat_default(folder: Path, mountinfo: str | Path = '/proc/self/mountinfo') -> bool:
    """Whether names for this folder should work on FAT: it is on a FAT, exFAT or NTFS file system, or on a volume
    mounted under /run/media or /media, where removable drives appear. Windows refuses the same characters on every
    file system, so there the answer is always yes."""
    if sys.platform == 'win32':
        return True
    try:
        here = os.path.realpath(folder)
    except OSError:
        here = str(folder)
    best = None
    for m in read_mounts(mountinfo).values():
        mp = m['mount'].rstrip('/') or '/'
        if (here == mp or here.startswith(mp.rstrip('/') + '/')) and (best is None or len(mp) > len(best[0])):
            best = (mp, m['fs'])
    if best is None:
        return False
    return (best[1] in ('vfat', 'msdos', 'fat', 'exfat', 'ntfs', 'ntfs3', 'fuseblk')
            or best[0].startswith(('/run/media/', '/media/')))


# how big the results will be

_GUESS = {'flac': 0.62, 'alac': 0.64, 'wavpack': 0.6, 'ape': 0.55}       # share of the plain PCM size, until measured


def ratios_path() -> Path:
    return plat_paths.cache_dir() / 'convert-ratios.json'


def load_ratios(path: Path | None = None) -> dict:
    """{'flac:5': [share of the PCM size, weight]} measured on finished files in earlier runs, {} when there are none."""
    try:
        d = json.loads((path or ratios_path()).read_text(encoding='utf-8'))
        return {str(k): [float(v[0]), float(v[1])] for k, v in d.items()
                if isinstance(v, list) and len(v) == 2 and 0 < float(v[0]) < 4 and float(v[1]) > 0}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_ratios(ratios: dict, path: Path | None = None) -> bool:
    path = path or ratios_path()
    tmp = path.with_name('.%s.%d.tmp' % (path.name, os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(ratios), encoding='utf-8')
        plat_fs.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _seconds(info: dict, opts: Options) -> float:
    """Seconds of sound one listed file will give: its length, or for a chip file the length a conversion will take."""
    d = info.get('duration')
    if info.get('kind') == 'chip' and not d:
        d = opts.chip_seconds
    n = info.get('songs') or 1
    return float(d or 0) * (n if info.get('kind') == 'chip' and opts.subsong == 'all' else 1)


def pcm_bytes(info: dict, fmt: Format, q: Quality, opts: Options) -> int:
    """The size of a listed file's sound as plain PCM at the rate, channels and depth the result will have."""
    t = info.get('tags') or {}
    rate = pick_rate(fmt, opts.rate or t.get('rate') or 44100) or 44100
    ch = opts.channels or min(t.get('channels') or 2, fmt.max_channels or 99)
    depth = {'16': 2, '24': 3, '32': 4}.get(q.id, 2) if fmt.id in ('wav', 'aiff') else (2 if (t.get('bits') or 16) <= 16 else 3)
    return int(_seconds(info, opts) * rate * ch * depth)


def estimate_size(fmt: Format, q: Quality, opts: Options, infos, ratios: dict | None = None) -> tuple[int, bool] | None:
    """(bytes, measured) the results of these listed files should come to. Lossy formats follow bit rate times length.
    WAV and AIFF are exact. The lossless ones take the share of the PCM size that finished files showed (measured), or a
    guess for the format until some did. None when no length is known."""
    infos = list(infos)
    if not any(_seconds(i, opts) for i in infos):
        return None
    if q.kbps:
        return int(sum(_seconds(i, opts) for i in infos) * q.kbps * 125), True
    pcm = sum(pcm_bytes(i, fmt, q, opts) for i in infos)
    if fmt.id in ('wav', 'aiff'):
        return pcm, True
    hit = (ratios or {}).get('%s:%s' % (fmt.id, q.id)) or (ratios or {}).get(fmt.id)
    return int(pcm * (hit[0] if hit else _GUESS.get(fmt.id, 0.6))), bool(hit)


def note_ratio(ratios: dict, fmt: Format, q: Quality, info: dict, opts: Options, bytes_out: int):
    """Fold one finished lossless result into the measured shares."""
    pcm = pcm_bytes(info, fmt, q, opts)
    if fmt.lossless and fmt.id not in ('wav', 'aiff') and pcm > 0 and bytes_out > 0:
        for key in ('%s:%s' % (fmt.id, q.id), fmt.id):
            r, w = ratios.get(key, [bytes_out / pcm, 0.0])
            w2 = min(w + pcm, 2e10)
            ratios[key] = [(r * w + bytes_out) / (w + pcm) if w else bytes_out / pcm, w2]


def elapsed_text(seconds: float) -> str:
    """'48 s', '3 min 12 s', '1 h 02 min'."""
    s = int(round(seconds))
    if s < 1:
        return 'under 1 s'
    if s < 60:
        return '%d s' % s
    if s < 3600:
        return '%d min %02d s' % (s // 60, s % 60)
    return '%d h %02d min' % (s // 3600, s % 3600 // 60)


def human_size(n: int) -> str:
    if n < 1024:
        return '%d B' % n
    if n < 1024 ** 2:
        return '%d KB' % round(n / 1024)
    if n < 1024 ** 3:
        mb = n / 1024 ** 2
        return ('%.1f MB' % mb) if mb < 10 else '%d MB' % round(mb)
    return '%.1f GB' % (n / 1024 ** 3)


def human_time(seconds) -> str:
    if not seconds or seconds <= 0:
        return ''
    s = int(round(seconds))
    return '%d:%02d:%02d' % (s // 3600, s % 3600 // 60, s % 60) if s >= 3600 else '%d:%02d' % (s // 60, s % 60)


# what a source is

@functools.cache
def exts() -> tuple[frozenset, frozenset, frozenset]:
    """(audio, video, chip) extensions the player accepts."""
    from player import AUDIO_EXTS, VIDEO_EXTS
    chip = frozenset(af.CHIP_EXTS)
    return frozenset(AUDIO_EXTS) - chip, frozenset(VIDEO_EXTS) - frozenset(AUDIO_EXTS) - chip, chip


def kind_of(path: Path) -> str:
    """'audio', 'video', 'chip' or '' for something this cannot convert."""
    e = path.suffix.lower()
    audio, video, chip = exts()
    return 'chip' if e in chip else 'audio' if e in audio else 'video' if e in video else ''


def expand(paths) -> list[Path]:
    """Files as given and the convertible files below folders, in order, each once."""
    a, v, c = exts()
    seen, out = set(), []
    for p in af.expand_sources([Path(x) for x in paths], a | v | c):
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def ffprobe_path() -> str | None:
    exe = ffmpeg_path()
    if exe:
        for name in ('ffprobe', 'ffprobe.exe'):
            cand = os.path.join(os.path.dirname(exe), name)
            if os.path.isfile(cand):
                return cand
    return shutil.which('ffprobe')


def probe(path, timeout: float = 30) -> dict | None:
    """What ffprobe says about a file: duration, the first audio stream's rate, channels and codec, and the tags with
    lower case keys. None when the file cannot be read at all."""
    exe = ffprobe_path()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams',
                            'file:' + os.path.abspath(path)], capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, **plat_proc.popen_kwargs(group=False))
        data = json.loads(r.stdout.decode('utf-8', 'replace')) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = data.get('format') or {}
    audio = next((s for s in data.get('streams') or [] if s.get('codec_type') == 'audio'), None)

    def num(v):
        try:
            x = float(v)
            return x if math.isfinite(x) and x >= 0 else None
        except (TypeError, ValueError):
            return None
    tags = {}
    for src in (audio or {}, fmt):
        for k, v in (src.get('tags') or {}).items():
            tags.setdefault(str(k).lower(), str(v))
    return {'duration': num(fmt.get('duration')) or num((audio or {}).get('duration')), 'has_audio': audio is not None,
            'rate': int(num((audio or {}).get('sample_rate')) or 0), 'channels': int((audio or {}).get('channels') or 0),
            'codec': (audio or {}).get('codec_name') or '', 'format': fmt.get('format_name') or '',
            'bits': int(num((audio or {}).get('bits_per_raw_sample')) or (audio or {}).get('bits_per_sample') or 0),
            'tags': tags}


def merge_probe_tags(tags: dict, pr: dict, src: Path) -> dict:
    """Fill in what mutagen did not find (WAV info chunks, Matroska and the like) from ffprobe's tags."""
    pt = pr.get('tags') or {}
    if tags['title'] == printable(src.stem) and pt.get('title'):
        tags['title'] = pt['title'].strip()
    for k, alt in (('artist', 'artist'), ('album', 'album'), ('albumartist', 'album_artist'), ('genre', 'genre')):
        if not tags[k]:
            tags[k] = (pt.get(alt) or pt.get(k) or '').strip()
    if tags['artist'] and not tags['artists']:
        tags['artists'] = split_artists([tags['artist']], tags['albumartist'])
        tags['artist'] = join_artists(tags['artists'])
    for key, a, b in (('track', 'track', 'tracks'), ('disc', 'disc', 'discs')):
        m = re.match(r'\s*(\d+)(?:\s*/\s*(\d+))?', pt.get(key) or '')
        if m and not tags[a]:
            tags[a], tags[b] = int(m.group(1)), int(m.group(2) or 0)
    m = re.match(r'\d{4}', pt.get('date') or pt.get('year') or '')
    if m and not tags['year']:
        tags['year'] = int(m.group())
    if not tags['duration'] and pr.get('duration'):
        tags['duration'] = pr['duration']
    return tags


# chip and tracker files

_GME_EXTS = frozenset({'.vgm', '.vgz', '.nsf', '.nsfe', '.spc', '.gbs', '.ay', '.gym', '.hes', '.kss', '.sap',
                       '.sgc'})
_OPENMPT_EXTS = frozenset(getattr(cli_render, '_OPENMPT_EXTS', ()))
_GME_RATE = 44100
_SID_RATE = 44100
_MAX_SONGS = 999


@functools.cache
def _gme():
    """The libgme binding the player already made, or None."""
    try:
        import vgm_player
        return vgm_player
    except Exception:
        return None


def _gme_text(p) -> str:
    return p.decode(errors='replace').strip() if p else ''


def _gme_info(path: Path, track: int = 0) -> dict:
    """Title, author, game, the lengths libgme knows for one song (0 based) and how many songs the file holds."""
    vp = _gme()
    if vp is None:
        return {}
    emu = ctypes.c_void_p()
    if vp.gme_open(vp._lib, os.fsencode(path), emu, _GME_RATE):
        return {}
    out = {}
    try:
        out['tracks'] = int(vp._lib.gme_track_count(emu))
        info = ctypes.POINTER(vp._GmeInfo)()
        if not vp._lib.gme_track_info(emu, ctypes.byref(info), track) and info:
            i = info.contents
            out.update({'title': _gme_text(i.song) or _gme_text(i.game), 'artist': _gme_text(i.author),
                        'album': _gme_text(i.game), 'length': i.length / 1000, 'intro': i.intro_length / 1000,
                        'loop': i.loop_length / 1000, 'song': _gme_text(i.song)})
            vp._lib.gme_free_info(info)
    finally:
        vp._lib.gme_delete(emu)
    return out


def _gme_titles(path: Path) -> list[str]:
    """The title of every song in a libgme file, '' where the file gives none."""
    vp = _gme()
    if vp is None:
        return []
    emu = ctypes.c_void_p()
    if vp.gme_open(vp._lib, os.fsencode(path), emu, _GME_RATE):
        return []
    try:
        n = min(int(vp._lib.gme_track_count(emu)), _MAX_SONGS)
        titles = []
        for t in range(n):
            info = ctypes.POINTER(vp._GmeInfo)()
            title = ''
            if not vp._lib.gme_track_info(emu, ctypes.byref(info), t) and info:
                title = _gme_text(info.contents.song)
                vp._lib.gme_free_info(info)
            titles.append(title)
        return titles
    finally:
        vp._lib.gme_delete(emu)


@functools.lru_cache(maxsize=256)
def _openmpt_count(path: str, stamp) -> int:
    exe = plat_proc.which('openmpt123')
    if not exe:
        return 1
    try:
        out = subprocess.run([exe, '--info', path], capture_output=True, timeout=20, stdin=subprocess.DEVNULL,
                             text=True, encoding='utf-8', errors='replace', **plat_proc.popen_kwargs(group=False)).stdout
    except (OSError, subprocess.SubprocessError):
        return 1
    m = re.search(r'^Subsongs\.*:\s*(\d+)', out, re.M)
    return max(1, min(int(m.group(1)), _MAX_SONGS)) if m else 1


def chip_songs(path: Path) -> list[str]:
    """The titles of the songs in a chip or tracker file, '' where a song has none; one entry for a file with one song.
    Never raises. libgme formats, SID, Furnace and the tracker modules that libopenmpt splits into subsongs count
    here. The zxtune formats other than AY, which libgme also reads, play as one song."""
    ext = path.suffix.lower()
    try:
        if ext in sid_player.EXTS:
            i = sid_player.read_info(path)
            return [''] * min(i.subsongs if i else 1, _MAX_SONGS)
        if ext == '.fur':
            return [''] * min(_fur_song_count(path), _MAX_SONGS)
        if ext in _GME_EXTS:
            return _gme_titles(path) or ['']
        if ext in _OPENMPT_EXTS and plat_proc.which('openmpt123'):
            st = path.stat()
            return [''] * _openmpt_count(str(path), (st.st_mtime_ns, st.st_size))
    except Exception:
        log.exception('counting the songs in %s', path)
    return ['']


def first_song(path: Path) -> int:
    """The number (from 1) of the song a player starts with: a SID file names its own, the others start with the
    first."""
    if path.suffix.lower() in sid_player.EXTS:
        i = sid_player.read_info(path)
        return max(1, i.start_song) if i else 1
    return 1


def song_plan(path: Path, subsong, titles: list[str] | None = None) -> list[tuple[int, str]]:
    """[(song number from 1, its title)] for what Options.subsong asks of this file: 'first', 'all' or a number.
    Raises ConvertError for a number the file does not have."""
    titles = chip_songs(path) if titles is None else titles
    n = len(titles)
    if subsong == 'all':
        return [(k + 1, titles[k]) for k in range(n)]
    k = first_song(path) if subsong == 'first' else int(subsong)
    if k > n:
        raise ConvertError("This file has %s, so song %d is not there." % ('one song' if n == 1 else '%d songs' % n, k))
    return [(k, titles[k - 1])]


def _gme_reads(ext: str) -> bool:
    """Whether libgme is what renders this extension here: zxtune takes AY and VGM where it is installed."""
    return ext in _GME_EXTS and not cli_render.supported(ext) and _gme() is not None


def chip_info(path: Path) -> dict:
    """Title, artist, album and the length when the file or its player knows it, for a chip or tracker file.
    Never raises."""
    ext, out = path.suffix.lower(), {}
    try:
        if ext in sid_player.EXTS:
            i = sid_player.read_info(path)
            if i:
                out = {'title': i.title, 'artist': i.author}
                n = sid_player.known_length(path, i.start_song)
                out['length'] = n
        elif ext == '.fur':
            import fur_player
            t = fur_player.fur_tags(path)
            out = {'title': t.get('title', ''), 'artist': t.get('artist', ''), 'album': t.get('album', ''),
                   'length': (t.get('duration_us') or 0) / 1e6 or None}
        elif _gme_reads(ext):
            out = _gme_info(path)
    except Exception:
        log.exception('reading %s', path)
    return out


def chip_tags(path: Path) -> dict:
    t = read_tags(path)
    info = chip_info(path)
    for k in ('title', 'artist', 'album'):
        if info.get(k):
            t[k] = info[k]
    return t


def chip_uses(path: Path, info: dict) -> dict:
    """Which of the length settings change anything for this file: {'loops': bool, 'seconds': bool}. A Furnace file
    plays its loops. A libgme tune that says how long it is, or has a loop, does not need the seconds, and only one
    with a loop takes the loops. A SID that the song length database knows has its length, and the rest are
    measured when they are converted. Tracker modules and the zxtune formats play through once."""
    ext = path.suffix.lower()
    if ext == '.fur':
        return {'loops': True, 'seconds': False}
    if ext in sid_player.EXTS:
        try:
            known = bool(sid_player._db_length_s(path, first_song(path)))
        except Exception:
            known = False
        return {'loops': not known, 'seconds': not known}
    if _gme_reads(ext):
        if (info.get('loop') or 0) > 0:
            return {'loops': True, 'seconds': False}
        return {'loops': False, 'seconds': not (info.get('length') or 0) > 0}
    return {'loops': False, 'seconds': False}


def describe(path) -> dict:
    """What a list of files shows for one: name, extension, kind, size, length and tags. Reads the file, so it
    belongs on a worker thread. A chip file also says how many songs it holds ('songs', 1 for one) and which of the
    length settings apply to it ('uses')."""
    path = Path(path)
    kind = kind_of(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    extra = {}
    if kind == 'chip':
        info = chip_info(path)
        tags = read_tags(path)
        tags.update({k: info[k] for k in ('title', 'artist', 'album') if info.get(k)})
        tags['duration'] = info.get('length')
        extra = {'songs': len(chip_songs(path)), 'uses': chip_uses(path, info)}
    else:
        tags = read_tags(path)
        pr = probe(path, timeout=10)
        if pr:                                  # the same tags and length the converter will use for the name
            merge_probe_tags(tags, pr, path)
        tags['rate'] = tags.get('rate') or (pr or {}).get('rate') or 0
        tags['channels'] = tags.get('channels') or (pr or {}).get('channels') or 0
        tags['bits'] = tags.get('bits') or (pr or {}).get('bits') or 0
    return {'path': path, 'name': printable(path.name), 'ext': path.suffix.lower().lstrip('.'), 'kind': kind,
            'size': size, 'duration': tags['duration'], 'tags': tags, **extra}


def _wav_seconds(path) -> float:
    try:
        with wave.open(str(path), 'rb') as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        return 0.0


def _song_number(opts: Options) -> int:
    return opts.subsong if isinstance(opts.subsong, int) and not isinstance(opts.subsong, bool) and opts.subsong >= 1 else 1


def _render_gme(path: Path, opts: Options, cancel: threading.Event, tmp: Path):
    """A libgme file as a WAV in tmp: the song's own length, intro plus the loops for a looping tune, else the
    chosen number of seconds. Returns (wav, seconds, note) or (None, None)."""
    vp = _gme()
    if vp is None:
        return None, None
    song = _song_number(opts)
    lib, info = vp._lib, _gme_info(path, song - 1)
    note = ''
    if info.get('loop', 0) > 0:
        loops = max(opts.chip_loops, 1)
        length = max(info.get('intro', 0), 0) + loops * info['loop']
        note = '%d loop%s of %.1f s' % (loops, '' if loops == 1 else 's', info['loop'])
    elif info.get('length', 0) > 0:
        length = info['length']
    else:
        length = float(opts.chip_seconds)
    emu = ctypes.c_void_p()
    if vp.gme_open(lib, os.fsencode(path), emu, _GME_RATE):
        return None, None
    out = tmp / 'gme.wav'
    try:
        if lib.gme_start_track(emu, song - 1):
            return None, None
        try:
            from scipy.signal import lfilter        # the player removes DC the same way
            import numpy as np
            zi = np.zeros((1, 2))
        except ImportError:
            lfilter = None
        total, done, chunk = int(length * _GME_RATE), 0, 4096
        buf = (ctypes.c_short * (chunk * 2))()
        with wave.open(str(out), 'wb') as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(_GME_RATE)
            while done < total:
                if cancel.is_set():
                    raise Cancelled
                n = min(chunk, total - done)
                if lib.gme_play(emu, n * 2, buf):
                    break
                data = bytes(buf)[:n * 4]
                if lfilter is not None:
                    x = np.frombuffer(data, dtype=np.int16).reshape(-1, 2).astype(np.float64)
                    y, zi = lfilter([1.0, -1.0], [1.0, -0.999], x, axis=0, zi=zi)
                    data = np.clip(y, -32768, 32767).astype(np.int16).tobytes()
                w.writeframes(data)
                done += n
                if lib.gme_track_ended(emu):
                    break
    finally:
        lib.gme_delete(emu)
    return (str(out), done / _GME_RATE, note) if done else (None, None)


def _render_sid(path: Path, opts: Options, cancel: threading.Event, tmp: Path):
    """One SID song as a WAV in tmp. The length is the song length database's when it knows the tune. Else the tune is
    rendered for a while and looked at: a tune that stops ends there, one that loops plays its loop `chip_loops`
    times, and one that does neither plays for `chip_seconds`. Returns (wav, seconds, note) or (None, None)."""
    import numpy as np
    import loop_detect
    song = opts.subsong if isinstance(opts.subsong, int) else first_song(path)
    db = sid_player._db_length_s(path, song)
    window = int(min(max(300, opts.chip_seconds), 3600))
    pcm = sid_player._render_pcm(path, song, int(min(window, db + 1)) + 1 if db else window)
    if pcm is None or not len(pcm):
        return None, None
    loops = max(opts.chip_loops, 1)
    if db:
        length, note = db, 'The length comes from the song length database.'
    else:
        mono = pcm.mean(axis=1).astype(np.float32) / 32768.0
        r = loop_detect.analyze(mono, _SID_RATE, loops=1)
        if r.reason == 'loop':
            length = r.loop_s * loops
            note = '%d loop%s of %.1f s' % (loops, '' if loops == 1 else 's', r.loop_s)
            need = int(length) + 2
            if need > len(pcm) / _SID_RATE and need <= 3600:       # more loops than the first render holds
                pcm = sid_player._render_pcm(path, song, need)
                if pcm is None or not len(pcm):
                    return None, None
        elif r.reason == 'silence':
            length, note = r.length_s, 'This tune ends by itself after %.0f s.' % r.length_s
        else:
            length = float(min(opts.chip_seconds, len(pcm) / _SID_RATE))
            note = 'No end was found, so %d seconds were taken.' % round(length)
    out = pcm[:int(length * _SID_RATE)]
    fade = min(len(out), int(0.05 * _SID_RATE))          # a cut at a loop point must not click
    if fade:
        out = out.copy()
        out[-fade:] = (out[-fade:].astype(np.float32) * np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]).astype(np.int16)
    wav = tmp / 'sid.wav'
    with wave.open(str(wav), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(_SID_RATE)
        w.writeframes(out.tobytes())
    return str(wav), len(out) / _SID_RATE, note


# A Furnace file is rendered in a program of its own. The Furnace engine is one shared object in the player's process,
# and rendering a second song in it would stop the song the player is streaming (the library refuses, so the conversion
# would fail). In its own process the engine is private, and it can pick a song other than the first.
_FUR_CHILD = r'''
import ctypes, os, sys, wave
import numpy as np
lib = ctypes.CDLL(sys.argv[1])
path, out, loops, song = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
class Info(ctypes.Structure):
    _fields_ = [("title", ctypes.c_char * 256), ("author", ctypes.c_char * 256), ("system", ctypes.c_char * 256),
                ("channel_count", ctypes.c_int), ("order_len", ctypes.c_int), ("pattern_len", ctypes.c_int),
                ("hz", ctypes.c_double), ("duration_us", ctypes.c_int64), ("subsong_count", ctypes.c_int)]
if out == "-":                          # only say how many songs the file holds
    lib.fw_read_meta_path.argtypes = [ctypes.c_char_p, ctypes.POINTER(Info), ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lib.fw_read_meta_path.restype = ctypes.c_int
    info = Info()
    rc = lib.fw_read_meta_path(path.encode(), ctypes.byref(info), ctypes.create_string_buffer(4096), 32, 128)
    print(info.subsong_count if rc == 0 else 0, flush=True)
    os._exit(0)
if song == 0:                           # the render the player's own stream is made by
    lib.fw_render_wav_path.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.fw_render_wav_path.restype = ctypes.c_int
    rc = lib.fw_render_wav_path(path.encode(), out.encode(), loops, ctypes.byref(ctypes.c_int(0)))
    os._exit(0 if rc == 0 else 2)      # the library hangs when the interpreter shuts down, so leave at once
vp, ip, fp = ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_float)
lib.fw_create.restype = vp
for name, args, res in (("fw_load", [vp, ctypes.c_char_p, ctypes.POINTER(Info)], ip), ("fw_set_sample_rate", [vp, ip], None),
                        ("fw_sample_rate", [vp], ip), ("fw_select_subsong", [vp, ip, ctypes.POINTER(Info)], ip),
                        ("fw_play", [vp, ip], None), ("fw_is_playing", [vp], ip), ("fw_render", [vp, fp, ip], ip)):
    f = getattr(lib, name)
    f.argtypes, f.restype = args, res
h = lib.fw_create()
lib.fw_set_sample_rate(h, 44100)
info = Info()
if lib.fw_load(h, path.encode(), ctypes.byref(info)) != 0:
    os._exit(2)
if lib.fw_select_subsong(h, song, ctypes.byref(info)) != 0:
    os._exit(3)
rate = lib.fw_sample_rate(h) or 44100
lib.fw_play(h, loops)
buf = (ctypes.c_float * 4096)()
chunks = []
while lib.fw_is_playing(h):
    n = lib.fw_render(h, buf, 2048)
    if n <= 0:
        break
    chunks.append(np.ctypeslib.as_array(buf)[:n * 2].copy())
if not chunks:
    os._exit(4)
x = np.concatenate(chunks).reshape(-1, 2).astype(np.float64)
try:                                    # this engine has its own high pass switched off: take the DC out as the others do
    from scipy.signal import lfilter
    x = lfilter([1.0, -1.0], [1.0, -0.999], x, axis=0)
except ImportError:
    pass
pcm = np.clip(x * 32767.0, -32768, 32767).astype('<i2')
with wave.open(out, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(pcm.tobytes())
os._exit(0)
'''


def _fur_child_ok() -> bool:
    """Whether a Furnace file can be rendered in a program of its own: the interpreter can run a script, and the
    library is known."""
    import fur_player
    return not getattr(sys, 'frozen', False) and fur_player._lib is not None and bool(sys.executable)


def _fur_song_count(path: Path) -> int:
    """How many songs a Furnace file can give. The library answers from its cache or its shared engine, and while the
    player streams a Furnace file it cannot, so then a program of its own asks."""
    import fur_player
    if not _fur_child_ok():                 # only the first song can be rendered then, so only one is offered
        return 1
    n = int(fur_player.fur_tags(path).get('subsong_count') or 0)
    if n:
        return n
    try:
        r = subprocess.run([sys.executable, '-c', _FUR_CHILD, str(fur_player._lib._name), str(path), '-', '1', '0'],
                           capture_output=True, timeout=60, stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                           **plat_proc.popen_kwargs(group=False))
        return max(int(r.stdout.split()[-1]), 1) if r.returncode == 0 and r.stdout.split() else 1
    except (OSError, subprocess.SubprocessError, ValueError):
        return 1


def _render_fur_child(path: Path, opts: Options, cancel: threading.Event, tmp: Path, own):
    import fur_player
    out = tmp / 'fur.wav'
    args = [sys.executable, '-c', _FUR_CHILD, str(fur_player._lib._name), str(path), str(out), str(max(opts.chip_loops, 1)),
            str(_song_number(opts) - 1)]
    err = tempfile.TemporaryFile()
    p = subprocess.Popen(plat_proc.low_priority(args), stdout=subprocess.DEVNULL,
                         stderr=err, stdin=subprocess.DEVNULL, **plat_proc.popen_kwargs(low=True))
    plat_proc.contain(p)
    own(p)
    end = time.monotonic() + 900            # a render takes seconds, so a program that is still there after this is stuck
    try:
        while p.poll() is None and time.monotonic() < end:
            if cancel.wait(0.1):
                raise Cancelled
    finally:
        if p.poll() is None:
            plat_proc.kill_tree(p)
            p.wait()
        own(None)
    return (str(out), _wav_seconds(out)) if p.returncode == 0 and out.exists() else (None, None)


def _render_fur_here(path: Path, opts: Options, cancel: threading.Event, tmp: Path):
    """The first song through the library's own render, in this process, for a build that cannot start a script.
    The library refuses to render while the player streams a Furnace file, so this waits until it stops."""
    import fur_player as fp
    from native_lock import RENDER_LOCK
    out, flag = tmp / 'fur.wav', ctypes.c_int(0)
    stop = threading.Event()

    def watch():
        while not stop.wait(0.1):
            if cancel.is_set():
                flag.value = 1
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        while True:
            with RENDER_LOCK, fp._global_engine_lock:
                rc = fp._lib.fw_render_wav_path(os.fsencode(path), os.fsencode(out), ctypes.c_int(max(opts.chip_loops, 1)),
                                                ctypes.byref(flag))
            if rc == 0 or cancel.is_set() or not fp._stream_busy:
                break
            if cancel.wait(0.5):                    # the player is streaming a Furnace file: try again when it stops
                break
    finally:
        stop.set()
    if cancel.is_set():
        raise Cancelled
    return (str(out), _wav_seconds(out)) if rc == 0 and out.exists() else (None, None)


def _render_fur(path: Path, opts: Options, cancel: threading.Event, tmp: Path, own=None):
    if _fur_child_ok():
        return _render_fur_child(path, opts, cancel, tmp, own or (lambda p: None))
    if _song_number(opts) != 1:
        raise ConvertError("Only the first song of a Furnace file can be converted in this build.")
    return _render_fur_here(path, opts, cancel, tmp)


def _render_openmpt(path: Path, opts: Options, cancel: threading.Event, tmp: Path):
    """One song of a tracker module that has several, rendered by openmpt123 into tmp."""
    exe = plat_proc.which('openmpt123')
    if not exe:
        return None, None
    link = tmp / ('module' + path.suffix.lower())
    try:
        link.symlink_to(path.resolve())
    except OSError:
        shutil.copy2(path, link)
    p = subprocess.Popen([exe, '--render', '--output-type', 'wav', '--subsong', str(_song_number(opts) - 1), str(link)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         **plat_proc.popen_kwargs())
    plat_proc.contain(p)
    try:
        while p.poll() is None:
            if cancel.wait(0.1):
                raise Cancelled
    finally:
        if p.poll() is None:
            plat_proc.kill_tree(p)
            p.wait()
    wav = Path(str(link) + '.wav')
    return (str(wav), _wav_seconds(wav) or None) if p.returncode == 0 and wav.exists() else (None, None)


def _plain(fn, ext: str):
    """A renderer of the devices kind, (path, cancel) to a WAV, as one that also says how long it is. A song other than
    the first comes from libgme where it reads the format (AY) and from openmpt123 for tracker modules."""
    def run(path, opts, cancel, tmp):
        song = _song_number(opts)
        if song > 1 and ext in _GME_EXTS and _gme() is not None:
            return _render_gme(path, opts, cancel, tmp)
        if ext in _OPENMPT_EXTS and (song > 1 or len(chip_songs(path)) > 1):   # openmpt123 would play every subsong in turn
            return _render_openmpt(path, opts, cancel, tmp)
        wav = fn(path, cancel)
        return (wav, _wav_seconds(wav) or None) if wav else (None, None)
    return run


def default_renderers() -> dict:
    """Extension to a function (path, options, cancel, temp folder) giving (WAV path, seconds or None) or, with a
    third item, a sentence about how the length was found. Options.subsong is the song number from 1. Only what can be
    rendered here is listed. zxtune and openmpt win over libgme where both take a format, as in the player."""
    r = {}
    if _gme() is not None:
        r.update({e: _render_gme for e in _GME_EXTS})
    for e, fn in af.base_renderers().items():
        r[e] = _render_sid if e in sid_player.EXTS else _plain(fn, e)
    try:
        import fur_player
        if fur_player._lib is not None:
            r['.fur'] = _render_fur
    except Exception:
        pass
    return r


# the ffmpeg command

def replaygain_db(pr: dict) -> float | None:
    """The gain in dB to apply from a file's ReplayGain track tags, held back so that the peak stays under full
    scale. None when the file has none."""
    tg = pr.get('tags') or {}
    m = re.search(r'[+-]?\d+(?:[.,]\d+)?', tg.get('replaygain_track_gain') or '')
    if not m:
        return None
    gain = float(m.group().replace(',', '.'))
    p = re.search(r'\d+(?:[.,]\d+)?', tg.get('replaygain_track_peak') or '')
    if p and float(p.group().replace(',', '.')) > 0:
        gain = min(gain, -20 * math.log10(float(p.group().replace(',', '.'))))
    return gain


_RG_KEYS = ('replaygain_track_gain', 'replaygain_track_peak', 'replaygain_album_gain', 'replaygain_album_peak',
            'replaygain_track_range', 'replaygain_album_range', 'replaygain_reference_loudness')
# keys a container fills with facts about itself, which turn into tags when they are copied to another one
_JUNK = re.compile(r'^(major_brand|minor_version|compatible_brands|handler_name|vendor_id|duration(-\w+)?|bps(-\w+)?|'
                   r'number_of_\w+(-\w+)?|_statistics_\w+(-\w+)?)$')
_LYRICS_ALIASES = ('unsyncedlyrics', 'uslt')
_SILENCE = 'silencedetect=noise=-60dB:d=0.05'
_KEEP_QUIET = 0.05             # seconds of the silence that stay at each end, as a trimmed file always had
_FIRST = ['-nostdin', '-hide_banner', '-loglevel', 'error', '-y']


def silence_span(text: str, duration: float | None) -> tuple[float, float | None] | None:
    """(where the sound starts, where it ends) in seconds, from what ffmpeg's silencedetect printed over the whole
    file. The end is None when the file does not end in silence. None when the file is silent all through."""
    periods: list = []
    for kind, v in re.findall(r'silence_(start|end): (-?[\d.]+(?:e[+-]?\d+)?)', text):
        if kind == 'start':
            periods.append([float(v), None])
        elif periods:
            periods[-1][1] = float(v)
    if not periods:
        return 0.0, None
    total = duration or next((p[1] for p in reversed(periods) if p[1] is not None), None)
    reaches_end = lambda p: p[1] is None or (total is not None and p[1] >= total - 0.06)
    lead = 0.0
    if periods[0][0] <= 0.02:
        if reaches_end(periods[0]):
            return None
        lead = periods.pop(0)[1]
    end = None
    if periods and reaches_end(periods[-1]):
        end = periods[-1][0] + _KEEP_QUIET
        if total is not None:
            end = min(end, total)
    return max(lead - _KEEP_QUIET, 0.0), end


def loudness_values(text: str) -> dict | None:
    """What a loudnorm pass printed about the file, as the values the second pass takes. None when it has no loudness
    (silence) or the text has none."""
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', text)
    if not m:
        return None
    try:
        d = json.loads(m.group())
        got = {k: float(d[key]) for k, key in (('i', 'input_i'), ('lra', 'input_lra'), ('tp', 'input_tp'), ('thresh', 'input_thresh'),
                                                 ('offset', 'target_offset'))}
    except (ValueError, KeyError, TypeError):
        return None
    return {k: '%.2f' % v for k, v in got.items()} if all(math.isfinite(v) for v in got.values()) else None


def encode_args(exe: str, source: str, out: str, fmt: Format, q: Quality, opts: Options, *, rate: int = 0,
                channels: int = 0, cover: str | None = None, meta: dict | None = None, drop: tuple = (),
                gain_db: float | None = None, start: float = 0.0, limit: float | None = None, fade: float = 0.0,
                piped: bool = False, original: str | None = None, pcm: int = 0, pipe_rate: int = 44100,
                loud: dict | None = None) -> list[str]:
    """The ffmpeg command that encodes `source` (or, piped, raw float audio on stdin at pipe_rate whose tags come from
    `original`) into `out`. `start` and `limit` cut the input to that many seconds from there, `meta` is tags to write
    that the source does not carry and `drop` tags to leave out. `loud` is what loudness() measured, so that loudnorm
    can apply one gain. `pcm` (16 or 24) makes it write WAV to standard output instead, for an encoder that is a program
    of its own. An argument list, so a name that begins with a dash is only ever a value."""
    a = [exe] + _FIRST
    if piped:
        a += ['-f', 'f32le', '-ar', str(pipe_rate), '-ac', '2', '-i', 'pipe:0', '-i', 'file:' + os.path.abspath(original)]
        meta_in, n = 1, 2
    else:
        if start:
            a += ['-ss', '%.3f' % start]
        if limit:
            a += ['-t', '%.3f' % limit]
        a += ['-i', 'file:' + os.path.abspath(source)]
        meta_in, n = 0, 1
    if cover:                                   # every input comes before the first output option
        a += ['-i', cover]
    a += ['-map', '0:a:0']
    if cover:
        a += ['-map', '%d:v:0' % n, '-c:v', 'copy', '-disposition:v:0', 'attached_pic']
        if fmt.id == 'mp3':
            a += ['-metadata:s:v', 'title=Album cover', '-metadata:s:v', 'comment=Cover (front)']
    a += ['-sn', '-dn', '-map_chapters', '-1']
    tagged = opts.tags and not pcm and fmt.tags_by != 'ape'    # APE tags are written afterwards, under the usual names
    a += ['-map_metadata', str(meta_in), '-map_metadata:g', '%d:s:a:0' % meta_in] if tagged else ['-map_metadata', '-1']
    if tagged:
        for k, v in (meta or {}).items():
            if v:
                a += ['-metadata', '%s=%s' % (k, v)]
        for k in drop + (_RG_KEYS if opts.normalise != 'off' else ()):    # a changed level must not be changed again
            a += ['-metadata', k + '=']
    chain = []
    if opts.normalise == 'replaygain' and gain_db is not None:
        chain.append('volume=%.2fdB' % gain_db)
    elif opts.normalise == 'loudnorm':
        chain.append('loudnorm=I=-16:TP=-1.5:LRA=11' + (
            ':measured_I=%(i)s:measured_LRA=%(lra)s:measured_TP=%(tp)s:measured_thresh=%(thresh)s:offset=%(offset)s:linear=true' % loud
            if loud else ''))
        if rate:        # it works at 192 kHz and, on a short file, hands over everything in one frame that no encoder takes
            chain += ['aresample=%d' % rate, 'asetnsamples=n=4096:p=0']
    if fade > 0 and limit:
        fade = min(fade, limit)
        chain.append('afade=t=out:st=%.3f:d=%.3f' % (limit - fade, fade))
    if q.bits == 16 or pcm == 16:
        chain.append('aresample=out_sample_fmt=s16:dither_method=triangular')
    if chain:
        a += ['-af', ','.join(chain)]
    if rate:
        a += ['-ar', str(rate)]
    if channels:
        a += ['-ac', str(channels)]
    if pcm:
        return a + ['-c:a', 'pcm_s%dle' % pcm, '-f', 'wav', 'pipe:1']
    a += ['-c:a', fmt.encoder] if not any(x == '-c:a' for x in q.args) else []
    a += list(q.args) + list(fmt.flags) + ['-f', fmt.muxer, '-progress', 'pipe:1', '-nostats', out]
    return a


def external_args(fmt: Format, q: Quality, out: str) -> list[str]:
    """The command of a format that a program of its own encodes: WAV in on standard input, the file out."""
    return [shutil.which(fmt.external) or fmt.external, '-', out, *q.args, '-readfullinput']


def decode_args(exe: str, source: str, start: float = 0.0, limit=None, rate: int = 44100) -> list[str]:
    """The first half of a run through the player's sound chain: decode to stereo float on stdout at `rate`."""
    a = [exe] + _FIRST[:-1]
    if start:
        a += ['-ss', '%.3f' % start]
    if limit:
        a += ['-t', '%.3f' % limit]
    a += ['-i', 'file:' + os.path.abspath(source), '-map', '0:a:0', '-vn', '-sn', '-dn']
    return a + ['-ac', '2', '-ar', str(rate), '-f', 'f32le', 'pipe:1']


def _run_pair(dec: list, enc: list, cancel: threading.Event, on_progress=None, own=None, timeout: float | None = None):
    """ffmpeg decodes to WAV on a pipe and a program of its own encodes it. Returns (exit code, the end of what the
    one that failed said). on_progress(seconds) hears how much audio has gone through. `own` is told of each process,
    so a canceller can kill them. Raises Cancelled when cancel is set."""
    derr, eerr = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    pd = subprocess.Popen(plat_proc.low_priority(dec), stdout=subprocess.PIPE, stderr=derr, stdin=subprocess.DEVNULL,
                          **plat_proc.popen_kwargs(low=True))
    plat_proc.contain(pd)
    pe = subprocess.Popen(plat_proc.low_priority(enc), stdin=subprocess.PIPE, stdout=eerr, stderr=subprocess.STDOUT,
                          **plat_proc.popen_kwargs(low=True))
    plat_proc.contain(pe)
    if own:
        own(pd)
        own(pe)
    end = time.monotonic() + timeout if timeout else None
    bad = 0
    try:
        head = pd.stdout.read(44)                 # the WAV header says how many bytes make a second
        byterate = struct.unpack('<I', head[28:32])[0] if len(head) == 44 and head[:4] == b'RIFF' else 0
        done = 0
        try:
            pe.stdin.write(head)
            while head:
                if cancel.is_set():
                    raise Cancelled
                if end is not None and time.monotonic() > end:
                    bad = -1
                    break
                head = pd.stdout.read(65536)
                if head:
                    pe.stdin.write(head)
                    done += len(head)
                    if on_progress and byterate:
                        on_progress(done / byterate)
        except BrokenPipeError:
            pass
        try:
            pe.stdin.close()
        except OSError:
            pass
        left = lambda: None if end is None else max(end - time.monotonic(), 0.1)
        try:
            rc_e = pe.wait(timeout=left()) if bad == 0 else bad
            rc_d = pd.wait(timeout=left()) if bad == 0 else bad
        except subprocess.TimeoutExpired:
            rc_e = rc_d = -1
    finally:
        for p in (pd, pe):
            if p.poll() is None:
                plat_proc.kill_tree(p)
                p.wait()
        for f in (pd.stdout, pe.stdin):
            try:
                f.close()
            except (OSError, ValueError):
                pass
        if own:
            own(None)
    if cancel.is_set():
        raise Cancelled
    which = eerr if rc_e else derr
    which.seek(0)
    return (rc_e or rc_d), which.read()[-300:].decode('utf-8', 'replace').strip()


# tags that mutagen writes after the encode (WAV, WavPack, Monkey's Audio)

_ID3_TEXT = {'title': 'TIT2', 'artist': 'TPE1', 'album': 'TALB', 'album_artist': 'TPE2', 'albumartist': 'TPE2',
             'composer': 'TCOM', 'genre': 'TCON', 'copyright': 'TCOP', 'track': 'TRCK', 'disc': 'TPOS',
             'date': 'TDRC', 'year': 'TDRC'}
_APE_NAMES = {'title': 'Title', 'artist': 'Artist', 'album': 'Album', 'album_artist': 'Album Artist',
              'albumartist': 'Album Artist', 'composer': 'Composer', 'genre': 'Genre', 'copyright': 'Copyright',
              'track': 'Track', 'disc': 'Disc', 'date': 'Year', 'year': 'Year', 'comment': 'Comment', 'lyrics': 'Lyrics'}
_SKIP_TAGS = frozenset({'encoder', 'encoded_by', 'language'})


def final_tags(pr: dict | None, meta: dict, drop: tuple, opts: Options) -> dict:
    """The text tags a result should carry, lower case keys: what the source has, less what was dropped (container facts,
    ReplayGain after a level change), plus the lyrics and the chip tune facts in `meta`."""
    out = {k: v for k, v in ((pr or {}).get('tags') or {}).items()
           if v and k not in drop and k not in _SKIP_TAGS and not (opts.normalise != 'off' and k in _RG_KEYS)}
    out.update({k: str(v) for k, v in meta.items() if v})
    for alias, key in (('tracknumber', 'track'), ('discnumber', 'disc'), ('albumartist', 'album_artist'), ('year', 'date')):
        if alias in out:
            out.setdefault(key, out.pop(alias))
    return out


def write_id3(path: Path, tags: dict, cover: bytes | None):
    """Put the tags and the cover in an ID3v2.3 block inside a WAV file (the ffmpeg here cannot)."""
    import mutagen
    from mutagen.id3 import APIC, COMM, TXXX, USLT, Frames
    f = mutagen.File(str(path))
    if f is None:
        raise ValueError('not an audio file')
    if f.tags is None:
        f.add_tags()
    t = f.tags
    for k, v in tags.items():
        frame = _ID3_TEXT.get(k)
        if frame and frame in Frames:
            t.add(Frames[frame](encoding=3, text=[v]))
        elif k == 'comment':
            t.add(COMM(encoding=3, lang='eng', desc='', text=v))
        elif k == 'lyrics':
            t.add(USLT(encoding=3, lang='eng', desc='', text=v))
        else:
            t.add(TXXX(encoding=3, desc=k, text=[v]))
    if cover:
        t.add(APIC(encoding=3, mime='image/jpeg' if cover[:3] == b'\xff\xd8\xff' else 'image/png', type=3, desc='Cover',
                   data=cover))
    f.save(v2_version=3)


def write_ape(path: Path, tags: dict, cover: bytes | None, text: bool = True):
    """Put the tags and the cover in an APEv2 block at the end of a WavPack or Monkey's Audio file."""
    from mutagen.apev2 import APEv2File, APEValue, BINARY
    f = APEv2File(str(path))
    if f.tags is None:
        f.add_tags()
    t = f.tags
    if text:
        for k in list(t.keys()):                # what ffmpeg wrote under its own key names gives way to the usual ones
            if k.lower() not in ('cover art (front)',):
                del t[k]
        for k, v in tags.items():
            t[_APE_NAMES.get(k, k)] = v
    if cover:
        ext = b'jpg' if cover[:3] == b'\xff\xd8\xff' else b'png'
        t['Cover Art (Front)'] = APEValue(b'cover.' + ext + b'\x00' + cover, BINARY)
    f.save()


def head_audio(path, rate: int, seconds: float = 60.0):
    """The first `seconds` of a file as stereo float32 at `rate`, shape (n, 2), empty when it cannot be decoded. A
    program that measures a whole track from its start asks for this."""
    import numpy as np
    exe = ffmpeg_path()
    if exe:
        try:
            r = subprocess.run([exe] + _FIRST[:-1] + ['-t', '%.3f' % seconds, '-i', 'file:' + os.path.abspath(path), '-map',
                                                      '0:a:0', '-vn', '-sn', '-dn', '-ac', '2', '-ar', str(rate), '-f', 'f32le',
                                                      'pipe:1'], capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
                               **plat_proc.popen_kwargs(group=False))
            raw = r.stdout[:len(r.stdout) // 8 * 8]
            if r.returncode == 0 and raw:
                return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()
        except (OSError, subprocess.SubprocessError):
            pass
    return np.zeros((0, 2), np.float32)


def tag_plan(pr: dict | None) -> tuple[dict, tuple]:
    """(tags to write, tags to drop) so that what a source's container says about itself is not copied and
    lyrics always sit under the key 'lyrics', which every muxer here knows."""
    tg = (pr or {}).get('tags') or {}
    drop = [k for k, v in tg.items() if _JUNK.match(k) or (k == 'language' and v == 'und')]
    keys = [k for k in tg if k.startswith('lyrics-') or k in _LYRICS_ALIASES]
    text = next((tg[k] for k in ['lyrics'] + sorted(keys) if (tg.get(k) or '').strip()), '')
    return ({'lyrics': text[:100000]} if text else {}), tuple(drop + keys)


def fix_id3(path: Path):
    """ffmpeg writes a comment and lyrics into ID3 as plain TXXX frames, which players do not show. Make them
    COMM and USLT frames."""
    import mutagen
    from mutagen.id3 import COMM, USLT
    f = mutagen.File(str(path))
    t = getattr(f, 'tags', None)
    if t is None or not hasattr(t, 'getall'):
        return
    changed = False
    for fr in list(t.getall('TXXX')):
        d, text = fr.desc.lower(), '\n'.join(fr.text)
        if d == 'comment':
            if not t.getall('COMM'):
                t.add(COMM(encoding=3, lang='eng', desc='', text=text))
        elif d == 'uslt' or d.startswith('lyrics'):
            lang = d[7:10] if d.startswith('lyrics-') and len(d) >= 10 else 'eng'
            t.add(USLT(encoding=3, lang=lang, desc='', text=text))
        else:
            continue
        t.delall('TXXX:' + fr.desc)
        changed = True
    if changed:
        f.save(v2_version=3)


def explain(rc: int, tail: str) -> str:
    """One plain sentence for a failed ffmpeg run."""
    t = tail.lower()
    if rc == -getattr(signal, 'SIGXFSZ', 0) != 0 or 'no space left' in t or 'file too large' in t:     # the signal is a file size limit
        return "There is not enough room at the destination."
    if 'permission denied' in t or 'read-only file system' in t:
        return "The destination folder cannot be written to."
    if any(k in t for k in ('invalid data', 'moov atom', 'does not contain any stream', 'could not find codec',
                            'end of file', 'error while decoding', 'no such file')):
        return "This file could not be read. It may be damaged."
    return "This file could not be converted."


def os_message(e: OSError) -> str:
    return {errno.ENOSPC: "There is not enough room at the destination.",
            errno.EFBIG: "There is not enough room at the destination.",
            errno.EACCES: "The destination folder cannot be written to.",
            errno.EPERM: "The destination folder cannot be written to.",
            errno.EROFS: "The destination is read only.",
            errno.ENAMETOOLONG: "The file name is too long for the destination.",
            errno.EIO: "The drive stopped answering. Was it unplugged?",
            errno.ENODEV: "The drive stopped answering. Was it unplugged?"}.get(
        e.errno, "The file could not be saved: %s." % (e.strerror or e))


# the player's sound chain
# One function is the seam. The window sets it to a factory that takes (path, rate) and returns an object with
# process(block), block being a float32 array of shape (n, 2), or None for no processing. path is the file that is
# decoded (for a chip file the rendered WAV) and rate is stage_rate() of the source's rate. With a factory set, the
# converter decodes to stereo float at that rate, runs the blocks through it and pipes them into the encoder. An
# object may also have finish(), which gives back what it still held at the end (echoes, look-ahead) as one block.
_dsp_factory = None
STAGE_RATES = (44100, 48000, 88200, 96000)


def stage_rate(rate: int) -> int:
    """The rate the player's chain runs at for a source at `rate`: the same rate up to 96 kHz, so the file is not
    resampled at all. Rates below 44.1 kHz and above 96 kHz are brought to the nearest of the standard ones once, keeping
    to the 44.1 or the 48 kHz family where they belong to one."""
    if rate in STAGE_RATES:
        return rate
    fam44 = rate % 11025 == 0
    if rate > 96000:
        return 88200 if fam44 else 96000
    if rate < 44100:
        return 44100 if fam44 else 48000
    return min(STAGE_RATES, key=lambda r: abs(math.log(r / rate)))


def set_dsp_factory(fn):
    global _dsp_factory
    _dsp_factory = fn


def dsp_available() -> bool:
    return _dsp_factory is not None


# jobs

WAITING, RENDERING, ENCODING, DONE, SKIPPED, FAILED, CANCELLED = ('waiting', 'rendering', 'encoding', 'done',
                                                                  'skipped', 'failed', 'cancelled')
FINAL = frozenset({DONE, SKIPPED, FAILED, CANCELLED})
TMP_AGE = 6 * 3600             # an older convert folder in the cache was left by a crash


class ConvertError(Exception):
    """The message is a plain sentence for the person using the player."""


class Skip(Exception):
    pass


class ConvertJob:
    """One source file and what happens to it. The converter fills in the rest while it runs."""
    _ids = 0

    def __init__(self, src, opts: Options):
        ConvertJob._ids += 1
        self.id = ConvertJob._ids
        self.src = Path(src)
        self.opts = opts
        self.state, self.message, self.detail = WAITING, '', ''
        self.percent = 0
        self.dest: Path | None = None
        self.fat = True                 # whether the name was made FAT safe
        self.bytes_in = self.bytes_out = 0
        self.outputs: list[Path] = []     # every file made, more than one for a file that holds several songs
        self.songs = 0
        self.started = self.ended = 0.0
        self.cancel_event = threading.Event()
        self.procs: list = []
        self._last = 0.0

    @property
    def name(self) -> str:
        return printable(self.src.name)

    @property
    def finished(self) -> bool:
        return self.state in FINAL


class Converter:
    """A bounded pool of conversions. start(jobs) returns at once. on_update(job) is called from a worker thread
    whenever a job's state, message or percent changes (progress at most ten times a second per job) and on_done(stats)
    once, from the last worker, when everything has finished or been cancelled."""

    def __init__(self, on_update=None, on_done=None, workers: int | None = None, renderers: dict | None = None,
                 dsp_factory=None):
        self.on_update, self.on_done = on_update, on_done
        self.workers = max(1, workers or min(4, max(1, (os.cpu_count() or 2) // 2)))
        self._renderers = renderers
        self._dsp = dsp_factory
        self._lock = threading.Lock()
        self._jobs: list[ConvertJob] = []
        self._taken: dict = {}
        self._src_ids: set = set()
        self._fat: dict = {}
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._alive = 0
        self._stop = threading.Event()
        self._t0 = self._t1 = 0.0
        self._finished = threading.Event()
        self._finished.set()

    @property
    def renderers(self) -> dict:
        """Built on first use, since it loads the chip players."""
        if self._renderers is None:
            self._renderers = default_renderers()
        return self._renderers

    @renderers.setter
    def renderers(self, value: dict):
        self._renderers = value

    # control

    @property
    def running(self) -> bool:
        return not self._finished.is_set()

    def start(self, jobs: list[ConvertJob]):
        if self.running:
            raise RuntimeError('a conversion is already running')
        self._jobs = list(jobs)
        self._taken, self._fat = {}, {}
        self._src_ids = {i for i in map(_ident, (j.src for j in self._jobs)) if i}
        self._stop.clear()
        self._queue = queue.SimpleQueue()
        for j in self._jobs:
            self._queue.put(j)
        n = min(self.workers, len(self._jobs))
        self._t0, self._t1 = time.monotonic(), 0.0
        if not n:
            self._t1 = self._t0
            self._finish_run()
            return
        self._finished.clear()
        self._alive = n
        sweep_tmp()
        self._threads = [threading.Thread(target=self._worker, daemon=True, name='convert-%d' % i) for i in range(n)]
        for t in self._threads:
            t.start()

    def cancel(self, job: ConvertJob | None = None):
        """Cancel one job, or all of them. A running ffmpeg is killed with everything it started."""
        idle = []
        with self._lock:
            todo = [job] if job is not None else list(self._jobs)
            if job is None:
                self._stop.set()
            for j in todo:
                if not j.finished:
                    j.cancel_event.set()
                    if not j.started:               # no worker has it yet
                        j.state, j.message, j.ended = CANCELLED, 'Cancelled', time.monotonic()
                        idle.append(j)
        for j in todo:
            for p in list(j.procs):
                plat_proc.kill_tree(p)
        for j in idle:
            self._set(j, force=True)

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    @property
    def stats(self) -> dict:
        n = {s: 0 for s in (WAITING, RENDERING, ENCODING, DONE, SKIPPED, FAILED, CANCELLED)}
        bin_ = bout = 0
        for j in self._jobs:
            n[j.state] += 1
            if j.state == DONE:
                bin_ += j.bytes_in
                bout += j.bytes_out
        end = self._t1 or time.monotonic()
        return {'total': len(self._jobs), 'done': n[DONE], 'skipped': n[SKIPPED], 'failed': n[FAILED],
                'cancelled': n[CANCELLED], 'waiting': n[WAITING], 'active': n[RENDERING] + n[ENCODING],
                'bytes_in': bin_, 'bytes_out': bout, 'elapsed': max(end - self._t0, 0.0) if self._t0 else 0.0}

    # reporting

    def _set(self, job: ConvertJob, state=None, message=None, percent=None, force=False):
        now = time.monotonic()
        with self._lock:
            if state is not None:
                job.state = state
                if state in FINAL:
                    job.ended = now
                    if state == DONE:
                        job.percent = 100
            if message is not None:
                job.message = message
            if percent is not None:
                job.percent = percent
            if not (force or state is not None or message is not None) and now - job._last < 0.1:
                return
            job._last = now
        if self.on_update:
            try:
                self.on_update(job)
            except Exception:
                log.exception('converter update callback')

    def _finish_run(self):
        self._t1 = time.monotonic()
        self._finished.set()
        if self.on_done:
            try:
                self.on_done(self.stats)
            except Exception:
                log.exception('converter done callback')

    # workers

    def _worker(self):
        try:
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                with self._lock:
                    if job.finished:
                        continue
                    if self._stop.is_set() or job.cancel_event.is_set():
                        job.state, job.message, job.ended = CANCELLED, 'Cancelled', time.monotonic()
                        skip = True
                    else:
                        job.started = time.monotonic()
                        skip = False
                if skip:
                    self._set(job, force=True)
                    continue
                self._run(job)
        finally:
            with self._lock:
                self._alive -= 1
                last = self._alive == 0
            if last:
                self._finish_run()

    def _run(self, job: ConvertJob):
        tmp, part, made = [], [], []
        try:
            self._convert(job, tmp, part, made)
        except Cancelled:
            self._set(job, state=CANCELLED, message='Cancelled', force=True)
        except Skip as e:
            self._set(job, state=SKIPPED, message=str(e), force=True)
        except ConvertError as e:
            self._set(job, state=FAILED, message=str(e), force=True)
        except OSError as e:
            job.detail = repr(e)
            self._set(job, state=FAILED, message=os_message(e), force=True)
        except Exception:
            log.exception('converting %s', job.src)
            self._set(job, state=FAILED, message="Something went wrong while converting this file.", force=True)
        finally:
            for p in part:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            for d in tmp:
                shutil.rmtree(d, ignore_errors=True)
            if job.state != DONE:
                for d in reversed(made):                # folders this job made for a result it did not finish
                    try:
                        d.rmdir()
                    except OSError:
                        pass
            job.procs.clear()

    # one file

    def _convert(self, job: ConvertJob, tmp: list, part: list, made: list):
        opts, src = job.opts, job.src
        fmt = format_by_id(opts.format)
        exe = ffmpeg_path()
        if not exe or not ffprobe_path():
            raise ConvertError("ffmpeg is not installed, so files cannot be converted.")
        if fmt is None:
            raise ConvertError("This computer's ffmpeg cannot write that format.")
        q = quality_of(fmt, opts.quality)
        try:
            job.bytes_in = src.stat().st_size
            if not src.is_file():
                raise OSError(errno.EISDIR, 'not a file')
        except OSError:
            raise ConvertError("This file could not be found.")
        kind = kind_of(src)
        if not kind:
            raise ConvertError("AudioCrate does not know this kind of file.")
        if kind == 'chip':
            titles = chip_songs(src)
            plan = song_plan(src, opts.subsong, titles)
            many = opts.subsong == 'all' and len(titles) > 1
        else:
            plan, many = [(None, '')], False
        job.songs = len(plan)
        skipped, note, last = 0, '', None
        for n, (song, title) in enumerate(plan):
            try:
                note = self._convert_one(job, fmt, q, exe, kind, song, title, many, n, len(plan), tmp, part, made) or note
            except Skip as e:
                skipped += 1
                if not many:
                    raise
                last = e
        if not job.outputs:
            raise last or ConvertError("This file has no song to convert.")
        if many:
            note = '%d songs%s' % (len(job.outputs), ', %d skipped' % skipped if skipped else '')
        self._set(job, state=DONE, message=note or 'Done', force=True)

    def _convert_one(self, job: ConvertJob, fmt: Format, q: Quality, exe: str, kind: str, song, title: str, many: bool,
                     index: int, count: int, tmp: list, part: list, made: list) -> str:
        """One result: a file, or one song of a file that holds several. Returns a sentence for the row, or ''."""
        opts, src = job.opts, job.src
        chip = kind == 'chip'
        pr = None
        if chip:
            tags = chip_tags(src)
            if many:
                tags['title'] = title or 'Song %d' % song
                tags['track'], tags['tracks'] = song, count
        else:
            pr = probe(src)
            if pr is None:
                raise ConvertError("This file could not be read. It may be damaged.")
            if not pr['has_audio']:
                raise ConvertError("This file has no sound to convert.")
            tags = merge_probe_tags(read_tags(src), pr, src)
        named = src.with_name('%s - %02d %s%s' % (src.stem, song, tags['title'], src.suffix)) if many else None
        job.dest = dest = self._plan(job, tags, fmt, named)
        self._writable(dest.parent)
        which = ' song %d of %d' % (index + 1, count) if many else ''

        def new_tmp() -> Path:
            root = tmp_root()
            root.mkdir(parents=True, exist_ok=True)
            d = Path(tempfile.mkdtemp(prefix='convert-', dir=root))
            tmp.append(d)
            return d

        def own(p):
            if p is None:
                job.procs.clear()
            else:
                job.procs.append(p)

        def spread(percent: int) -> int:
            return min(int((index * 100 + percent) / count), 99)

        source, limit, meta, note = src, None, {}, ''
        if chip:
            fn = self.renderers.get(src.suffix.lower())
            if fn is None:
                raise ConvertError("This kind of file cannot be turned into audio here.")
            self._set(job, state=RENDERING, message='Rendering' + which, force=True)
            got = af.run_until_cancel(fn, job.cancel_event, src, replace(opts, subsong=song or 1), job.cancel_event, new_tmp())
            if job.cancel_event.is_set():
                raise Cancelled
            wav, length = got[0], got[1]
            note = got[2] if len(got) > 2 else ''
            if not wav:
                raise ConvertError("This file could not be turned into audio.")
            source = Path(wav)
            pr = probe(source)
            if pr is None or not pr['has_audio']:
                raise ConvertError("This file could not be turned into audio.")
            have = pr['duration'] or _wav_seconds(source)
            limit = min(length or float(opts.chip_seconds), have) if have else (length or float(opts.chip_seconds))
            meta = {k: tags[k] for k in ('title', 'artist', 'album') if tags.get(k)}
            if many:
                meta['track'] = '%d/%d' % (song, count)
            duration = limit
        else:
            duration = pr['duration']
        self._set(job, state=ENCODING, message='Converting' + which, percent=spread(0), force=True)
        start = 0.0
        if opts.trim:
            span = self._silence(job, exe, source, limit, own)
            if span is None:
                note = note or 'This file is silent, so nothing was trimmed.'
            else:
                start, end = span
                total = limit or duration
                stop = total if end is None else end
                if start > 0 or end is not None:
                    if stop is None:
                        limit = None
                    elif stop - start > 0.1:
                        limit = stop - start if (end is not None or chip) else None
                        duration = stop - start
                    else:
                        start = 0.0
        cover = self._cover(job, fmt, new_tmp) if opts.art and fmt.art else None
        gain, loud = None, None
        if opts.normalise == 'replaygain':
            gain = replaygain_db(pr)
            if gain is None:
                note = 'This file has no ReplayGain tags, so its volume was not changed.'
        wanted, drop = tag_plan(pr)
        meta = {**wanted, **meta}
        several = tags.get('artist_values') or []
        if len(several) > 1:        # ffmpeg keeps the first of several values, or joins them with a bare ";"
            meta['artist'] = '; '.join(several)
        processor, stage = None, 44100
        factory = self._dsp or _dsp_factory
        if opts.dsp and factory is not None:
            stage = stage_rate(pr['rate'] if pr and pr['rate'] else 44100)
            processor = factory(source, stage)
        if opts.normalise == 'loudnorm' and processor is None:      # with the sound chain on, the chain changes what is measured
            self._set(job, message='Measuring the loudness' + which)
            loud = self._loudness(job, exe, source, start, limit, own)
            self._set(job, message='Converting' + which)
        rate = pick_rate(fmt, opts.rate or (stage if processor is not None else (pr['rate'] if pr else 0)))
        if opts.normalise == 'loudnorm' and not rate:
            rate = pr['rate'] if pr else 0          # loudnorm works at 192 kHz inside and has to be brought back
        channels = opts.channels or (fmt.max_channels if fmt.max_channels and pr and pr['channels'] > fmt.max_channels
                                     else 0)
        out = dest.parent / ('.%s.part' % dest.name)
        part.append(out)
        pcm = (24 if pr and pr['bits'] > 16 else 16) if fmt.external else 0
        kw = dict(rate=rate, channels=channels, cover=str(cover[0]) if cover and fmt.art_by == 'ffmpeg' else None,
                  meta=meta, drop=drop, gain_db=gain, start=start, limit=limit, fade=opts.chip_fade if chip else 0.0,
                  pcm=pcm, loud=loud)
        seen = [0.0]

        def progress(seconds):
            seen[0] = seconds
            if duration:
                self._set(job, percent=spread(min(int(100 * seconds / duration), 99)))

        if job.cancel_event.is_set():
            raise Cancelled
        made += self._make_folder(dest.parent)
        tail_s = 0.0
        if processor is not None:
            rc, text, tail_s = self._run_piped(job, exe, source, out, fmt, q, opts, kw, processor, progress, own, stage)
        elif fmt.external:
            dec = encode_args(exe, str(source), '', fmt, q, opts, **kw)
            rc, text = _run_pair(dec, external_args(fmt, q, str(out)), job.cancel_event, progress, own)
        else:
            args = plat_proc.low_priority(encode_args(exe, str(source), str(out), fmt, q, opts, **kw))
            rc, text = af.run_ffmpeg(args, job.cancel_event, progress, own)
        if rc != 0:
            job.detail = text
            raise ConvertError(explain(rc, text))
        self._verify(out, duration, pr, seen[0], tail_s)
        if opts.tags and fmt.id in ('mp3', 'aiff'):
            fix_id3(out)
        post = self._finish_tags(out, fmt, opts, pr, meta, drop, cover, several)
        self._place(job, out, dest)
        part.clear()
        job.outputs.append(job.dest)
        job.bytes_out += job.dest.stat().st_size
        return note or post

    def _silence(self, job: ConvertJob, exe: str, source: Path, limit, own) -> tuple | None:
        """(where the sound starts, where it ends or None) from one pass over the file with silencedetect, or None when
        the file is silent all through. Nothing is held in memory, whatever the length."""
        a = [exe, '-nostdin', '-hide_banner', '-loglevel', 'info', '-progress', 'pipe:1', '-nostats']
        if limit:
            a += ['-t', '%.3f' % limit]
        a += ['-i', 'file:' + os.path.abspath(source), '-map', '0:a:0', '-vn', '-sn', '-dn', '-af', _SILENCE, '-f', 'null', '-']
        rc, text = af.run_ffmpeg(plat_proc.low_priority(a), job.cancel_event, None, own, keep=4 << 20)
        if rc != 0:
            job.detail = text[-300:]
            raise ConvertError(explain(rc, text[-300:]))
        total = limit
        if not total:
            m = re.findall(r'Duration: (\d+):(\d+):(\d+\.?\d*)', text)
            total = int(m[-1][0]) * 3600 + int(m[-1][1]) * 60 + float(m[-1][2]) if m else None
        return silence_span(text, total)

    def _loudness(self, job: ConvertJob, exe: str, source: Path, start: float, limit, own) -> dict | None:
        """One measuring pass over the part of the file that will be encoded, for a loudness setting that is a single gain
        and leaves the dynamics alone. None when the file has no loudness to measure."""
        a = [exe, '-nostdin', '-hide_banner', '-loglevel', 'info', '-progress', 'pipe:1', '-nostats']
        if start:
            a += ['-ss', '%.3f' % start]
        if limit:
            a += ['-t', '%.3f' % limit]
        a += ['-i', 'file:' + os.path.abspath(source), '-map', '0:a:0', '-vn', '-sn', '-dn', '-af',
              'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-']
        rc, text = af.run_ffmpeg(plat_proc.low_priority(a), job.cancel_event, None, own, keep=64 << 10)
        if rc != 0:
            job.detail = text[-300:]
            raise ConvertError(explain(rc, text[-300:]))
        return loudness_values(text)

    @staticmethod
    def _finish_tags(out: Path, fmt: Format, opts: Options, pr, meta: dict, drop: tuple, cover, artists=()) -> str:
        """What mutagen adds after the encode: the artists as separate values, the Ogg picture, or the tags and the
        picture of WAV, WavPack and Monkey's Audio. A failure here keeps the file and says what is missing. `artists`
        is the values of the source's artist tag when it had several."""
        pic = cover[1] if cover else None
        try:
            if len(artists) > 1 and opts.tags and fmt.id in ('opus', 'vorbis', 'flac', 'aac', 'alac'):
                put_artists(out, artists)
            if fmt.art_by == 'mutagen' and pic:
                Converter._put_picture(out, pic)
            elif fmt.tags_by in ('id3', 'ape') or fmt.art_by in ('id3', 'ape'):
                tags = final_tags(pr, meta, drop, opts) if opts.tags and fmt.tags else {}
                if len(artists) > 1 and fmt.tags_by == 'ape':
                    tags['artist'] = list(artists)
                if not tags and not pic:
                    return ''
                if fmt.art_by == 'id3' or fmt.tags_by == 'id3':
                    write_id3(out, tags, pic)
                else:
                    write_ape(out, tags, pic, text=opts.tags)
        except Exception:
            log.exception('tags for %s', out)
            return 'Done, but the tags or the cover could not be saved.'
        return ''

    def _plan(self, job: ConvertJob, tags: dict, fmt: Format, named: Path | None = None) -> Path:
        """Where the result goes. Decided under the lock so that two jobs never get one name. `named` stands in for the
        source in the file name (a song of a file that holds several)."""
        opts, src = job.opts, job.src
        base = Path(os.path.abspath(os.path.expanduser(opts.folder))) if opts.folder else Path(os.path.abspath(src)).parent
        if opts.fat is not None:
            fat = opts.fat
        else:
            with self._lock:
                fat = self._fat.get(base)
            if fat is None:
                fat = fat_default(base)
                with self._lock:
                    self._fat[base] = fat
        job.fat = fat
        dest = base.joinpath(*render_name(opts.template, tags, named or src, fmt.ext, fat))
        with self._lock:
            planned = self._taken.setdefault(dest.parent, set())
            clash = _norm(dest.name) in planned
            exists = os.path.lexists(dest)
            if clash and opts.exists == 'skip':
                raise Skip("Another file in the list has the same name.")
            if exists and not clash:
                there = _ident(dest)
                own = plat_fs.same_file(dest, src)
                if opts.exists == 'skip':
                    raise Skip("That is the original file." if own else "A file with this name already exists.")
                if opts.exists == 'overwrite':
                    if own:
                        raise ConvertError("The result would replace the original file. Choose another folder or name.")
                    if there in self._src_ids:
                        raise ConvertError("The result would replace another file in the list. Choose another folder or name.")
                    if os.path.isdir(dest):
                        raise ConvertError("A folder with this name already exists.")
            if clash or (exists and opts.exists == 'number'):
                dest = dest.with_name(unique_name(dest.parent, dest.name, planned, fat))
            planned.add(_norm(dest.name))
        return dest

    @staticmethod
    def _writable(folder: Path):
        """Fail early, before a long render, when nothing can be written there."""
        p = folder
        while not p.exists() and p.parent != p:
            p = p.parent
        if not p.is_dir() or not os.access(p, os.W_OK | os.X_OK):
            raise ConvertError("The destination folder cannot be written to.")

    @staticmethod
    def _make_folder(folder: Path) -> list[Path]:
        """Make the folder and the ones above it that are missing. Returns the ones that were made."""
        missing = []
        p = folder
        while not p.exists() and p.parent != p:
            missing.append(p)
            p = p.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            raise ConvertError("The destination folder cannot be written to.")
        except OSError as e:
            raise ConvertError(os_message(e))
        return missing

    @staticmethod
    def _cover(job: ConvertJob, fmt: Format, new_tmp):
        """(file, bytes) of the cover as JPEG or PNG, or None."""
        try:
            from player import extract_art
            data = extract_art(job.src)
            if not data:
                return None
            ext = '.jpg' if data[:3] == b'\xff\xd8\xff' else '.png' if data[:8] == b'\x89PNG\r\n\x1a\n' else ''
            if not ext:
                from PIL import Image
                buf = io.BytesIO()
                Image.open(io.BytesIO(data)).convert('RGB').save(buf, 'JPEG', quality=92)
                data, ext = buf.getvalue(), '.jpg'
            path = new_tmp() / ('cover' + ext)
            path.write_bytes(data)
            return path, data
        except Exception:
            log.warning('no cover for %s', job.src)
            return None

    @staticmethod
    def _put_picture(path: Path, data: bytes):
        """Ogg Vorbis and Opus keep their cover in a METADATA_BLOCK_PICTURE tag, which ffmpeg does not write."""
        import mutagen
        from mutagen.flac import Picture
        pic = Picture()
        pic.type, pic.data = 3, data
        pic.mime = 'image/jpeg' if data[:3] == b'\xff\xd8\xff' else 'image/png'
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            pic.width, pic.height, pic.depth = im.width, im.height, 24
        except Exception:
            pass
        f = mutagen.File(str(path))
        if f is None:
            return
        f['metadata_block_picture'] = [base64.b64encode(pic.write()).decode('ascii')]
        f.save()

    @staticmethod
    def _verify(out: Path, duration, pr, seen: float, tail: float = 0.0):
        """The result has to be a file with sound in it and about the length of what it should hold. `tail` is the
        extra length a sound chain may add when its echoes ring out after the end."""
        r = probe(out)
        if r is None or not r['has_audio'] or not r['duration']:
            raise ConvertError("The result could not be checked, so it was not kept.")
        d = r['duration']
        if duration:
            tol = max(1.0, 0.01 * duration)
            ok = -tol <= d - duration <= tol + tail
            if not ok and pr and 'mp3' in (pr.get('format') or '') and seen:
                ok = abs(d - seen) <= max(1.0, 0.01 * seen)     # a VBR file without a header has only an estimate
            if not ok:
                raise ConvertError("The result came out the wrong length, so it was not kept.")

    def _place(self, job: ConvertJob, part: Path, dest: Path):
        """Rename the finished temp file to its name. A file that appeared there meanwhile is not replaced unless
        replacing was asked for."""
        if job.opts.exists != 'overwrite' and os.path.lexists(dest):
            with self._lock:
                dest = dest.with_name(unique_name(dest.parent, dest.name, self._taken.get(dest.parent), job.fat))
                job.dest = dest
        plat_fs.replace(part, dest)

    def _run_piped(self, job, exe, source, out, fmt, q, opts, kw, processor, progress, own, stage):
        """Decode at the stage's rate, run the blocks through the player's chain, encode. Processes and a pump in this
        thread. A format that a program of its own encodes gets WAV from ffmpeg on a pipe. Returns (exit code, the end
        of what the failing one said, seconds the chain added after the end)."""
        import numpy as np
        dec = decode_args(exe, str(source), kw['start'], kw['limit'], stage)
        ext = external_args(fmt, q, str(out)) if fmt.external else None
        enc = encode_args(exe, '', '' if ext else str(out), fmt, q, replace(opts, trim=False), piped=True, original=str(source),
                          pipe_rate=stage, **{**kw, 'start': 0.0})
        derr, eerr, merr = tempfile.TemporaryFile(), tempfile.TemporaryFile(), tempfile.TemporaryFile()
        pd = subprocess.Popen(plat_proc.low_priority(dec), stdout=subprocess.PIPE, stderr=derr, stdin=subprocess.DEVNULL,
                              **plat_proc.popen_kwargs(low=True))
        plat_proc.contain(pd)
        pe = subprocess.Popen(plat_proc.low_priority(enc), stdout=subprocess.PIPE, stderr=eerr, stdin=subprocess.PIPE, text=False,
                              **plat_proc.popen_kwargs(low=True))
        plat_proc.contain(pe)
        pm = None
        if ext:
            pm = subprocess.Popen(plat_proc.low_priority(ext), stdin=pe.stdout, stdout=merr, stderr=subprocess.STDOUT,
                                  **plat_proc.popen_kwargs(low=True))
            plat_proc.contain(pm)
            pe.stdout.close()
        own(pd)
        own(pe)
        if pm:
            own(pm)
        reader = None
        if not ext:
            def read_progress():
                for line in pe.stdout:
                    if line.startswith(b'out_time_us='):
                        try:
                            progress(max(int(line[12:]), 0) / 1e6)
                        except ValueError:
                            pass
            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
        block, fed, tail_s = 8192 * 8, 0, 0.0
        try:
            while True:
                if job.cancel_event.is_set():
                    raise Cancelled
                data = pd.stdout.read(block)
                if not data:
                    break
                x = np.frombuffer(data[:len(data) // 8 * 8], dtype=np.float32).reshape(-1, 2).copy()
                y = processor.process(x)
                try:
                    pe.stdin.write(np.ascontiguousarray(y, dtype=np.float32).tobytes())
                except BrokenPipeError:
                    break
                fed += len(x)
                if ext:
                    progress(fed / stage)
            finish = getattr(processor, 'finish', None)
            if finish is not None:                  # what the chain still held: echoes and the look-ahead
                try:
                    rest = np.ascontiguousarray(finish(), dtype=np.float32)
                    if len(rest):
                        pe.stdin.write(rest.tobytes())
                        tail_s = len(rest) / stage
                except BrokenPipeError:
                    pass
            try:
                pe.stdin.close()
            except OSError:
                pass
            rc_m = pm.wait() if pm else 0
            rc_e = pe.wait()
            rc_d = pd.wait()
        finally:
            for p in (pd, pe, pm):
                if p is not None and p.poll() is None:
                    plat_proc.kill_tree(p)
                    p.wait()
            for f in (pd.stdout, pe.stdin):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass
            if reader is not None:
                reader.join(2)
        if job.cancel_event.is_set():
            raise Cancelled
        bad = rc_m or rc_e or rc_d
        which = merr if rc_m else eerr if rc_e else derr
        which.seek(0)
        return bad, which.read()[-300:].decode('utf-8', 'replace').strip(), tail_s


# temp files

_ident = plat_fs.file_id            # what a path leads to, or None: two paths are one file when these match


def tmp_root() -> Path:
    return plat_paths.cache_dir() / 'convert-tmp'


def sweep_tmp():
    """Remove the convert-* folders that a crash left in the cache, the ones older than TMP_AGE."""
    try:
        now = time.time()
        for d in tmp_root().iterdir():
            if d.name.startswith('convert-') and now - d.stat().st_mtime > TMP_AGE:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
