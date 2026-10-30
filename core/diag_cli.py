"""`python main.py --debug-dump` and `--debug-stacks`: ask the running AudioCrate what it is doing.

Finds the instance through its pid file (diag.find_instance, which checks that the process is yours, still the one that
wrote the file and running main.py), sends it SIGUSR1 (stacks) and SIGUSR2 (state snapshot) and nothing else (on Windows
it sets the two named events that stand in for them), waits up to five seconds for the new files and prints where they
are with a short summary. It never ends or restarts anything.
Exit status: 0 files written, 1 no instance, 2 the instance did not answer in time.
"""
from __future__ import annotations

import json
import sys
import time

import diag
import plat_diag

WAIT_S = 5.0


def _newer(pattern: str, since: float):
    try:
        found = [p for p in diag.DEBUG_DIR.glob(pattern) if p.stat().st_mtime >= since]
    except OSError:
        return None
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _main_line(stacks) -> str:
    try:
        for line in stacks.read_text(encoding="utf-8", errors="replace").splitlines()[:6]:
            if line.startswith("main thread:"):
                return line
    except OSError:
        pass
    return ""


def _summary(snap: dict) -> list[str]:
    out = []
    proc, audio, play = snap.get("process", {}), snap.get("audio", {}), snap.get("player", {})
    if proc and "swap_mb" in proc:
        out.append(f"memory: {proc.get('rss_mb', '?')} MB resident, {proc.get('pss_mb', '?')} MB proportional, "
                   f"{proc.get('swap_mb', '?')} MB in swap, {proc.get('thread_count', '?')} threads")
    elif proc:                                  # Windows has no swap or sharing figures per process
        out.append(f"memory: {proc.get('rss_mb', '?')} MB resident, {proc.get('private_mb', '?')} MB private, "
                   f"{proc.get('thread_count', '?')} threads")
    if audio and "error" not in audio:
        state = "playing" if audio.get("playing_intent") else "not playing"
        out.append(f"audio: {state}, {audio.get('device') or 'no device name'}, {audio.get('rate')} Hz, "
                   f"{audio.get('queue_ms')} ms queued, {audio.get('underruns', 0)} underruns")
    if play.get("track"):
        out.append(f"track: {play['track']} at {play.get('position_ms', 0) // 1000} s")
    stalls = snap.get("stalls") or []
    if stalls:
        last = stalls[-1]
        out.append(f"UI stalls recorded: {len(stalls)}, the last {last['seconds']:.1f} s in {last['where']}")
    age = snap.get("ui_heartbeat_age_s")
    if age is not None and age > diag.STALL_S:
        out.append(f"the window has not answered for {age:.1f} s")
    return out


def main(argv: list[str]) -> int:
    stacks_only = "--debug-stacks" in argv
    pid, why = diag.find_instance()
    if pid is None:
        print(why, file=sys.stderr)
        return 1
    raw = diag.DEBUG_DIR / f"stacks-{pid}-raw.txt"
    since = time.time()
    try:
        plat_diag.send_trigger(pid, "stacks")
        if not stacks_only:
            plat_diag.send_trigger(pid, "snapshot")
    except OSError as e:
        print(f"Could not signal AudioCrate (pid {pid}): {e.strerror}.", file=sys.stderr)
        return 1
    stacks = snap = None
    end = time.monotonic() + WAIT_S
    while time.monotonic() < end:
        stacks = stacks or _newer(f"stacks-{pid}-*[0-9].txt", since)
        snap = snap or _newer(f"snapshot-{pid}-*.json", since)
        if stacks and (snap or stacks_only):
            break
        time.sleep(0.05)
    print(f"AudioCrate pid {pid}")
    if stacks is None:
        print(f"It did not write the files within {WAIT_S:.0f} s, so its Python threads are probably stuck in native code.",
              file=sys.stderr)
        try:
            lines = raw.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        if lines:
            starts = [i for i, ln in enumerate(lines) if ln.startswith(("Thread ", "Current thread"))]
            cur = starts[-1] if starts else 0                  # faulthandler lists the oldest thread, the main one, last
            print(f"The signal handler did write the raw stacks to {raw}. The main thread:", file=sys.stderr)
            print("\n".join("  " + ln for ln in lines[cur:cur + 9]), file=sys.stderr)
        return 2
    print(f"stacks:   {stacks}")
    if not stacks_only:
        print(f"snapshot: {snap if snap else 'not written in time'}")
    line = _main_line(stacks)
    if line:
        print(line)
    if snap:
        try:
            for ln in _summary(json.loads(snap.read_text(encoding="utf-8"))):
                print(ln)
        except (OSError, ValueError):
            pass
    return 0
