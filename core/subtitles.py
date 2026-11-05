"""Subtitle files (SRT, WebVTT, ASS and SSA) read into timed lines of text, and the line to show at a time.

Styling is dropped: the lines come out as plain text, and a drawing in an ASS file is skipped.
"""
from __future__ import annotations

import html
import re
from bisect import bisect_right
from pathlib import Path

EXTS = (".srt", ".vtt", ".ass", ".ssa")

_STAMP = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_MARKUP = re.compile(r"\{[^}]*\}|<[^>]*>")
_ASS_BREAK = re.compile(r"\\[Nn]")
_BOMS = (("utf-8-sig", b"\xef\xbb\xbf"), ("utf-16", b"\xff\xfe"), ("utf-16", b"\xfe\xff"))


def _ms(stamp: str) -> int | None:
    m = _STAMP.fullmatch(stamp.strip())
    if m is None:
        return None
    h, mi, s, frac = int(m.group(1) or 0), int(m.group(2)), int(m.group(3)), m.group(4).ljust(3, "0")[:3]
    return ((h * 60 + mi) * 60 + s) * 1000 + int(frac)


def _text(raw: str) -> str:
    lines = _ASS_BREAK.sub("\n", raw)
    lines = html.unescape(_MARKUP.sub("", lines))
    return "\n".join(ln.strip() for ln in lines.splitlines() if ln.strip())


def _blocks(text: str) -> list[tuple[int, int, str]]:
    """SRT and WebVTT: blocks of lines separated by an empty line, the one with --> holding the times."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip("\n").split("\n")
        for i, ln in enumerate(lines):
            if "-->" in ln:
                a, _, rest = ln.partition("-->")
                start, end = _ms(a), _ms(rest.split()[0]) if rest.split() else None   # WebVTT may follow the end with settings
                body = _text("\n".join(lines[i + 1:]))
                if start is not None and end is not None and body:
                    out.append((start, end, body))
                break
    return out


def _ass(text: str) -> list[tuple[int, int, str]]:
    out, fields, in_events = [], [], False
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("["):
            in_events = ln.lower() == "[events]"
        elif in_events and ln.lower().startswith("format:"):
            fields = [f.strip().lower() for f in ln[7:].split(",")]
        elif in_events and ln.lower().startswith("dialogue:") and fields:
            parts = ln[9:].split(",", len(fields) - 1)
            row = dict(zip(fields, parts))
            start, end, raw = _ms(row.get("start", "")), _ms(row.get("end", "")), row.get("text", "")
            if start is not None and end is not None and "\\p1" not in raw:
                body = _text(raw)
                if body:
                    out.append((start, end, body))
    return out


class Cues:
    """The lines of one subtitle file, looked up by time."""

    def __init__(self, cues: list[tuple[int, int, str]]):
        self._cues = sorted(cues, key=lambda c: c[0])
        self._starts = [c[0] for c in self._cues]

    def __len__(self) -> int:
        return len(self._cues)

    def at(self, ms: int) -> str:
        """The text to show at this time: every line whose time covers it (they overlap now and then)."""
        i = bisect_right(self._starts, ms)
        shown = [c[2] for c in self._cues[max(0, i - 4):i] if c[0] <= ms < c[1]]
        return "\n".join(shown)


def parse(raw: bytes, ext: str) -> Cues:
    text = None
    for codec, bom in _BOMS:
        if raw.startswith(bom):
            text = raw.decode(codec, errors="replace")
            break
    if text is None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    return Cues(_ass(text) if ext.lower() in (".ass", ".ssa") else _blocks(text))


def load(path: Path) -> Cues:
    return parse(Path(path).read_bytes(), Path(path).suffix)


def sidecars(video: Path) -> list[Path]:
    """Subtitle files beside a video that start with its name (movie.srt, movie.en.srt), shortest name first."""
    video = Path(video)
    try:
        found = [p for p in video.parent.iterdir()
                 if p.suffix.lower() in EXTS and p.name.lower().startswith(video.stem.lower()) and p.is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: (len(p.name), p.name.lower()))
