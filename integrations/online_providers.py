"""Sites the Online panel knows by name, and the link clean-up that goes with them.

No Qt in here, so it can be imported and tested on its own. A provider is only a
label, a few hosts and the feeds that make sense for it. Anything yt-dlp can read
that is not listed falls under 'link'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

MAX_URL = 2048

ACCOUNT_NOTE = ("Using an account with a downloader can get it limited or banned. "
                "A spare account is safer.")


@dataclass(frozen=True)
class Feed:
    id: str            # 'search', 'subscriptions', 'liked', ...
    label: str         # shown on a chip, sentence case
    target: str        # URL or yt-dlp pseudo-URL; '' for search
    needs_login: bool


@dataclass(frozen=True)
class Provider:
    id: str            # 'youtube', 'x', 'link'
    name: str          # 'YouTube', 'X', 'Any link'
    glyph: str         # Nerd Font glyph used in the UI
    hosts: tuple[str, ...]        # registrable domains; subdomains match
    search_prefix: str | None     # 'ytsearch'; None when search is not offered
    feeds: tuple[Feed, ...]
    cookie_domains: tuple[str, ...]
    login_note: str    # one plain sentence shown next to the sign in controls
    save_policy: str = "ok"       # 'ok', 'ask' or 'no'; for sites that license streaming only


YOUTUBE = Provider(
    id="youtube", name="YouTube", glyph="\U000f05c3",
    hosts=("youtube.com", "youtu.be", "youtube-nocookie.com"),
    search_prefix="ytsearch",
    feeds=(
        Feed("search", "Search", "", False),
        Feed("subscriptions", "Subscriptions", ":ytsubs", True),
        Feed("liked", "Liked videos", ":ytfav", True),
        Feed("watchlater", "Watch later", ":ytwatchlater", True),
        Feed("history", "History", ":ythis", True),
        Feed("recommended", "Recommended", ":ytrec", True),
    ),
    cookie_domains=("youtube.com",),
    login_note=("Signing in is optional. It gives access to your subscriptions, likes and "
                "history, and to age-restricted videos."),
)

X = Provider(
    id="x", name="X", glyph="\U000f0544",
    hosts=("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com",
           "fixvx.com", "twittpr.com"),
    search_prefix=None,                       # yt-dlp has no X search
    feeds=(Feed("bookmarks", "Bookmarks", "https://x.com/i/bookmarks", True),),
    cookie_domains=("x.com", "twitter.com"),
    login_note="Public posts play without signing in. Sensitive posts and protected accounts need your X session.",
)

LINK = Provider(
    id="link", name="Any link", glyph="\U000f0337",
    hosts=(), search_prefix=None, feeds=(), cookie_domains=(),
    login_note="Some sites need a signed in session. Choose a cookies file in Accounts.",
)

PROVIDERS: dict[str, Provider] = {p.id: p for p in (YOUTUBE, X, LINK)}

# Mirrors of the same post that all point at x.com once cleaned up.
_X_ALIASES = {"twitter.com", "mobile.twitter.com", "www.twitter.com", "m.twitter.com",
              "x.com", "www.x.com", "mobile.x.com", "fxtwitter.com", "vxtwitter.com",
              "fixupx.com", "fixvx.com", "twittpr.com", "xcancel.com", "nitter.net"}

# Parameters that only say where a link was shared from.
_TRACKING = {"si", "feature", "igshid", "fbclid", "gclid", "mc_cid", "mc_eid", "mibextid"}
_TRACKING_YT = {"pp", "ab_channel"}
_TRACKING_X = {"s", "t", "ref_src", "ref_url"}

# Files that are audio on their own, so no site extractor is needed. Chiptune and
# tracker formats are here on purpose: ffmpeg cannot read them, yt-dlp cannot make
# sense of them, and AudioCrate plays them from the file.
DIRECT_EXTS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".aif",
    ".aiff", ".wma", ".ape", ".mka", ".wv", ".tta", ".mpc",
    ".fur", ".dmf", ".sid", ".psid", ".rsid", ".vgm", ".vgz", ".nsf", ".nsfe", ".spc",
    ".gbs", ".ay", ".ym", ".gym", ".hes", ".kss", ".sap", ".vtx", ".pt3", ".psg",
    ".mod", ".xm", ".it", ".s3m", ".mptm", ".mtm", ".669", ".far", ".ult", ".okt",
    ".med", ".stm", ".umx",
}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _in_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def provider_for_url(url: str) -> Provider:
    """The provider whose host matches, or the 'link' provider."""
    host = _host(url) if isinstance(url, str) else ""
    if host:
        for p in PROVIDERS.values():
            if any(_in_domain(host, d) for d in p.hosts):
                return p
    return LINK


def is_url(text: str) -> bool:
    """http or https, one token, with a host. Says nothing about whether it is safe."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or any(c.isspace() or ord(c) < 32 for c in s):
        return False
    try:
        u = urlsplit(s)
        return u.scheme.lower() in ("http", "https") and bool(u.hostname)
    except ValueError:
        return False


def _drop_params(query: str, names: set[str]) -> str:
    """Remove query parameters by name without re-encoding the ones that stay."""
    kept = []
    for part in query.split("&"):
        key = part.split("=", 1)[0].lower()
        if part and key not in names and not key.startswith("utm_"):
            kept.append(part)
    return "&".join(kept)


def normalize_url(url: str) -> str:
    """A clean, safe version of a link. Raises ValueError for anything else.

    Only http and https, no whitespace or control characters, no user name or
    password in the link, at most 2048 characters. Mirrors of an X post become
    x.com, short and mobile YouTube links become www.youtube.com, and share
    tracking is dropped. Everything that picks the content stays.
    """
    if not isinstance(url, str):
        raise ValueError("That is not a link.")
    s = url.strip()
    if not s:
        raise ValueError("That is not a link.")
    if len(s) > MAX_URL:
        raise ValueError("That link is too long.")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in s):
        raise ValueError("That is not a single link.")
    try:
        u = urlsplit(s)
        host, port = (u.hostname or "").lower().rstrip("."), u.port
    except ValueError:
        raise ValueError("That is not a valid link.") from None
    if u.scheme.lower() not in ("http", "https"):
        raise ValueError("Only http and https links work.")
    if not host or u.username is not None or u.password is not None:
        raise ValueError("That is not a valid link.")

    path, query = u.path, u.query
    if host in _X_ALIASES:
        host = "x.com"
        query = _drop_params(query, _TRACKING | _TRACKING_X)
    elif _in_domain(host, "youtube.com") or host == "youtu.be":
        query = _drop_params(query, _TRACKING | _TRACKING_YT)
        if host == "youtu.be":
            vid = path.strip("/").split("/", 1)[0]
            if not vid:
                raise ValueError("That YouTube link has no video in it.")
            host, path = "www.youtube.com", "/watch"
            query = "&".join(["v=" + quote(vid, safe="")] + ([query] if query else []))
        elif host == "m.youtube.com" or (host == "music.youtube.com" and path in ("/watch", "/playlist")):
            host = "www.youtube.com"
    else:
        query = _drop_params(query, _TRACKING)

    netloc = f"[{host}]" if ":" in host else host
    if port and port != (443 if u.scheme.lower() == "https" else 80):
        netloc += f":{port}"
    return urlunsplit((u.scheme.lower(), netloc, path, query, u.fragment))


_YT_CHANNEL_ROOT = re.compile(r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")


def listing_url(url: str) -> str:
    """The link to list. A bare YouTube channel lists its tabs, not its videos, so it gets /videos."""
    try:
        u = urlsplit(url)
    except ValueError:
        return url
    host = (u.hostname or "").lower()
    if _in_domain(host, "youtube.com") and _YT_CHANNEL_ROOT.match(u.path):
        return urlunsplit((u.scheme, "www.youtube.com", u.path.rstrip("/") + "/videos", "", ""))
    return url


def is_direct_audio(url: str) -> bool:
    """True when the link path ends in an audio or chiptune extension."""
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return any(path.endswith(e) for e in DIRECT_EXTS) and is_url(url)


_X_POST = re.compile(r"^/(?:[^/]+|i/web)/status/\d+")
_X_SPACE = re.compile(r"^/i/spaces/\w+")
_X_BROADCAST = re.compile(r"^/i/(?:broadcasts|events)/")
_X_RESERVED = {"i", "home", "explore", "notifications", "messages", "search", "settings",
               "compose", "login", "tos", "privacy"}
_X_PROFILE_TABS = {"media", "timeline", "tweets", "with_replies", "likes"}


def x_url_kind(url: str) -> str:
    """'post', 'space', 'broadcast', 'bookmarks', 'profile' or 'other' for an x.com link."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return "other"
    if _X_POST.match(path):
        return "post"
    if _X_SPACE.match(path):
        return "space"
    if _X_BROADCAST.match(path):
        return "broadcast"
    if path.rstrip("/") == "/i/bookmarks":
        return "bookmarks"
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] not in _X_RESERVED and (len(parts) == 1 or parts[1] in _X_PROFILE_TABS):
        return "profile"
    return "other"

