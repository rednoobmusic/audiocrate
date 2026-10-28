import sys
import os
import signal

# A frozen build has no script to start for the iPod helper, so the app answers for it (see ipod.helper_cmd).
if getattr(sys, "frozen", False) and "--ipod-helper" in sys.argv:
    import _bootstrap  # noqa: F401
    import ipod
    sys.exit(ipod.main(sys.argv[sys.argv.index("--ipod-helper") + 1:]))


if __name__ == "__main__" and ("--debug-dump" in sys.argv[1:] or "--debug-stacks" in sys.argv[1:]):
    import _bootstrap  # noqa: F401
    import diag_cli
    sys.exit(diag_cli.main(sys.argv[1:]))


def _supervise() -> int | None:
    """Run the app in a child and start it again if it dies of a native crash
    right after launch.

    The sound server's client library occasionally takes the whole process down
    with a segfault while Qt asks it for the output device, which happens once at
    startup and cannot be caught from Python. The child is the real program; this
    parent only watches. A crash in the first 20 seconds is retried (twice at
    most), anything later, or a normal exit, is passed straight through. Set
    AUDIOCRATE_NO_SUPERVISOR=1 to run without it.
    """
    import subprocess
    import time
    crashed = {signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGILL}
    code = 0
    for attempt in range(3):
        started = time.monotonic()
        child = subprocess.Popen([sys.executable, *sys.orig_argv[1:]],
                                 env={**os.environ, "AUDIOCRATE_SUPERVISED": "1"})
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda n, _f, c=child: c.poll() is None and c.send_signal(n))
        code = child.wait()
        died = -code in crashed if code < 0 else False
        if not died or time.monotonic() - started > 20 or attempt == 2:
            break
        print(f"AudioCrate crashed on startup ({signal.Signals(-code).name}), starting it again",
              file=sys.stderr)
        time.sleep(1.0)
    return code if code >= 0 else 128 - code


if (__name__ == "__main__" and not getattr(sys, "frozen", False)
        and not os.environ.get("AUDIOCRATE_SUPERVISED")
        and not os.environ.get("AUDIOCRATE_NO_SUPERVISOR")):
    sys.exit(_supervise())

import _bootstrap  # noqa: F401  (puts core, panels, integrations and ui on sys.path)
import memtune  # noqa: E402

memtune.early()
if getattr(sys, "frozen", False):
    import ipod
    ipod.FROZEN_HELPER = True

# Must happen before any Qt audio class exists: libpulse and libpipewire read
# this process's identity out of the environment when they initialise, and fall
# back to the interpreter's name ("python3") when it isn't there. Importing
# QtMultimedia is what eventually loads them. See core/audio_identity.py.
import audio_identity

audio_identity.apply()

# After audio_identity, because that has to run before any Qt audio class
# exists, and before everything else, so a crash on the way up still leaves a
# trace. faulthandler turns a bare "Segmentation fault" into the Python stack
# of every thread, written to the terminal and to ~/.config/audiocrate/crash.log.
import logs  # noqa: E402

logs.setup()

import diag  # noqa: E402

diag.install()

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import Qt, QTimer, QUrl  # noqa: E402
from main_window import MainWindow  # noqa: E402


def _suppress_alsa_noise():

    class _FilteredStderr:
        _SUPPRESS = (
            "No sample format supported",
            "Failed to open VDPAU backend",
            "alsa_input.",
            "alsa_output.",
        )

        def write(self, s):
            if any(tok in s for tok in self._SUPPRESS):
                return len(s)
            return sys.__stderr__.write(s)

        def flush(self):
            sys.__stderr__.flush()

        def fileno(self):
            return sys.__stderr__.fileno()

    sys.stderr = _FilteredStderr()


def _keep_resident():
    """Stop the kernel swapping this program out. On a machine that runs short of memory
    the pages a song change touches (styling, art, the engine's bookkeeping) are the
    ones that have gone to swap, and every one of them is a wait for the disk or the
    compressor: seconds of freeze, and dropped audio when the feed thread is the one
    waiting. MCL_ONFAULT pins what is in memory and whatever comes in later, without
    pulling in pages already swapped out or filling empty address space.
    AUDIOCRATE_NO_MLOCK=1 turns it off."""
    memtune.trim(force=True)    # free heap goes back first, pinned it could not be
    if os.environ.get("AUDIOCRATE_NO_MLOCK"):
        return
    try:
        import ctypes
        MCL_CURRENT, MCL_FUTURE, MCL_ONFAULT = 1, 2, 4
        libc = ctypes.CDLL(None, use_errno=True)
        ok = libc.mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT) == 0
        err = ctypes.get_errno()
    except Exception as e:
        ok, err = False, str(e)
    from applog import log
    if ok:
        log.info("memory kept resident (mlockall)")
    else:
        log.warning("could not keep memory resident: %s (the memlock limit is probably low)", err)


if __name__ == "__main__":
    _suppress_alsa_noise()

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    # Name, display name, version, and the audiocrate.desktop association that
    # Wayland and the XDG portals use to find our icon.
    audio_identity.apply_to_qt(app)

    # bundled Nerd Font, the icon glyphs come from it
    from PySide6.QtGui import QFontDatabase, QIcon
    from pathlib import Path
    _font_dir = Path(__file__).parent / "JetBrainsMono"
    for _name in ("JetBrainsMonoNerdFontPropo-Regular.ttf",
                  "JetBrainsMonoNerdFontPropo-Bold.ttf",
                  "JetBrainsMonoNerdFontPropo-Italic.ttf"):
        QFontDatabase.addApplicationFont(str(_font_dir / _name))

    # the theme choice has to be known before any widget exists
    import json
    _cfg_file = Path.home() / ".config" / "audiocrate" / "settings.json"
    _system_theme = True
    try:
        _system_theme = json.loads(_cfg_file.read_text(encoding="utf-8")).get("system_theme", True)
    except Exception:
        pass

    if not _system_theme:
        app.setStyle("Fusion")
        from theme import QSS
        app.setStyleSheet(QSS)

    # next to the executable, which works from the source tree and from the AppImage
    _icon_path = Path(__file__).parent / "icon.png"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))

    w = MainWindow()
    w.show()
    diag.attach(w)

    # Everything built so far lives as long as the program does. Frozen, it is left
    # out of later garbage collections: a collection stops every thread, the audio
    # feed included, for as long as it takes to walk the heap, and this heap is big.
    import gc
    gc.collect()
    gc.freeze()

    # After the first query of the sound server for the same reason as the switch interval
    # below: pinning pages while its client library is starting up is a risk it does not need.
    _keep_resident()

    # The GUI thread shares the interpreter lock with the decode, waveform, art and
    # network threads, and every Python event filter or paint handler it runs has to
    # win the lock back first. At the default 5 ms a busy thread can keep it that long
    # each time, which turned a menu opening or a song change into a visible stall.
    # Set only now, so that startup, which includes the first query of the sound
    # server, runs with the interpreter's default timing.
    sys.setswitchinterval(0.0005)

    # Files and links from the file manager or the command line (Exec=... %U): files go to
    # the playlist and play, a web link goes to the Online panel.
    def _as_path(a):
        if not a.startswith("file://"):
            return a
        u = QUrl(a)
        return u.path() if u.host() in ("", "localhost") else u.toLocalFile()

    _args = [_as_path(a) for a in sys.argv[1:]]
    _open = [a for a in _args if os.path.isfile(a)]
    if _open:
        QTimer.singleShot(0, lambda: w.open_files(_open))
    _dirs = [Path(a) for a in _args if os.path.isdir(a)]
    if _dirs:
        QTimer.singleShot(0, lambda: w._scan_folders_async(_dirs))
    _links = [a for a in _args if a.startswith(("http://", "https://"))]
    if _links:
        QTimer.singleShot(0, lambda: w.open_link(_links[0]))
    w.start_tour(skip=bool(_open or _links or _dirs))

    for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(_sig, lambda *_: w.close())
    # Python-level signal handlers only run when the interpreter wakes up, so tick once a second.
    _sig_timer = QTimer()
    _sig_timer.setInterval(1000)
    _sig_timer.timeout.connect(lambda: None)
    _sig_timer.start()

    ret = app.exec()
    diag.shutdown()
    # libfurnace's engine (created up-front for .fur support) starts a native
    # thread that never terminates and that fw_destroy doesn't stop, so a normal
    # interpreter shutdown hangs the process. State is already persisted in the
    # window's closeEvent, so hard-exit to guarantee the process actually quits.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(ret if ret is not None else 0)
