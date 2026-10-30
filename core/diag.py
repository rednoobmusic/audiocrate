"""Finding out what a running, or frozen, AudioCrate is doing.

install() runs once from main.py after logging is up. From then on:

  SIGUSR1  writes the stack of every Python thread to debug/stacks-<pid>-<time>.txt in the config folder
  SIGUSR2  writes a state snapshot to snapshot-<pid>-<time>.json next to it
           (Windows has no signals: a named event for each, set by `--debug-dump`, see plat_diag.py)
  a heartbeat on the GUI thread and a watcher thread record every stretch where the window did not answer for
  more than half a second (the stack the GUI thread was in), in the log and for the Diagnostics window

Both signals work while the GUI thread is stuck. faulthandler writes a first dump from inside the signal handler,
which needs no Python code to run, and chains to Python's own handler, which writes a byte to a pipe (the wakeup
fd). The watcher thread reads the pipe and writes the readable files. The watcher sleeps in select() with a
100 ms timeout; the heartbeat is a 100 ms timer that stores one number. Nothing else runs.

The module imports only the standard library, so `--debug-dump` (diag_cli) can use it without loading Qt.
"""
from __future__ import annotations

import collections
import faulthandler
import json
import logging
import os
import select
import signal
import sys
import threading
import time
import traceback
import types
import weakref
from pathlib import Path

import plat_diag
import plat_paths

CONFIG_DIR = plat_paths.config_dir()
DEBUG_DIR = CONFIG_DIR / "debug"
KEEP_FILES = 10             # of each kind
STACKS_GLOB = "stacks-*[0-9].txt"           # not the -raw one faulthandler writes
BEAT_MS = 100
STALL_S = 0.5               # the heartbeat this late is a stall
LONG_STALL_S = 3.0          # a stall still going after this long is logged while it lasts, not only when it ends
LOG_BURST = (10, 60.0)       # at most this many stall lines in this many seconds; the rest are only kept in memory

log = logging.getLogger("audiocrate.diag")


_S = types.SimpleNamespace(
    window=None,                # weak reference to the MainWindow
    beat=0.0,                   # monotonic time of the latest heartbeat
    beat_n=0,
    timer=None,
    main_ident=0,
    stall=None,                 # the stall in progress
    stalls=collections.deque(maxlen=50),
    stall_logged=collections.deque(maxlen=LOG_BURST[0]),
    cache={},                   # what only the GUI thread may read, refreshed by the heartbeat
    docks=[],
    rfd=-1, wfd=-1,
    trigger=None,               # the named events that stand in for the signals on Windows
    raw=None,                   # file faulthandler writes into from the signal handler
    raw_path=None,
    stop=threading.Event(),
    installed=False,
    started=time.monotonic(),
    static=None,
)


# which process is the running AudioCrate

def runtime_dir() -> Path:
    return plat_paths.runtime_dir()             # install() makes it with mode 700


def pid_file() -> Path:
    return runtime_dir() / "audiocrate.pid"


def find_instance() -> tuple[int | None, str]:
    """(pid, "") of the AudioCrate that is running for this user, or (None, a plain sentence saying why not). The pid
    comes from the pid file, and is only believed when that process still started when the file says it did."""
    pf = pid_file()
    try:
        pid_s, start_s = pf.read_text(encoding="utf-8").split()[:2]
        pid, start = int(pid_s), int(start_s)
    except (OSError, ValueError):
        return None, "No running AudioCrate was found (there is no pid file, or it is not readable)."
    if pid == os.getpid():
        return None, "The pid file names this process, not a running AudioCrate."
    if plat_diag.proc_start(pid) != start:
        return None, f"The pid file names process {pid}, which is not running any more."
    if not plat_diag.looks_like_app(pid):
        return None, f"Process {pid} is not an AudioCrate of yours, so nothing was sent to it."
    return pid, ""


def _stamp() -> str:
    t = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"-{int(t * 1000) % 1000:03d}"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _check_previous_run() -> None:
    """A run that did not end through the normal exit leaves its marker behind. Say so, with where the evidence is."""
    marker = DEBUG_DIR / "running.json"
    try:
        old = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pid, start = old.get("pid"), old.get("start")
    if isinstance(pid, int) and pid != os.getpid() and plat_diag.proc_start(pid) == start and plat_diag.looks_like_app(pid):
        return                                  # another AudioCrate is running right now, not a dead one
    parts = [f"the previous run (pid {pid}, started {old.get('started', '?')}) did not end normally"]
    crash = CONFIG_DIR / "crash.log"
    try:
        if crash.stat().st_size:
            parts.append(f"crash traces are in {crash}")
    except OSError:
        pass
    newest = _newest("snapshot-*.json") or _newest(STACKS_GLOB)
    if newest is not None:
        parts.append(f"its last dump is {newest}")
    parts.append(f"its log lines come just before this run's first line in {CONFIG_DIR / 'audiocrate.log'}")
    log.warning("; ".join(parts))


def _newest(pattern: str) -> Path | None:
    try:
        found = sorted(DEBUG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return found[-1] if found else None


def _prune(pattern: str) -> None:
    try:
        found = sorted(DEBUG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
        for p in found[:-KEEP_FILES]:
            p.unlink(missing_ok=True)
    except OSError:
        pass


# install

def install(detailed_default: bool = False) -> bool:
    """Writes the markers, takes over SIGUSR1 and SIGUSR2 and starts the watcher. Call once, on the main thread, with
    logging already set up. False (and nothing changed beyond what was done) when it cannot run, for instance off the
    main thread."""
    if _S.installed:
        return True
    if threading.current_thread() is not threading.main_thread():
        return False
    _S.main_ident = threading.main_thread().ident
    try:
        from applog import set_detailed
        set_detailed(detailed_default or _saved_detailed())
    except Exception:
        pass
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        _check_previous_run()
        for f in DEBUG_DIR.glob("stacks-*-raw.txt"):          # left by runs that were killed
            if plat_diag.proc_start(int(f.name.split("-")[1])) is None:
                f.unlink(missing_ok=True)
        start = plat_diag.proc_start(os.getpid())
        _write_private(DEBUG_DIR / "running.json", json.dumps(
            {"pid": os.getpid(), "start": start, "started": time.strftime("%Y-%m-%d %H:%M:%S")}))
        _write_private(pid_file(), f"{os.getpid()} {start}\n")
    except OSError as e:
        log.warning("diagnostics files could not be written: %s", e)
    events = plat_diag.uses_events()
    try:
        (_install_events if events else _install_signals)()
    except (OSError, ValueError, RuntimeError, AttributeError) as e:
        log.warning("diagnostic %s could not be installed: %s", "events" if events else "signals", e)
        return False
    _S.installed = True
    log.debug("pid file %s, dumps go to %s", pid_file(), DEBUG_DIR)
    _S.thread = threading.Thread(target=_watch, name="diag-watch", daemon=True)
    _S.thread.start()
    log.info("diagnostics ready: %s, both into %s", plat_diag.how_to(os.getpid()), DEBUG_DIR)
    return True


def _saved_detailed() -> bool:
    try:
        return bool(json.loads((CONFIG_DIR / "settings.json").read_text(encoding="utf-8")).get("debug_logs", False))
    except Exception:
        return False


def _install_signals() -> None:
    if not hasattr(signal, "SIGUSR1"):
        raise ValueError("no SIGUSR1 on this platform")
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.set_blocking(w, False)
    _S.rfd, _S.wfd = r, w
    try:
        faulthandler.unregister(signal.SIGUSR1)           # logs.setup registered it without a chain
    except (ValueError, OSError):
        pass
    # Python's handlers before faulthandler registers: it chains to the one installed at that moment, and a signal
    # with a Python handler is what writes to the wakeup fd.
    signal.signal(signal.SIGUSR1, lambda *_: None)
    signal.signal(signal.SIGUSR2, lambda *_: None)
    signal.set_wakeup_fd(w, warn_on_full_buffer=False)
    _open_raw()
    faulthandler.register(signal.SIGUSR1, file=_S.raw, all_threads=True, chain=True)


def _install_events() -> None:
    _S.trigger = plat_diag.Trigger()
    _open_raw()


def _open_raw() -> None:
    """The file faulthandler writes into when Python cannot: from the signal handler, or after a freeze."""
    try:
        _S.raw_path = DEBUG_DIR / f"stacks-{os.getpid()}-raw.txt"
        fd = os.open(_S.raw_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
        _S.raw = os.fdopen(fd, "wb", buffering=0)
    except OSError:
        _S.raw = sys.stderr


def attach(window) -> None:
    """On the GUI thread, once the window exists: starts the heartbeat the watcher compares itself against."""
    if not _S.installed or _S.timer is not None:
        return
    from PySide6.QtCore import QTimer
    _S.window = weakref.ref(window)
    t = _S.timer = QTimer(window)
    t.setInterval(BEAT_MS)
    t.timeout.connect(_on_beat)
    t.start()
    _S.beat_n = 0
    _on_beat()


def shutdown() -> None:
    """The normal exit: the markers go, so the next start knows this run ended properly."""
    _S.stop.set()
    plat_diag.freeze_dump_off()
    if _S.timer is not None:
        try:
            _S.timer.stop()
        except RuntimeError:
            pass
    for path in (DEBUG_DIR / "running.json", pid_file(), DEBUG_DIR / f"stacks-{os.getpid()}-raw.txt"):
        try:
            if path.name == "audiocrate.pid" and path.read_text(encoding="utf-8").split()[0] != str(os.getpid()):
                continue                       # a newer instance owns it
            path.unlink()
        except (OSError, IndexError):
            pass


# heartbeat and watcher

def _on_beat() -> None:
    _S.beat = time.monotonic()
    _S.beat_n += 1
    plat_diag.freeze_dump(_S.raw)
    if _S.beat_n % 20 == 1:
        _refresh_cache()


def _refresh_cache() -> None:
    """GUI thread: the few facts that are only safe to ask Qt for here, kept for the watcher to read."""
    c: dict = {}
    win = _S.window() if _S.window else None
    try:
        from PySide6.QtWidgets import QDockWidget
        if win is not None:
            if not _S.docks:
                _S.docks = [weakref.ref(d) for d in win.findChildren(QDockWidget)]
            docks = [d() for d in _S.docks]
            c["panels"] = sorted(d.windowTitle() for d in docks if d is not None and d.isVisible() and not d.isFloating())
            c["floating"] = sorted(d.windowTitle() for d in docks if d is not None and d.isVisible() and d.isFloating())
            c["window"] = {"visible": win.isVisible(), "minimized": win.isMinimized(), "active": win.isActiveWindow(),
                           "size": [win.width(), win.height()]}
            dev = getattr(getattr(getattr(win, "_player", None), "_dsp", None), "_dev", None)
            if dev is not None:
                c["device"] = dev.description()
        from PySide6.QtGui import QGuiApplication
        c["platform"] = QGuiApplication.platformName()
    except Exception as e:
        c["error"] = repr(e)
    _S.cache = c


def _watch() -> None:
    while not _S.stop.is_set():
        if _S.trigger is not None:
            fired = _S.trigger.wait(0.1)
            now = time.monotonic()
            for which in fired:
                _on_trigger(which)
        else:
            try:
                ready = select.select([_S.rfd], [], [], 0.1)[0]
            except (OSError, ValueError):
                return
            now = time.monotonic()
            if ready:
                try:
                    data = os.read(_S.rfd, 64)
                except OSError:
                    data = b""
                for sig in set(data):
                    _on_signal(sig)
        try:
            _check_stall(now)
        except Exception:
            log.exception("diagnostics watcher failed")
    if _S.trigger is not None:
        _S.trigger.close()


def _on_signal(sig: int) -> None:
    if sig == signal.SIGUSR1:
        write_stacks("SIGUSR1")
    elif sig == signal.SIGUSR2:
        write_snapshot("SIGUSR2")


def _on_trigger(which: str) -> None:
    if which == "stacks":
        write_stacks("stacks event")
    elif which == "snapshot":
        write_snapshot("snapshot event")


def _frame_summary(frame) -> tuple[str, list[str]]:
    """('function (file.py:line)', the stack from the innermost call out, one short line each)."""
    entries = traceback.extract_stack(frame)
    top = entries[-1]
    where = f"{top.name} ({os.path.basename(top.filename)}:{top.lineno})"
    if top.name == "<module>" and "exec()" in (top.line or ""):
        where = "the Qt event loop (native code, no Python was running)"
    lines = [f"{e.name} ({os.path.basename(e.filename)}:{e.lineno})" for e in reversed(entries)]
    return where, lines


def _check_stall(now: float) -> None:
    beat = _S.beat
    if not beat:
        return
    st = _S.stall
    if st is None:
        if now - beat - BEAT_MS / 1000.0 <= STALL_S:
            return
        frame = sys._current_frames().get(_S.main_ident)
        if frame is None:
            return
        where, lines = _frame_summary(frame)
        st = _S.stall = {"time": time.strftime("%H:%M:%S"), "beat": beat, "where": where,
                         "stack": lines, "seconds": now - beat - BEAT_MS / 1000.0, "ended": False, "told": False}
        _S.stalls.append(st)
        return
    if beat != st["beat"]:                                # the heartbeat is back
        st["seconds"] = beat - st["beat"] - BEAT_MS / 1000.0
        st["ended"] = True
        _S.stall = None
        _log_stall(st, False, now)
        return
    st["seconds"] = now - st["beat"] - BEAT_MS / 1000.0
    if not st["told"] and st["seconds"] >= LONG_STALL_S:
        _log_stall(st, True, now)


def _log_stall(st: dict, ongoing: bool, now: float) -> None:
    if ongoing:
        fmt = "UI has not answered for %.1f s, it is in %s, stack: %s"
        st["told"] = True
    elif st["told"]:
        fmt = "UI answered again after %.1f s (it was in %s), stack: %s"
    else:
        fmt = "UI stalled %.1f s in %s, stack: %s"
    n, span = LOG_BURST
    while _S.stall_logged and now - _S.stall_logged[0] > span:
        _S.stall_logged.popleft()
    if len(_S.stall_logged) >= n:
        return
    _S.stall_logged.append(now)
    log.warning(fmt, st["seconds"], st["where"], " < ".join(st["stack"][:8]))


def stalls() -> list[dict]:
    """The recorded UI stalls, oldest first (the last 50)."""
    return [dict(s) for s in list(_S.stalls)]


# stacks

def thread_stacks() -> list[tuple[str, int, bool, list[str]]]:
    """(name, ident, is the main thread, formatted stack lines) for every Python thread, main thread first."""
    names = {t.ident: t for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        t = names.get(ident)
        name = t.name if t is not None else f"thread {ident}"
        out.append((name, ident, ident == _S.main_ident or ident == threading.main_thread().ident,
                    traceback.format_stack(frame)))
    out.sort(key=lambda x: (not x[2], x[0]))
    return out


def stacks_text(reason: str = "") -> str:
    head = [f"AudioCrate {_version()} stacks, pid {os.getpid()}, {time.strftime('%Y-%m-%d %H:%M:%S')}"
            + (f", reason: {reason}" if reason else "")]
    body = []
    for name, ident, main, lines in thread_stacks():
        body.append(f'Thread "{name}" (id {ident}){" [main thread]" if main else ""}')
        body.extend("  " + ln.rstrip("\n").replace("\n", "\n  ") for ln in lines)
        body.append("")
        if main:
            frame = sys._current_frames().get(ident)
            if frame is not None:
                head.append(f"main thread: {_frame_summary(frame)[0]}")
    return "\n".join(head + [""] + body)


def write_stacks(reason: str = "") -> Path | None:
    """The stacks file. The text is made and written first and the log line comes last, so a log that is itself stuck
    cannot keep the file from being there."""
    path = DEBUG_DIR / f"stacks-{os.getpid()}-{_stamp()}.txt"
    text = stacks_text(reason)
    raw = _take_raw()
    if raw:
        text += "\nFrom the signal handler (faulthandler):\n" + raw
    try:
        _write_private(path, text)
    except OSError as e:
        log.warning("could not write the stacks file: %s", e)
        return None
    _prune(STACKS_GLOB)
    log.info("thread stacks written to %s", path)
    return path


def _take_raw() -> str:
    """What faulthandler wrote from inside the signal handler since the last call (the file is emptied)."""
    f = _S.raw
    if f is None or f is sys.stderr or _S.raw_path is None:
        return ""
    try:
        data = _S.raw_path.read_text(encoding="utf-8", errors="replace")
        os.ftruncate(f.fileno(), 0)
        return data
    except OSError:
        return ""


# snapshot

def _version() -> str:
    try:
        from _version import VERSION
        return VERSION
    except Exception:
        return "unknown"


def _section(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": repr(e)}


def _static() -> dict:
    if _S.static is None:
        d = {"version": _version(), "frozen": bool(getattr(sys, "frozen", False)),
             "python": sys.version.split()[0], "executable": sys.executable,
             "program": os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""}
        try:
            import PySide6
            from PySide6.QtCore import qVersion
            d["pyside6"], d["qt"] = PySide6.__version__, qVersion()
        except Exception:
            pass
        for mod in ("numpy", "scipy"):
            m = sys.modules.get(mod)
            if m is not None:
                d[mod] = getattr(m, "__version__", "?")
        d.update(plat_diag.system_info())
        _S.static = d
    return dict(_S.static)


def _percentiles(values: list[float]) -> dict:
    v = sorted(x for x in values if x > 0)
    if not v:
        return {}
    pick = lambda q: round(v[min(len(v) - 1, int(q * len(v)))] * 1000, 2)       # noqa: E731
    return {"n": len(v), "p50_ms": pick(0.5), "p95_ms": pick(0.95), "p99_ms": pick(0.99), "max_ms": round(v[-1] * 1000, 2)}


def audio_state(win) -> dict:
    """The audio engine's numbers: device, rate, sink, queue, underruns, feed tick timing, decode progress, normalizer."""
    player = win._player
    eng = player._dsp
    w = eng._worker
    fs = eng.fs
    device = _S.cache.get("device")
    if device is None and threading.current_thread() is threading.main_thread():
        device = eng._dev.description()                   # the cache is the only safe way from other threads
    out = {"device": device, "rate": fs, "channels": 2,
           "sink_alive": eng._sink_alive, "sink_suspended": eng._sink_suspended, "sink_state": getattr(w, "sink_state", ""),
           "sink_error": w._last_error, "playing_intent": eng._playing, "loaded": eng._loaded,
           "queue_ms": round(w.buffered_frames / fs * 1000, 1), "underruns": getattr(w, "underruns", 0),
           "stressed": eng._stressed_until > time.monotonic(),
           "feed_ticks": _percentiles(list(getattr(w, "ticks", []))),
           "decoded_s": round(eng._decoded / fs, 1), "total_s": round(eng._total / fs, 1),
           "cursor_s": round(eng._cursor / fs, 1), "decode_complete": eng._complete,
           "buffer_mb": round(eng._buf.nbytes / 1e6, 1), "volume": round(eng._volume, 3),
           "start_latency_ms": getattr(eng, "start_latency_ms", None)}
    ng = eng._ng
    st = eng._norm_settings()
    out["normalizer"] = {"on": st is not None, "mode": (st or {}).get("mode"),
                         "gain": round(float(ng.t), 3) if ng is not None else None}
    lvl = eng._lvl
    if lvl is not None:
        out["normalizer"]["measured"] = {("with_eq" if k is not None else "as_decoded"): v for k, v in list(lvl.vals.items())}
    return out


def chain_state(win) -> dict:
    """The DSP chain: master, limiter, trip state, latency, load, and the cost of each module."""
    ch = win._dsp_chain
    st = ch.stats()
    keep = ("load", "avg_ms", "peak_ms", "latency", "limiting", "limiter_db", "active", "tripped", "master", "blocks")
    out = {k: st[k] for k in keep}
    out["limiter_on"] = ch.limiter
    out["latency_ms"] = round(st["latency"] / max(1, win._player._dsp.fs) * 1000, 2)
    out["modules"] = [{k: m[k] for k in ("name", "enabled", "failed", "avg_ms", "peak_ms")} for m in st["modules"]]
    return out


def player_state(win) -> dict:
    p = win._player
    t = p.current
    out = {"track": str(t.path) if t is not None else None, "index": p.index, "queue": len(p.queue),
           "engine": "dsp" if p._dsp_active else "vgm" if p._vgm_active else "fur" if p._fur_active else "media",
           "mode": p.mode, "shuffle": p.shuffle}
    if p._dsp_active:
        out["playing"] = p._dsp.is_playing
        out["position_ms"] = p._dsp.position_ms
        out["duration_ms"] = p._dsp.duration_ms
    return out


def snapshot(reason: str = "") -> dict:
    """The state of the program as a plain dict (what SIGUSR2 writes). Safe from any thread and while the GUI thread
    is stuck: it reads Python attributes and /proc, and what only the GUI thread may ask Qt for comes from the cache
    the heartbeat keeps."""
    win = _S.window() if _S.window else None
    now = time.monotonic()
    snap = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason, "pid": os.getpid(),
            "uptime_s": round(now - _S.started), "app": _static(), "process": _section(plat_diag.memory),
            "python_threads": [t.name for t in threading.enumerate()], "os_threads": plat_diag.thread_names(),
            "ui": dict(_S.cache)}
    snap["ui_heartbeat_age_s"] = round(now - _S.beat, 2) if _S.beat else None
    if win is not None:
        snap["audio"] = _section(lambda: audio_state(win))
        snap["player"] = _section(lambda: player_state(win))
        snap["dsp_chain"] = _section(lambda: chain_state(win))
    snap["stalls"] = [{k: s[k] for k in ("time", "seconds", "where", "stack", "ended")} for s in stalls()]
    try:
        from applog import tail
        from diag_report import scrub_text
        snap["log_tail"] = [scrub_text(ln) for ln in tail.last(300)]
    except Exception as e:
        snap["log_tail"] = [f"unavailable: {e!r}"]
    return snap


def write_snapshot(reason: str = "") -> Path | None:
    path = DEBUG_DIR / f"snapshot-{os.getpid()}-{_stamp()}.json"
    try:
        _write_private(path, json.dumps(snapshot(reason), indent=1, default=str, ensure_ascii=False))
    except Exception as e:
        log.warning("could not write the state snapshot: %s", e)
        return None
    _prune("snapshot-*.json")
    log.info("state snapshot written to %s", path)
    return path
