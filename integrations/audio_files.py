"""What the device copier and the converter share: safe file names, the tags of a file, the mount table, one
ffmpeg run with progress and cancel, and the list of music below a folder. There is no Qt in here, so a check can
use it without a display.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path

import cli_render
import sid_player
from artists import join_artists, split_artists

FAT_32_FS = ('vfat', 'msdos', 'fat')         # exFAT has no 4 GB limit
# files that are not recordings: a player or an emulator has to make audio of them first
CHIP_EXTS = ({'.fur', '.vgm', '.vgz', '.nsf', '.nsfe', '.spc', '.gbs', '.ay', '.gym', '.hes', '.kss', '.sap',
              '.sgc'} | cli_render.EXTS | sid_player.EXTS)


class Cancelled(Exception):
    pass


# names that FAT32 and exFAT accept

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_CONTROL = re.compile(r'[/\x00-\x1f\x7f]')       # all a Linux file system refuses
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}


def printable(text: str) -> str:
    """A name Python read with surrogateescape, as text that can be encoded again. Names that are not UTF-8 are
    nearly always Latin-1 or Windows-1252, so they are read as that."""
    if text.isascii():
        return text
    try:
        raw = text.encode('utf-8', 'surrogateescape')
    except UnicodeEncodeError:
        return text.encode('utf-8', 'replace').decode('utf-8')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp1252', 'replace')


def fat_name(name: str, limit: int = 200, strict: bool = True) -> str:
    """A file or folder name FAT accepts: no <>:"/\\|?* or control characters, no trailing dot or space, not a
    device name such as CON, at most `limit` bytes of UTF-8 with the extension kept. strict=False keeps the
    characters a Linux file system allows and only removes the slash and control characters."""
    name = (_BAD if strict else _CONTROL).sub('_', unicodedata.normalize('NFC', printable(name))).strip(' .')
    stem, ext = os.path.splitext(name)
    stem = stem.strip(' .') or '_'
    if stem.split('.')[0].rstrip(' ').upper() in _RESERVED:      # Windows reserves CON.txt and CON.a.b as well
        stem = '_' + stem
    room = max(limit - len(ext.encode()), 1)
    stem = stem.encode()[:room].decode('utf-8', 'ignore').rstrip(' .') or '_'
    return stem + ext


def _norm(name: str) -> str:
    return unicodedata.normalize('NFC', name).casefold()


def unique_name(folder: Path, name: str, taken: set | None = None, strict: bool = True) -> str:
    """name, or 'name (2)' and so on, so that it differs from everything in folder and in taken, ignoring case
    as FAT does. strict is passed on to fat_name."""
    try:
        have = {_norm(n) for n in os.listdir(folder)}
    except OSError:
        have = set()
    have |= {_norm(n) for n in taken or ()}
    stem, ext = os.path.splitext(name)
    out, n = name, 1
    while _norm(out) in have:
        n += 1
        out = fat_name(_numbered(stem, ext, n), strict=strict)
    return out


def _numbered(stem: str, ext: str, n: int, limit: int = 200) -> str:
    """'stem (n).ext' in at most `limit` bytes. The stem gives way, so the number is never cut off."""
    tail = ' (%d)' % n
    room = max(limit - len(ext.encode()) - len(tail.encode()), 1)
    return (stem.encode()[:room].decode('utf-8', 'ignore').rstrip(' .') or '_') + tail + ext


def free_path(dest: Path, taken: dict) -> Path:
    """dest, or dest with a number in its name when that is a file already or is planned for this job."""
    have = taken.setdefault(dest.parent, set())
    dest = dest.with_name(unique_name(dest.parent, dest.name, have))
    have.add(_norm(dest.name))
    return dest


# what is in a file

_FRAMES = {'title': 'TIT2', 'artist': 'TPE1', 'album': 'TALB', 'albumartist': 'TPE2', 'genre': 'TCON',
           'tracknumber': 'TRCK', 'discnumber': 'TPOS', 'date': 'TDRC'}


def blank_tags(path: Path) -> dict:
    return {'title': printable(path.stem), 'artist': '', 'artists': [], 'artist_values': [], 'album': '',
            'albumartist': '', 'genre': '', 'track': 0,
            'tracks': 0, 'disc': 0, 'discs': 0, 'year': 0, 'duration': None, 'bitrate': 0, 'rate': 0, 'channels': 0,
            'bits': 0, 'codec': ''}


def read_tags(path: Path) -> dict:
    """Tags and technical facts of an audio file. Never raises: a file mutagen cannot read gives its name as
    the title."""
    t = blank_tags(path)
    try:
        import mutagen
        f = mutagen.File(str(path), easy=True)
    except Exception:
        return t
    if f is None:
        return t

    def values(key):
        tg = f.tags or {}
        v = tg.get(key) or tg.get(_FRAMES[key])       # WAV and AIFF files hold plain ID3 frames
        v = getattr(v, 'text', v)
        return [x for x in (str(x).strip() for x in v) if x] if v else []

    def first(key):
        return (values(key) or [''])[0]

    t['title'] = first('title') or t['title']
    for k in ('album', 'albumartist', 'genre'):
        t[k] = first(k)
    # artist is every name on one line, artists the names one by one, artist_values what the tag holds
    t['artist_values'] = values('artist')
    t['artists'] = split_artists(t['artist_values'], t['albumartist'])
    t['artist'] = join_artists(t['artists'])
    for key, a, b in (('tracknumber', 'track', 'tracks'), ('discnumber', 'disc', 'discs')):
        m = re.match(r'\s*(\d+)(?:\s*/\s*(\d+))?', first(key))
        if m:
            t[a], t[b] = int(m.group(1)), int(m.group(2) or 0)
    m = re.match(r'\d{4}', first('date'))
    t['year'] = int(m.group()) if m else 0
    i = f.info
    t['duration'] = float(i.length) if getattr(i, 'length', 0) else None
    t['bitrate'] = int(getattr(i, 'bitrate', 0) or 0) // 1000
    t['rate'] = int(getattr(i, 'sample_rate', 0) or 0)
    t['channels'] = int(getattr(i, 'channels', 0) or 0)
    t['bits'] = int(getattr(i, 'bits_per_sample', 0) or 0)
    t['codec'] = str(getattr(i, 'codec', '') or '')
    return t


def put_artists(path: Path, names: list[str]):
    """Give a Vorbis, Opus, FLAC or MP4 file its artists as separate values."""
    import mutagen
    f = mutagen.File(str(path), easy=True)
    f['artist'] = list(names)
    f.save()


def read_mounts(path: str | Path = '/proc/self/mountinfo') -> dict[str, dict]:
    """{device node: {'mount','fs','ro'}} from the mount table. It is read as text and nothing is statted, so a
    stalled network mount cannot hold this up."""
    out: dict = {}
    try:
        lines = Path(path).read_text(errors='surrogateescape').splitlines()
    except OSError:
        return out
    for line in lines:
        a, _, b = line.partition(' - ')
        f, g = a.split(), b.split()
        if len(f) < 6 or len(g) < 2:
            continue
        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), f[4])
        if g[1] not in out or f[3] == '/':
            out[g[1]] = {'mount': mount, 'fs': g[0], 'ro': 'ro' in f[5].split(',')}
    return out


# music below a folder, and the programs that make audio out of chip files

def expand_sources(paths: list[Path], exts) -> list[Path]:
    """Files as given, folders replaced by the files below them, anything whose extension is not in `exts` left
    out."""
    out = []
    for p in paths:
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
                out += [Path(root) / f for f in sorted(files)
                        if Path(f).suffix.lower() in exts and not f.startswith('.')]
        elif p.is_file() and p.suffix.lower() in exts:
            out.append(p)
    return out


def base_renderers() -> dict:
    """File extension to a function (path, cancel event) giving the path of a WAV, for chiptunes that have to
    become audio before they can be converted. Only what can be rendered here is listed."""
    r = {e: (lambda p, c=None: cli_render.render(p)) for e in cli_render.EXTS if cli_render.supported(e)}
    if sid_player.available():
        r.update({e: (lambda p, c=None: sid_player.scan(p)[0] or None) for e in sid_player.EXTS})
    return r


def run_until_cancel(fn, cancel: threading.Event, *args):
    """fn(*args) on a helper thread, so that cancel is heard at once. A cancelled run is left to finish by itself:
    it only fills a cache (the renderers are not ours to stop) and ends at its own timeout."""
    box: list = []

    def run():
        try:
            box.append(('ok', fn(*args)))
        except BaseException as e:
            box.append(('err', e))
    t = threading.Thread(target=run, daemon=True, name='audio-render')
    t.start()
    while t.is_alive():
        if cancel.wait(0.1):
            raise Cancelled
    if box[0][0] == 'err':
        raise box[0][1]
    return box[0][1]


# ffmpeg

def kill_group(p: subprocess.Popen):
    """Kill a child started with start_new_session=True together with everything it started."""
    if p.poll() is not None:                # gone and reaped: its number may belong to someone else now
        return
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass


def run_ffmpeg(args: list, cancel: threading.Event, on_progress=None, on_proc=None, keep: int = 300) -> tuple[int, str]:
    """One ffmpeg run, which has to ask for `-progress pipe:1`. on_progress(seconds) hears how far the output has
    got, on_proc(process) gets the process when it starts and None when it is over, so that a canceller can kill
    it. Returns the exit code and the last `keep` bytes of what ffmpeg said on stderr. Raises Cancelled when cancel is set."""
    err = tempfile.TemporaryFile()
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, text=True,
                         start_new_session=True)
    if on_proc:
        on_proc(p)
    try:
        for line in p.stdout:                   # ffmpeg's -progress output: key=value lines
            if cancel.is_set():
                raise Cancelled
            if on_progress and line.startswith('out_time_us='):
                try:
                    on_progress(max(int(line[12:]), 0) / 1e6)
                except ValueError:
                    continue
        rc = p.wait()
    finally:
        if on_proc:
            on_proc(None)
        if p.poll() is None:
            kill_group(p)
            p.wait()
        p.stdout.close()
    if cancel.is_set():
        raise Cancelled
    err.seek(0)
    return rc, err.read()[-keep:].decode('utf-8', 'replace').strip()
