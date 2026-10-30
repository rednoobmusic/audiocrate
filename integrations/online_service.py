"""Online media: search, links and fetching, through yt-dlp.

yt-dlp runs as a child process in its own process group, never inside this
process, so a slow site cannot hold the GIL against the audio thread. Worker
threads read what it prints and report back with Qt signals. Every signal is
posted to the thread the service lives in, so it never fires inside the call
that started the work, and the request or job id a caller got back is always
known by the time its first signal arrives.

Nothing here asks for a password. Accounts work through the cookies of a browser
the user is already signed in with, or through a cookies file they pick.
"""
from __future__ import annotations

import atexit
import copy
import hashlib
import http.client
import itertools
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QObject, Qt, Signal

from applog import log
import plat_fs
import plat_paths
import plat_proc
from ffmpeg_util import ffmpeg_path
from online_providers import (LINK, PROVIDERS, Provider, is_direct_audio, listing_url,
                              normalize_url, provider_for_url, x_url_kind)

try:
    from _version import VERSION as _APP_VERSION
except Exception:
    _APP_VERSION = "1.0"
_UA = f"AudioCrate/{_APP_VERSION}"

# yt-dlp release files. The base can be pointed elsewhere for tests.
_RELEASES = "https://github.com/yt-dlp/yt-dlp/releases"
_NIGHTLY = "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases"

SIDECAR = ".audiocrate.json"
MIN_FREE_BYTES = 500 * 1024 * 1024
LIST_TIMEOUT = 60          # seconds for a search or a listing
STALL_TIMEOUT = 120        # seconds without output while a download is running
LIST_SLOTS = 2
BATCH_SIZE = 4             # this many unfinished jobs from one site count as a batch
BATCH_GAP = 4.0            # seconds between the starts of jobs in a batch
BROWSERS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "edge", "opera")
_PROFILE = re.compile(r"[A-Za-z0-9 ._-]{0,64}")
# name, lowest version, highest version, how to read it. yt-dlp says deno 2.3, node 22 and bun up to 1.3.14.
_JS_RUNTIMES = (
    ("deno", (2, 3, 0), None, r"^deno (\d+(?:\.\d+)*)"),
    ("node", (22, 0, 0), None, r"^v(\d+(?:\.\d+)*)"),
    ("bun", (1, 2, 11), (1, 3, 14), r"^(\d+(?:\.\d+)*)"),
)

# errors

ERROR_MESSAGES = {
    "no_ytdlp": "yt-dlp is not installed. Install it, or use Update in Settings to get a copy.",
    "no_ffmpeg": "ffmpeg is missing. AudioCrate needs it to play and save audio from links.",
    "network": "Could not reach the site. Check your connection and try again.",
    "login_required": "This needs a signed in session. Sign in through your browser cookies in Settings.",
    "age_restricted": "This video is age restricted. Sign in with a browser that is signed in to YouTube.",
    "geo_blocked": "The site does not offer this where you are.",
    "private": "This is private. Sign in with an account that has access to it.",
    "unavailable": "This is not available any more. It may have been removed or made private.",
    "rate_limited": "The site is limiting requests from this connection. Wait a few minutes and try again.",
    "unsupported_url": "AudioCrate cannot read links like this one.",
    "no_media": "No audio or video was found at this link.",
    "cancelled": "Cancelled.",
    "timeout": "The site took too long to answer. Try again.",
    "disk_full": "There is not enough free disk space.",
    "too_large": "This is bigger than the largest file AudioCrate fetches.",
    "failed": "yt-dlp could not get this one. Updating yt-dlp in Settings often fixes it.",
    "write_failed": "AudioCrate could not write the file. Check the save folder in Settings.",
    "refused": "The site refused to send this file.",
}

# What the UI offers next to the message: sign_in, update or retry.
ERROR_ACTIONS = {
    "no_ytdlp": "update", "no_ffmpeg": "", "network": "retry", "login_required": "sign_in",
    "age_restricted": "sign_in", "geo_blocked": "", "private": "sign_in", "unavailable": "",
    "rate_limited": "retry", "unsupported_url": "", "no_media": "", "cancelled": "",
    "timeout": "retry", "disk_full": "", "too_large": "", "failed": "update", "write_failed": "", "refused": "",
}

_BOT = ("YouTube asks you to prove you are not a bot. Signing in through browser cookies "
        "can help, or try again later.")
_MEMBERS = "This is for channel members. Sign in with an account that has the membership."
_NEWER = "This site needs a newer yt-dlp. Update it in Settings."
_RELOAD = "YouTube refused this stream. Updating yt-dlp in Settings usually fixes it."
_LIVE = "This is a live stream. Live streams cannot be played or saved yet."
_SILENT = "This has no sound. Try saving it as a video."
_LATER = "This has not started yet. Try again later."
_NOT_READY = "This is not ready yet. Try again later."
_COOKIES_BROWSER = "AudioCrate could not read the cookies of that browser. Check the browser and profile in Settings, Accounts."
_COOKIES_FILE = "The saved cookies file could not be read. Choose it again in Settings, Accounts."

# First match wins, tested against the text of the last ERROR line without its "[site] id:" start, its links
# and its quoted paths, since words in those say nothing about the failure. Order matters: a private video
# also says "Sign in", a bot check also says "Sign in", and so on.
_RULES = (
    ("disk_full", r"No space left on device|Errno 28|Disk quota exceeded", None),
    ("write_failed", r"unable to open for writing|unable to write|Read-only file system|File name too long|\[Errno (?:13|30|36)\]", None),
    ("no_ffmpeg", r"ffprobe and ffmpeg not found|ffmpeg(?: and ffprobe)? (?:is )?not found|ffmpeg is not installed", None),
    ("unsupported_url", r"Unsupported URL", None),
    ("login_required", r"cookies database|cookies from browser|firefox container|containers\.json|unsupported browser", _COOKIES_BROWSER),
    ("login_required", r"does not look like a Netscape format cookies file|failed to load cookies", _COOKIES_FILE),
    ("login_required", r"not a bot", _BOT),
    ("age_restricted", r"confirm your age|age.restricted|inappropriate for some users|age verification", None),
    ("private", r"Private video|granted access|protected tweet|not authorized to view|This video is private|(?:this )?playlist is private|private (?:playlist|account)", None),
    ("login_required", r"members.only|Join this channel|channel members", _MEMBERS),
    ("login_required", r"\bSign in\b|Login details are needed|registered users|requires authentication|authentication is required|log ?in to (?:view|continue)|cookies.{0,40}(?:required|needed)|for the authentication", None),
    ("geo_blocked", r"not made this video available in your country|not available in your country|blocked it in your country|geo.?restrict|geographic|in your region|not available in your location", None),
    ("too_large", r"larger than max-filesize|File is larger than", None),
    ("rate_limited", r"HTTP Error 429|Too Many Requests|rate.limited|Rate limit exceeded|isn't available, try again later", None),
    ("timeout", r"timed out|timeout", None),
    ("failed", r"no impersonate target", _NEWER),
    ("failed", r"Playback on other websites|page needs to be reloaded", _RELOAD),
    ("no_media", r"unable to obtain file audio codec", _SILENT),
    ("no_media", r"No video could be found|No video formats found|Requested format is not available|no suitable|No media found|Only images are available|There is no video|does not contain any media|is not a video", None),
    ("unavailable", r"Premieres in|live event will begin|not started yet|not yet started|will premiere", _LATER),
    ("unavailable", r"not downloadable yet", _NOT_READY),
    ("unavailable", r"HTTP Error (?:404|410)|replay is disabled|requires payment|Video unavailable|(?:video|Video #\d+) is (?:not available|unavailable)|content is not available|has been removed|no longer available|has been terminated|taken down|tweet is unavailable|no longer exists|(?:was|has been) deleted|does not exist|copyright|Space not found|suspended", None),
    ("network", r"Failed to establish a new connection|Name or service not known|Failed to resolve|Temporary failure in name resolution|Connection refused|Connection reset|Network is unreachable|No route to host|Remote end closed|IncompleteRead|TransportError|certificate verify failed|SSL:|ProxyError|Connection aborted|URLError|getaddrinfo", None),
)

# Warnings that explain a failure that yt-dlp itself words badly.
_HINTS = (
    (r"No supported JavaScript runtime", "YouTube needs a JavaScript runtime. Install deno, then try again."),
    (r"account cookies are no longer valid", "The browser cookies have expired. Sign in again in the browser, then press Check in Settings."),
    (r"no impersonate target is available", _NEWER),
    (r"SABR|PO Token|n challenge solving failed|Only images are available", _RELOAD),
)

_DETAIL = re.compile(r"HTTP Error \d+: [A-Za-z ]+")
_ERROR_HEAD = re.compile(r"^ERROR:\s*(?:\[[^\]]*\]\s*(?:[^\s:]+:\s+)?)?")
_LINKS = re.compile(r"https?://\S+")
_PATHS = re.compile(r"""(['"])[^'"]*/[^'"]*\1""")
_QUOTED_END = re.compile(r"""[:,]\s+(['"]).*?\1\s*$""")


def _message_of(line: str) -> str:
    """What an error line says, without the site and id it starts with and without links and quoted paths."""
    text = _LINKS.sub(" ", _ERROR_HEAD.sub("", line))
    return " ".join(_QUOTED_END.sub("", _PATHS.sub(" ", text)).split())


def classify_error(text, exit_code=None) -> tuple[str, str]:
    """(code, plain message) for what yt-dlp printed. Takes the last 40 lines."""
    lines = text.splitlines() if isinstance(text, str) else list(text)
    lines = [str(x).strip() for x in lines if str(x).strip()][-40:]
    errs = [x for x in lines if x.startswith("ERROR:")]
    if errs:
        basis = errs[-1]
    else:
        basis = next((x for x in reversed(lines) if re.search(r"File is larger than|does not pass filter", x)), "")
        if not basis and exit_code not in (0, None) and lines:
            basis = lines[-1]
    said = _message_of(basis)
    for code, rx, msg in _RULES:
        if re.search(rx, said, re.I):
            break
    else:
        code, msg = "failed", None
    if "does not pass filter" in basis:
        return "unavailable", _LIVE
    if msg is None and code in ("failed", "no_media"):
        blob = "\n".join(lines)
        for rx, hint in _HINTS:
            if re.search(rx, blob):
                return code, hint
    if msg is None and code == "failed":
        if exit_code is not None and exit_code < 0 and not errs:
            return code, "yt-dlp stopped before it was done. Try again."
        m = _DETAIL.search(basis)
        if m:
            return code, f"yt-dlp could not get this one ({m.group(0).strip()}). Updating yt-dlp in Settings often fixes it."
    return code, msg or ERROR_MESSAGES[code]


def _write_error(e: OSError) -> tuple[str, str]:
    """(code, message) for an OSError raised while writing a file: a full disk says so, anything else is the save folder."""
    code, msg = classify_error(f"ERROR: {e}")
    if code in ("failed", "write_failed"):
        return "write_failed", f"AudioCrate could not write the file ({e.strerror or e}). Check the save folder in Settings."
    return code, msg


# small helpers

def clean_query(text) -> str:
    s = "".join(c if c.isprintable() else " " for c in str(text or ""))
    return " ".join(s.split())[:200]


def parse_version(text: str) -> tuple[int, ...]:
    """(2026, 8, 19) or (2026, 9, 27, 232945) from anything that holds a yt-dlp version."""
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?", text or "")
    return tuple(int(g) for g in m.groups() if g is not None) if m else ()


def version_age_days(text: str, now: datetime | None = None) -> int | None:
    v = parse_version(text)
    if len(v) < 3:
        return None
    try:
        born = datetime(v[0], v[1], v[2], tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, ((now or datetime.now(timezone.utc)) - born).days)


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_eta(sec) -> str:
    if not isinstance(sec, (int, float)) or sec < 0:
        return ""
    sec = int(sec)
    if sec < 60:
        return f"{sec} s"
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def memory_cost_mb(duration, rate: int = 48000) -> int:
    """What the engine needs to hold a track of this length: int16 stereo at the output rate."""
    return int((duration or 0) * rate * 4 / (1024 * 1024))


def _atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        plat_fs.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _rmtree(path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 1 << 62


def _safe_name(text: str, limit: int = 150) -> str:
    """A file name part: no slashes or control characters, cut to limit bytes."""
    s = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', " ", str(text or ""))
    s = " ".join(s.split()).strip(" .")
    b = s.encode("utf-8")[:limit]
    return b.decode("utf-8", "ignore").strip(" .") or "download"


def _inside(path, folder) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except (OSError, ValueError):
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item_key(item: dict, mode: str = "audio") -> str:
    """A folder-safe name for one item: same item and mode, same name."""
    raw = f"{item.get('provider')}|{item.get('id')}|{mode}"
    return f"{item.get('provider') or 'link'}-{hashlib.sha1(raw.encode()).hexdigest()[:16]}"


# yt-dlp output to item dicts

_AUDIO_SITES = {"soundcloud", "soundcloudset", "soundcloudplaylist", "soundcloudtrack", "bandcamp",
                "bandcampalbum", "bandcampuser", "bandcampweekly", "mixcloud", "audiomack",
                "internetarchive", "applepodcasts", "libsyn", "spreaker", "podbean"}
_LIVE_STATES = {"is_live"}
_CHANNEL_URL = re.compile(r"youtube\.com/(?:@|channel/|c/|user/)")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _thumb(info: dict, vid: str, youtube: bool) -> str:
    """The biggest 16:9 thumbnail no wider than 640 px, else the YouTube medium one."""
    best, best_w = "", 0
    for t in info.get("thumbnails") or []:
        if not isinstance(t, dict):
            continue
        u, w, h = t.get("url"), _num(t.get("width")), _num(t.get("height"))
        if isinstance(u, str) and u.startswith(("http://", "https://")) and w and 0 < w <= 640 \
                and w > best_w and (not h or 1.6 <= w / h <= 1.9):
            best, best_w = u, w
    if best:
        return best
    if youtube and _YT_ID.match(vid or ""):
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    t = info.get("thumbnail")
    if isinstance(t, str) and t.startswith(("http://", "https://")):
        return t
    for t in reversed(info.get("thumbnails") or []):
        u = t.get("url") if isinstance(t, dict) else None
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return u
    return ""


def _upload_date(info: dict) -> str:
    d = info.get("upload_date")
    if isinstance(d, str) and re.fullmatch(r"\d{8}", d):
        return d
    for key in ("timestamp", "release_timestamp"):
        ts = _num(info.get(key))
        if ts:
            try:
                return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
            except (OverflowError, OSError, ValueError):
                pass
    return ""


def _x_url(url: str) -> str:
    try:
        return normalize_url(url)
    except ValueError:
        return url


def item_from_info(info, parent=None, n: int = 0) -> dict:
    """One yt-dlp result, flat entry or full video, as an item dict.

    Never raises on a missing field. n is the 1 based position of a video inside an
    X post that holds several, which becomes part of the id (post#2).
    """
    info = info if isinstance(info, dict) else {}
    parent = parent if isinstance(parent, dict) else {}
    ie = str(info.get("extractor_key") or info.get("ie_key") or parent.get("extractor_key") or "").lower()
    url = str(info.get("webpage_url") or info.get("url") or "")
    vid = str(info.get("id") or "")
    if ie == "twitter":
        post = str(info.get("display_id") or parent.get("id") or vid)
        who = str(info.get("uploader_id") or parent.get("uploader_id") or "")
        url = f"https://x.com/{who}/status/{post}" if who and post.isdigit() else _x_url(url)
        vid = post + (f"#{n}" if n else "")
    prov = provider_for_url(url)
    if prov.id == "link" and (url or vid):
        if ie in ("generic", ""):
            vid = hashlib.sha1(url.encode()).hexdigest()[:12]
        vid = f"{ie or 'generic'}:{vid}"
    typ = info.get("_type")
    if typ == "playlist":
        is_channel = (ie == "youtubetab" and vid.startswith("UC")) or bool(_CHANNEL_URL.search(url))
        kind = "channel" if is_channel else "playlist"
    elif ie in ("youtubetab", "youtube:tab"):
        kind = "channel" if _CHANNEL_URL.search(url) else "playlist"
    elif ie == "twitterspaces":
        kind = "space"
    elif ie == "twitter":
        kind = "tweet"
    elif ie.split(":")[0] in _AUDIO_SITES or (info.get("vcodec") == "none" and info.get("acodec")):
        kind = "audio"
    else:
        kind = "video"
    entries = info.get("entries")
    count = _num(info.get("playlist_count"))
    if count is None and isinstance(entries, list) and kind in ("playlist", "channel"):
        count = len(entries)
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or info.get("artist") or ""
    if not uploader or str(uploader).lower() in ("playlist", "mix"):          # what a channel's playlists tab says
        uploader = parent.get("uploader") or parent.get("channel") or ""
        if not uploader and str(parent.get("extractor_key") or "").lower() == "generic":
            uploader = parent.get("title") or ""
    uploader_url = str(info.get("uploader_url") or info.get("channel_url") or parent.get("uploader_url") or "")
    if uploader_url and provider_for_url(uploader_url).id == "x":
        uploader_url = _x_url(uploader_url)
    live = info.get("is_live") is True or info.get("live_status") in _LIVE_STATES
    dur = _num(info.get("duration"))
    views = _num(info.get("view_count"))
    if views is None:
        views = _num(info.get("concurrent_view_count"))
    return {
        "id": vid,
        "provider": prov.id,
        "url": url,
        "title": str(info.get("title") or info.get("fulltitle") or ""),
        "uploader": str(uploader),
        "uploader_url": uploader_url,
        "duration": dur if dur is None or dur >= 0 else None,
        "thumbnail": _thumb(info, str(info.get("id") or ""), prov.id == "youtube"),
        "view_count": views,
        "upload_date": _upload_date(info),
        "description": str(info.get("description") or "")[:400],
        "kind": kind,
        "count": int(count) if count is not None else None,
        "is_live": bool(live),
        "availability": str(info.get("availability") or ""),
        "entries": [],
    }


def norm_item(item) -> dict:
    """An item dict with every key present and the right type, whatever the caller passed in."""
    base = item_from_info({})
    if not isinstance(item, dict):
        return base
    for k, v in item.items():
        if k not in base or k == "entries":
            continue
        if k in ("duration", "view_count", "count"):
            v = _num(v)
        elif k == "is_live":
            v = bool(v)
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            v = str(v)
        else:
            continue
        base[k] = v
    if base["provider"] not in PROVIDERS:
        base["provider"] = provider_for_url(base["url"]).id
    if base["kind"] not in ("video", "audio", "playlist", "channel", "tweet", "space"):
        base["kind"] = "video"
    return base


def parse_listing(data, limit: int, source: str, query: str) -> tuple[list[dict], dict]:
    """The items and the info dict for one decoded yt-dlp JSON answer."""
    data = data if isinstance(data, dict) else {}
    ie = str(data.get("extractor_key") or "").lower()
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
    if data.get("_type") != "playlist":
        item = item_from_info(data)
        return [item], {"title": item["title"], "count": 1, "more": False, "source": source, "query": query,
                        "kind": item["kind"], "url": item["url"], "uploader": item["uploader"],
                        "thumbnail": item["thumbnail"]}
    multi = ie == "twitter"
    items = []
    for i, e in enumerate(entries, 1):
        if not (e.get("id") or e.get("url") or e.get("webpage_url")):
            continue
        items.append(item_from_info(e, data, i if multi else 0))
    head = item_from_info(data)
    # yt-dlp fills in playlist_count itself with what it fetched when the site gave no total, so a
    # full page counts as "maybe more" even when the count says otherwise.
    total = _num(data.get("playlist_count"))
    more = len(entries) >= limit or bool(total and total > len(entries))
    # The rows are the entries. The container (the playlist, the channel, the post)
    # is described by the extra keys.
    title = query if source == "feed" else str(data.get("title") or query)
    info = {"title": title, "count": int(total) if total else len(items),
            "more": bool(more), "source": source, "query": query,
            "kind": "tweet" if multi else head["kind"], "url": head["url"],
            "uploader": head["uploader"], "thumbnail": head["thumbnail"]}
    return items, info


def parse_output_line(line: str, token: str = ""):
    """('AC', dict) progress, ('AP', dict) post step, ('AQ', dict) what was picked, ('AF', path) the final file, or ('', line).

    The final file is only named by the line that starts with AF and the token of this run. The token is made
    for the run and only yt-dlp is told it, so a site cannot put a result line of its own into what yt-dlp
    prints (it can write anything into an error message)."""
    head = line[:3]
    if head in ("AC ", "AP ", "AQ "):
        try:
            d = json.loads(line[3:])
            if isinstance(d, dict):
                return head[:2], d
        except ValueError:
            pass
    elif token and line.startswith("AF" + token + " "):
        return "AF", line[len("AF" + token) + 1:]
    return "", line


_CODECS = (("opus", "Opus"), ("mp4a", "AAC"), ("aac", "AAC"), ("vorbis", "Vorbis"), ("mp3", "MP3"),
           ("flac", "FLAC"), ("avc1", "H.264"), ("h264", "H.264"), ("vp09", "VP9"), ("vp9", "VP9"),
           ("av01", "AV1"), ("hev", "H.265"), ("hvc", "H.265"))


def quality_label(d: dict, mode: str) -> str:
    """'Opus, 128 kbps' or 'H.264, 1080p' from what yt-dlp says it picked. Empty when it does not say."""
    def codec(c) -> str:
        return next((name for key, name in _CODECS if str(c or "").lower().startswith(key)), "")
    if mode == "video":
        h = _num(d.get("height"))
        bits = [codec(d.get("vcodec")), f"{int(h)}p" if h else ""]
    else:
        abr = _num(d.get("abr"))
        bits = [codec(d.get("acodec")) or str(d.get("ext") or "").upper(), f"{abr:.0f} kbps" if abr else ""]
    return ", ".join(b for b in bits if b)


_POST_NAMES = {"ExtractAudio": "Extracting audio", "Merger": "Joining video and audio",
               "Metadata": "Writing tags", "EmbedThumbnail": "Adding cover art",
               "MoveFiles": "Finishing", "FixupM3u8": "Fixing the stream", "FixupM4a": "Fixing the file",
               "VideoConvertor": "Converting", "VideoRemuxer": "Converting"}


# settings

def _download_dir() -> str:
    return str(plat_paths.music_dir() / "AudioCrate")


DEFAULTS = {
    "save_dir": None,            # filled in per user: ~/Music/AudioCrate
    "cache_max_mb": 2048, "audio_format": "best", "video_height": 1080, "max_file_mb": 4096,
    "concurrent": 2, "rate_limit": "", "cookies": {}, "recent": [],
    # beyond the basic set
    "max_minutes": 180,          # longest audio item it will fetch; 0 turns the limit off
    "square_art": True,          # crop cover art of audio to a square
    "channel": "nightly",        # which yt-dlp build Update installs: nightly or stable
    "notice_version": "",        # which first run notice the user accepted
}


def _int_in(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        n = int(float(v))
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    return max(lo, min(hi, n))


def validate_setting(key: str, value):
    """The cleaned value for a setting, or ValueError."""
    if key == "save_dir":
        p = Path(os.path.expanduser(str(value or "")))
        if not str(value or "").strip() or not p.is_absolute():
            raise ValueError("The save folder must be a full path")
        return str(p)
    if key == "cache_max_mb":
        return _int_in(value, 64, 1_000_000, key)
    if key == "audio_format":
        if value not in ("best", "opus", "m4a", "mp3"):
            raise ValueError("Audio format is best, opus, m4a or mp3")
        return value
    if key == "video_height":
        return _int_in(value, 144, 4320, key)
    if key == "max_file_mb":
        return _int_in(value, 16, 1_000_000, key)
    if key == "concurrent":
        return _int_in(value, 1, 4, key)
    if key == "rate_limit":
        s = str(value or "").strip()
        if s and not re.fullmatch(r"\d+(?:\.\d+)?[KkMm]?", s):
            raise ValueError("The speed limit looks like 500K or 2M")
        return s
    if key == "recent":
        if not isinstance(value, (list, tuple)):
            raise ValueError("recent is a list")
        return [clean_query(x) for x in value if clean_query(x)][:12]
    if key == "max_minutes":
        return _int_in(value, 0, 1440, key)
    if key == "square_art":
        return bool(value)
    if key == "channel":
        if value not in ("nightly", "stable"):
            raise ValueError("The update channel is nightly or stable")
        return value
    if key == "notice_version":
        return str(value or "")[:40]
    if key == "cookies":
        raise ValueError("Use set_cookie_source for accounts")
    raise ValueError(f"Unknown setting {key}")


class _Config:
    """online.json: written atomically, readable only by the user."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        data = {k: copy.deepcopy(v) for k, v in DEFAULTS.items()}
        data["save_dir"] = _download_dir()
        try:
            fd = plat_fs.open_regular(self.path)
            with os.fdopen(fd, encoding="utf-8") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return data
                raw = json.load(f)
        except (OSError, ValueError):
            return data
        if not isinstance(raw, dict):
            return data
        for k, v in raw.items():
            if k == "cookies":
                if isinstance(v, dict):
                    data["cookies"] = {str(p): c for p, c in v.items() if isinstance(c, dict)}
            elif k in DEFAULTS:
                try:
                    data[k] = validate_setting(k, v)
                except ValueError:
                    pass
            else:
                data[k] = v
        return data

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.data.get(key))

    def all(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def set(self, key, value) -> None:
        with self.lock:
            self.data[key] = value
            self.save()

    def save(self) -> None:
        with self.lock:
            try:
                _atomic_write(self.path, json.dumps(self.data, indent=1, sort_keys=True))
            except OSError:
                log.exception("saving %s", self.path)


# child processes

_children: set = set()
_children_lock = threading.Lock()


def _spawn(args, merge: bool = False, cwd=None, tmpdir=None, private=None) -> subprocess.Popen:
    """A child in its own session and process group, at a lower priority, with a guard that ends it if this
    process disappears. Never a shell. private is a folder that goes with the child and goes with it."""
    p = subprocess.Popen([str(a) for a in args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                         env=plat_proc.child_env(tmpdir), cwd=cwd, close_fds=True, **plat_proc.popen_kwargs(low=True))
    plat_proc.lower_priority(p)                 # the audio engine matters more
    plat_proc.watch(p, private)
    with _children_lock:
        _children.add(p)
    return p


def _stop(procs, sigs=plat_proc.LADDER, grace: float = 3.0) -> None:
    """Stop whole process groups, all at once, and forget them. Ask first, then insist."""
    plat_proc.stop(procs, sigs, grace)
    with _children_lock:
        _children.difference_update(procs)


def _kill_children() -> None:
    with _children_lock:
        procs = list(_children)
    if procs:
        _stop(procs, (plat_proc.TERM, plat_proc.KILL), 1.5)


atexit.register(_kill_children)


class _Ctl:
    """Lets another thread cancel work that is waiting on a child process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.proc = None

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def attach(self, proc) -> None:
        with self.lock:
            self.proc = proc
            gone = self.event.is_set()
        if gone:
            self._kill(proc)

    def cancel(self) -> None:
        with self.lock:
            self.event.set()
            proc = self.proc
        if proc is not None:
            self._kill(proc)

    @staticmethod
    def _kill(proc) -> None:
        threading.Thread(target=_stop, args=([proc],), daemon=True).start()


def _text(b: bytes | None) -> str:
    return (b or b"").decode("utf-8", "surrogateescape")


def _capture(args, timeout: float, ctl: _Ctl | None = None, cwd=None, tmpdir=None, private=None):
    """Run to the end. Returns (exit code, stdout, stderr, timed out), decoded as UTF-8."""
    p = _spawn(args, cwd=cwd, tmpdir=tmpdir, private=private)
    if ctl is not None:
        ctl.attach(p)
    timed = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed = True
        _stop([p], (plat_proc.TERM, plat_proc.KILL), 2.0)
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    finally:
        _stop([p], (plat_proc.TERM, plat_proc.KILL), 1.0)    # anything it left behind
    return p.returncode, _text(out), _text(err), timed


class _Slots:
    """Starts jobs in order, at most `limit` at a time, each in its own daemon thread.

    Daemon threads on purpose: a non-daemon thread would hold the interpreter at exit
    until its download ended, before any exit handler could stop the child.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.lock = threading.Lock()
        self.waiting: deque = deque()
        self.threads: set = set()
        self.closed = False

    def submit(self, fn) -> None:
        with self.lock:
            if self.closed:
                return
            self.waiting.append(fn)
        self._pump()

    def withdraw(self, fn) -> bool:
        """Take a job back out of the line. False when it already started."""
        with self.lock:
            try:
                self.waiting.remove(fn)
                return True
            except ValueError:
                return False

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.waiting.clear()

    def join(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        with self.lock:
            threads = list(self.threads)
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))

    def _pump(self) -> None:
        while True:
            with self.lock:
                if self.closed or not self.waiting or len(self.threads) >= self.limit:
                    return
                fn = self.waiting.popleft()
                t = threading.Thread(target=self._run, args=(fn,), daemon=True)
                self.threads.add(t)
                t.start()

    def _run(self, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("online job")
        finally:
            with self.lock:
                self.threads.discard(threading.current_thread())
            self._pump()


# files, sidecars and direct links

def direct_item(url: str) -> dict:
    """An item for a link to an audio file, made from the link alone."""
    u = urlsplit(url)
    item = norm_item({})
    item.update(id="direct:" + hashlib.sha1(url.encode()).hexdigest()[:12], provider=provider_for_url(url).id,
                url=url, title=Path(unquote(u.path)).stem or (u.hostname or ""), uploader=u.hostname or "",
                kind="audio")
    return item


def write_sidecar(media: Path, item: dict, mode: str, quality: str = "") -> None:
    """<name>.audiocrate.json next to a file: what it is, so it is not fetched twice."""
    data = {"provider": item["provider"], "id": item["id"], "url": item["url"], "title": item["title"],
            "uploader": item["uploader"], "thumbnail": item["thumbnail"], "saved_at": _now_iso(),
            "mode": mode, "quality": quality}
    _atomic_write(media.with_name(media.name + SIDECAR), json.dumps(data, indent=1), 0o644)


def read_sidecar(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def find_media(folder: Path, item: dict, mode: str) -> Path | None:
    """The file in folder that a sidecar says is this item in this mode, if there is one."""
    try:
        names = [e.name for e in os.scandir(folder) if e.name.endswith(SIDECAR)]
    except OSError:
        return None
    tag = str(item["id"]).split("#")[0].split(":", 1)[-1][:40]
    named = tag.isascii() and bool(tag) and not str(item["id"]).startswith("generic:")
    for name in names:
        if named and tag not in name:                       # the file name carries the id
            continue
        sc = read_sidecar(folder / name)
        if sc and sc.get("provider") == item["provider"] and sc.get("id") == item["id"] \
                and sc.get("mode", "audio") == mode:
            media = folder / name[:-len(SIDECAR)]
            if media.is_file():
                return media
    return None


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects to http and https only, not to files or ftp."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith(("http://", "https://")):
            raise urllib.error.HTTPError(newurl, code, "redirected somewhere AudioCrate will not go", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_SafeRedirect)


def _hang_up(resp) -> None:
    """Cut the connection under a response, which wakes a thread that is blocked reading from it."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


def _interruptible(ctl: _Ctl, fn):
    """Run fn(stop, live) on a thread and give back its result, but never wait past a cancel.

    A read that is blocked on a socket cannot be interrupted from outside, and it only gives up when its
    timeout runs out. So on a cancel the connections fn put in `live` are hung up, `stop` is set, and the
    thread is left to notice, clean up after itself and end. fn raises or returns as usual.
    """
    stop, live, box, ended = threading.Event(), [], {}, threading.Event()

    def run():
        try:
            box["value"] = fn(stop, live)
        except BaseException as e:
            box["error"] = e
        finally:
            ended.set()

    threading.Thread(target=run, daemon=True).start()
    while not ended.wait(0.1):
        if ctl.cancelled:
            stop.set()
            for r in list(live):
                _hang_up(r)
            raise _Cancel()
    if "error" in box:
        if ctl.cancelled:
            raise _Cancel()
        raise box["error"]
    return box.get("value")


class _Progress:
    """Turns yt-dlp's progress lines into one steady percentage.

    A video is fetched as two streams, so the bar runs over both instead of filling
    twice. It stops at 99 until the whole job is done.
    """

    def __init__(self, streams: int):
        self.streams, self.done = streams, 0

    def update(self, d: dict):
        """(percent or None, bytes per second, seconds left or None)."""
        got = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        speed = d.get("speed") or 0
        frac = min(1.0, got / total) if total else None
        if d.get("status") == "finished":
            self.done += 1
            frac = 0.0
        eta = d.get("eta")
        if eta is None and speed and total and got <= total:
            eta = (total - got) / speed
        if frac is None:
            return None, speed, eta
        return min(99.0, 100.0 * (min(self.done, self.streams) + frac) / self.streams), speed, eta


_PROGRESS = ["--newline", "--no-quiet", "--progress-delta", "0.125",
             "--progress-template",
             "download:AC %(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta,fragment_index,fragment_count})j",
             "--progress-template", "postprocess:AP %(progress.{status,postprocessor})j",
             "--print", "after_move:AQ %(.{acodec,vcodec,abr,height,ext})j"]

# Tags: use the real artist and track when the site has them. Otherwise, a title like
# "Artist - Title" fills in what is missing, and the uploader is the artist after all.
_TAGS = ["--replace-in-metadata", "uploader,channel", " - Topic$", "",        # YouTube's "Artist - Topic" channels
         "--embed-metadata",
         "--parse-metadata", "title:(?P<temp_artist>.+?) - (?P<temp_title>.+)",
         "--parse-metadata", "%(artist,artists,temp_artist|)l:%(meta_artist)s",
         "--parse-metadata", "%(track,temp_title|)s:%(meta_title)s"]

# A video frame is 16:9, album art is square. Only applies when yt-dlp converts the picture.
_SQUARE = "ThumbnailsConvertor+ffmpeg_o:-vf \"crop='min(iw,ih)':'min(iw,ih)'\""


# cookies

_COOKIE_MAX = 5 * 1024 * 1024
LINK_SITES = 2             # Any link has no list of domains to keep, so a cookies file for it may name this many sites
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go", "gob", "nom"}

# What a signed in session looks like in a cookie jar, as yt-dlp itself reads it.
_SIGNED_IN = {"youtube": ({"LOGIN_INFO"}, {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}),
              "x": ({"auth_token"}, set())}


def _cookie_lines(text: str):
    """(domain, name, line) for each valid line of a Netscape cookies file."""
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if not line.strip() or (line.startswith("#") and not http_only):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        dom = parts[0][len("#HttpOnly_"):] if http_only else parts[0]
        yield dom.lstrip(".").lower(), parts[5], line


def _in_domains(dom: str, domains) -> bool:
    return not domains or any(dom == d or dom.endswith("." + d) for d in domains)


def cookie_site(dom: str) -> str:
    """The site a cookie domain belongs to: example.org for www.example.org, example.co.uk for shop.example.co.uk."""
    labels = [x for x in dom.split(".") if x]
    if len(labels) <= 2 or re.fullmatch(r"[0-9.]+|[0-9a-f:]+", dom):
        return dom
    return ".".join(labels[-(3 if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2):])


def cookie_sites(text: str) -> set:
    return {cookie_site(dom) for dom, _name, _line in _cookie_lines(text)}


def filter_cookies(text: str, domains, sites=None) -> tuple[str, int]:
    """A cookies file with only the lines of the given domains (every line when domains is empty),
    and with sites only the lines of those sites."""
    keep = [line for dom, _name, line in _cookie_lines(text)
            if _in_domains(dom, domains) and (sites is None or cookie_site(dom) in sites)]
    return "# Netscape HTTP Cookie File\n" + "".join(x + "\n" for x in keep), len(keep)


def cookie_names(text: str, domains) -> set:
    return {name for dom, name, _line in _cookie_lines(text) if _in_domains(dom, domains)}


def jar_signed_in(provider_id: str, names: set) -> bool | None:
    """Do these cookie names look like a signed in session? None when there is no way to tell."""
    rule = _SIGNED_IN.get(provider_id)
    if rule is None:
        return None
    need, any_of = rule
    return need <= names and (not any_of or bool(any_of & names))


# gallery-dl, for X profiles and bookmarks

def parse_gdl(text: str) -> list[dict]:
    """Items for the videos in gallery-dl's one message per line output (-j with output.jsonl)."""
    posts: dict[str, list[dict]] = {}
    for line in text.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not (isinstance(msg, list) and len(msg) >= 3 and msg[0] == 3 and isinstance(msg[2], dict)):
            continue
        m = msg[2]
        tid = str(m.get("tweet_id") or "")
        if tid and m.get("type") in ("video", "animated_gif"):
            posts.setdefault(tid, []).append(m)
    items = []
    for tid, files in posts.items():
        for k, m in enumerate(files, 1):
            author = m.get("author") if isinstance(m.get("author"), dict) else {}
            handle, nick = str(author.get("name") or ""), str(author.get("nick") or author.get("name") or "")
            text_ = " ".join(str(m.get("content") or "").split())
            it = norm_item({})
            it.update(
                id=tid + (f"#{k}" if len(files) > 1 else ""), provider="x", kind="tweet",
                url=f"https://x.com/{handle}/status/{tid}" if handle else f"https://x.com/i/status/{tid}",
                title=f"{nick} - {text_[:72]}" + ("..." if len(text_) > 72 else ""), uploader=nick,
                uploader_url=f"https://x.com/{handle}" if handle else "",
                duration=_num(m.get("duration")), view_count=_num(m.get("view_count")),
                description=text_[:400], upload_date=re.sub(r"\D", "", str(m.get("date") or ""))[:8])
            items.append(it)
    return items


# the service

class _Req:
    """A search, listing or check that is waiting or running."""
    __slots__ = ("ctl", "run", "slots")

    def __init__(self):
        self.ctl = _Ctl()
        self.run = None
        self.slots = None


class _Job:
    """One fetch. state is what the UI sees, the rest is private to the service."""

    def __init__(self, jid: str, n: int, item: dict, mode: str, dest: str, play: bool, play_next: bool):
        self.id, self.n = jid, n
        self.item, self.mode, self.dest = item, mode, dest
        self.ctl = _Ctl()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.run = None
        self.last_emit = 0.0
        self.finished_at = 0.0
        self.state = {"id": jid, "state": "queued", "percent": 0.0, "speed": "", "eta": "",
                      "speed_bps": 0.0, "eta_s": -1, "text": "Waiting", "path": "", "item": item,
                      "mode": mode, "dest": dest, "play": play, "play_next": play_next,
                      "error_code": "", "error": "", "quality": "", "indeterminate": False}

    @property
    def finished(self) -> bool:
        return self.state["state"] in ("done", "error", "cancelled")


class _Cancel(Exception):
    pass


class _Fail(Exception):
    def __init__(self, code: str, msg: str | None = None):
        super().__init__(code)
        self.code = code
        self.msg = msg or ERROR_MESSAGES[code]


def _noop(ok: bool = False) -> None:
    pass


_noop.dir = None


def _provider(pid) -> Provider | None:
    return PROVIDERS.get(pid) if isinstance(pid, str) else None


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class OnlineService(QObject):
    """Everything the Online panel needs. Every call returns at once; results arrive as signals."""

    results_ready = Signal(int, list, dict)      # request id, items, info
    listing_ready = Signal(int, dict)            # request id, what yt-dlp printed for one page of a channel or playlist
    request_failed = Signal(int, str, str)       # request id, error code, message
    job_updated = Signal(str, dict)              # job id, state dict
    login_changed = Signal(str)                  # provider id
    tools_changed = Signal()
    _post = Signal(object)                       # runs a callable on this object's thread

    def __init__(self, parent=None, config_dir: Path | None = None, cache_dir: Path | None = None,
                 data_dir: Path | None = None):
        super().__init__(parent)
        cfg = Path(config_dir) if config_dir else plat_paths.config_dir(xdg=True)
        cache = Path(cache_dir) if cache_dir else plat_paths.cache_dir(xdg=True) / "online"
        data = Path(data_dir) if data_dir else plat_paths.data_dir(xdg=True)
        self.config_dir, self.cache_dir = cfg, cache
        self.media_dir, self.thumbs_dir = cache / "media", cache / "thumbs"
        self.tmp_dir, self.ytdlp_cache = cache / "tmp", cache / "ytdlp"
        self.cookie_dir, self.bin_dir = cfg / "online" / "cookies", data / "bin"
        self._cfg = _Config(cfg / "online.json")
        self._lock = threading.RLock()
        self._closed = False
        self._req_ids = itertools.count(1)
        self._job_ids = itertools.count(1)
        self._reqs: dict[int, _Req] = {}
        self._jobs: dict[str, _Job] = {}
        self._live: dict[tuple, _Job] = {}         # (provider, id, mode, dest) of unfinished jobs
        self._tools: dict | None = None            # result of the last probe
        self._probing = False
        self._pace_lock = threading.Lock()
        self._last_start: dict[str, float] = {}
        self.protect = None                        # optional: returns paths that must not be pruned
        self._lists = _Slots(LIST_SLOTS)
        self._dl = _Slots(self._cfg.get("concurrent"))
        self._upd = _Slots(1)
        self._post.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)
        self._sweep_jars()

    # plumbing

    def _run_posted(self, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("online signal")

    def _later(self, fn, *args) -> None:
        """Run fn(*args) on the service's thread, after the current call has returned."""
        if self._closed:
            return
        try:
            self._post.emit(partial(fn, *args))
        except RuntimeError:                      # the object is gone, the app is closing
            pass

    def _begin(self) -> tuple[int, _Req]:
        rid, req = next(self._req_ids), _Req()
        with self._lock:
            self._reqs[rid] = req
        return rid, req

    def _end(self, rid: int) -> None:
        with self._lock:
            self._reqs.pop(rid, None)

    def _fail(self, rid: int, code: str, msg: str | None = None) -> None:
        self._later(self.request_failed.emit, rid, code, msg or ERROR_MESSAGES[code])

    def _ok(self, rid: int, items: list, info: dict) -> None:
        self._later(self.results_ready.emit, rid, items, info)

    def _reject(self, rid: int, code: str, msg: str | None = None) -> int:
        self._end(rid)
        self._fail(rid, code, msg)
        return rid

    def _mkdir(self, path: Path, mode: int = 0o755) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return path

    def _private_dir(self) -> Path:
        self._mkdir(self.tmp_dir)
        return Path(tempfile.mkdtemp(prefix=f"jar_{os.getpid()}_", dir=self.tmp_dir))      # mkdtemp is 0700

    def _sweep_jars(self) -> None:
        """Remove the cookie copies of a process that no longer exists, which a kill or a crash left behind."""
        try:
            names = [e.name for e in os.scandir(self.tmp_dir) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        for name in names:
            m = re.match(r"jar_(\d+)_", name)
            if m and not plat_proc.is_alive(int(m.group(1))):
                _rmtree(self.tmp_dir / name)

    def _submit_list(self, rid: int, req: _Req, fn) -> None:
        req.run, req.slots = fn, self._lists
        self._lists.submit(fn)

    # settings

    def settings(self) -> dict:
        return self._cfg.all()

    def set_setting(self, key: str, value) -> None:
        """Change one setting. Raises ValueError for an unknown key or a bad value."""
        value = validate_setting(key, value)
        self._cfg.set(key, value)
        if key == "concurrent":
            self._dl.limit = value
            self._dl._pump()

    def library_dir(self, provider_id: str) -> Path:
        """Where saved files from a provider go: save_dir/<Provider name>."""
        prov = _provider(provider_id) or LINK
        return Path(self._cfg.get("save_dir")) / _safe_name(prov.name, 60)

    def _remember(self, query: str) -> None:
        recent = [query] + [q for q in self._cfg.get("recent") if q.lower() != query.lower()]
        self._cfg.set("recent", recent[:12])

    # yt-dlp

    def _ytdlp_bin(self) -> str | None:
        """AUDIOCRATE_YTDLP if set, else the best copy the last probe found, else the first one on the path."""
        env = os.environ.get("AUDIOCRATE_YTDLP")
        if env:
            return env
        t = self._tools
        if t and t["ytdlp"]["path"]:
            return t["ytdlp"]["path"]
        owned = self.bin_dir / "yt-dlp"
        if owned.is_file():
            return str(owned)
        return shutil.which("yt-dlp", path=plat_proc.child_env()["PATH"])

    def _js_args(self) -> list[str]:
        """Tell yt-dlp where the JavaScript runtime is, when the probe found one it cannot find itself."""
        t = self._tools
        rt = t and t["js"].get("ok") and t["js"]
        v = parse_version(t["ytdlp"]["version"]) if t else ()
        if not rt or v < (2025, 11, 1):
            return []
        if rt["name"] == "deno" and shutil.which("deno", path=plat_proc.child_env()["PATH"]) == rt["path"]:
            return []
        return ["--js-runtimes", f"{rt['name']}:{rt['path']}"]

    def _base(self) -> list[str]:
        self._mkdir(self.ytdlp_cache)
        return ["--ignore-config", "--no-update", "--color", "never", "--socket-timeout", "15",
                "--retries", "3", "--fragment-retries", "3", "--extractor-retries", "2",
                "--no-exec", "--cache-dir", str(self.ytdlp_cache)] + self._js_args()

    def _cwd(self) -> str:
        return str(self._mkdir(self.tmp_dir))

    def _proc_tmp(self) -> Path:
        return self._mkdir(self.tmp_dir / "proc")

    @staticmethod
    def _tweak_args(tweaks: set) -> list[str]:
        if "client" in tweaks:
            return ["--extractor-args", "youtube:player_client=default,web_embedded", "-4"]
        return []

    def _next_tweak(self, prov: Provider, code: str, text: str, tweaks: set, art: bool = False) -> str | None:
        """The next thing worth trying after a failure, once each: cookies, another YouTube client, no cover art."""
        if code in ("login_required", "age_restricted", "private") and "cookies" not in tweaks \
                and self._cookies_available(prov.id):
            return "cookies"
        if prov.id == "youtube" and "client" not in tweaks and code in ("failed", "no_media") \
                and re.search(r"HTTP Error 40[13]|Requested format is not available|Only images are available|"
                              r"SABR|missing a URL", text):
            return "client"
        if art and "noart" not in tweaks and re.search(r"thumbnail embedding|EmbedThumbnail", text):
            return "noart"
        return None

    def _ytdlp_json(self, ctl: _Ctl, prov: Provider, mid: list, target: str, cookies: bool = False,
                    timeout: float = LIST_TIMEOUT):
        """Run yt-dlp for one JSON answer. Returns (data, error code, message, stderr)."""
        exe = self._ytdlp_bin()
        if exe is None:
            return None, "no_ytdlp", ERROR_MESSAGES["no_ytdlp"], ""
        self._ensure_probe()
        tweaks = {"cookies"} if cookies else set()
        while True:
            try:
                base, cwd, tmp = self._base(), self._cwd(), self._proc_tmp()
            except OSError as e:
                return None, "write_failed", (f"AudioCrate cannot write to its cache folder ({e.strerror or e}). "
                                              f"Check the permissions of {self.cache_dir}."), ""
            cargs, done = self._cookie_args(prov.id) if "cookies" in tweaks else ([], _noop)
            args = [exe] + base + mid + self._tweak_args(tweaks) + cargs + ["--", target]
            ok = False
            try:
                rc, out, err, timed = _capture(args, timeout, ctl, cwd=cwd, tmpdir=tmp, private=done.dir)
                if rc == 0 and not timed and not ctl.cancelled:
                    try:
                        data = json.loads(out)
                    except ValueError:
                        data = None
                    if isinstance(data, dict):
                        ok = True
                        return data, "", "", err
            except OSError:
                return None, "no_ytdlp", ERROR_MESSAGES["no_ytdlp"], ""
            finally:
                done(ok)
            if ctl.cancelled:
                return None, "cancelled", ERROR_MESSAGES["cancelled"], err
            if timed:
                code, msg = "timeout", ERROR_MESSAGES["timeout"]
            elif rc == 0:
                code, msg = "failed", "yt-dlp answered with something AudioCrate could not read."
            else:
                code, msg = classify_error(err or out, rc)
            nxt = self._next_tweak(prov, code, err, tweaks)
            if nxt is None:
                log.info("yt-dlp failed (%s): %s", code, (err or out).strip().splitlines()[-1:] or "")
                return None, code, msg, err
            tweaks.add(nxt)

    # search, browse, resolve

    def search(self, provider_id: str, query: str, limit: int = 25) -> int:
        rid, req = self._begin()
        prov = _provider(provider_id)
        q = clean_query(query)
        if prov is None or not prov.search_prefix:
            name = prov.name if prov else "this site"
            return self._reject(rid, "unsupported_url", f"Search is not offered for {name}. Paste a link instead.")
        if not q:
            return self._reject(rid, "no_media", "Type something to search for.")
        n = _clamp(limit, 1, 100, 25)
        mid = ["--flat-playlist", "--dump-single-json"]
        if prov.id == "youtube":
            mid += ["--extractor-args", "youtubetab:approximate_date"]
        self._submit_list(rid, req, partial(self._do_list, rid, req, prov, mid, f"{prov.search_prefix}{n}:{q}",
                                            n, "search", q, False))
        return rid

    def browse(self, provider_id: str, feed_id: str, limit: int = 50) -> int:
        rid, req = self._begin()
        prov = _provider(provider_id)
        feed = next((f for f in prov.feeds if f.id == feed_id), None) if prov else None
        if feed is None or not feed.target:
            return self._reject(rid, "unsupported_url", "That list is not available.")
        if feed.needs_login and not self._cookies_available(prov.id):
            return self._reject(rid, "login_required")
        n = _clamp(limit, 1, 200, 50)
        if prov.id == "x":
            self._submit_list(rid, req, partial(self._do_gdl, rid, req, feed.target, n, feed.label))
        else:
            mid = ["--flat-playlist", "--dump-single-json", "--playlist-end", str(n), "--yes-playlist"]
            self._submit_list(rid, req, partial(self._do_list, rid, req, prov, mid, feed.target, n,
                                                "feed", feed.label, feed.needs_login))
        return rid

    def resolve(self, url: str, limit: int = 100) -> int:
        rid, req = self._begin()
        try:
            u = normalize_url(url)
        except ValueError as e:
            return self._reject(rid, "unsupported_url", str(e))
        prov = provider_for_url(u)
        n = _clamp(limit, 1, 500, 100)
        if is_direct_audio(u):
            item = direct_item(u)
            self._end(rid)
            self._ok(rid, [item], {"title": item["title"], "count": 1, "more": False, "source": "link",
                                   "query": u, "kind": "audio", "url": u, "uploader": item["uploader"],
                                   "thumbnail": ""})
            return rid
        if prov.id == "x" and x_url_kind(u) in ("profile", "bookmarks"):
            self._submit_list(rid, req, partial(self._do_gdl, rid, req, u, n, ""))
            return rid
        mid = ["--flat-playlist", "--no-playlist", "--playlist-end", str(n), "--dump-single-json",
               "--skip-download", "-f", "ba/b"]
        self._submit_list(rid, req, partial(self._do_list, rid, req, prov, mid, listing_url(u), n, "link", u, False))
        return rid

    def listing(self, url: str, start: int = 1, count: int = 30) -> int:
        """Items start to start+count-1 of a channel, a channel tab or a playlist, flat. The answer is the decoded
        JSON, through listing_ready, since the page that asked reads the channel's name and picture from it too."""
        rid, req = self._begin()
        try:
            u = normalize_url(url)
        except ValueError as e:
            return self._reject(rid, "unsupported_url", str(e))
        prov = provider_for_url(u)
        if prov.id == "x":
            return self._reject(rid, "unsupported_url", "X has no channel pages here.")
        first, n = _clamp(start, 1, 100000, 1), _clamp(count, 1, 100, 30)
        mid = ["--flat-playlist", "--dump-single-json", "--yes-playlist", "--playlist-items", f"{first}:{first + n - 1}"]
        if prov.id == "youtube":
            mid += ["--extractor-args", "youtubetab:approximate_date"]
        self._submit_list(rid, req, partial(self._do_listing, rid, req, prov, mid, u))
        return rid

    def _do_listing(self, rid: int, req: _Req, prov: Provider, mid: list, target: str) -> None:
        try:
            if req.ctl.cancelled:
                self._fail(rid, "cancelled")
                return
            data, code, msg, _ = self._ytdlp_json(req.ctl, prov, mid, target)
            if data is None:
                self._fail(rid, code, msg)
                return
            self._later(self.listing_ready.emit, rid, data)
        except Exception:
            log.exception("listing %s", target)
            self._fail(rid, "failed")
        finally:
            self._end(rid)

    def _do_list(self, rid: int, req: _Req, prov: Provider, mid: list, target: str, n: int,
                 source: str, query: str, cookies: bool) -> None:
        try:
            if req.ctl.cancelled:
                self._fail(rid, "cancelled")
                return
            data, code, msg, _ = self._ytdlp_json(req.ctl, prov, mid, target, cookies)
            if data is None:
                self._fail(rid, code, msg)
                return
            items, info = parse_listing(data, n, source, query)
            if source == "search":
                self._remember(query)
            self._ok(rid, items, info)
        except Exception:
            log.exception("listing %s", target)
            self._fail(rid, "failed")
        finally:
            self._end(rid)

    # jobs

    def start_download(self, item: dict, mode: str = "audio", dest: str = "cache",
                       play: bool = False, play_next: bool = False) -> str:
        """Queue a fetch and return its job id at once.

        The same item asked for twice while it is queued or running gets the same job
        back (the play and play_next flags are merged), unless that job is being
        cancelled. A file already in the cache or the library finishes straight away
        with state done.
        """
        it = norm_item(item)
        mode = "video" if mode == "video" else "audio"
        dest = "library" if dest == "library" else "cache"
        key = (it["provider"], it["id"], mode, dest)
        with self._lock:
            old = self._live.get(key)
            if old is not None and not old.finished and not old.ctl.cancelled:
                with old.lock:
                    old.state["play"] = old.state["play"] or bool(play)
                    old.state["play_next"] = old.state["play_next"] or bool(play_next)
                return old.id
            n = next(self._job_ids)
            job = _Job(f"j{n}", n, it, mode, dest, bool(play), bool(play_next))
            self._jobs[job.id] = job
            self._live[key] = job
            for j in [j for j in self._jobs.values() if j.finished][:-100]:
                del self._jobs[j.id]
        self._publish(job, force=True)
        job.run = partial(self._run_job, job)
        self._dl.submit(job.run)
        return job.id

    def jobs(self) -> list[dict]:
        with self._lock:
            return [self._snap(j) for j in self._jobs.values()]

    def clear_finished(self) -> None:
        with self._lock:
            for j in [j for j in self._jobs.values() if j.finished]:
                del self._jobs[j.id]

    def cancel(self, ident) -> None:
        """Cancel a request (an int) or a job (a string). Partial files are kept so a retry can resume."""
        if isinstance(ident, str):
            with self._lock:
                job = self._jobs.get(ident)
            if job is not None and not job.finished:
                job.ctl.cancel()
                if self._dl.withdraw(job.run):            # it never started
                    self._finish(job, "cancelled", "cancelled", text="Cancelled")
            return
        if not isinstance(ident, int):
            return
        with self._lock:
            req = self._reqs.get(ident)
        if req is not None:
            req.ctl.cancel()
            if req.slots is not None and req.slots.withdraw(req.run):
                self._end(ident)
                self._fail(ident, "cancelled")

    def _snap(self, job: _Job) -> dict:
        with job.lock:
            return dict(job.state)

    def _publish(self, job: _Job, force: bool = False, **fields) -> None:
        """Update a job and tell the UI, at most 8 times a second unless force. A job that has ended stays ended."""
        with job.lock:
            if job.state["state"] in ("done", "error", "cancelled"):
                return
            job.state.update(fields)
            now = time.monotonic()
            if not force and now - job.last_emit < 0.125:
                return
            job.last_emit = now
            snap = dict(job.state)
        self._later(self.job_updated.emit, job.id, snap)

    def _finish(self, job: _Job, state: str, code: str = "", msg: str = "", path: str = "", text: str = "") -> None:
        if job.finished:
            return
        self._publish(job, force=True, state=state, speed="", eta="", speed_bps=0.0, eta_s=-1, path=path,
                      indeterminate=False,
                      percent=100.0 if state == "done" else job.state["percent"], error_code=code,
                      error=msg, text=text or msg)
        job.finished_at = time.monotonic()
        with self._lock:
            key = (job.item["provider"], job.item["id"], job.mode, job.dest)
            if self._live.get(key) is job:
                del self._live[key]
        job.done.set()

    def _run_job(self, job: _Job) -> None:
        item, mode = job.item, job.mode
        tmp = self.tmp_dir / item_key(item, mode)
        keep = False
        try:
            if job.ctl.cancelled:
                raise _Cancel()
            self._publish(job, force=True, state="running", text="Starting")
            self._wait_for_older(job)
            hit = self._reuse(job)
            if hit is not None:
                path, text = hit
            else:
                self._check_start(job)
                path, text = self._fetch(job, tmp), "Done"
                if job.dest == "cache":
                    self._auto_prune()
            self._finish(job, "done", path=str(path), text=text)
        except _Cancel:
            keep = True
            self._finish(job, "cancelled", "cancelled", text="Cancelled")
        except _Fail as e:
            keep = e.code in ("network", "timeout", "rate_limited")      # a retry can resume these
            self._finish(job, "error", e.code, e.msg)
        except OSError as e:
            self._finish(job, "error", *_write_error(e))
        except Exception:
            log.exception("download %s", item.get("url"))
            self._finish(job, "error", "failed", ERROR_MESSAGES["failed"])
        finally:
            if not keep:
                _rmtree(tmp)

    def _wait_for_older(self, job: _Job) -> None:
        """Two jobs for one item (a play and a save) take turns, so the second one finds the first one's file."""
        while True:
            with self._lock:
                older = [j for j in self._live.values()
                         if j is not job and j.n < job.n and not j.finished and j.mode == job.mode
                         and j.item["provider"] == job.item["provider"] and j.item["id"] == job.item["id"]]
            if not older:
                return
            if job.ctl.cancelled:
                raise _Cancel()
            older[0].done.wait(0.25)

    def _target_dir(self, job: _Job) -> Path:
        if job.dest == "library":
            return self.library_dir(job.item["provider"])
        return self.media_dir / item_key(job.item, job.mode)

    def _reuse(self, job: _Job):
        """(path, text) when the item is already in the cache or the library."""
        it, mode = job.item, job.mode
        cache, lib = self.media_dir / item_key(it, mode), self.library_dir(it["provider"])
        if job.dest == "library":
            hit = find_media(lib, it, mode)
            if hit:
                return hit, "Already saved"
            hit = find_media(cache, it, mode)
            if hit:
                self._publish(job, force=True, text="Saving")
                return self._copy_to(job, hit, lib), "Saved"
            return None
        hit = find_media(lib, it, mode)           # the saved copy first: the cache may be emptied under a playlist
        if hit:
            return hit, "Already saved"
        hit = find_media(cache, it, mode)
        if hit:
            for p in (hit, hit.with_name(hit.name + SIDECAR)):
                try:
                    os.utime(p)                                   # most recently used, for pruning
                except OSError:
                    pass
            return hit, "From the cache"
        return None

    def _copy_to(self, job: _Job, src: Path, folder: Path) -> Path:
        self._mkdir(folder)
        if _free_bytes(folder) < MIN_FREE_BYTES + src.stat().st_size:
            raise _Fail("disk_full")
        dst, n = folder / src.name, 1
        while dst.exists():
            n += 1
            dst = folder / f"{src.stem} ({n}){src.suffix}"
        part = dst.with_name(dst.name + ".part")
        try:
            shutil.copy2(src, part)
            plat_fs.replace(part, dst)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise _Fail(*_write_error(e)) from None
        write_sidecar(dst, job.item, job.mode)
        return dst

    def _check_start(self, job: _Job) -> None:
        it = job.item
        if it["is_live"]:
            raise _Fail("unavailable", _LIVE)
        if it["kind"] in ("playlist", "channel"):
            raise _Fail("unsupported_url", "That is a list. Open it and pick a track.")
        try:
            url = normalize_url(it["url"])
        except ValueError as e:
            raise _Fail("unsupported_url", str(e)) from None
        if not is_direct_audio(url):
            if self._ytdlp_bin() is None:
                raise _Fail("no_ytdlp")
            if not ffmpeg_path():
                raise _Fail("no_ffmpeg")
        cap = self._cfg.get("max_minutes")
        if job.mode == "audio" and job.dest == "cache" and cap and it["duration"] and it["duration"] > cap * 60:
            mins = int(it["duration"] // 60)
            raise _Fail("too_large", f"This is {mins} minutes long, about {memory_cost_mb(it['duration'])} MB "
                                     f"in memory. AudioCrate plays up to {cap} minutes from a link. Save it to your "
                                     f"library instead, or raise Longest audio in Settings.")
        if _free_bytes(self._target_dir(job)) < MIN_FREE_BYTES:
            raise _Fail("disk_full", "Less than 500 MB is free.")

    def _fetch(self, job: _Job, tmp: Path) -> Path:
        target = self._mkdir(self._target_dir(job))
        url = normalize_url(job.item["url"])
        if is_direct_audio(url):
            return self._fetch_direct(job, url, target, tmp)
        return self._fetch_ytdlp(job, url, target, tmp)

    def _pace(self, job: _Job) -> None:
        """In a big batch from one site, start jobs a few seconds apart so the site does not push back."""
        prov = job.item["provider"]
        with self._lock:
            busy = sum(1 for j in self._live.values() if j.item["provider"] == prov and not j.finished)
        if busy < BATCH_SIZE:
            return
        with self._pace_lock:
            now = time.monotonic()
            wait = max(0.0, self._last_start.get(prov, 0.0) + BATCH_GAP - now)
            self._last_start[prov] = now + wait
        if wait and job.ctl.event.wait(wait):
            raise _Cancel()

    # fetching with yt-dlp

    def _download_args(self, job: _Job, target: Path, tmp: Path, tweaks: set, token: str) -> list[str]:
        s, it, mode = self._cfg.all(), job.item, job.mode
        ff = ffmpeg_path()
        multi = re.search(r"#(\d+)$", it["id"]) if it["provider"] == "x" else None
        a = self._base()
        a += ["--yes-playlist", "--playlist-items", multi.group(1)] if multi else ["--no-playlist"]
        a += ["--max-filesize", f"{s['max_file_mb']}M", "--match-filters", "!is_live"]
        if s["rate_limit"]:
            a += ["--limit-rate", s["rate_limit"]]
        if ff:
            a += ["--ffmpeg-location", ff]
        # the uploader is cut to 50 bytes before " - " is added, so a long name still has its separator
        name = ("%(title).150B [%(display_id,id).40B].%(ext)s" if it["provider"] == "x"
                else "%(uploader,channel|).50B%(uploader,channel& - |)s%(title).110B [%(id).40B].%(ext)s")
        a += ["-P", f"home:{target}", "-P", f"temp:{tmp}", "-o", name, "--windows-filenames"]
        a += _PROGRESS + ["--print", f"after_move:AF{token} %(filepath)s"] + _TAGS
        if ff and "noart" not in tweaks:
            a += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
            if mode == "audio" and s["square_art"]:
                a += ["--postprocessor-args", _SQUARE]
        if mode == "audio":
            fmt = s["audio_format"]
            # m4a first when keeping the source codec, so no lossy step is added; opus and mp3
            # start from the best stream instead
            a += ["-f", "ba[ext=m4a]/ba/b" if fmt in ("best", "m4a") else "ba/b", "-x", "--audio-format", fmt]
            if fmt == "mp3":
                a += ["--audio-quality", "0"]
        else:
            h = s["video_height"]
            a += ["-f", f"bv*[height<=?{h}]+ba/b[height<=?{h}]/b", "-S", "vcodec:h264,res,acodec:aac",
                  "--merge-output-format", "mp4", "--concurrent-fragments", "3"]
        return a + self._tweak_args(tweaks)

    def _fetch_ytdlp(self, job: _Job, url: str, target: Path, tmp: Path) -> Path:
        prov = PROVIDERS.get(job.item["provider"], LINK)
        self._pace(job)
        tweaks: set = set()
        while True:
            path, code, msg, text = self._attempt(job, url, target, tmp, tweaks)
            if path is not None:
                return path
            if job.ctl.cancelled:
                raise _Cancel()
            nxt = self._next_tweak(prov, code, text, tweaks, art=True)
            if nxt is None:
                log.info("fetch failed (%s): %s", code, text.strip().splitlines()[-1:] or "")
                raise _Fail(code, msg)
            tweaks.add(nxt)
            self._publish(job, force=True, state="running", percent=0.0, text="Trying another way")

    def _attempt(self, job: _Job, url: str, target: Path, tmp: Path, tweaks: set):
        """One run of yt-dlp. Returns (path or None, error code, message, last output)."""
        exe = self._ytdlp_bin()
        self._mkdir(tmp)
        token = secrets.token_hex(8)                # yt-dlp is told it, so only yt-dlp can name the result
        cargs, done = self._cookie_args(job.item["provider"]) if "cookies" in tweaks else ([], _noop)
        args = [exe] + self._download_args(job, target, tmp, tweaks, token) + cargs + ["--", url]
        try:
            proc = _spawn(args, merge=True, cwd=str(tmp), tmpdir=self._proc_tmp(), private=done.dir)
        except OSError:
            done(False)
            return None, "no_ytdlp", ERROR_MESSAGES["no_ytdlp"], ""
        job.ctl.attach(proc)
        path, tail, stalled = None, [], False
        try:
            path, tail, stalled = self._read(job, proc, token)
        finally:
            _stop([proc], (plat_proc.TERM, plat_proc.KILL), 1.0)          # anything it left behind
            proc.stdout.close()
            done(path is not None and proc.returncode == 0)
        text = "\n".join(tail)
        if job.ctl.cancelled:
            return None, "cancelled", ERROR_MESSAGES["cancelled"], text
        if path and proc.returncode == 0:
            found = Path(path)
            if not _inside(found, target):          # not ours to use, and not ours to delete either
                return None, "write_failed", "The file ended up in an unexpected place, so AudioCrate did not use it.", text
            if found.is_file():
                write_sidecar(found, job.item, job.mode, job.state["quality"])
                return found, "", "", text
        if stalled:
            return None, "timeout", ERROR_MESSAGES["timeout"], text
        code, msg = classify_error(tail, proc.returncode)
        return None, code, msg, text

    def _read(self, job: _Job, proc: subprocess.Popen, token: str = ""):
        """Read yt-dlp's output until it ends. Returns (final path or None, last lines, stalled)."""
        reader = plat_proc.PipeReader(proc.stdout)
        prog = _Progress(2 if job.mode == "video" else 1)
        buf, tail, path = b"", deque(maxlen=60), None
        last, stalled = time.monotonic(), False

        def line_in(raw: bytes) -> None:
            nonlocal path
            line = _text(raw).rstrip()
            if not line:
                return
            tail.append(line)
            kind, val = parse_output_line(line, token)
            if kind == "AC":
                self._on_progress(job, prog, val)
            elif kind == "AP":
                self._on_post(job, val)
            elif kind == "AQ":
                self._publish(job, quality=quality_label(val, job.mode))
            elif kind == "AF":
                path = val

        try:
            while True:
                ready = reader.wait(1.0)
                if job.ctl.cancelled:
                    break
                if not ready:
                    if proc.poll() is not None:             # gone, but something else holds the pipe
                        break
                    if job.state["state"] == "running" and time.monotonic() - last > STALL_TIMEOUT:
                        stalled = True
                        break
                    continue
                chunk = ready[0][1]
                if not chunk:
                    break
                last = time.monotonic()
                *lines, buf = (buf + chunk).replace(b"\r", b"\n").split(b"\n")
                if len(buf) > 1 << 20:
                    buf = b""
                for raw in lines:
                    line_in(raw)
            if buf:
                line_in(buf)
        finally:
            reader.close()
        if stalled:
            _stop([proc])
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            _stop([proc])
        return path, list(tail), stalled

    def _on_progress(self, job: _Job, prog: _Progress, d: dict) -> None:
        pct, speed, eta = prog.update(d)
        got = d.get("downloaded_bytes") or 0
        bits = []
        if pct is not None:
            bits.append(f"{pct:.0f}%")
        else:
            bits.append(f"Downloading {human_bytes(got)}")
        if speed:
            bits.append(f"{human_bytes(speed)}/s")
        if eta is not None and eta >= 0:
            bits.append(f"{human_eta(eta)} left")
        fields = {"state": "running", "speed": f"{human_bytes(speed)}/s" if speed else "",
                  "eta": human_eta(eta) if eta is not None else "", "speed_bps": float(speed or 0),
                  "eta_s": int(eta) if eta is not None and eta >= 0 else -1, "text": ", ".join(bits)}
        fields["indeterminate"] = pct is None             # no total known: the bar should just pulse
        if pct is not None:
            fields["percent"] = pct
        self._publish(job, **fields)

    def _on_post(self, job: _Job, d: dict) -> None:
        name = d.get("postprocessor")
        if d.get("status") != "started" or name in ("MetadataParser", "ThumbnailsConvertor"):
            return                                          # these run before the download starts
        self._publish(job, force=True, state="postprocessing", speed="", eta="", speed_bps=0.0, eta_s=-1,
                      percent=99.0, indeterminate=False, text=_POST_NAMES.get(name, "Finishing"))

    # fetching a direct link

    def _fetch_direct(self, job: _Job, url: str, target: Path, tmp: Path) -> Path:
        """A link to an audio file: plain HTTP with the same limits, no yt-dlp needed."""
        limit = self._cfg.get("max_file_mb") * 1024 * 1024
        path = Path(unquote(urlsplit(url).path))
        tag = job.item["id"].split(":", 1)[-1]
        final = target / f"{_safe_name(path.stem, 100)} [{tag}]{path.suffix.lower()}"
        part = final.with_name(final.name + ".part")          # next to where it ends up, so the rename never crosses a disk
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "audio/*, */*;q=0.5"})

        def work(stop, live):
            got = 0
            try:
                with _opener().open(req, timeout=15) as r:
                    live.append(r)
                    if r.headers.get_content_type() in ("text/html", "application/xhtml+xml", "application/json"):
                        raise _Fail("no_media", "That link is a web page, not an audio file.")
                    total = int(r.headers.get("Content-Length") or 0)
                    if total > limit:
                        raise _Fail("too_large")
                    t0 = time.monotonic()
                    try:
                        f = open(part, "wb")
                    except OSError as e:
                        raise _Fail(*_write_error(e)) from None
                    with f:
                        while True:
                            if stop.is_set():
                                raise _Cancel()
                            chunk = r.read(65536)
                            if not chunk:
                                break
                            got += len(chunk)
                            if got > limit:
                                raise _Fail("too_large")
                            try:
                                f.write(chunk)
                            except OSError as e:
                                raise _Fail(*_write_error(e)) from None
                            speed = got / max(0.001, time.monotonic() - t0)
                            pct = min(99.0, 100.0 * got / total) if total else None
                            self._publish(job, state="running", speed=f"{human_bytes(speed)}/s", speed_bps=speed,
                                          text=(f"{pct:.0f}%, " if pct is not None else "") + f"{human_bytes(speed)}/s",
                                          **({"percent": pct} if pct is not None else {}))
                    if total and got < total:
                        raise _Fail("network", "The connection closed before the whole file arrived.")
                if stop.is_set():
                    raise _Cancel()
                try:
                    plat_fs.replace(part, final)
                    write_sidecar(final, job.item, job.mode)
                except OSError as e:
                    raise _Fail(*_write_error(e)) from None
            except (_Fail, _Cancel):
                part.unlink(missing_ok=True)
                raise
            except urllib.error.HTTPError as e:
                part.unlink(missing_ok=True)
                code, msg = classify_error(f"ERROR: {e}")
                if code == "failed":
                    code, msg = "refused", f"The site refused to send this file ({e.code} {e.reason})."
                raise _Fail(code, msg) from None
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
                part.unlink(missing_ok=True)
                code, msg = classify_error(f"ERROR: {e}")
                if code == "failed":
                    code, msg = ("refused", "The site sent something AudioCrate could not read.") if isinstance(e, ValueError) \
                        else ("network", ERROR_MESSAGES["network"])
                raise _Fail(code, msg) from None
            return final

        return _interruptible(job.ctl, work)

    # cache

    def _cache_entries(self) -> list:
        """(last used, bytes, folder) for every cached item."""
        out = []
        try:
            dirs = [e.path for e in os.scandir(self.media_dir) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return out
        for d in dirs:
            size, used = 0, 0.0
            try:
                for f in os.scandir(d):
                    st = f.stat(follow_symlinks=False)
                    size += st.st_size
                    if not f.name.endswith(SIDECAR):
                        used = max(used, st.st_mtime)
                out.append((used or os.stat(d).st_mtime, size, Path(d)))
            except OSError:
                continue
        return out

    def cache_info(self) -> dict:
        entries = self._cache_entries()
        other = sum(_dir_size(p) for p in (self.tmp_dir, self.thumbs_dir) if p.is_dir())
        return {"bytes": sum(e[1] for e in entries) + other, "files": len(entries), "path": str(self.cache_dir)}

    def _in_use(self, protect) -> set:
        """The cache folders that must not be pruned: the ones that hold what is playing or queued (protect),
        what is being fetched and what just finished. A set, because a long queue is thousands of files."""
        root = self.media_dir.resolve()
        keep = set()

        def hold(p) -> None:
            try:
                rel = Path(p).resolve().relative_to(root)
            except (OSError, TypeError, ValueError):
                return                                       # not in the cache, so nothing here to protect
            if rel.parts:
                keep.add(root / rel.parts[0])

        for p in protect if isinstance(protect, (list, tuple, set, frozenset)) else ():
            hold(p)
        now = time.monotonic()
        with self._lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            if not j.finished:
                keep.add(root / item_key(j.item, j.mode))
            elif j.state["path"] and now - j.finished_at < 900:
                hold(j.state["path"])
        return keep

    def prune_cache(self, max_bytes: int | None = None, protect=()) -> int:
        """Delete the least recently used cached items until the cache fits. Returns bytes freed.

        max_bytes defaults to the cache_max_mb setting, and 0 empties it. Files in
        protect (the one that is playing), items being fetched and items that finished in
        the last fifteen minutes are never deleted. Leftovers of cancelled fetches go
        after two days, thumbnails first, since they are cheap to get again.
        """
        limit = self._cfg.get("cache_max_mb") * 1024 * 1024 if max_bytes is None else _clamp(max_bytes, 0, 1 << 50, 0)
        keep = self._in_use(protect)
        freed = 0
        if self.thumbs_dir.is_dir() and (limit == 0 or _dir_size(self.thumbs_dir) > 100 * 1024 * 1024):
            freed += self._prune_files(self.thumbs_dir, 0 if limit == 0 else 50 * 1024 * 1024)
        entries = sorted(self._cache_entries())
        total = sum(e[1] for e in entries)
        for _used, size, folder in entries:
            if total <= limit:
                break
            if folder.resolve() in keep:
                continue
            _rmtree(folder)
            total -= size
            freed += size
        return freed + self._prune_tmp(everything=limit == 0)

    def _prune_tmp(self, everything: bool) -> int:
        """Partial fetches that nobody resumed, and cookie copies a crash left behind."""
        active = {item_key(j.item, j.mode) for j in list(self._jobs.values()) if not j.finished}
        try:
            dirs = [e for e in os.scandir(self.tmp_dir) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return 0
        freed, now = 0, time.time()
        try:                                                  # what a killed standalone yt-dlp unpacked
            dirs += [e for e in os.scandir(self.tmp_dir / "proc") if e.is_dir(follow_symlinks=False)
                     and now - e.stat().st_mtime > 86400]
        except OSError:
            pass
        for e in dirs:
            if e.name == "proc" or e.name in active:
                continue
            try:
                age = now - e.stat().st_mtime
                m = re.match(r"jar_(\d+)_", e.name)
                if m:                                         # a private cookie folder of this or another process
                    gone = not plat_proc.is_alive(int(m.group(1)))
                elif "-" in e.name:                           # a partial fetch, named by item_key
                    gone = everything or age > 2 * 86400
                else:                                         # a cookie folder from before they were named by process
                    gone = age > 3600
                if gone:
                    freed += _dir_size(Path(e.path))
                    _rmtree(e.path)
            except OSError:
                pass
        return freed

    @staticmethod
    def _prune_files(folder: Path, keep_bytes: int) -> int:
        """Delete the oldest files of a flat folder until at most keep_bytes remain."""
        files = []
        for root, _d, names in os.walk(folder):
            for n in names:
                p = os.path.join(root, n)
                try:
                    st = os.lstat(p)
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass
        total, freed = sum(f[1] for f in files), 0
        for _m, size, p in sorted(files):
            if total <= keep_bytes:
                break
            try:
                os.unlink(p)
                total -= size
                freed += size
            except OSError:
                pass
        return freed

    def _auto_prune(self) -> None:
        try:
            self.prune_cache(None, self.protect() if self.protect else ())
        except Exception:
            log.exception("pruning the online cache")

    # accounts

    def cookie_source(self, provider_id: str) -> dict:
        src = (self._cfg.get("cookies") or {}).get(provider_id) if isinstance(provider_id, str) else None
        src = src or {}
        return {"kind": src.get("kind") if src.get("kind") in ("browser", "file") else "none",
                "browser": str(src.get("browser") or ""), "profile": str(src.get("profile") or ""),
                "file": str(src.get("file") or "")}

    def _cookies_available(self, provider_id: str) -> bool:
        s = self.cookie_source(provider_id)
        return s["kind"] == "browser" or (s["kind"] == "file" and Path(s["file"]).is_file())

    def set_cookie_source(self, provider_id: str, kind: str, browser: str = "", profile: str = "",
                          file: str = "") -> None:
        """Choose where a provider's cookies come from: 'none', 'browser' or 'file'.

        Raises ValueError, with a sentence the UI can show, for anything it will not accept.
        A cookies file is copied into AudioCrate's own folder with only that site's lines in it.
        """
        prov = _provider(provider_id)
        if prov is None:
            raise ValueError("Unknown site.")
        entry = {"kind": "none", "browser": "", "profile": "", "file": ""}
        if kind == "browser":
            if browser not in BROWSERS:
                raise ValueError("The browser is one of " + ", ".join(BROWSERS) + ".")
            profile = (profile or "").strip()
            named = _PROFILE.fullmatch(profile) and ".." not in profile
            folder = os.path.isabs(profile) and ":" not in profile and os.path.isdir(profile)
            if profile and not (named or folder):
                raise ValueError("That profile name is not allowed. Use letters, digits, spaces, dots, "
                                 "dashes and underscores, or the full path of the profile folder.")
            entry.update(kind="browser", browser=browser, profile=profile)
        elif kind == "file":
            entry.update(kind="file", file=str(self._store_cookies(prov, file)))
        elif kind != "none":
            raise ValueError("Unknown way to sign in.")
        if kind != "file":
            (self.cookie_dir / f"{prov.id}.txt").unlink(missing_ok=True)
        cookies = self._cfg.get("cookies") or {}
        cookies[provider_id] = entry
        self._cfg.set("cookies", cookies)
        self._later(self.login_changed.emit, provider_id)

    def forget_login(self, provider_id: str) -> None:
        if _provider(provider_id):
            self.set_cookie_source(provider_id, "none")

    def _store_cookies(self, prov: Provider, file) -> Path:
        src = Path(os.path.expanduser(str(file or "")))
        try:
            if src.stat().st_size > _COOKIE_MAX:
                raise ValueError("That file is too big to be a cookies file.")
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise ValueError("That file could not be read.") from None
        body, n = filter_cookies(text, prov.cookie_domains)
        if not n:
            what = f"{prov.name} cookies" if prov.cookie_domains else "cookies"
            raise ValueError(f"No {what} were found in that file. It has to be in the cookies.txt format "
                             f"that browser extensions export.")
        sites = cookie_sites(body)
        if not prov.cookie_domains and len(sites) > LINK_SITES:
            raise ValueError(f"That cookies file holds cookies for {len(sites)} sites. {prov.name} keeps at most "
                             f"{('no', 'one', 'two', 'three', 'four')[min(LINK_SITES, 4)]} sites, so export only the ones you need.")
        self._mkdir(self.cookie_dir, 0o700)
        dst = self.cookie_dir / f"{prov.id}.txt"
        _atomic_write(dst, body, 0o600)
        return dst

    def _cookie_args(self, provider_id: str, tool: str = "ytdlp"):
        """(arguments, done) for the provider's cookies. done(ok) cleans up, and keeps refreshed cookies when ok.

        Each run gets its own copy of a cookies file, because yt-dlp rewrites the file it is
        given when it exits and two runs would trample each other.
        """
        src = self.cookie_source(provider_id)
        if src["kind"] == "browser":
            spec = src["browser"] + (":" + src["profile"] if src["profile"] else "")
            return ["--cookies-from-browser", spec], _noop
        if src["kind"] == "file" and Path(src["file"]).is_file():
            work = self._private_dir()
            jar = work / "cookies.txt"
            try:
                shutil.copyfile(src["file"], jar)
                os.chmod(jar, 0o600)
            except OSError:
                _rmtree(work)
                return [], _noop

            def done(ok: bool = False) -> None:
                if ok:
                    self._keep_cookies(provider_id, jar)
                _rmtree(work)

            done.dir = work                          # the guard of the run removes it too, if this process disappears
            return ["-C" if tool == "gdl" else "--cookies", str(jar)], done
        return [], _noop

    def _keep_cookies(self, provider_id: str, jar: Path) -> None:
        prov = _provider(provider_id)
        try:
            stored = self.cookie_dir / f"{provider_id}.txt"
            sites = None if prov.cookie_domains else cookie_sites(stored.read_text(encoding="utf-8", errors="replace"))
            body, n = filter_cookies(jar.read_text(encoding="utf-8", errors="replace"), prov.cookie_domains, sites)
            if n:
                _atomic_write(stored, body, 0o600)
        except (OSError, AttributeError):
            pass

    def check_login(self, provider_id: str) -> int:
        """Look at the cookies and, when they look signed in, ask the site once.

        Ends in results_ready with info logged_in (True, False, or None when it cannot be
        told), name (always empty), cookies_found and a plain note.
        """
        rid, req = self._begin()
        prov = _provider(provider_id)
        if prov is None:
            return self._reject(rid, "unsupported_url", "Unknown site.")
        self._submit_list(rid, req, partial(self._do_check, rid, req, prov))
        return rid

    def _do_check(self, rid: int, req: _Req, prov: Provider) -> None:
        try:
            src = self.cookie_source(prov.id)
            names, note, signed = set(), "", None
            if src["kind"] == "none":
                signed, note = False, "Not signed in."
            else:
                names, why = self._jar_names(req.ctl, prov, src)
                signed = jar_signed_in(prov.id, names)
                if req.ctl.cancelled:
                    self._fail(rid, "cancelled")
                    return
                if not names:
                    signed, note = False, why or "No cookies for this site were found. Sign in in the browser first."
                elif signed is False:
                    note = "The cookies do not hold a signed in session. Sign in in the browser, then check again."
                elif signed is None:
                    note = "Cookies found."
                else:
                    signed, note = self._ask_site(req.ctl, prov)
            if req.ctl.cancelled:
                self._fail(rid, "cancelled")
                return
            info = {"title": "Sign in check", "count": 0, "more": False, "source": "login", "query": prov.id,
                    "logged_in": signed, "name": "", "cookies_found": bool(names), "note": note}
            self._ok(rid, [], info)
        except Exception:
            log.exception("checking the login of %s", prov.id)
            self._fail(rid, "failed")
        finally:
            self._end(rid)

    def _jar_names(self, ctl: _Ctl, prov: Provider, src: dict):
        """(cookie names of this site, a note when there are none)."""
        if src["kind"] == "file":
            try:
                text = Path(src["file"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return set(), "The saved cookies file is missing. Choose it again."
            return cookie_names(text, prov.cookie_domains), ""
        exe = self._ytdlp_bin()
        if exe is None:
            return set(), ERROR_MESSAGES["no_ytdlp"]
        work = self._private_dir()
        jar = work / "jar.txt"
        try:
            # Made by us so it is private (yt-dlp would make it readable by everyone), and with the
            # header line because yt-dlp refuses to load a cookies file that is empty.
            with os.fdopen(os.open(jar, os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
            spec = src["browser"] + (":" + src["profile"] if src["profile"] else "")
            # A closed local port: yt-dlp loads the browser's cookies, fails to fetch, and still saves them.
            args = [exe] + self._base() + ["--cookies-from-browser", spec, "--cookies", str(jar), "-s",
                                           "--", "http://127.0.0.1:9/x"]
            _rc, _out, err, _timed = _capture(args, 40, ctl, cwd=self._cwd(), tmpdir=self._proc_tmp(), private=work)
            text = jar.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set(), "The browser's cookies could not be read."
        finally:
            _rmtree(work)
        names = cookie_names(text, prov.cookie_domains)
        if names:
            return names, ""
        if re.search(r"could not find .{0,40}cookies database|does not exist", err):
            return names, "Could not find that browser or profile."
        if re.search(r"could not be decrypted|Failed to decrypt|keyring|wallet", err, re.I):
            return names, "The browser's cookies could not be decrypted. Open the keyring or wallet, then try again."
        return names, ""

    def _ask_site(self, ctl: _Ctl, prov: Provider):
        """One request with the cookies. (True, note), (False, note) or (None, note) when it cannot be told."""
        if prov.id == "youtube":
            data, code, msg, _ = self._ytdlp_json(ctl, prov, ["--flat-playlist", "--dump-single-json",
                                                              "--playlist-end", "1", "--yes-playlist"],
                                                  ":ytsubs", cookies=True)
            if data is not None:
                return True, "Signed in."
            if code == "login_required":
                return False, "YouTube does not accept these cookies. They may have expired. Sign in again in the browser."
            return None, "Cookies found, but YouTube could not be asked: " + msg
        if prov.id == "x" and self._gdl_bin():
            out, code, msg = self._gdl_run(ctl, "https://x.com/i/bookmarks", 1)
            if out is not None:
                return True, "Signed in."
            if code == "login_required":
                return False, "X does not accept these cookies. They may have expired. Sign in again in the browser."
            return None, "Cookies found, but X could not be asked: " + msg
        return None, "Cookies found."

    # gallery-dl

    def _gdl_bin(self) -> str | None:
        env = os.environ.get("AUDIOCRATE_GALLERY_DL")
        if env:
            return env
        found = shutil.which("gallery-dl", path=plat_proc.child_env()["PATH"])
        if found:
            return found
        extra = Path.home() / ".local" / "python311" / "bin" / "gallery-dl"
        return str(extra) if extra.is_file() else None

    def _gdl_run(self, ctl: _Ctl, url: str, n: int):
        """gallery-dl's output for a link. Returns (output or None, error code, message)."""
        exe = self._gdl_bin()
        if exe is None:
            return None, "failed", "This needs gallery-dl, which is not installed. Install it with pip install gallery-dl."
        cargs, done = self._cookie_args("x", "gdl")
        args = [exe, "--config-ignore", "--no-input", "-j", "-o", "output.jsonl=true", "--range", f"1-{n}",
                "-R", "2", "--http-timeout", "15", "--sleep-request", "1.0", "-o", "ratelimit=abort",
                "-o", "extractor.twitter.retweets=false"] + cargs + ["--", url]
        ok = False
        try:
            rc, out, err, timed = _capture(args, 120, ctl, cwd=self._cwd(), tmpdir=self._proc_tmp(), private=done.dir)
            if ctl.cancelled:
                return None, "cancelled", ERROR_MESSAGES["cancelled"]
            if timed:
                return None, "timeout", ERROR_MESSAGES["timeout"]
            problem = re.search(r'^\[-1,.*$', out, re.M)
            if rc == 0 and not problem:
                ok = True
                return out, "", ""
            blob = (problem.group(0) if problem else "") + "\n" + err
            if re.search(r"auth|log ?in|credential|cookies|401|403", blob, re.I):
                return None, "login_required", "X did not accept the cookies. Sign in again in the browser."
            if re.search(r"429|rate.?limit", blob, re.I):
                return None, "rate_limited", ERROR_MESSAGES["rate_limited"]
            if re.search(r"404|not ?found", blob, re.I):
                return None, "unavailable", ERROR_MESSAGES["unavailable"]
            return None, "failed", "gallery-dl could not read this. Updating gallery-dl often fixes it."
        except OSError:
            return None, "failed", "gallery-dl could not be started."
        finally:
            done(ok)

    def _do_gdl(self, rid: int, req: _Req, url: str, n: int, label: str) -> None:
        try:
            if req.ctl.cancelled:
                self._fail(rid, "cancelled")
                return
            if not self._cookies_available("x"):
                self._fail(rid, "login_required")
                return
            out, code, msg = self._gdl_run(req.ctl, url, n)
            if out is None:
                self._fail(rid, code, msg)
                return
            items = parse_gdl(out)
            self._ok(rid, items, {"title": label or url, "count": len(items), "more": len(items) >= n,
                                  "source": "feed" if label else "link", "query": label or url,
                                  "kind": "playlist", "url": url, "uploader": "", "thumbnail": ""})
        except Exception:
            log.exception("gallery-dl %s", url)
            self._fail(rid, "failed")
        finally:
            self._end(rid)

    # tools

    def tools(self) -> dict:
        """What is installed: ytdlp, ffmpeg, js and gallery_dl, each with path, version, ok and a note.

        Returns at once. The first call starts the probe and tools_changed fires when it is done.
        """
        self._ensure_probe()
        with self._lock:
            t = self._tools
        if t is not None:
            return copy.deepcopy(t)
        exe = self._ytdlp_bin()
        quick = {"path": "", "version": "", "ok": False, "note": "Checking."}
        return {"ytdlp": {**quick, "path": exe or "", "age_days": None, "owned_by_system": False, "owned": False,
                          "can_update": True, "stale": False, "channel": self._cfg.get("channel"),
                          "checking": True},
                "ffmpeg": dict(quick), "js": {**quick, "name": "", "found": []}, "gallery_dl": dict(quick)}

    def refresh_tools(self) -> None:
        """Look again, for example after the user installed something. tools_changed fires when done."""
        self._ensure_probe(force=True)

    def _ensure_probe(self, force: bool = False) -> None:
        with self._lock:
            if self._closed or self._probing or (self._tools is not None and not force):
                return
            self._probing = True
        threading.Thread(target=self._probe_tools, daemon=True).start()

    def _probe_tools(self) -> None:
        try:
            tools = {"ytdlp": self._probe_ytdlp(), "ffmpeg": self._probe_ffmpeg(), "js": self._probe_js(),
                     "gallery_dl": self._probe_gdl()}
        except Exception:
            log.exception("probing tools")
            with self._lock:
                self._probing = False
            return
        with self._lock:
            self._tools, self._probing = tools, False
        self._later(self.tools_changed.emit)

    def _version_of(self, args: list) -> str:
        try:
            cwd, tmp = self._cwd(), self._proc_tmp()
        except OSError:                           # a cache folder that cannot be written must not hide an installed tool
            cwd = tmp = None
        try:
            rc, out, _err, _timed = _capture(args, 15, cwd=cwd, tmpdir=tmp)
        except OSError:
            return ""
        return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""

    def _probe_ytdlp(self) -> dict:
        owned = str(self.bin_dir / "yt-dlp")
        env = os.environ.get("AUDIOCRATE_YTDLP")
        cands = [env] if env else []
        if not env:
            for c in (owned, shutil.which("yt-dlp", path=plat_proc.child_env()["PATH"])):
                if c and c not in cands and Path(c).is_file():
                    cands.append(c)
        found = [(parse_version(v), c, v) for c in cands if (v := self._version_of([c, "--version"]))]
        best = max(found, key=lambda f: (f[0], f[1] == owned)) if found else None
        path = best[1] if best else ""
        ver = best[2] if best else ""
        age = version_age_days(ver)
        return {"path": path, "version": ver, "age_days": age, "ok": bool(best),
                "owned_by_system": path.startswith(("/usr/", "/opt/", "/bin/", "/snap/")),
                "owned": path == owned, "can_update": True, "stale": age is not None and age > 90,
                "channel": self._cfg.get("channel"),
                "note": "" if best else "yt-dlp was not found. Use Update to get a copy."}

    def _probe_ffmpeg(self) -> dict:
        path = ffmpeg_path() or ""
        line = self._version_of([path, "-version"]) if path else ""
        m = re.search(r"version (\S+)", line)
        return {"path": path, "version": m.group(1) if m else "", "ok": bool(m),
                "note": "" if m else "ffmpeg was not found. Playing and saving audio from links needs it."}

    def _probe_js(self) -> dict:
        found = []
        for name, low, high, rx in _JS_RUNTIMES:
            path = shutil.which(name, path=plat_proc.child_env()["PATH"])
            if not path:
                continue
            m = re.search(rx, self._version_of([path, "--version"]))
            v = tuple(int(x) for x in m.group(1).split(".")) if m else ()
            found.append({"name": name, "path": path, "version": m.group(1) if m else "",
                          "ok": bool(v) and v >= low and (high is None or v <= high)})
        best = next((f for f in found if f["ok"]), None)
        if best:
            return {**best, "note": "", "found": found}
        if found:
            f = found[0]
            note = (f"The JavaScript runtime found ({f['name']} {f['version']}) is not usable. YouTube needs "
                    f"deno 2.3 or newer, or node 22 or newer.")
        else:
            note = "No JavaScript runtime found. YouTube needs deno 2.3 or newer, or node 22 or newer. Install deno."
        return {"name": "", "path": "", "version": "", "ok": False, "note": note, "found": found}

    def _probe_gdl(self) -> dict:
        path = self._gdl_bin() or ""
        ver = self._version_of([path, "--version"]) if path else ""
        return {"path": path, "version": ver, "ok": bool(ver),
                "note": "" if ver else "gallery-dl is not installed. It is only needed for X profiles and bookmarks."}

    # updating yt-dlp

    def update_ytdlp(self) -> int:
        """Install, or bring up to date, AudioCrate's own yt-dlp in ~/.local/share/audiocrate/bin.

        Downloads the official standalone build from yt-dlp's release page and checks its
        SHA-256 before it replaces anything. Never touches a copy the system package
        manager owns, and never uses sudo. Ends in results_ready with info updated and
        version, or request_failed.
        """
        rid, req = self._begin()
        req.run, req.slots = partial(self._do_update, rid, req), self._upd
        self._upd.submit(req.run)
        return rid

    def _do_update(self, rid: int, req: _Req) -> None:
        try:
            info = self._update(req.ctl)
            with self._lock:
                self._tools = None
            self._ensure_probe()
            self._ok(rid, [], info)
        except _Cancel:
            self._fail(rid, "cancelled")
        except _Fail as e:
            self._fail(rid, e.code, e.msg)
        except Exception:
            log.exception("updating yt-dlp")
            self._fail(rid, "failed", "yt-dlp could not be updated.")
        finally:
            self._end(rid)

    def _update(self, ctl: _Ctl) -> dict:
        asset = {"x86_64": "yt-dlp_linux", "aarch64": "yt-dlp_linux_aarch64"}.get(platform.machine())
        if asset is None:
            raise _Fail("failed", "AudioCrate can only install yt-dlp on 64 bit Linux. Use your package manager.")
        nightly = self._cfg.get("channel") == "nightly"
        base = os.environ.get("AUDIOCRATE_YTDLP_RELEASES") or (_NIGHTLY if nightly else _RELEASES)
        owned = self.bin_dir / "yt-dlp"
        have = self._version_of([str(owned), "--version"]) if owned.is_file() else ""
        tag = self._latest_tag(base, ctl)
        if have and parse_version(tag) and parse_version(tag) <= parse_version(have):
            return {"title": "yt-dlp", "count": 0, "more": False, "source": "update", "query": "",
                    "updated": False, "version": have, "message": "yt-dlp is up to date."}
        sums = self._http_get(f"{base}/latest/download/SHA2-256SUMS", 1 << 20, ctl).decode("utf-8", "replace")
        want = next((ln.split()[0].lower() for ln in sums.splitlines()
                     if len(ln.split()) == 2 and ln.split()[1].lstrip("*") == asset), "")
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            raise _Fail("failed", "The release has no checksum for this build, so it was not installed.")
        self._mkdir(self.bin_dir)
        new = self.bin_dir / ".yt-dlp.new"
        try:
            blob = self._http_get(f"{base}/latest/download/{asset}", 200 * 1024 * 1024, ctl)
            if hashlib.sha256(blob).hexdigest() != want:
                raise _Fail("failed", "The download did not match its checksum, so it was not installed.")
            fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            version = self._version_of([str(new), "--version"])
            if not parse_version(version):
                raise _Fail("failed", "The downloaded yt-dlp did not start, so it was not installed.")
            plat_fs.replace(new, owned)
        finally:
            new.unlink(missing_ok=True)
        return {"title": "yt-dlp", "count": 0, "more": False, "source": "update", "query": "",
                "updated": True, "version": version, "message": f"yt-dlp {version} is installed."}

    @staticmethod
    def _latest_tag(base: str, ctl: _Ctl) -> str:
        """The newest release tag, read from where /releases/latest redirects. No API quota used."""
        class _Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        req = urllib.request.Request(f"{base}/latest", method="HEAD", headers={"User-Agent": _UA})

        def work(stop, live):
            try:
                urllib.request.build_opener(_Stay).open(req, timeout=15).close()
                return ""
            except urllib.error.HTTPError as e:
                return (e.headers.get("Location") or "").rstrip("/").rsplit("/", 1)[-1] if 300 <= e.code < 400 else ""
            except (urllib.error.URLError, OSError):
                raise _Fail("network") from None

        return _interruptible(ctl, work)

    def _http_get(self, url: str, cap: int, ctl: _Ctl) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})

        def work(stop, live):
            buf = bytearray()
            try:
                with _opener().open(req, timeout=20) as r:
                    live.append(r)
                    while not stop.is_set():
                        chunk = r.read(65536)
                        if not chunk:
                            return bytes(buf)
                        buf += chunk
                        if len(buf) > cap:
                            raise _Fail("too_large", "The download is bigger than expected, so it was stopped.")
                raise _Cancel()
            except urllib.error.HTTPError as e:
                code, msg = classify_error(f"ERROR: {e}")
                raise _Fail(code, msg) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                raise _Fail("network") from None

        return _interruptible(ctl, work)

    # shutdown

    def shutdown(self) -> None:
        """Cancel everything and stop every child process group this process started. Blocks briefly."""
        if self._closed:
            return
        with self._lock:
            ctls = [j.ctl for j in self._jobs.values()] + [r.ctl for r in self._reqs.values()]
        self._closed = True
        slots = (self._lists, self._dl, self._upd)
        for s in slots:
            s.close()
        for c in ctls:
            c.event.set()
        _stop([c.proc for c in ctls], (plat_proc.INT, plat_proc.KILL), 0.15)      # the window is closing: be quick
        for s in slots:
            s.join(5)
        _kill_children()

