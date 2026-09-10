from __future__ import annotations
import json
import base64
from pathlib import Path
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QStatusBar, QFileDialog, QWidget,
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QApplication, QMenu,
)
from PySide6.QtCore import Qt, QByteArray, Signal, QTimer, QUrl
from PySide6.QtGui  import QPainter, QPainterPath, QAction
from PySide6.QtMultimediaWidgets import QVideoWidget

_STATE_FILE = Path.home() / ".config" / "audiocrate" / "ui_state.json"

_DEFAULT_LAYOUT = json.loads('{"geometry": "AdnQywADAAAAAAABAAAGRwAABcsAAAr4AAAAAQAABkcAAAXLAAAK+AAAAAAAAAAACgAAAAABAAAGRwAABcsAAAr4", "dock_state": "AAAA/wAAAAD9AAAABAAAAAAAAALhAAAEhPwCAAAAAfwAAAAbAAAEhAAAAToA/////AIAAAAD/AAAABsAAAJUAAAAAAD////8AQAAAAH8AAACIAAAAbwAAAAAAP////r/////AgAAAAD7AAAAFgBOAG8AdwAgAFAAbABhAHkAaQBuAGcBAAAAGwAAA3AAAACnAP///vsAAAAQAEMAbwBuAHQAcgBvAGwAcwEAAAOPAAABEAAAAI8A///+AAAAAQAAAuIAAASE/AIAAAAD/AAAABsAAAL3AAAA3QEAABz6AAAAAwEAAAAE+wAAABAAUABsAGEAeQBsAGkAcwB0AQAAAAD/////AAAAsgD////7AAAADgBMAGkAYgByAGEAcgB5AQAAAAD/////AAAA7AD////7AAAACABJAG4AZgBvAQAAAAD/////AAAAbwD////7AAAAEABTAGUAdAB0AGkAbgBnAHMBAAAAAP////8AAACJAP////sAAAAMAEwAeQByAGkAYwBzAQAAAxYAAAGJAAAALwD///78AAACwQAAAd4AAAAAAP////r/////AgAAAAAAAAACAAACkwAAAIz8AQAAAAH7AAAAEABTAHAAZQBjAHQAcgB1AG0AAAABnAAAApMAAACWAP///wAAAAMAAAIaAAAEgPwBAAAAAfsAAAAYAE8AcwBjAGkAbABsAG8AcwBjAG8AcABlAAAAAZwAAAB1AAAApAD///8AAAAAAAAEhAAAAAEAAAACAAAAAQAAAAL8AAAAAA==", "docks": {"Info": {"geo": "AdnQywADAAD///sq///7M////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAACgD///sq///7M////gv///4L", "visible": true}, "Library": {"geo": "AdnQywADAAD///sq///7M////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAACgD///sq///7M////gv///4L", "visible": true}, "Playlist": {"geo": "AdnQywADAAD///sq///7M////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAACgD///sq///7M////gv///4L", "visible": true}, "Lyrics": {"geo": "AdnQywADAAAAAALpAAADFgAABcoAAASeAAAAAAAAAAD//////////wAAAAAAAAAACgAAAALpAAADFgAABcoAAASe", "visible": true}, "Settings": {"geo": "AdnQywADAAAAAALpAAAAGwAABcoAAALzAAAAAAAAAAD//////////wAAAAAAAAAACgAAAALpAAAAGwAABcoAAALz", "visible": true}, "Oscilloscope": {"geo": "AdnQywADAAAAAAGcAAAAGwAAAhAAAASaAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAGcAAAAGwAAAhAAAASa", "visible": false}, "Controls": {"geo": "AdnQywADAAAAAAAAAAADjwAAAuAAAASeAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAAAAAADjwAAAuAAAASe", "visible": true}, "Now Playing": {"geo": "AdnQywADAAAAAAAAAAAAGwAAAuAAAAOKAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAAAAAAAGwAAAuAAAAOK", "visible": true}, "Spectrum": {"geo": "AdnQywADAAAAAAGcAAAAFwAABC4AAACiAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAGcAAAAFwAABC4AAACi", "visible": false}, "Video": {"geo": "AdnQywADAAAAAAAAAAAAGwAAAuAAAAOKAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAAAAAAAGwAAAuAAAAOK", "visible": false}}}')


def _dock_of(widget: QWidget) -> QDockWidget | None:
    p = widget.parentWidget()
    while p:
        if isinstance(p, QDockWidget):
            return p
        p = p.parentWidget()
    return None


class _CollapsibleMenu(QMenu):
    """A QMenu whose actions tagged `section_header` collapse/expand their group
    in place without dismissing the menu (so categories fold like a tree)."""

    def _is_header(self, act):
        return act is not None and bool(act.property("section_header"))

    def mouseReleaseEvent(self, e):
        act = self.actionAt(e.position().toPoint())
        if self._is_header(act):
            act.trigger()          # run the collapse toggle, keep the menu open
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        act = self.activeAction()
        if self._is_header(act) and e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            act.trigger()
            return
        super().keyPressEvent(e)


class _CollapseNotch(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Hide title bar")

    def paintEvent(self, _):
        p   = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        fg  = pal.text().color()
        fg.setAlpha(120)
        p.setPen(Qt.NoPen)
        p.setBrush(fg)
        path = QPainterPath()
        w, h = self.width(), self.height()
        if not self._collapsed:
            path.moveTo(w * 0.2, h * 0.65)
            path.lineTo(w * 0.5, h * 0.35)
            path.lineTo(w * 0.8, h * 0.65)
        else:
            path.moveTo(w * 0.2, h * 0.35)
            path.lineTo(w * 0.5, h * 0.65)
            path.lineTo(w * 0.8, h * 0.35)
        path.closeSubpath()
        p.drawPath(path)
        p.end()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        dock = _dock_of(self)
        if not dock:
            return
        self._collapsed = not self._collapsed
        if self._collapsed:
            dock.setTitleBarWidget(_RestoreTab())
        else:
            dock.setTitleBarWidget(_DockTitleBar())


class _RestoreTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Show title bar")

    def paintEvent(self, _):
        p   = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        fg  = pal.highlight().color()
        fg.setAlpha(160)
        p.setBrush(fg)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        notch_w = min(40, w // 4)
        cx      = w // 2
        p.drawRoundedRect(cx - notch_w // 2, 1, notch_w, h - 2, 2, 2)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            dock = _dock_of(self)
            if dock:
                dock.setTitleBarWidget(_DockTitleBar())


class _DockTitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 2, 2)
        layout.setSpacing(2)

        self._lbl = QLabel()
        self._lbl.setStyleSheet("font-size: 11px; font-weight: bold;")
        self._lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._lbl)

        notch = _CollapseNotch(self)
        layout.addWidget(notch)

        float_btn = QPushButton("⧉")
        float_btn.setFlat(True)
        float_btn.setFixedSize(18, 18)
        float_btn.setToolTip("Float / dock")
        float_btn.clicked.connect(self._toggle_float)
        layout.addWidget(float_btn)

    def _toggle_float(self):
        dock = _dock_of(self)
        if dock:
            dock.setFloating(not dock.isFloating())

    def showEvent(self, e):
        super().showEvent(e)
        dock = _dock_of(self)
        if dock:
            self._lbl.setText(dock.windowTitle())


try:
    from shiboken6 import isValid as _qt_valid
except ImportError:
    def _qt_valid(obj) -> bool:
        return obj is not None


def _native_float(dock: QDockWidget):
    dock.setTitleBarWidget(_DockTitleBar())

    def _on_float(floating: bool):
        if not _qt_valid(dock):
            return
        if floating:
            geo = dock.saveGeometry()
            dock.setWindowFlags(
                Qt.Window |
                Qt.WindowTitleHint |
                Qt.WindowSystemMenuHint |
                Qt.WindowMinimizeButtonHint |
                Qt.WindowMaximizeButtonHint |
                Qt.WindowCloseButtonHint
            )
            dock.restoreGeometry(geo)
            dock.setTitleBarWidget(None)
            dock.show()
        else:
            dock.setTitleBarWidget(_DockTitleBar())
    dock.topLevelChanged.connect(_on_float)

from player            import Player, VIDEO_EXTS, PLAYABLE_EXTS, _GME_EXTS, extract_art
import cli_render as _cli_render
from analyzer          import Analyzer
from panel_controls    import ControlsWidget
from panel_library     import LibraryWidget
from panel_playlist    import PlaylistPanel
from panel_visualizer  import VisualizerWidget
from panel_lyrics      import LyricsWidget
from panel_oscilloscope import OscilloscopeWidget
from panel_metadata    import MetadataWidget
from panel_nowplaying  import NowPlayingWidget
from panel_upnext      import UpNextWidget
from panel_eq          import EqWidget
from panel_settings    import SettingsWidget
from mpris             import MPRISService
from discord_rpc       import DiscordRPCService
from scrobbler         import ScrobbleController
from play_stats        import PlayStatsTracker
from panel_stats       import StatsWidget
from sleep_timer       import SleepTimer
from theme             import make_qss

# Transition duration: 30 ticks × 28ms ≈ 840ms

# Settings-key prefix -> ScrobbleController service name
_SCROBBLE_SERVICES = {"lastfm": "LastFM", "librefm": "LibreFM"}

class MainWindow(QMainWindow):
    _palette_ready = Signal(object)
    _fur_wave_ready = Signal(str)   # full .fur waveform WAV, rendered in the background
    _folder_scanned = Signal(list)  # background folder walk finished (add folder / drop)

    def __init__(self):
        super().__init__()
        try:
            from _version import VERSION
            self.setWindowTitle(f"AudioCrate {VERSION}")
        except Exception:
            self.setWindowTitle("AudioCrate")
        self.resize(1280, 800)

        self._player         = Player(self)
        self._analyzer       = Analyzer()
        self._analyzer_loaded_path = None   # track the analyzer has decoded (RAM gating)
        self._state_restored = False
        self._art_color_on   = False
        self._last_browse_dir = str(Path.home())
        from concurrent.futures import ThreadPoolExecutor
        self._palette_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="palette")
        # Created before _build_ui because _wire_settings configures it on startup.
        self._discord = DiscordRPCService(self._player, self)
        self._scrobbler = ScrobbleController(self._player, self)
        self._play_stats = PlayStatsTracker(self._player, self)

        self._build_ui()
        self._build_menu()

        self._mpris = MPRISService(self._player, self)

        # Mobile web remote — off unless AUDIOCRATE_WEB=1 or settings.json says
        # so. Installed after _build_ui() because it hands enqueues to the
        # playlist panel to keep the desktop list in sync.
        try:
            import webremote
            self._webremote = webremote.install(self._player, self, self)
        except Exception:
            from applog import log
            log.exception("web remote failed to start")
            self._webremote = None

        # Sleep timer: timed countdown pauses playback; "end of track" is handled
        # by the player (stop_after_current). Both clear the menu selection.
        self._sleep = SleepTimer(self)
        self._sleep.triggered.connect(self._on_sleep_elapsed)
        self._sleep.tick.connect(self._on_sleep_tick)
        self._player.sleep_stopped.connect(self._on_sleep_elapsed)

        self._palette_ready.connect(self._apply_art_palette)
        self._player.track_changed.connect(self._on_track_changed)
        self._player.state_changed.connect(self._on_player_state)
        self._player.queue_changed.connect(self._on_queue_changed)
        self._player.chip_rendering.connect(
            lambda on: self._show_status("Rendering…")
            if on else self._status_bar.clearMessage())
        self._player.error.connect(lambda m: self._show_status(m, 6000))
        self._folder_scanned.connect(self._on_folder_scanned)
        self._player.chip_ready.connect(self._on_chip_ready)
        # .fur output is streamed here → analyzer (scope/spectrum) + waveform bar.
        if self._player._fur is not None:
            self._player._fur.waveform_chunk.connect(self._on_fur_pcm)
        # VGM/GME chiptunes stream their PCM the same way (int16, separate handler).
        self._player._vgm.waveform_chunk.connect(self._on_vgm_pcm)
        self._fur_wave_ready.connect(self._on_fur_wave_ready)
        self._fur_wav_tmp = None
        self._fur_wave_cancel = None

        # Global Space = play/pause (except when typing or in a dialog).
        QApplication.instance().installEventFilter(self)

        # Drop audio files/folders anywhere on the window to enqueue them.
        self.setAcceptDrops(True)

        # Pause heavy visualizers when the app is unfocused/minimized (perf).
        QApplication.instance().applicationStateChanged.connect(
            lambda *_: self._update_visual_suspend()
        )

        from PySide6.QtCore import QTimer as _QTimer
        self._save_timer = _QTimer(self)
        self._save_timer.setInterval(10_000)
        self._save_timer.timeout.connect(self._save_ui_state)
        self._save_timer.start()

        # Debounce timer: coalesces rapid queue_changed bursts (e.g. loading
        # a big folder) into a single prefetch sweep 300ms after the last add.
        self._prefetch_timer = _QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(300)
        self._prefetch_timer.timeout.connect(self._prefetch_all_palettes)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if (event.type() == QEvent.Type.KeyPress
                and not event.isAutoRepeat()
                and QApplication.activeModalWidget() is None):
            if self._handle_media_key(event):
                return True
        return super().eventFilter(obj, event)

    def _handle_media_key(self, event) -> bool:
        """App-wide playback hotkeys. Skips text inputs entirely, and leaves plain
        arrows alone when a list/tree/slider is focused so normal navigation works.
        Multimedia keys are handled too (in addition to MPRIS)."""
        from PySide6.QtWidgets import (
            QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox,
            QAbstractItemView, QAbstractSlider,
        )
        K = Qt.Key
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit,
                           QAbstractSpinBox, QComboBox)):
            return False   # user is typing

        k    = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        if k in (K.Key_Space, K.Key_MediaTogglePlayPause,
                 K.Key_MediaPlay, K.Key_MediaPause):
            self._player.toggle(); return True
        if k in (K.Key_MediaStop, K.Key_S):
            self._player.stop(); return True
        if k == K.Key_MediaNext or k == K.Key_N or (ctrl and k == K.Key_Right):
            self._player.next(); return True
        if k == K.Key_MediaPrevious or k == K.Key_P or (ctrl and k == K.Key_Left):
            self._player.prev(); return True
        if k in (K.Key_M, K.Key_VolumeMute):
            self._toggle_mute(); return True
        if k == K.Key_R:
            self._controls._cycle_mode(); return True
        if k == K.Key_E:
            if self._controls._wand.isEnabled():
                self._controls._wand.toggle()
            return True
        if k == K.Key_Period:
            self._player.seek_delta(5); return True
        if k == K.Key_Comma:
            self._player.seek_delta(-5); return True
        if k == K.Key_VolumeUp:
            self._nudge_volume(+5); return True
        if k == K.Key_VolumeDown:
            self._nudge_volume(-5); return True

        # Plain arrows: only when a navigable widget isn't focused.
        if not isinstance(fw, (QAbstractItemView, QAbstractSlider)) and not ctrl:
            if k == K.Key_Right:
                self._player.seek_delta(5);  return True
            if k == K.Key_Left:
                self._player.seek_delta(-5); return True
            if k == K.Key_Up:
                self._nudge_volume(+5); return True
            if k == K.Key_Down:
                self._nudge_volume(-5); return True
        return False

    def _nudge_volume(self, delta: int):
        sl = self._controls._vol
        sl.setValue(max(0, min(100, sl.value() + delta)))

    def _toggle_mute(self):
        sl = self._controls._vol
        if getattr(self, "_pre_mute_vol", None) is not None:
            sl.setValue(self._pre_mute_vol)
            self._pre_mute_vol = None
            self._show_status("Unmuted", 1500)
        else:
            self._pre_mute_vol = sl.value()
            sl.setValue(0)
            self._show_status("Muted", 1500)

    def changeEvent(self, e):
        from PySide6.QtCore import QEvent
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange:
            self._update_visual_suspend()

    # drag & drop: enqueue dropped audio files / folders
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        locals_ = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if not locals_:
            return
        e.acceptProposedAction()
        files = [p for p in locals_ if p.is_file() and p.suffix.lower() in PLAYABLE_EXTS]
        dirs  = [p for p in locals_ if p.is_dir()]
        if files:
            self._playlist.add_paths(files)
            self._show_status(f"Added {len(files)} track(s)", 3000)
        if dirs:
            self._scan_folders_async(dirs)

    def _scan_folders_async(self, folders: list[Path]):
        """Walk dropped/chosen folders off the GUI thread — a big folder used to
        freeze the whole window during rglob."""
        self._show_status("Scanning folder(s)…")
        import threading

        def _walk(dirs=list(folders)):
            out: list[Path] = []
            for d in dirs:
                try:
                    out += sorted((f for f in d.rglob("*")
                                   if f.is_file() and f.suffix.lower() in PLAYABLE_EXTS),
                                  key=lambda p: p.name.lower())
                except Exception:
                    pass
            if _qt_valid(self):
                self._folder_scanned.emit(out)

        threading.Thread(target=_walk, daemon=True).start()

    def _on_folder_scanned(self, paths: list):
        self._status_bar.clearMessage()
        if paths:
            self._playlist.add_paths(paths)
            self._show_status(f"Added {len(paths)} track(s)", 3000)
        else:
            self._show_status("No playable files found", 3000)

    def _update_visual_suspend(self):
        """Suspend the spectrum/oscilloscope timers while minimized or unfocused
        (opt-out via Settings > Layout > Auto-suspend visualizers)."""
        if not getattr(self, "_viz_auto_suspend", True):
            suspend = False
        else:
            app = QApplication.instance()
            suspend = self.isMinimized() or (app.applicationState() != Qt.ApplicationActive)
        for w in (getattr(self, "_viz", None), getattr(self, "_osc", None),
                  getattr(self, "_eq_panel", None), getattr(self, "_milkdrop", None)):
            if w is not None and hasattr(w, "set_suspended"):
                w.set_suspended(suspend)

    def _on_visual_visibility(self, _visible: bool):
        """For VIDEO only: decode the track for the analyzer while a visualizer is
        shown, free it when hidden. (Audio reads the engine buffer; .fur is live.)"""
        cur = self._player.current
        is_video = cur is not None and cur.path.suffix.lower() in VIDEO_EXTS
        if not is_video:
            return
        if self._visuals_shown():
            if self._analyzer_loaded_path != cur.path:
                self._analyzer.set_engine_source(None)
                self._analyzer.set_live(False)
                self._analyzer.load_track(cur.path)
                self._analyzer_loaded_path = cur.path
        elif self._analyzer_loaded_path is not None:
            self._analyzer.stop()
            self._analyzer_loaded_path = None

    def showEvent(self, e):
        super().showEvent(e)
        if not self._state_restored:
            self._state_restored = True
            # Defer one event loop tick so Qt finishes its initial dock layout
            # before we overwrite it with the saved state.
            QTimer.singleShot(0, self._restore_ui_state)
            # and another after that, so the cascade runs on the docks as they
            # finally sit rather than on the layout Qt started with
            QTimer.singleShot(0, self._start_fx)

    def _start_fx(self):
        import fx
        docks = self.findChildren(QDockWidget)
        fx.stagger_in([d for d in docks if d.isVisible()])
        self._particles = fx.CursorParticles(self)
        self._particles.set_accent(self._current_art_palette())
        self._shine = fx.Shine(self._status_bar)
        fx.install_menu_warp(QApplication.instance())

    def _show_status(self, msg: str, ms: int = 0):
        self._status_bar.showMessage(msg, ms)
        # the messages that end in an ellipsis are the ones still working
        shine = getattr(self, "_shine", None)
        if shine is not None:
            shine.start() if msg.rstrip().endswith("\u2026") else shine.stop()

    def _visuals_shown(self) -> bool:
        viz = getattr(self, "_viz", None)
        osc = getattr(self, "_osc", None)
        return bool((viz is not None and viz.isVisible())
                    or (osc is not None and osc.isVisible()))

    def _on_track_changed(self, track):
        self._show_status(f"Now playing: {track.display()}", 6000)
        ext = track.path.suffix.lower()
        if ext == ".fur":
            # .fur is rendered to WAV up-front and played through the DSP engine
            # (same as the other chiptunes), so the scope/spectrum read the engine
            # buffer and the waveform bar loads from the render via chip_ready.
            self._analyzer.set_live(False)
            self._analyzer.set_engine_source(self._player.mono_window)
            self._analyzer_loaded_path = None
        elif _cli_render.supported(ext):
            # AY/YM/VGM/modules: rendered to WAV → played via the DSP engine, so the
            # scope/spectrum read the engine buffer (like plain audio). The waveform
            # bar is filled from the rendered WAV once chip_ready fires.
            self._analyzer.set_live(False)
            self._analyzer.set_engine_source(self._player.mono_window)
            self._analyzer_loaded_path = None
        elif ext in _GME_EXTS:
            # chiptune: spectrum/scope from live PCM; waveform pre-rendered in full via libgme.
            self._analyzer.set_engine_source(None)
            self._analyzer.set_live(True)
            self._controls._wf.load_gme(track.path)
            self._analyzer_loaded_path = None
        elif ext in VIDEO_EXTS:
            # Video plays on QMediaPlayer (no DSP engine buffer) — decode for the
            # analyzer only while a visualizer is shown (RAM gating).
            self._analyzer.set_engine_source(None)
            self._analyzer.set_live(False)
            if self._visuals_shown():
                self._analyzer.load_track(track.path)
                self._analyzer_loaded_path = track.path
            else:
                self._analyzer.stop()
                self._analyzer_loaded_path = None
        else:
            # Plain audio: scope/spectrum read the engine's decode directly — no
            # second decode (no RAM, no track-change lag).
            self._analyzer.set_live(False)
            self._analyzer.set_engine_source(self._player.mono_window)
            self._analyzer_loaded_path = None
        if self._art_color_on:
            import threading
            threading.Thread(
                target=self._fetch_and_apply_palette,
                args=(track.path,), daemon=True,
            ).start()
            self._prefetch_timer.start()   # advance the next-5 prefetch window

    def _on_chip_ready(self, wav: str):
        """A chiptune/module finished rendering — load its waveform bar from the WAV
        (the engine is already playing it)."""
        try:
            self._controls._wf.load(Path(wav))
        except Exception:
            pass

    def _on_fur_pcm(self, frames_so_far, total_frames, rate, pcm_bytes):
        """Rendered .fur output (interleaved stereo f32) → analyzer + waveform."""
        import numpy as np
        arr = np.frombuffer(pcm_bytes, dtype=np.float32)
        if arr.size < 2:
            return
        if arr.size % 2:
            arr = arr[:-1]
        mono = arr.reshape(-1, 2).mean(axis=1)
        self._analyzer.feed_live(mono)
        try:
            self._controls._wf.feed_fur(frames_so_far, total_frames, rate, mono)
        except Exception:
            pass

    def _on_vgm_pcm(self, frames_so_far, total_frames, rate, pcm_bytes):
        """VGM/GME output (interleaved stereo int16) → analyzer + waveform."""
        import numpy as np
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        if arr.size < 2:
            return
        if arr.size % 2:
            arr = arr[:-1]
        mono = (arr.reshape(-1, 2).mean(axis=1).astype(np.float32)) / 32768.0
        self._analyzer.feed_live(mono)
        try:
            self._controls._wf.feed_fur(frames_so_far, total_frames, rate, mono)
        except Exception:
            pass

    def _on_osc_dialog_changed(self, d):
        """Scope popup-dialog edits → push into the Settings panel so they persist
        (settings.json) and both UIs stay in sync."""
        s = self._settings
        s._osc_trigger.setChecked(d["osc_trigger"])
        s._osc_win_sl.setValue(d["osc_window_ms"])
        s._osc_gain_sl.setValue(d["osc_gain"])
        s._osc_smooth_sl.setValue(d["osc_smooth"])
        s._osc_lw_sl.setValue(d["osc_line_width"])
        if "osc_fps" in d:
            s._osc_fps_sl.setValue(d["osc_fps"])
        s._apply_oscilloscope()

    def _start_fur_waveform(self, path):
        """Render the whole .fur to a temp WAV in the background, then load it as the
        full waveform (replaces the progressive 'fills at the line' behaviour)."""
        fur = self._player._fur
        if fur is None:
            return
        import threading
        if self._fur_wave_cancel is not None:
            self._fur_wave_cancel.set()
        cancel = threading.Event()
        self._fur_wave_cancel = cancel
        self._fur_wave_for = path   # guard: hand off only to the track it was rendered for

        def _run():
            wav = None
            try:
                wav = fur.render_region_wav(path, cancel=cancel)
            except Exception:
                wav = None
            if wav and not cancel.is_set():
                self._fur_wave_ready.emit(wav)
            elif wav:
                try:
                    import os
                    os.unlink(wav)
                except Exception:
                    pass
        threading.Thread(target=_run, daemon=True).start()

    def _on_fur_wave_ready(self, wav):
        # Legacy path — .fur now renders up-front and arrives via chip_ready.
        try:
            self._controls._wf.load(Path(wav))
        except Exception:
            pass

    def _on_queue_changed(self):
        if self._art_color_on:
            self._prefetch_timer.start()  # restarts the 300ms debounce window

    def _prefetch_all_palettes(self):
        # Only the current track + the next few in play order — prefetching the
        # whole queue meant thousands of image decodes after adding a big folder.
        from art_color import _palette_cache, _cache_lock
        q = self._player.queue
        idxs = ([self._player.index] if self._player.index >= 0 else [0]) \
            + self._player.upcoming(5)
        paths = [q[i].path for i in idxs if 0 <= i < len(q)]
        uncached = []
        with _cache_lock:
            for p in paths:
                if str(p) not in _palette_cache and p not in uncached:
                    uncached.append(p)
        for path in uncached:
            self._palette_pool.submit(self._prefetch_palette, path)

    def _prefetch_palette(self, path):
        from art_color import _dominant, _derive_palette, _palette_cache, _cache_lock
        key = str(path)
        with _cache_lock:
            if key in _palette_cache:
                return
        data = extract_art(path)
        palette = None
        if data:
            rgb = _dominant(data)
            if rgb:
                palette = _derive_palette(*rgb)
        with _cache_lock:
            _palette_cache[key] = palette

    def _on_player_state(self, playing: bool):
        if not playing and (self._player._normalize or self._player.enhance) and self._player.queue:
            self._show_status("Transcoding\u2026", 2500)

    def _fetch_and_apply_palette(self, path):
        from art_color import _dominant, _derive_palette, _palette_cache, _cache_lock
        key = str(path)
        with _cache_lock:
            if key in _palette_cache:
                palette = _palette_cache[key]
                if _qt_valid(self):
                    self._palette_ready.emit((path, palette))
                return
        data = extract_art(path)
        palette = None
        if data:
            rgb = _dominant(data)
            if rgb:
                palette = _derive_palette(*rgb)
        with _cache_lock:
            _palette_cache[key] = palette
        if _qt_valid(self):
            self._palette_ready.emit((path, palette))

    def _make_qss(self, palette=None) -> str:
        s = self._settings
        return make_qss(
            palette,
            text_font=s.get("text_font", "") or None,
            ui_size=s.get("ui_size", 13),
            accent=s.get("accent_color", "#8040c8"),
            bg=s.get("bg_color", "") or None,
            text=s.get("text_color", "") or None,
            frame_shade=s.get("frame_shade", 0),
        )

    def _on_theme_style_changed(self, d: dict):
        acc = d.get("accent", "#8040c8")
        for v in (getattr(self, "_osc", None), getattr(self, "_viz", None),
                  getattr(self, "_eq_panel", None),
                  getattr(getattr(self, "_controls", None), "_wf", None)):
            if v is not None and hasattr(v, "set_default_accent"):
                v.set_default_accent(acc)
        self._refresh_idle_accents()
        self._reapply_qss()

    def _current_art_palette(self):
        if self._art_color_on and self._player.current:
            from art_color import _palette_cache, _cache_lock
            with _cache_lock:
                return _palette_cache.get(str(self._player.current.path))
        return None

    def _apply_art_palette(self, payload):
        if not _qt_valid(self):
            return
        # One fetch thread per track change, so a slow one can land after the
        # user has moved on. Without this a video, whose art read fails fast and
        # returns nothing, keeps the previous track's colours.
        path, palette = payload
        cur = self._player.current
        if cur is None or str(cur.path) != str(path):
            return
        # Re-polishing every widget is the most expensive thing a track change
        # does — skip it when the palette didn't actually change (same album).
        if palette == getattr(self, "_last_art_palette", "unset"):
            return
        self._last_art_palette = palette
        QApplication.instance().setStyleSheet(self._make_qss(palette))
        self._viz.set_accent(palette)
        self._osc.set_accent(palette)
        self._controls._wf.set_accent(palette)
        self._eq_panel.set_accent(palette)
        # built later than this handler is connected, so it may not exist yet
        np = getattr(self, "_np_widget", None)
        if np is not None:
            np.set_accent(palette)
        parts = getattr(self, "_particles", None)
        if parts is not None:
            parts.set_accent(palette)

    def _refresh_idle_accents(self):
        """Visualizers/waveform follow the OS highlight only on a *pure* system
        theme; with the app's own (purple) theme or art-derived colours, the
        no-art fallback is the app accent — so it matches what's on screen."""
        use_sys = self._system_theme and not self._art_color_on
        self._osc.set_system_accent(use_sys)
        self._viz.set_system_accent(use_sys)
        self._controls._wf.set_system_accent(use_sys)
        eqp = getattr(self, "_eq_panel", None)
        if eqp is not None:
            eqp.set_system_accent(use_sys)

    def _on_system_theme_changed(self, on: bool):
        self._system_theme = on
        app = QApplication.instance()
        if on:
            app.setStyle("")          # restore OS default style
            app.setStyleSheet("")     # clear; rebuilt below once palette settles
        else:
            app.setStyle("Fusion")
        # Defer one tick so the palette swap has propagated before we rebuild.
        QTimer.singleShot(0, self._after_theme_change)

    def _after_theme_change(self):
        self._reapply_qss()
        self._refresh_idle_accents()

    def _on_font_changed(self, font_name: str):
        app = QApplication.instance()
        if app.styleSheet():
            self._reapply_qss()
        else:
            f = app.font()
            f.setFamily(font_name or "JetBrains Mono")
            app.setFont(f)

    def _on_ui_size_changed(self, size: int):
        # Works on any theme: set the global app font size, and if a custom QSS
        # is active (dark/art/size-customised) rebuild it so the new size lands.
        app = QApplication.instance()
        f = app.font()
        f.setPixelSize(int(size))
        app.setFont(f)
        if app.styleSheet():
            self._reapply_qss()

    def _reapply_qss(self):
        """Single source of truth for the active stylesheet.

        - album-art theme  → full app QSS built from the art palette
        - system theme     → keep the OS theme (no app QSS)
        - app (dark) theme → full app QSS (accent / background / text / shade)
        """
        app = QApplication.instance()
        palette = self._current_art_palette()
        self._last_art_palette = palette   # keep the no-op guard in sync
        if palette is not None:
            app.setStyleSheet(self._make_qss(palette))
        elif self._system_theme:
            app.setStyleSheet("")
        else:
            app.setStyleSheet(self._make_qss())
        self._restyle_toggle_buttons()

    def _restyle_toggle_buttons(self):
        """Make every checkable button visibly invert when active. The app/art QSS
        styles ``QPushButton:checked`` itself, but the system theme has no such rule —
        so under it we set the rule per-button (only ``:checked``, so the native
        unchecked look is preserved) using the OS highlight colour. Covers Controls,
        Library mode buttons, EQ bypass, Milkdrop toggles, and any future toggles."""
        from PySide6.QtWidgets import QPushButton
        use_system = self._system_theme and self._current_art_palette() is None
        if use_system:
            pal = self.palette()
            bg = pal.highlight().color().name()
            fg = pal.highlightedText().color().name()
            ss = ("QPushButton:checked{background:%s;border:1px solid %s;color:%s;}"
                  "QPushButton:checked:hover{background:%s;}" % (bg, bg, fg, bg))
        else:
            ss = ""   # app/art theme → global QSS already styles :checked
        for b in self.findChildren(QPushButton):
            if b.isCheckable():
                b.setStyleSheet(ss)

    def _build_ui(self):
        self._viz   = VisualizerWidget(self._analyzer, self._player)

        self._video = QVideoWidget()
        self._video.setAspectRatioMode(Qt.KeepAspectRatio)
        self._video.setStyleSheet("background: black;")

        # Use a zero-size placeholder as the central widget so all panels including
        # the visualizer can be docked/floated as QDockWidgets.
        _placeholder = QWidget()
        _placeholder.setFixedSize(0, 0)
        self.setCentralWidget(_placeholder)

        self.setDockNestingEnabled(True)

        # left and right docks own all four corners so they span full height.
        self.setCorner(Qt.TopLeftCorner,     Qt.LeftDockWidgetArea)
        self.setCorner(Qt.BottomLeftCorner,  Qt.LeftDockWidgetArea)
        self.setCorner(Qt.TopRightCorner,    Qt.RightDockWidgetArea)
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)

        _DOCK_FEATURES = (
            QDockWidget.DockWidgetMovable |
            QDockWidget.DockWidgetFloatable |
            QDockWidget.DockWidgetClosable
        )

        def _make_dock(title, widget, area, *, features=_DOCK_FEATURES):
            d = QDockWidget(title, self)
            d.setObjectName(title)
            d.setWidget(widget)
            d.setAllowedAreas(Qt.AllDockWidgetAreas)
            d.setFeatures(features)
            self.addDockWidget(area, d)
            _native_float(d)
            return d

        viz_dock  = _make_dock("Spectrum", self._viz,   Qt.TopDockWidgetArea)
        viz_dock.hide()
        viz_dock.visibilityChanged.connect(self._on_visual_visibility)
        video_dock = _make_dock("Video", self._video, Qt.TopDockWidgetArea)
        video_dock.hide()

        self._controls = ControlsWidget(self._player)
        ctrl_dock = _make_dock("Controls", self._controls, Qt.BottomDockWidgetArea)

        self._library = LibraryWidget(self._player)
        self._library.add_to_playlist.connect(self._on_add)
        lib_dock = _make_dock("Library", self._library, Qt.LeftDockWidgetArea)

        self._playlist = PlaylistPanel(self._player)
        self._playlist.notify.connect(lambda m: self._show_status(m, 8000))
        pl_dock = _make_dock("Playlist", self._playlist, Qt.RightDockWidgetArea)

        self._upnext = UpNextWidget(self._player)
        upnext_dock = _make_dock("Up Next", self._upnext, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(pl_dock, upnext_dock)
        upnext_dock.hide()   # opt-in via the View menu; its own floatable window

        self._stats_widget = StatsWidget(self._play_stats)
        stats_dock = _make_dock("Stats", self._stats_widget, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(pl_dock, stats_dock)
        stats_dock.hide()   # opt-in via the View menu

        self._lyrics = LyricsWidget(self._player)
        lyr_dock = _make_dock("Lyrics", self._lyrics, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(pl_dock, lyr_dock)

        self._meta = MetadataWidget(self._player)
        meta_dock = _make_dock("Info", self._meta, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(lyr_dock, meta_dock)
        pl_dock.raise_()

        self._osc = OscilloscopeWidget(self._analyzer, self._player)
        osc_dock = _make_dock("Oscilloscope", self._osc, Qt.BottomDockWidgetArea)
        osc_dock.visibilityChanged.connect(self._on_visual_visibility)
        self.tabifyDockWidget(ctrl_dock, osc_dock)

        self._eq_panel = EqWidget(self._player)
        eq_dock = _make_dock("Equalizer", self._eq_panel, Qt.BottomDockWidgetArea)
        self.tabifyDockWidget(ctrl_dock, eq_dock)
        eq_dock.hide()   # opt-in via the View menu, so it doesn't disturb saved layouts

        # Milkdrop (projectM) visualizer — docked inside the window; opt-in.
        try:
            from visualizer_window import VisualizerPanel
            self._milkdrop = VisualizerPanel(self._analyzer, self._player)
            self._milkdrop_dock = _make_dock("Milkdrop", self._milkdrop, Qt.BottomDockWidgetArea)
            self.tabifyDockWidget(ctrl_dock, self._milkdrop_dock)
            self._milkdrop_dock.hide()
        except Exception as e:
            from applog import log
            log.warning("Milkdrop panel unavailable: %s", e)
            self._milkdrop = self._milkdrop_dock = None
        ctrl_dock.raise_()

        self._np_widget = NowPlayingWidget(self._player)
        self._player.set_video_output(self._np_widget.video_widget)
        np_dock = _make_dock("Now Playing", self._np_widget, Qt.LeftDockWidgetArea)
        self.tabifyDockWidget(lib_dock, np_dock)
        lib_dock.raise_()

        self._settings = SettingsWidget()
        self._wire_settings()
        # Apply saved font/size over the base QSS loaded in main.py
        _saved_tf = self._settings.get("text_font", "") or None
        _saved_sz = self._settings.get("ui_size", 13)
        if _saved_tf or _saved_sz != 13:
            QApplication.instance().setStyleSheet(self._make_qss())
        self._settings_dock = _make_dock("Settings", self._settings, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(meta_dock, self._settings_dock)
        pl_dock.raise_()

        self._viz.open_settings.connect(self._open_viz_settings)

        self.resizeDocks([lib_dock, pl_dock], [280, 340], Qt.Horizontal)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

        from PySide6.QtGui import QShortcut, QKeySequence
        _flt_sc = QShortcut(QKeySequence("Ctrl+F"), self)
        _flt_sc.activated.connect(lambda: (self._playlist._filter.setFocus(), self._playlist._filter.selectAll()))

    def _wire_settings(self):
        s = self._settings
        s.visualizer_changed.connect(self._viz.apply_config)
        s.oscilloscope_changed.connect(self._osc.apply_config)
        self._osc.config_changed.connect(self._on_osc_dialog_changed)
        s.lyrics_changed.connect(lambda d: self._lyrics.set_font_size(d["font_size"]))
        s.appearance_changed.connect(self._on_appearance_changed)
        s.font_changed.connect(self._on_font_changed)
        s.ui_size_changed.connect(self._on_ui_size_changed)
        s.system_theme_changed.connect(self._on_system_theme_changed)
        s.docking_changed.connect(self._apply_docking)
        s.viz_auto_suspend_changed.connect(self._apply_viz_auto_suspend)
        s.scrobble_config_changed.connect(self._on_scrobble_config_changed)
        s.scrobble_connect.connect(self._on_scrobble_connect)
        s.scrobble_disconnect.connect(self._on_scrobble_disconnect)
        self._scrobbler.auth_status_changed.connect(self._on_scrobble_auth_status)
        s.play_stats_changed.connect(self._play_stats.set_enabled)
        s.layout_export.connect(self.export_layout)
        s.layout_import.connect(self.import_layout)
        s.config_export.connect(self.export_all_config)
        s.config_import.connect(self.import_all_config)
        s.audio_device_changed.connect(self._player.set_audio_device)
        s.normalize_changed.connect(self._player.set_normalize)
        s.normalize_changed.connect(
            lambda on: self._show_status(
                "Normalize enabled \u2014 applies from the next track" if on else "Normalize disabled",
                3000,
            )
        )
        s.discord_changed.connect(self._on_discord_changed)
        s.crossfade_changed.connect(self._player.set_crossfade)
        s.theme_style_changed.connect(self._on_theme_style_changed)
        self._viz.apply_config({
            "fft_enabled": s.get("viz_fft_enabled", True),
            "fft_gain":    s.get("viz_fft_gain", 100),
            "show_art":    s.get("viz_show_art", True),
            "art_opacity": s.get("viz_art_opacity", 28) / 100.0,
            "bar_count":   s.get("viz_bar_count", 128),
            "bar_gap":     s.get("viz_bar_gap", 2),
            "corner_r":    s.get("viz_corner_r", 3),
            "peak_hold":   s.get("viz_peak_hold", True),
        })
        self._osc.apply_config({
            "window_ms":  s.get("osc_window_ms", 40),
            "gain":       s.get("osc_gain", 100),
            "smooth":     s.get("osc_smooth", 1),
            "line_width": s.get("osc_line_width", 15) / 10.0,
            "trigger":    s.get("osc_trigger", True),
            "fps":        s.get("osc_fps", 60),
        })
        self._lyrics.set_font_size(s.get("lyr_font_size", 18))
        self._art_color_on   = s.get("art_color_theme", False)
        self._system_theme   = s.get("system_theme", True)
        # Push the custom accent into the canvas widgets (they paint outside QSS).
        _acc = s.get("accent_color", "#8040c8")
        for v in (self._osc, self._viz, self._eq_panel, self._controls._wf):
            if hasattr(v, "set_default_accent"):
                v.set_default_accent(_acc)
        self._refresh_idle_accents()
        # Apply the saved theme (accent / background / text / frame shade) at launch.
        self._reapply_qss()   # also restyles all toggle buttons for the active theme
        self._apply_docking(s.get("docking_enabled", True))
        self._apply_viz_auto_suspend(s.get("viz_auto_suspend", True))
        self._play_stats.set_enabled(s.get("play_stats_enabled", False))
        self._player.set_normalize(s.get("normalize", False))
        self._player.set_crossfade(s.get("crossfade_ms", 0))
        self._discord.configure(
            enabled=s.get("discord_rpc_enabled", False),
            client_id=s.get("discord_client_id", ""),
            art_base_url=s.get("discord_art_base_url", ""),
            art_upload_cmd=s.get("discord_art_upload_cmd", ""),
        )
        for _svc, _name in _SCROBBLE_SERVICES.items():
            self._scrobbler.configure(
                _name,
                enabled=s.get(f"{_svc}_enabled", False),
                api_key=s.get(f"{_svc}_api_key", ""),
                api_secret=s.get(f"{_svc}_api_secret", ""),
            )
            if self._scrobbler.is_authenticated(_name):
                self._settings.set_scrobble_status(_svc, True, self._scrobbler.username(_name))
        # Restore saved audio output device
        saved_id = s.get("audio_device_id", "")
        if saved_id:
            from PySide6.QtMultimedia import QMediaDevices
            for dev in QMediaDevices.audioOutputs():
                if dev.id().data() == saved_id:
                    self._player.set_audio_device(dev)
                    break

    def _on_appearance_changed(self, d: dict):
        self._art_color_on = d.get("art_color_theme", False)
        if not self._art_color_on:
            self._viz.set_accent(None)
            self._osc.set_accent(None)
            self._controls._wf.set_accent(None)
            self._eq_panel.set_accent(None)
            np = getattr(self, "_np_widget", None)
            if np is not None:
                np.set_accent(None)
            # Revert to system theme or dark theme depending on current setting
            if self._system_theme:
                QApplication.instance().setStyle("")
                QApplication.instance().setStyleSheet("")
            else:
                QApplication.instance().setStyleSheet(self._make_qss())
        elif self._player.current:
            import threading
            threading.Thread(
                target=self._fetch_and_apply_palette,
                args=(self._player.current.path,), daemon=True,
            ).start()
            self._prefetch_timer.start()  # warm up the rest of the queue
        self._refresh_idle_accents()

    def _on_discord_changed(self, d: dict):
        self._discord.configure(
            enabled=bool(d.get("enabled", False)),
            client_id=str(d.get("client_id", "")),
            art_base_url=str(d.get("art_base_url", "")),
            art_upload_cmd=str(d.get("art_upload_cmd", "")),
        )

    def _on_scrobble_config_changed(self, service: str, d: dict):
        self._scrobbler.configure(
            _SCROBBLE_SERVICES[service],
            enabled=bool(d.get("enabled", False)),
            api_key=str(d.get("api_key", "")),
            api_secret=str(d.get("api_secret", "")),
        )

    def _on_scrobble_connect(self, service: str):
        self._scrobbler.connect_service(_SCROBBLE_SERVICES[service])

    def _on_scrobble_disconnect(self, service: str):
        self._scrobbler.disconnect_service(_SCROBBLE_SERVICES[service])
        self._settings.set_scrobble_status(service, False, "")

    def _on_scrobble_auth_status(self, name: str, ok: bool, message: str):
        service = next((k for k, v in _SCROBBLE_SERVICES.items() if v == name), None)
        if service is not None:
            self._settings.set_scrobble_status(service, ok, message)
            if ok:
                self._show_status(f"Connected to {name}", 3000)
            elif message:
                self._show_status(f"{name}: {message}", 4000)

    def _apply_docking(self, enabled: bool):
        self.setDockNestingEnabled(enabled)
        for dock in self.findChildren(QDockWidget):
            dock.setAllowedAreas(
                Qt.AllDockWidgetAreas if enabled else Qt.NoDockWidgetArea
            )

    def _apply_viz_auto_suspend(self, enabled: bool):
        self._viz_auto_suspend = enabled
        self._update_visual_suspend()

    def _open_viz_settings(self):
        self._settings_dock.setVisible(True)
        self._settings._tabs.setCurrentIndex(1)
        QTimer.singleShot(0, self._settings_dock.raise_)

    def _build_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("File")
        fm.addAction("Add Files…",  self._add_files)
        fm.addAction("Add Folder…", self._add_folder)
        fm.addSeparator()
        fm.addAction("Quit", self.close)

        pm = mb.addMenu("Playback")
        pm.addAction("Play / Pause", self._player.toggle)
        pm.addAction("Stop",         self._player.stop)
        pm.addAction("Next",         self._player.next)
        pm.addAction("Previous",     self._player.prev)
        pm.addSeparator()
        self._build_sleep_menu(pm)

        vm = _CollapsibleMenu("View", self)
        mb.addMenu(vm)
        self._build_view_menu(vm)

        hm = mb.addMenu("Help")
        hm.addAction("Keyboard Shortcuts", self._show_shortcuts)
        hm.addAction("About AudioCrate",   self._show_about)

    def _show_shortcuts(self):
        from PySide6.QtWidgets import QMessageBox
        rows = [
            ("Space", "Play / Pause"),
            ("S", "Stop"),
            ("N  ·  Ctrl+→", "Next track"),
            ("P  ·  Ctrl+←", "Previous track"),
            ("→ / ←  ·  . / ,", "Seek ± 5 seconds"),
            ("↑ / ↓", "Volume ± 5%"),
            ("M", "Mute"),
            ("R", "Cycle play mode (SEQ / REP / RP1)"),
            ("E", "Toggle Enhance"),
            ("Ctrl+F", "Focus the playlist filter"),
            ("Del", "Remove selected playlist tracks"),
            ("Enter", "Play the selected playlist track"),
            ("Ctrl+Z / C / V", "Playlist undo / copy / paste"),
            ("Media keys", "Play / Pause / Stop / Next / Prev"),
            ("Drag &amp; drop", "Add files or folders to the queue"),
        ]
        body = "<table cellspacing=0 cellpadding=5>" + "".join(
            f"<tr><td><b>{k}</b></td><td>&nbsp;&nbsp;{v}</td></tr>" for k, v in rows
        ) + "</table>"
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard Shortcuts")
        box.setTextFormat(Qt.RichText)
        box.setText(body)
        box.exec()

    def _show_about(self):
        from PySide6.QtWidgets import QMessageBox
        from _version import VERSION
        try:
            from PySide6 import __version__ as _pyside
        except Exception:
            _pyside = "?"
        extra = ""
        try:
            import projectm_engine
            if projectm_engine.available():
                extra = f"<br>Milkdrop visualizer: projectM {projectm_engine.version()}"
        except Exception:
            pass
        QMessageBox.about(
            self, "About AudioCrate",
            f"<h3>AudioCrate {VERSION}</h3>"
            "<p>A native audio player with a live, bypassable DSP chain, "
            "chiptune playback (VGM/NSF/AY/Furnace), and a Milkdrop visualizer.</p>"
            f"<p>PySide6 {_pyside}{extra}</p>",
        )

    # View menu: collapsible categories + per-item glyph icons
    _VIEW_ICONS = {
        "Spectrum":     "\U000f0128",   # chart-bar
        "Oscilloscope": "\U000f095b",   # sine-wave
        "Milkdrop":     "\U000f0264",   # image-filter-vintage
        "Now Playing":  "\U000f0387",   # music
        "Info":         "\U000f02fd",   # information-outline
        "Lyrics":       "\U000f09b8",   # text-box-outline
        "Up Next":      "\U000f05a5",   # playlist-play
        "Controls":     "\U000f0e66",   # play-box-outline
        "Equalizer":    "\U000f0ead",   # equalizer
        "Video":        "\U000f0567",   # video
        "Library":      "\U000f125a",   # bookshelf
        "Playlist":     "\U000f0ca0",   # playlist-music
        "Stats":        "\U000f0762",   # chart-line
        "Settings":     "\U000f0493",   # cog
    }
    _VIEW_CATEGORIES = [
        ("Visualizers", ["Spectrum", "Oscilloscope", "Milkdrop"]),
        ("Now Playing", ["Now Playing", "Info", "Lyrics", "Up Next"]),
        ("Playback",    ["Controls", "Equalizer", "Video"]),
        ("Library",     ["Library", "Playlist", "Stats"]),
        ("App",         ["Settings"]),
    ]

    def _build_view_menu(self, vm: "_CollapsibleMenu"):
        actions = {d.windowTitle(): d.toggleViewAction()
                   for d in self.findChildren(QDockWidget)}
        used = set()
        for cat_name, titles in self._VIEW_CATEGORIES:
            members = []
            for t in titles:
                act = actions.get(t)
                if act is None:
                    continue
                glyph = self._VIEW_ICONS.get(t, "")
                act.setText(f"    {glyph}   {t}" if glyph else f"    {t}")
                members.append(act)
                used.add(t)
            if members:
                self._add_view_section(vm, cat_name, members)
        # future-proof: any dock not in a category still shows up
        leftover = [a for t, a in actions.items() if t not in used]
        if leftover:
            self._add_view_section(vm, "Other", leftover)

    def _add_view_section(self, vm: "_CollapsibleMenu", name: str, members: list):
        DOWN, RIGHT = "\U000f0140", "\U000f0142"   # chevron down / right
        header = QAction(vm)
        header.setProperty("section_header", True)
        f = header.font(); f.setBold(True); header.setFont(f)
        state = {"open": True}

        def refresh():
            header.setText(f"{DOWN if state['open'] else RIGHT}  {name}")
            for m in members:
                m.setVisible(state["open"])

        def toggle():
            state["open"] = not state["open"]
            refresh()

        header.triggered.connect(toggle)
        vm.addAction(header)
        for m in members:
            vm.addAction(m)
        vm.addSeparator()
        refresh()

    def _build_sleep_menu(self, parent_menu):
        from PySide6.QtGui import QActionGroup
        self._sleep_menu = parent_menu.addMenu("Sleep Timer")
        grp = QActionGroup(self)
        grp.setExclusive(True)
        self._sleep_actions = {}

        def _add(label, handler, key=None):
            act = self._sleep_menu.addAction(label)
            act.setCheckable(True)
            grp.addAction(act)
            act.triggered.connect(handler)
            if key is not None:
                self._sleep_actions[key] = act
            return act

        off = _add("Off", self._sleep_off, key="off")
        off.setChecked(True)
        self._sleep_menu.addSeparator()
        for mins in (15, 30, 45, 60, 90):
            _add(f"{mins} minutes", lambda _=False, m=mins: self._sleep_start(m), key=mins)
        self._sleep_menu.addSeparator()
        _add("End of track", self._sleep_end_of_track, key="eot")
        _add("Custom…", self._sleep_custom, key="custom")

    # sleep timer handlers

    def _sleep_off(self, *_):
        self._sleep.cancel()
        self._player.set_stop_after_current(False)
        self._set_sleep_label(None)
        self._show_status("Sleep timer off", 2000)

    def _sleep_start(self, minutes: float):
        self._player.set_stop_after_current(False)
        self._sleep.start(minutes)
        self._show_status(f"Sleep timer: {minutes:g} min", 2500)

    def _sleep_end_of_track(self, *_):
        self._sleep.cancel()
        self._player.set_stop_after_current(True)
        self._set_sleep_label("end of track")
        self._show_status("Sleep timer: end of current track", 2500)

    def _sleep_custom(self, *_):
        from PySide6.QtWidgets import QInputDialog
        mins, ok = QInputDialog.getInt(self, "Sleep Timer",
                                       "Stop playback after (minutes):", 20, 1, 600)
        if ok:
            self._sleep_start(mins)
        else:
            # revert the radio selection to whatever was active
            self._sync_sleep_radio()

    def _on_sleep_tick(self, secs: int):
        self._set_sleep_label(SleepTimer.fmt(secs))

    def _on_sleep_elapsed(self):
        self._player.pause()
        self._sleep_actions["off"].setChecked(True)
        self._set_sleep_label(None)
        self._show_status("Sleep timer elapsed — paused", 4000)

    def _set_sleep_label(self, suffix):
        self._sleep_menu.setTitle("Sleep Timer" if not suffix else f"Sleep Timer ({suffix})")

    def _sync_sleep_radio(self):
        if self._sleep.active:
            return
        if not self._player.stop_after_current:
            self._sleep_actions["off"].setChecked(True)

    def _on_add(self, paths: list[Path]):
        self._playlist.add_paths(paths)

    def open_files(self, paths):
        """Files opened from the file manager / CLI: add to the playlist and play the first."""
        picks = [Path(p) for p in paths
                 if Path(p).is_file() and Path(p).suffix.lower() in PLAYABLE_EXTS]
        if not picks:
            return
        self._playlist.add_paths(picks)
        target = picks[0]
        q = self._player.queue
        for i in range(len(q) - 1, -1, -1):   # newest copy of the opened file
            if q[i].path == target:
                self._player.play_track(i)
                break

    def _add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(PLAYABLE_EXTS))
        # All Files is offered as well, because the extension filter hides a file
        # whose name only looks like it has a known suffix. The playlist says so
        # when it refuses one, which is more use than the file never appearing.
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Files", self._last_browse_dir,
            f"Audio/Video ({exts});;All Files (*)"
        )
        if paths:
            self._last_browse_dir = str(Path(paths[0]).parent)
            self._playlist.add_paths([Path(p) for p in paths])

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add Folder", self._last_browse_dir)
        if d:
            self._last_browse_dir = d
            self._scan_folders_async([Path(d)])

    def _layout_state(self) -> dict:
        """Serialize the full dock/window layout to a plain dict."""
        dock_data = {}
        for dock in self.findChildren(QDockWidget):
            dock_data[dock.windowTitle()] = {
                "geo":     base64.b64encode(bytes(dock.saveGeometry())).decode(),
                "visible": dock.isVisible(),
            }
        return {
            "geometry":   base64.b64encode(bytes(self.saveGeometry())).decode(),
            "dock_state": base64.b64encode(bytes(self.saveState())).decode(),
            "docks":      dock_data,
        }

    def _apply_layout_state(self, d: dict):
        """Restore layout from a dict produced by _layout_state()."""
        if geo := d.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        if state := d.get("dock_state"):
            self.restoreState(QByteArray.fromBase64(state.encode()))
        # restoreState handles docked positions and visibility.
        # For floating docks: apply OS window flags then geometry then visibility.
        dock_data = d.get("docks", {})
        for dock in self.findChildren(QDockWidget):
            info = dock_data.get(dock.windowTitle(), {})
            if dock.isFloating():
                # Set flags before restoreGeometry — setWindowFlags resets geometry
                dock.setWindowFlags(
                    Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint |
                    Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint |
                    Qt.WindowCloseButtonHint
                )
                if geo_b64 := info.get("geo"):
                    dock.restoreGeometry(QByteArray.fromBase64(geo_b64.encode()))
                dock.setTitleBarWidget(None)
                if info.get("visible", True):
                    dock.show()
                else:
                    dock.hide()
            else:
                dock.setTitleBarWidget(_DockTitleBar(dock))

    def _save_ui_state(self):
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = self._layout_state()
            data["volume"]          = self._player.volume
            data["waveform_on"]     = self._controls.waveform_on
            data["active_tab"]      = self._playlist._tabs.currentIndex()
            data["last_track"]      = str(self._player.current.path) if self._player.current else ""
            if self._milkdrop is not None:
                mp = self._milkdrop.current_preset_path()
                if mp:
                    data["milkdrop_preset"] = mp
                data["milkdrop_locked"]  = self._milkdrop.locked
                data["milkdrop_shuffle"] = self._milkdrop.shuffle_on
            data["seekbar_visible"] = self._np_widget._seek.isVisible()
            data["shuffle"]         = self._player.shuffle
            data["play_mode"]       = self._player.mode
            data["trim_silence"]    = self._player.trim_silence
            data["enhance"]         = self._player.enhance
            data["last_browse_dir"] = self._last_browse_dir
            import panel_playlist, panel_library
            data["last_playlist_dir"] = panel_playlist._last_dir
            data["last_library_dir"]  = panel_library._last_lib_dir
            payload = json.dumps(data, indent=2)
            if payload == getattr(self, "_last_saved_state", None):
                return   # nothing changed since the last 10s tick — skip the disk write
            self._last_saved_state = payload
            tmp = _STATE_FILE.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(_STATE_FILE)
        except Exception:
            import traceback; traceback.print_exc()

    def _restore_ui_state(self):
        try:
            d = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            d = _DEFAULT_LAYOUT  # no saved state — use baked default
        try:
            self._apply_layout_state(d)
        except Exception:
            import traceback; traceback.print_exc()
        try:
            if (vol := d.get("volume")) is not None:
                self._player.volume = float(vol)
                from panel_controls import _vol_to_slider
                self._controls._vol.setValue(_vol_to_slider(float(vol)))
            if d.get("waveform_on"):
                self._controls.set_waveform_on(True)
            if (tab := d.get("active_tab", 0)):
                self._playlist._tabs.setCurrentIndex(tab)
            if last := d.get("last_track"):
                # restore_selection switches to whichever tab holds the track. Sync the
                # player queue to that tab and load the track as current (NOT playing).
                # Missing file → not in any tab → nothing loaded (none).
                self._playlist.restore_selection(Path(last))
                lp = Path(last)
                lst = self._playlist._current_list()
                paths = lst.paths() if lst else []
                if lp in paths:
                    if [t.path for t in self._player.queue] != paths:
                        self._player.clear()
                        self._player.add_many(paths)
                    self._player.load_current(paths.index(lp))
            if self._milkdrop is not None:
                # toggles first: restoring "locked" stops auto-advance from
                # immediately replacing the preset we're about to restore
                self._milkdrop.restore_toggles(d.get("milkdrop_locked", False),
                                               d.get("milkdrop_shuffle", True))
                if mp := d.get("milkdrop_preset"):
                    self._milkdrop.set_pending_preset(mp)
            if not d.get("seekbar_visible", True):
                self._np_widget._set_seekbar_visible(False)
            if d.get("shuffle", False):
                self._player.shuffle = True
                self._controls._btn_shf.setChecked(True)
            if (mode := d.get("play_mode")) is not None:
                self._player.mode = int(mode)
                from panel_controls import _MODE_LABELS
                self._controls._btn_mode.setText(_MODE_LABELS.get(int(mode), ""))
            if d.get("trim_silence", False):
                self._player.trim_silence = True
                self._controls._trim.setChecked(True)
            if d.get("enhance", False):
                self._controls.set_enhance_on(True)
            # file dialogs reopen where the user last was, across sessions
            if lb := d.get("last_browse_dir"):
                self._last_browse_dir = lb
            import panel_playlist, panel_library
            if lp := d.get("last_playlist_dir"):
                panel_playlist._last_dir = lp
            if ll := d.get("last_library_dir"):
                panel_library._last_lib_dir = ll
        except Exception:
            import traceback; traceback.print_exc()

    def export_layout(self, path: Path):
        path.write_text(json.dumps(self._layout_state(), indent=2), encoding="utf-8")

    def import_layout(self, path: Path):
        d = json.loads(path.read_text(encoding="utf-8"))
        self._apply_layout_state(d)

    def export_all_config(self, path: Path):
        """Bundle settings + ui_state into one file."""
        bundle = {
            "settings": self._settings._cfg,
            "ui_state": self._layout_state(),
        }
        bundle["ui_state"]["volume"]       = self._player.volume
        bundle["ui_state"]["waveform_on"]  = self._controls.waveform_on
        bundle["ui_state"]["shuffle"]      = self._player.shuffle
        bundle["ui_state"]["play_mode"]    = self._player.mode
        bundle["ui_state"]["trim_silence"] = self._player.trim_silence
        bundle["ui_state"]["enhance"]      = self._player.enhance
        path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        self._show_status(f"Config exported to {path.name}", 3000)

    def import_all_config(self, path: Path):
        """Restore settings + layout from a bundle exported by export_all_config."""
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if "settings" in bundle:
            from panel_settings import _DEFAULTS
            cfg = dict(_DEFAULTS)
            cfg.update({k: v for k, v in bundle["settings"].items() if k in cfg})
            self._settings._cfg = cfg
            self._settings.save()
            # Re-apply all settings
            self._wire_settings()
            QApplication.instance().setStyleSheet(self._make_qss())
        if "ui_state" in bundle:
            self._apply_layout_state(bundle["ui_state"])
            d = bundle["ui_state"]
            if (vol := d.get("volume")) is not None:
                self._player.volume = float(vol)
                from panel_controls import _vol_to_slider
                self._controls._vol.setValue(_vol_to_slider(float(vol)))
            if d.get("waveform_on"):
                self._controls.set_waveform_on(True)
            if d.get("shuffle", False):
                self._player.shuffle = True
                self._controls._btn_shf.setChecked(True)
            if (mode := d.get("play_mode")) is not None:
                from panel_controls import _MODE_LABELS
                self._player.mode = int(mode)
                self._controls._btn_mode.setText(_MODE_LABELS.get(int(mode), ""))
            if d.get("trim_silence", False):
                self._player.trim_silence = True
                self._controls._trim.setChecked(True)
            if d.get("enhance", False):
                self._controls.set_enhance_on(True)
        self._show_status(f"Config imported from {path.name}", 3000)

    def closeEvent(self, e):
        e.accept()
        self._do_shutdown()

    def _do_shutdown(self):
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True

        # Stop all Qt timers before anything else
        for obj in (self._save_timer, self._prefetch_timer, self._player._fade_timer):
            try: obj.stop()
            except Exception: pass
        try: self._palette_pool.shutdown(wait=False, cancel_futures=True)
        except Exception: pass
        try: self._viz._tick_timer.stop()
        except Exception: pass
        try: self._osc._tick_timer.stop()
        except Exception: pass
        try: self._lyrics._canvas._anim_timer.stop()
        except Exception: pass
        try: self._lyrics._scroll_anim.stop()
        except Exception: pass
        self._player._fade_cb = None

        # Disconnect player signals so bg threads can't emit into dead widgets
        try: self._player.track_changed.disconnect()
        except Exception: pass
        try: self._player.state_changed.disconnect()
        except Exception: pass
        try: self._player.position_changed.disconnect()
        except Exception: pass

        # Stop the web remote's HTTP thread before the player goes away
        try:
            if getattr(self, "_webremote", None) is not None:
                self._webremote.stop()
        except Exception: pass

        # Unregister MPRIS D-Bus service before Qt destroys adaptors
        try:
            self._mpris._bus.unregisterObject(self._mpris._OBJECT)
            self._mpris._bus.unregisterService(self._mpris._BUS_NAME)
        except Exception: pass
        try: self._discord.shutdown()
        except Exception: pass
        try: self._scrobbler.shutdown()
        except Exception: pass
        try: self._play_stats.flush()
        except Exception: pass

        # Stop the real-time DSP engine (tears down QAudioSink + decode thread)
        try: self._player._dsp.stop()
        except Exception: pass
        # Stop the Furnace engine (joins its render thread)
        try:
            if self._player._fur is not None:
                self._player._fur.stop()
        except Exception: pass

        # Stop media and release source
        try: self._player._media.stop()
        except Exception: pass
        try: self._player._media.setSource(QUrl())
        except Exception: pass

        # Block dock signals during destruction
        for dock in self.findChildren(QDockWidget):
            try: dock.blockSignals(True)
            except Exception: pass

        # Persist state
        try: self._save_ui_state()
        except Exception: pass
        try: self._playlist.save_session()
        except Exception: pass
        try: self._settings.save()
        except Exception: pass
        try: self._eq_panel._save_state()   # flush EQ state even if debounce pending
        except Exception: pass
