import sys
import os
import signal
import _bootstrap  # noqa: F401  — puts core/panels/integrations/ui on sys.path

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

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
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


if __name__ == "__main__":
    _suppress_alsa_noise()

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    # Name, display name, version, and the audiocrate.desktop association that
    # Wayland and the XDG portals use to find our icon.
    audio_identity.apply_to_qt(app)

    # Load bundled Nerd Font so icons render correctly on all platforms.
    from PySide6.QtGui import QFontDatabase, QIcon
    from pathlib import Path
    _font_dir = Path(__file__).parent / "JetBrainsMono"
    for _name in ("JetBrainsMonoNerdFontPropo-Regular.ttf",
                  "JetBrainsMonoNerdFontPropo-Bold.ttf",
                  "JetBrainsMonoNerdFontPropo-Italic.ttf"):
        QFontDatabase.addApplicationFont(str(_font_dir / _name))

    # Read saved system_theme preference before creating any widgets
    import json
    _cfg_file = Path.home() / ".config" / "audiocrate" / "settings.json"
    _system_theme = True  # default: use system theme
    try:
        _system_theme = json.loads(_cfg_file.read_text(encoding="utf-8")).get("system_theme", True)
    except Exception:
        pass

    if not _system_theme:
        app.setStyle("Fusion")
        from theme import QSS
        app.setStyleSheet(QSS)

    # Window / taskbar icon — look next to the executable (works both dev and AppImage)
    _icon_path = Path(__file__).parent / "icon.png"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))

    w = MainWindow()
    w.show()

    # Files passed by the file manager / CLI (Exec=… %F): add to playlist + play.
    _open = [a for a in sys.argv[1:] if os.path.exists(a)]
    if _open:
        QTimer.singleShot(0, lambda: w.open_files(_open))

    signal.signal(signal.SIGINT, lambda *_: w.close())
    # Wake the interpreter so Python-level signal handlers run; 1s is plenty for
    # Ctrl-C latency and 5× fewer wakeups than the old 200ms.
    _sig_timer = QTimer()
    _sig_timer.setInterval(1000)
    _sig_timer.timeout.connect(lambda: None)
    _sig_timer.start()

    ret = app.exec()
    # libfurnace's engine (created up-front for .fur support) starts a native
    # thread that never terminates and that fw_destroy doesn't stop, so a normal
    # interpreter shutdown hangs the process. State is already persisted in the
    # window's closeEvent, so hard-exit to guarantee the process actually quits.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(ret if ret is not None else 0)
