"""The report zip of the Diagnostics window, and the scrubbing that keeps secrets out of it.

Nothing that holds a token goes into a report. Settings are cleaned by key name (anything that looks like a token,
secret, password, key, cookie, session or client id is replaced, whatever its value) and every text file, the log
lines and the snapshot are cleaned by value patterns: Discord and web tokens, Last.fm keys, query parameters, cookie
and authorization headers, and user:password pairs in URLs. Stdlib only.
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path

import plat_fs

HIDDEN = "<hidden>"

_KEY = re.compile(r"(token|secret|passw|pwd|api[_-]?key|apikey|api[_-]?sig|session|cookie|auth|credential|private"
                  r"|client[_-]?id|upload[_-]?cmd|bearer|(^|_)sk$)", re.I)

_QUERY_KEYS = r"api_key|apikey|api_sig|sk|token|access_token|refresh_token|session|sessionid|sid|key|secret|password|passwd|auth"

_PATTERNS = [
    (re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[\w-]{6,7}\.[\w-]{27,40}\b"), HIDDEN),               # Discord user and bot tokens
    (re.compile(r"\bmfa\.[\w-]{60,}"), HIDDEN),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), HIDDEN),                                  # JWT
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 " + HIDDEN),
    (re.compile(r"\b(authorization|cookie|set-cookie|x-api-key|x-auth-token)\s*[:=]\s*[^\r\n]+", re.I), r"\1: " + HIDDEN),
    (re.compile(r"([?&;\s\"'](?:" + _QUERY_KEYS + r")=)[^&\s\"';]+", re.I), r"\1" + HIDDEN),
    (re.compile(r"(\"?\b(?:\w*token|\w*secret|\w*password|api_key|cookie|session_key)\"?\s*[:=]\s*)\"?[^\s\",}]+\"?", re.I),
     r"\1" + HIDDEN),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://" + HIDDEN + "@"),                                  # user:password in a URL
    (re.compile(r"\b[0-9a-f]{32}\b", re.I), HIDDEN),                                                # Last.fm key, secret, session key
]


def scrub_text(text: str) -> str:
    for pat, sub in _PATTERNS:
        text = pat.sub(sub, text)
    return text


def scrub_obj(obj):
    """A copy of parsed JSON with the value of every secret-looking key replaced and every string cleaned."""
    if isinstance(obj, dict):
        return {k: (HIDDEN if _KEY.search(str(k)) and v not in ("", None, False, 0) else scrub_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


def _read(path: Path, limit: int = 8 * 1024 * 1024) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _json_clean(path: Path) -> str | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return json.dumps(scrub_obj(json.loads(raw)), indent=2, ensure_ascii=False)
    except ValueError:
        return scrub_text(raw)                  # not JSON after all, so cleaned as text


def build_report(dest, snapshot: dict, stacks: str, config_dir: Path, debug_dir: Path) -> Path:
    """Writes the report zip to dest and returns it. snapshot and stacks are what the program says about itself right
    now (diag.snapshot() and diag.stacks_text()); the rest is read from disk."""
    dest = Path(dest)
    config_dir, debug_dir = Path(config_dir), Path(debug_dir)
    parts: dict[str, str] = {}
    app = snapshot.get("app", {})
    parts["about.txt"] = (
        f"AudioCrate {app.get('version', '?')} report made {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Python {app.get('python', '?')}, Qt {app.get('qt', '?')}, PySide6 {app.get('pyside6', '?')}, {app.get('system', '?')}\n\n"
        "Settings and the DSP chain have every key that looks like a token, secret, password, key, cookie, session or client id\n"
        "replaced by <hidden>. Log lines, stacks and the snapshot are cleaned of token-like values.\n")
    parts["snapshot-now.json"] = scrub_text(json.dumps(snapshot, indent=1, default=str, ensure_ascii=False))
    parts["stacks-now.txt"] = scrub_text(stacks)
    for pattern, keep in (("stacks-*[0-9].txt", 1), ("snapshot-*.json", 1)):
        try:
            found = sorted(debug_dir.glob(pattern), key=lambda p: p.stat().st_mtime)[-keep:]
        except OSError:
            found = []
        for p in found:
            text = _read(p)
            if text is not None:
                parts[f"debug/{p.name}"] = scrub_text(text)
    for name in ("audiocrate.log", "audiocrate.log.1", "audiocrate.log.2", "crash.log"):
        text = _read(config_dir / name)
        if text is not None:
            parts[f"logs/{name}"] = scrub_text(text)
    for name in ("settings.json", "dsp.json"):
        text = _json_clean(config_dir / name)
        if text is not None:
            parts[name] = text
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    plat_fs.replace(tmp, dest)
    return dest
