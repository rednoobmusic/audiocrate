"""Sign in through the person's own browser.

The Online service reads a browser's cookies through yt-dlp. This opens a site's real sign in page in
the default browser, so passkeys, two step codes and captchas work the way they always do. When the
person says they are done, the account is pointed at that browser and the usual login check runs.
Nothing here sees a password, starts a program of its own or waits for the browser.

A browser that yt-dlp has no name for is used through the engine it is built on. A Firefox fork is
given the folder of its default profile, found by name in profiles.ini. A Chromium fork is given
its Default profile folder. No cookie or login database is ever opened here.
"""
from __future__ import annotations

import configparser
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from applog import log
from online_service import BROWSERS

LOGIN_URLS = {
    "youtube": ("https://accounts.google.com/ServiceLogin?service=youtube&continue="
                "https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26next%3D%252F"),
    "x": "https://x.com/i/flow/login",
}

IDLE, WAITING, WORKING = "idle", "waiting", "working"

TEXT_WAITING = "Sign in in your browser, then press Done."
TEXT_WORKING = "Setting up."
TEXT_CHOOSE = "Choose your browser in the list below, then press Done."
TEXT_NOT_OPENED = "AudioCrate could not open your browser. Choose it in the list below, or use a cookies file."
TEXT_CANCELLED = "Cancelled. Nothing was changed."
TEXT_LATE = ("If you only just signed in, the browser may not have saved it yet. "
             "Wait a minute or close the browser, then press Check.")
REDETECT_AFTER = 30.0           # seconds a found default browser is trusted


@dataclass(frozen=True)
class Browser:
    key: str = ""               # the name the Online service uses, '' when it cannot read this browser
    name: str = ""              # for the person
    desktop: str = ""           # the .desktop id the system named
    profile: str = ""           # a profile folder for a fork, '' when yt-dlp looks in the right place


@dataclass(frozen=True)
class _Engine:
    pattern: str                # matched against the lower case desktop id without ".desktop"
    key: str                    # yt-dlp's name for the engine
    name: str
    roots: tuple = ()           # where a fork keeps its data; ~ and $XDG_CONFIG_HOME are expanded


# More specific names first: a fork's id often contains its engine's name.
_ENGINES = (
    _Engine(r"waterfox", "firefox", "Waterfox", ("~/.waterfox", "~/.var/app/net.waterfox.waterfox/.waterfox")),
    _Engine(r"librewolf", "firefox", "LibreWolf", ("~/.librewolf", "$XDG_CONFIG_HOME/librewolf/librewolf",
                                                   "~/.var/app/io.gitlab.librewolf-community/.librewolf")),
    _Engine(r"floorp", "firefox", "Floorp", ("~/.floorp", "~/.var/app/one.ablaze.floorp/.floorp")),
    _Engine(r"(?:^|[._-])zen(?:[._-]|$)", "firefox", "Zen", ("~/.zen", "~/.var/app/app.zen_browser.zen/.zen")),
    _Engine(r"icecat", "firefox", "IceCat", ("~/.mozilla/icecat",)),
    _Engine(r"firefox", "firefox", "Firefox"),
    _Engine(r"thorium", "chromium", "Thorium", ("$XDG_CONFIG_HOME/thorium",)),
    _Engine(r"ungoogled", "chromium", "Ungoogled Chromium",
            ("$XDG_CONFIG_HOME/chromium", "~/.var/app/io.github.ungoogled_software.ungoogled_chromium/config/chromium")),
    _Engine(r"chromium", "chromium", "Chromium"),
    _Engine(r"brave", "brave", "Brave"),
    _Engine(r"google-chrome|^com\.google\.chrome", "chrome", "Chrome"),
    _Engine(r"vivaldi", "vivaldi", "Vivaldi"),
    _Engine(r"opera", "opera", "Opera"),
    _Engine(r"microsoft-edge|com\.microsoft\.edge|msedge", "edge", "Edge"),
    _Engine(r"whale", "whale", "Whale"),            # yt-dlp has it, the Online service does not
)

_DESKTOP_ID = re.compile(r"[\w.+-]+\.desktop")


def _expand(path: str) -> Path:
    path = path.replace("$XDG_CONFIG_HOME", os.environ.get("XDG_CONFIG_HOME") or "~/.config")
    return Path(os.path.expanduser(path))


def _usable(path) -> str:
    """The folder as a string the Online service accepts as a profile, or ''."""
    text = str(path)
    return text if os.path.isabs(text) and ":" not in text and os.path.isdir(text) else ""


def _ini(path: Path) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        ini.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeError):
        return configparser.ConfigParser(interpolation=None)
    return ini


def firefox_profile(root: Path) -> str:
    """The folder of the profile a Firefox fork opens by default.

    profiles.ini names it in an Install section (the one the install last used) or with Default=1, and
    a single profile is the answer by itself. When these say nothing the fork's own folder is returned:
    yt-dlp then takes the newest cookie store anywhere under it. '' when the folder is not there.
    """
    profiles = _ini(root / "profiles.ini")
    installs = _ini(root / "installs.ini")
    found = [root / ini.get(s, "default") for ini in (profiles, installs) for s in ini.sections()
             if ini.has_option(s, "default") and (ini is installs or s.lower().startswith("install"))]
    names = [s for s in profiles.sections() if s.lower().startswith("profile")]
    found += [_profile_path(root, profiles, s) for s in names
              if profiles.get(s, "default", fallback="0").strip() == "1"]
    if len(names) == 1:
        found.append(_profile_path(root, profiles, names[0]))
    return next(filter(None, map(_usable, found)), "") or _usable(root)


def _profile_path(root: Path, ini, section: str) -> Path:
    path = ini.get(section, "path", fallback="")
    return Path(path) if ini.get(section, "isrelative", fallback="1").strip() == "0" else root / path


def chromium_profile(root: Path) -> str:
    """Chromium keeps the first profile in Default. yt-dlp needs the folder of the profile itself."""
    return _usable(root / "Default")


def _pretty(ident: str) -> str:
    last = re.split(r"[./]", ident.strip(". "))[-1]
    return last.replace("-", " ").replace("_", " ").strip().title()


def _resolve(ident: str, desktop: str) -> Browser:
    low = ident.lower()
    if low.endswith(".desktop"):
        low = low[:-len(".desktop")]
    engine = next((e for e in _ENGINES if re.search(e.pattern, low)), None)
    if engine is None:
        return Browser("", _pretty(low), desktop)
    key, profile = engine.key, ""
    if engine.roots:
        pick = firefox_profile if key == "firefox" else chromium_profile
        profile = next((p for p in (pick(_expand(r)) for r in engine.roots if _expand(r).is_dir()) if p), "")
        if not profile:
            key = ""                                    # the fork keeps its data somewhere unexpected
    if key not in BROWSERS:
        key, profile = "", ""
    return Browser(key, engine.name, desktop, profile)


def _output(*cmd: str) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=4,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def detect() -> Browser:
    """The default web browser. Asks xdg-settings, then xdg-mime, then $BROWSER. Blocks for a moment."""
    for cmd in (("xdg-settings", "get", "default-web-browser"),
                ("xdg-mime", "query", "default", "x-scheme-handler/https")):
        found = _DESKTOP_ID.search((_output(*cmd).splitlines() or [""])[0])
        if found:
            return _resolve(found.group(0), found.group(0))
    for entry in (os.environ.get("BROWSER") or "").split(":"):
        try:
            words = shlex.split(entry)
        except ValueError:
            continue
        if words:
            return _resolve(os.path.basename(words[0]), "")
    return Browser()


def default_browser() -> tuple[str, str, str]:
    """(browser key the Online service supports or '', display name, desktop id)."""
    b = detect()
    return b.key, b.name, b.desktop


def _inline(fn, callback=None) -> None:
    try:
        res, err = fn(), None
    except Exception as e:
        res, err = None, e
    if callback is not None:
        callback(res, err)


class BrowserLogin(QObject):
    """One sign in at a time per site: open the browser, wait for Done, then use its cookies and check them.

    changed(provider id) comes whenever a site's state or sentence changes. check_started(provider id,
    request id) comes when Done has pointed the account at the browser and the login check is running:
    the Accounts page waits for that request like it does for a press of Check. Nothing waits in here.
    `run(fn, callback)` runs fn off the GUI thread and calls callback(result, error) on it, like
    online_widgets.Background.run. `gate(then)` may refuse to go on and run `then` later.
    """
    changed = Signal(str)
    browser_changed = Signal()
    check_started = Signal(str, int)

    def __init__(self, service, run=None, opener=None, gate=None, detector=None, parent=None):
        super().__init__(parent)
        self._svc = service
        self._run = run or _inline
        self.opener = opener or (lambda url: QDesktopServices.openUrl(QUrl(url)))
        self._gate = gate
        self._detect = detector or detect
        self._browser: Browser | None = None
        self._stamp = float("-inf")
        self._probing = False
        self._closed = False
        self._state: dict[str, str] = {}
        self._text: dict[str, str] = {}
        self._gen: dict[str, int] = {}
        self._signed: dict[str, bool] = {}
        self._fresh: set[str] = set()                   # sites whose next login check follows a Done

    # what the page shows

    @staticmethod
    def supported(provider_id: str) -> bool:
        return provider_id in LOGIN_URLS

    def state(self, provider_id: str) -> str:
        return self._state.get(provider_id, IDLE)

    def text(self, provider_id: str) -> str:
        return self._text.get(provider_id, "")

    def signed_in(self, provider_id: str) -> bool:
        """True when the last login check of this site said so."""
        return self._signed.get(provider_id, False)

    def browser(self) -> Browser:
        """The default browser as last found. Empty until the first search, which refresh_browser starts."""
        return self._browser or Browser()

    def refresh_browser(self) -> None:
        if self._closed or self._probing or time.monotonic() - self._stamp < REDETECT_AFTER:
            return
        self._probing = True
        self._run(self._detect, self._detected)

    def _detected(self, res, err) -> None:
        self._probing = False
        if self._closed:
            return
        self._stamp = time.monotonic()
        if isinstance(res, Browser) and res != self._browser:
            self._browser = res
            self.browser_changed.emit()

    def _set(self, provider_id: str, state: str, text: str) -> None:
        self._state[provider_id] = state
        self._text[provider_id] = text
        if not self._closed:
            self.changed.emit(provider_id)

    # the steps

    def start(self, provider_id: str) -> str:
        """Open the sign in page in the default browser. Returns the browser's name, '' when nothing opened."""
        url = LOGIN_URLS.get(provider_id)
        if url is None or self._closed or self.state(provider_id) != IDLE:
            return ""
        if self._gate is not None and not self._gate(partial(self.start, provider_id)):
            return ""
        self.refresh_browser()
        try:
            opened = bool(self.opener(url))
        except Exception:
            log.exception("opening the browser")
            opened = False
        if not opened:
            self._set(provider_id, IDLE, TEXT_NOT_OPENED)
            return ""
        self._set(provider_id, WAITING, TEXT_WAITING)
        return self.browser().name or "your browser"

    def finish(self, provider_id: str) -> bool:
        """Use the default browser's cookies for this site and check them. False when it is not waiting."""
        if self._closed or self.state(provider_id) != WAITING:
            return False
        gen = self._gen[provider_id] = self._gen.get(provider_id, 0) + 1
        self._set(provider_id, WORKING, TEXT_WORKING)

        def apply():
            b = self._browser or self._detect()
            if b.key:
                self._svc.set_cookie_source(provider_id, "browser", browser=b.key, profile=b.profile)
            elif self._svc.cookie_source(provider_id)["kind"] != "browser":
                return None                             # the person has to pick one by hand
            return b
        self._run(apply, partial(self._applied, provider_id, gen))
        return True

    def _applied(self, provider_id: str, gen: int, res, err) -> None:
        if self._closed or gen != self._gen.get(provider_id):
            return
        if err is not None:
            if not isinstance(err, ValueError):
                log.warning("pointing %s at the browser: %r", provider_id, err)
            self._set(provider_id, IDLE, str(err) if isinstance(err, ValueError) else "That did not work.")
        elif res is None:
            self._set(provider_id, WAITING, TEXT_CHOOSE)
        else:
            rid = self._svc.check_login(provider_id)
            self._fresh.add(provider_id)
            self._set(provider_id, IDLE, "")
            self.check_started.emit(provider_id, rid)

    def cancel(self, provider_id: str) -> None:
        if self.state(provider_id) == WAITING:
            self._set(provider_id, IDLE, TEXT_CANCELLED)

    def note_check(self, provider_id: str, logged_in, text: str) -> str:
        """Call with every login check's answer (logged_in is None when it cannot be told). Returns the sentence to show."""
        fresh = provider_id in self._fresh
        self._fresh.discard(provider_id)
        if logged_in is not None:
            self._signed[provider_id] = bool(logged_in)
        if self.state(provider_id) == IDLE:
            self._text[provider_id] = ""
        if not self._closed:
            self.changed.emit(provider_id)
        return text + " " + TEXT_LATE if fresh and logged_in is False else text

    def shutdown(self) -> None:
        self._closed = True
        self._state.clear()
        self._gen.clear()
