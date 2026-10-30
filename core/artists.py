"""Who the artists of a song are, from what its tags say.

A tag can hold several values (ID3v2.4, Vorbis, MP4 and APE do) or one string with several names in it.
split_artists turns either into a list of names, join_artists turns a list into the text shown on screen.
The settings (commas, "&", slashes and the names that are never split) are read from settings.json the first
time they are needed and replaced by configure() when the user changes them.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache

import plat_paths

_SETTINGS = plat_paths.config_dir() / "settings.json"

DEFAULT_EXCEPTIONS = (
    "Earth, Wind & Fire", "Crosby, Stills & Nash", "Crosby, Stills, Nash & Young", "Tyler, The Creator",
    "Emerson, Lake & Palmer", "Blood, Sweat & Tears", "Simon & Garfunkel", "Hall & Oates", "Mumford & Sons",
    "Belle & Sebastian", "Brooks & Dunn", "Kool & the Gang", "Echo & the Bunnymen", "Huey Lewis & the News",
    "Tom Petty & the Heartbreakers", "Bob Marley & the Wailers", "Gladys Knight & the Pips", "Years & Years",
    "Florence + the Machine", "Of Monsters and Men", "AC/DC", "K/DA",
)


@dataclass(frozen=True)
class Options:
    commas: bool = True
    amp: bool = True
    exceptions: tuple = DEFAULT_EXCEPTIONS
    slash: bool = False


_opts: Options | None = None


def exceptions_from_text(text: str) -> tuple:
    return tuple(dict.fromkeys(n.strip() for n in text.splitlines() if n.strip()))


def options() -> Options:
    global _opts
    if _opts is None:
        o = Options()
        try:
            cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
            o = Options(bool(cfg.get("artist_split_commas", True)), bool(cfg.get("artist_split_amp", True)),
                        exceptions_from_text(cfg["artist_exceptions"]) if "artist_exceptions" in cfg
                        else DEFAULT_EXCEPTIONS, bool(cfg.get("artist_split_slash", False)))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        _opts = o
    return _opts


def configure(commas: bool = True, amp: bool = True, exceptions=None, slash: bool = False):
    """Use these settings from now on. exceptions is a list of names or the text of the settings field."""
    global _opts
    if exceptions is None:
        exceptions = DEFAULT_EXCEPTIONS
    elif isinstance(exceptions, str):
        exceptions = exceptions_from_text(exceptions)
    _opts = Options(bool(commas), bool(amp), tuple(exceptions), bool(slash))
    _known.cache_clear()


_FEAT = re.compile(r"\s*[(\[]\s*(?:feat\.|ft\.|featuring)\s+([^)\]]+?)\s*[)\]]|\s+(?:feat\.|ft\.|featuring)\s+",
                   re.IGNORECASE)
_HARD = re.compile(r"\s*;\s*")
_COMMA = re.compile(r",\s+")
_AMP = re.compile(r"\s+&\s+")
_SLASH = re.compile(r"\s*/\s*")
_QUICK = re.compile(r";|,\s|\s&\s|feat\.|ft\.|featuring", re.IGNORECASE)


@lru_cache(maxsize=256)
def _known(names: tuple) -> re.Pattern | None:
    """One pattern for the names that must stay whole, longest first so a long name wins over a part of it."""
    names = sorted((n for n in names if n), key=len, reverse=True)
    if not names:
        return None
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(n) for n in names), re.IGNORECASE)


def _split_one(text: str, albumartist: str, o: Options) -> list[str]:
    if not _QUICK.search(text) and not (o.slash and "/" in text):
        return [text]
    keep: list[str] = []

    def hold(m):
        keep.append(m.group())
        return "%d" % (len(keep) - 1)

    pat = _known(o.exceptions + ((albumartist,) if albumartist else ()))
    if pat is not None:
        text = pat.sub(hold, text)
    parts = [text]
    seps = [_HARD, _FEAT, *([_COMMA] if o.commas else []), *([_AMP] if o.amp else []),
            *([_SLASH] if o.slash else [])]
    for sep in seps:
        parts = [q for p in parts for q in sep.split(p) if q]
    out = []
    for p in parts:
        out.append(re.sub("(\\d+)", lambda m: keep[int(m.group(1))], p))
    return out


def split_artists(values, albumartist: str = "") -> list[str]:
    """The names behind the artist tag of one file. values is the tag's value or its list of values.

    Several values are taken as they are. A single string is cut at ";", "feat.", "ft.", "featuring", ", ", " & "
    and "/" (the last three are optional), except where it holds a name from the exception list or the file's
    album artist.
    Names are trimmed, kept in their case and in order, and listed once.
    """
    if isinstance(values, str):
        values = [values]
    vals = [v.strip() for v in values or () if v and v.strip()]
    if len(vals) == 1:
        vals = _split_one(vals[0], (albumartist or "").strip(), options())
    seen, out = set(), []
    for v in vals:
        v = v.strip()
        if v and v.casefold() not in seen:
            seen.add(v.casefold())
            out.append(v)
    return out


def join_artists(names) -> str:
    """The names as one line of text, "A, B"."""
    return ", ".join(names)
