from __future__ import annotations
import contextlib
import json
import base64
import sys
import time
from pathlib import Path
import plat_fs
import plat_paths
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QStatusBar, QFileDialog, QWidget,
    QHBoxLayout, QVBoxLayout, QFrame, QLabel, QPushButton, QSizePolicy, QApplication, QMenu, QDialog, QTabBar,
)
from PySide6.QtCore import Qt, QByteArray, QEvent, Signal, QTimer, QUrl
from PySide6.QtGui  import QColor, QPainter, QPainterPath, QAction
import icon_font
from video_view import make_video_widget

_STATE_FILE = plat_paths.config_dir() / "ui_state.json"

_DEFAULT_LAYOUT = json.loads('{"geometry": "AdnQywADAAAAAAAAAAAAAAAABvkAAARbAAAAAQAAABkAAAb4AAAEWgAAAAACAAAABvoAAAAAAAAAAAAABvkAAARb", "dock_state": "AAAA/wAAAAD9AAAAAgAAAAAAAAN3AAAEJPwCAAAAA/wAAAAiAAAB8QAAANUBAAAb+gAAAAABAAAABvsAAAAYAE8AcwBjAGkAbABsAG8AcwBjAG8AcABlAQAAAAD/////AAAAmAD////7AAAAEABNAGkAbABrAGQAcgBvAHABAAAAAP////8AAACiAP////sAAAAWAE4AbwB3ACAAUABsAGEAeQBpAG4AZwEAAAAA/////wAAAJMA////+wAAAAwATwBuAGwAaQBuAGUBAAAAAP////8AAADKAP////sAAAAOAEQAZQB2AGkAYwBlAHMBAAAAAP////8AAADeAP////sAAAAKAFYAaQBkAGUAbwEAAAAA/////wAAAHIA/////AAAAhkAAAGHAAABhQEAABv6AAAAAAIAAAAC+wAAABIARQBxAHUAYQBsAGkAegBlAHIBAAAAAP////8AAAFpAP////sAAAAGAEQAUwBQAQAAAAD/////AAAApgD////7AAAAEABDAG8AbgB0AHIAbwBsAHMBAAADpgAAAKAAAACgAP///wAAAAEAAAN3AAAEJPwCAAAAA/wAAAAiAAADYAAAAWEBAAAb+gAAAAACAAAAB/sAAAAQAFAAbABhAHkAbABpAHMAdAEAAAAA/////wAAAOoA////+wAAAA4ATABpAGIAcgBhAHIAeQEAAAAA/////wAAAOIA////+wAAABAAUwBlAHQAdABpAG4AZwBzAQAAAAD/////AAAAhwD////7AAAACgBTAHQAYQB0AHMBAAAAAP////8AAAFFAP////sAAAAOAFUAcAAgAE4AZQB4AHQBAAAAAP////8AAACfAP////sAAAAMAEwAeQByAGkAYwBzAQAAAAD/////AAAAKAD////7AAAACABJAG4AZgBvAQAAAAD/////AAAAgAD////7AAAAEgBXAGEAdABlAHIAZgBhAGwAbAEAAAOIAAAAvgAAAEEA////+wAAABAAUwBwAGUAYwB0AHIAdQBtAgAAAMAAAAHzAAADdwAAAL4AAAAAAAAEJAAAAAEAAAACAAAAAQAAAAL8AAAAAA==", "docks": {"Settings": {"geo": "AdnQywADAAD///qV///6x////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///6x////gv///4L", "visible": true}, "Playlist": {"geo": "AdnQywADAAAAAAODAAAAIgAABvkAAANlAAAAAAAAAAD//////////wAAAAAAAAAABvoAAAODAAAAIgAABvkAAANl", "visible": true}, "Library": {"geo": "AdnQywADAAD///qV///6yv///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///6yv///gv///4L", "visible": true}, "Stats": {"geo": "AdnQywADAAD///qV///6yv///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///6yv///gv///4L", "visible": true}, "Up Next": {"geo": "AdnQywADAAD///qV///6yv///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///6yv///gv///4L", "visible": true}, "Lyrics": {"geo": "AdnQywADAAD///qV///6yv///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///6yv///gv///4L", "visible": true}, "Info": {"geo": "AdnQywADAAD///qV///6yv///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///6yv///gv///4L", "visible": true}, "Milkdrop": {"geo": "AdnQywADAAD///qV///8Of///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///8Of///gv///4L", "visible": true}, "Now Playing": {"geo": "AdnQywADAAD///qV///8Of///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///8Of///gv///4L", "visible": true}, "Online": {"geo": "AdnQywADAAD///qV///8Of///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///8Of///gv///4L", "visible": true}, "Devices": {"geo": "AdnQywADAAD///qV///8Of///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///8Of///gv///4L", "visible": true}, "Video": {"geo": "AdnQywADAAD///qV///8Of///gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///qV///8Of///gv///4L", "visible": true}, "Oscilloscope": {"geo": "AdnQywADAAAAAAAAAAAAIgAAA3YAAAH2AAAAAAAAAAD//////////wAAAAAAAAAABvoAAAAAAAAAIgAAA3YAAAH2", "visible": true}, "Waterfall": {"geo": "AdnQywADAAAAAAODAAADiAAABvkAAARFAAAAAAAAAAD//////////wAAAAAAAAAABvoAAAODAAADiAAABvkAAARF", "visible": true}, "Spectrum": {"geo": "AdnQywADAAAAAADAAAAB8wAABDYAAAKwAAAAwAAAAfMAAAQ2AAACsAAAAAAAAAAABvoAAADAAAAB8wAABDYAAAKw", "visible": false}, "Equalizer": {"geo": "AdnQywADAAAAAAAAAAACGQAAA3YAAAODAAAAAAAAAAD//////////wAAAAAAAAAABvoAAAAAAAACGQAAA3YAAAOD", "visible": true}, "DSP": {"geo": "AdnQywADAAD///1m///9I////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAABvr///1m///9I////gv///4L", "visible": true}, "Controls": {"geo": "AdnQywADAAAAAAAAAAADpgAAA3YAAARFAAAAAAAAAAD//////////wAAAAAAAAAABvoAAAAAAAADpgAAA3YAAARF", "visible": true}}}')
_COMPACT_LAYOUT = json.loads('{"geometry": "AdnQywADAAAAAAABAAAGRwAABcsAAAr4AAAAAQAABkcAAAXLAAAK+AAAAAAAAAAACgAAAAABAAAGRwAABcsAAAr4", "dock_state": "AAAA/wAAAAD9AAAABAAAAAAAAALhAAAEhPwCAAAAAfwAAAAbAAAEhAAAAToA/////AIAAAAD/AAAABsAAAJUAAAAAAD////8AQAAAAH8AAACIAAAAbwAAAAAAP////r/////AgAAAAD7AAAAFgBOAG8AdwAgAFAAbABhAHkAaQBuAGcBAAAAGwAAA3AAAACnAP///vsAAAAQAEMAbwBuAHQAcgBvAGwAcwEAAAOPAAABEAAAAI8A///+AAAAAQAAAuIAAASE/AIAAAAD/AAAABsAAAL3AAAA3QEAABz6AAAAAwEAAAAE+wAAABAAUABsAGEAeQBsAGkAcwB0AQAAAAD/////AAAAsgD////7AAAADgBMAGkAYgByAGEAcgB5AQAAAAD/////AAAA7AD////7AAAACABJAG4AZgBvAQAAAAD/////AAAAbwD////7AAAAEABTAGUAdAB0AGkAbgBnAHMBAAAAAP////8AAACJAP////sAAAAMAEwAeQByAGkAYwBzAQAAAxYAAAGJAAAALwD///78AAACwQAAAd4AAAAAAP////r/////AgAAAAAAAAACAAACkwAAAIz8AQAAAAH7AAAAEABTAHAAZQBjAHQAcgB1AG0AAAABnAAAApMAAACWAP///wAAAAMAAAIaAAAEgPwBAAAAAfsAAAAYAE8AcwBjAGkAbABsAG8AcwBjAG8AcABlAAAAAZwAAAB1AAAApAD///8AAAAAAAAEhAAAAAEAAAACAAAAAQAAAAL8AAAAAA==", "docks": {"Info": {"geo": "AdnQywADAAD///sq///7M////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAACgD///sq///7M////gv///4L", "visible": true}, "Library": {"geo": "AdnQywADAAD///sq///7M////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAACgD///sq///7M////gv///4L", "visible": true}, "Playlist": {"geo": "AdnQywADAAD///sq///7M////gv///4LAAAAAAAAAAD//////////wAAAAAAAAAACgD///sq///7M////gv///4L", "visible": true}, "Lyrics": {"geo": "AdnQywADAAAAAALpAAADFgAABcoAAASeAAAAAAAAAAD//////////wAAAAAAAAAACgAAAALpAAADFgAABcoAAASe", "visible": true}, "Settings": {"geo": "AdnQywADAAAAAALpAAAAGwAABcoAAALzAAAAAAAAAAD//////////wAAAAAAAAAACgAAAALpAAAAGwAABcoAAALz", "visible": true}, "Oscilloscope": {"geo": "AdnQywADAAAAAAGcAAAAGwAAAhAAAASaAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAGcAAAAGwAAAhAAAASa", "visible": false}, "Controls": {"geo": "AdnQywADAAAAAAAAAAADjwAAAuAAAASeAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAAAAAADjwAAAuAAAASe", "visible": true}, "Now Playing": {"geo": "AdnQywADAAAAAAAAAAAAGwAAAuAAAAOKAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAAAAAAAGwAAAuAAAAOK", "visible": true}, "Spectrum": {"geo": "AdnQywADAAAAAAGcAAAAFwAABC4AAACiAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAGcAAAAFwAABC4AAACi", "visible": false}, "Video": {"geo": "AdnQywADAAAAAAAAAAAAGwAAAuAAAAOKAAAAAAAAAAD//////////wAAAAAAAAAACgAAAAAAAAAAGwAAAuAAAAOK", "visible": false}}}')

_FULL_LAYOUT_MIN = 980       # the full layout needs a window this tall (936 px at 1280 wide on Linux, 944 under Windows; tools/check_default_layout.py keeps this true)


def _first_layout() -> dict:
    """What a start with no saved layout opens: the full layout, or the compact one on a screen too short to hold it
    (a 768 line laptop, or a 1080p one scaled to 125 percent), where the full one would run off the bottom of the screen."""
    screen = QApplication.primaryScreen()
    return _COMPACT_LAYOUT if screen is not None and screen.availableGeometry().height() < _FULL_LAYOUT_MIN else _DEFAULT_LAYOUT


def _baked(d: dict) -> bool:
    """Is d one of the layouts that ship with the program, not one the person saved."""
    return d is _DEFAULT_LAYOUT or d is _COMPACT_LAYOUT


def _dock_of(widget: QWidget) -> QDockWidget | None:
    p = widget.parentWidget()
    while p:
        if isinstance(p, QDockWidget):
            return p
        p = p.parentWidget()
    return None


def _dock_menu(dock: QDockWidget, parent: QWidget, pos):
    """Float, move to a side or close a docked panel. Shared by its title bar and its tab, because
    a tab has no bar of its own."""
    window = dock.parent()
    if not hasattr(window, "addDockWidget"):
        return
    m = QMenu(parent)
    m.addAction("Float", lambda: QTimer.singleShot(0, lambda: _qt_valid(dock) and dock.setFloating(True)))
    for label, area in (("Move to left", Qt.LeftDockWidgetArea), ("Move to right", Qt.RightDockWidgetArea),
                        ("Move to top", Qt.TopDockWidgetArea), ("Move to bottom", Qt.BottomDockWidgetArea)):
        m.addAction(label, lambda a=area: QTimer.singleShot(0, lambda: window.addDockWidget(a, dock)))
    m.addSeparator()
    m.addAction("Close", dock.close)
    m.exec(pos)


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

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def paintEvent(self, _):
        # the strip a title bar paints, with the notch as dim as its buttons until hovered
        import titlebar
        p   = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), titlebar.bar_background(pal))
        fg  = QColor(pal.windowText().color())
        if titlebar.show_line:
            line = QColor(fg)
            line.setAlpha(36)
            p.setPen(line)
            p.drawLine(0, 0, self.width(), 0)
            p.drawLine(0, 0, 0, self.height())
            p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        p.setRenderHint(QPainter.Antialiasing)
        fg.setAlpha(120 if self.underMouse() else 55)
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


class _DockTitleBar(QFrame):
    """The bar of a docked panel. A press on it that is not on a button goes to the dock, which drags the
    panel with Qt's own preview of where it would dock. The bar is not swapped for the floating one while the
    button is down (see _native_float), because that left the whole UI unclickable once. A panel also
    floats from the button, a double click or this bar's menu, and goes back from the same places."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dock_titlebar")      # the theme outlines it (see core/theme.py)
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
            QTimer.singleShot(0, lambda d=dock: _qt_valid(d) and d.setFloating(not d.isFloating()))

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._toggle_float()

    def contextMenuEvent(self, e):
        dock = _dock_of(self)
        if dock is not None:
            _dock_menu(dock, self, e.globalPos())

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


def _set_bar(dock: QDockWidget, bar):
    """setTitleBarWidget hides the bar it replaces and leaves it alive, one more with every float and re-dock."""
    old = dock.titleBarWidget()
    dock.setTitleBarWidget(bar)
    if old is not None and old is not bar:
        old.deleteLater()


def _make_floating(dock: QDockWidget, themed: bool):
    """Window flags and title bar for a dock that has become a window of its own:
    the app's themed bar (frameless window) or, with the custom title bar off, the
    compositor's."""
    if themed:
        from titlebar import TitleBar, install_grips
        # No window flags here: a dock with a title bar widget already floats frameless, and
        # recreating its window while a press is down is what lost the release.
        _set_bar(dock, TitleBar(dock, menu=False, kinds=("dock", "close"), outline=True))
        if not hasattr(dock, "_grips"):
            dock._grips = install_grips(dock)
    else:
        dock.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint |
            Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        _set_bar(dock, None)


def _native_float(dock: QDockWidget, themed: bool = False):
    _set_bar(dock, _DockTitleBar())
    waiting = []

    def _after_release(tries: int = 0):
        """Qt turns a panel into a window, or back, while the button is still down during its own drag. Replacing
        the bar then deletes a widget that took part in the press and the release goes nowhere, so the bar waits."""
        if not _qt_valid(dock):
            return
        if QApplication.mouseButtons() != Qt.NoButton and tries < 250:
            QTimer.singleShot(40, lambda: _after_release(tries + 1))
            return
        waiting.clear()
        _on_float(dock.isFloating(), settled=True)

    def _on_float(floating: bool, settled: bool = False):
        if not _qt_valid(dock):
            return
        if type(dock.parentWidget()).__name__ == "QDockWidgetGroupWindow":
            return                  # Qt's own tab group window: leave its frame alone
        if not settled and QApplication.mouseButtons() != Qt.NoButton:
            if not waiting:
                waiting.append(1)
                _after_release()
            return
        if floating:
            geo = dock.saveGeometry()
            _make_floating(dock, themed)
            dock.restoreGeometry(geo)
            dock.show()
        else:
            _set_bar(dock, _DockTitleBar())
        grips = getattr(dock, "_grips", None)
        if grips is not None:
            grips._place()          # only a floating dock has edges to drag
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
from panel_waterfall   import WaterfallWidget
from panel_online      import OnlineWidget, play_next_into
from panel_devices     import DevicesWidget
from panel_metadata    import MetadataWidget
from panel_nowplaying  import NowPlayingWidget
from panel_upnext      import UpNextWidget
from panel_eq          import EqWidget
from panel_dsp         import DspWidget
from panel_settings    import SettingsWidget
import anim
from artists           import configure as configure_artists
from media_session     import make_session
from discord_rpc       import DiscordRPCService
from scrobbler         import ScrobbleController
from play_stats        import PlayStatsTracker
from rgb_sync          import RgbSync
from panel_stats       import StatsWidget
from sleep_timer       import SleepTimer
from theme             import make_qss

# Settings-key prefix -> ScrobbleController service name
_SCROBBLE_SERVICES = {"lastfm": "LastFM", "librefm": "LibreFM"}

class MainWindow(QMainWindow):
    _palette_ready = Signal(object)
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
        # The DSP chain the DSP panel edits and the engine runs between the user EQ and Enhance.
        from dsp_user_chain import DspChain
        self._dsp_chain = DspChain(fs=self._player._dsp.fs, parent=self)
        self._dsp_chain.load()
        self._player._dsp.set_dsp_chain(self._dsp_chain)
        self._dsp_save = QTimer(self)           # writes dsp.json a moment after the last edit
        self._dsp_save.setSingleShot(True)
        self._dsp_save.setInterval(800)
        self._dsp_save.timeout.connect(self._dsp_chain.save)
        self._dsp_chain.changed.connect(self._dsp_save.start)
        self._analyzer       = Analyzer()
        self._analyzer_loaded_path = None   # track the analyzer has decoded (RAM gating)
        self._state_restored = False
        self._ui_restored    = False   # saving waits for this, or the defaults would overwrite the saved state
        self._new_panels     = []      # panels the saved layout did not know, see _place_new_panels
        self._art_color_on   = False
        self._button_down    = False   # see eventFilter
        self._panel_qss      = None    # sheet currently on the panels (see _restyle_panels)
        self._panel_gen      = 0
        self._art_bg         = None    # window background while panels carry an art sheet
        self._stale_panels   = set()   # hidden panels, restyled when they are first shown
        self._pending_art    = None    # (path, palette) waiting out the settle delay
        self._art_timer      = QTimer(self)
        self._art_timer.setSingleShot(True)
        self._art_timer.setInterval(220)
        self._art_timer.timeout.connect(self._commit_art_palette)
        self._last_browse_dir = str(Path.home())
        self._convert_dlg = None
        self._tour           = None    # the open tour dialog, see show_tour
        from concurrent.futures import ThreadPoolExecutor
        self._palette_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="palette")
        # Created before _build_ui because _wire_settings configures it on startup.
        self._discord = DiscordRPCService(self._player, self)
        self._scrobbler = ScrobbleController(self._player, self)
        self._play_stats = PlayStatsTracker(self._player, self)
        self._rgb = RgbSync(self._analyzer, self._player, self)

        # Created before the menus, which go into it. Settings "custom_titlebar" (on
        # by default) or AUDIOCRATE_TITLEBAR=0 turns it off; a change needs a restart.
        self._titlebar = None
        if self._wants_titlebar():
            from titlebar import TitleBar, install_grips
            self.setWindowFlag(Qt.FramelessWindowHint, True)
            self._titlebar = TitleBar(self)
            self.setMenuWidget(self._titlebar)
            self._grips = install_grips(self)

        self._build_ui()
        self._build_menu()

        self._mpris = make_session(self._player, self)
        self._mpris.open_files.connect(self.open_files)
        self._mpris.remote_changed.connect(self._controls.sync_from_player)
        self._controls._vol.valueChanged.connect(self._mpris.refresh)

        # Mobile web remote, off unless AUDIOCRATE_WEB=1 or settings.json says
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
        self._player.video_sound_changed.connect(lambda _on: self._video_analyzer_source())
        self._player.state_changed.connect(self._on_player_state)
        self._player.queue_changed.connect(self._on_queue_changed)
        self._player.chip_rendering.connect(
            lambda on: self._show_status("Rendering…")
            if on else self._status_bar.clearMessage())
        self._player.error.connect(lambda m: self._show_status(m, 6000))
        self._folder_scanned.connect(self._on_folder_scanned)
        self._player.chip_ready.connect(self._on_chip_ready)
        # .fur output is streamed here to the analyzer (scope/spectrum) and the waveform bar.
        if self._player._fur is not None:
            self._player._fur.waveform_chunk.connect(self._on_fur_pcm)
        # VGM/GME chiptunes stream their PCM the same way (int16, separate handler).
        self._player._vgm.waveform_chunk.connect(self._on_vgm_pcm)

        # App-wide playback keys, except while typing or in a dialog.
        QApplication.instance().installEventFilter(self)

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
        # a big folder) into a single prefetch sweep a moment after the last add.
        self._prefetch_timer = _QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(1500)   # after the restyle, not through it
        self._prefetch_timer.timeout.connect(self._prefetch_all_palettes)

        self._restore_layout()
        self._hook_all_tabs()
        icon_font.sweep()       # the panels were polished before the filter above saw them

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.MouseButtonPress:
            # A press while the last one never released, with a dock still holding the mouse:
            # the release was lost (a window that was recreated or moved by the compositor
            # mid-press). Let go, or every click in the app goes to that dock.
            g = QWidget.mouseGrabber()
            if g is not None and self._button_down and isinstance(g, QDockWidget):
                g.releaseMouse()
            self._button_down = True
        elif t == QEvent.Type.MouseButtonRelease and not (event.buttons() & ~event.button()):
            self._button_down = False
        if t == QEvent.Type.Polish and isinstance(obj, QTabBar) and obj.metaObject().className() == "QMainWindowTabBar":
            self._hook_dock_tabs(obj)
        if t == QEvent.Type.Polish:
            icon_font.fix(obj)
        if (self._titlebar is not None and event.type() == QEvent.Type.Polish
                and isinstance(obj, QDialog) and obj.isWindow()):
            from titlebar import decorate_dialog
            decorate_dialog(obj)
        if (self._stale_panels and event.type() == QEvent.Type.Show
                and obj in self._stale_panels):
            self._stale_panels.discard(obj)
            if self._panel_qss is not None:
                obj.setStyleSheet(self._panel_qss)
        if (event.type() == QEvent.Type.KeyPress
                and not event.isAutoRepeat()
                and QApplication.activeModalWidget() is None):
            if self._handle_media_key(event):
                return True
        return super().eventFilter(obj, event)

    def _hook_dock_tabs(self, bar):
        """Right click a dock tab for Float, Move to and Close, double click it to float the panel."""
        if bar.property("_ac_tabs"):
            return
        bar.setProperty("_ac_tabs", True)
        bar.setContextMenuPolicy(Qt.CustomContextMenu)
        bar.customContextMenuRequested.connect(lambda p, b=bar: self._tab_menu(b, p))
        bar.tabBarDoubleClicked.connect(lambda i, b=bar: self._tab_float(b, i))

    def _hook_all_tabs(self):
        for bar in self.findChildren(QTabBar):
            if bar.metaObject().className() == "QMainWindowTabBar":
                self._hook_dock_tabs(bar)

    def _tab_dock(self, bar, i: int):
        title = bar.tabText(i) if i >= 0 else ""
        return next((d for d in self.findChildren(QDockWidget) if d.windowTitle() == title), None)

    def _tab_menu(self, bar, pos):
        dock = self._tab_dock(bar, bar.tabAt(pos))
        if dock is not None:
            _dock_menu(dock, self, bar.mapToGlobal(pos))

    def _tab_float(self, bar, i: int):
        dock = self._tab_dock(bar, i)
        if dock is not None:
            QTimer.singleShot(0, lambda: _qt_valid(dock) and dock.setFloating(True))

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
        links = [u for u in e.mimeData().urls() if u.scheme() in ("http", "https")]
        if links:
            e.acceptProposedAction()
            self.open_link(links[0].toString())
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

    def _send_to_device(self, paths):
        self._devices_dock.show()
        self._devices_dock.raise_()
        self._devices.send_paths(None, paths)

    def _convert(self, paths=None):
        """The converter window. There is one: asking again brings it up and adds the files to it. It is not modal,
        so playback goes on."""
        dlg = self._convert_dlg
        if dlg is not None and _qt_valid(dlg):
            if paths:
                dlg.add_paths([Path(p) for p in paths])
        else:
            import converter
            from convert_dialog import ConvertDialog
            converter.set_dsp_factory(self._convert_stage)    # lets the dialog offer "Use my equalizer, DSP chain and Enhance"
            dlg = self._convert_dlg = ConvertDialog(self, [Path(p) for p in paths or []])
            dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _convert_stage(self, path, rate):
        """Called on a converter thread when a file starts: a private copy of the equalizer, DSP chain and Enhance."""
        from dsp_offline import offline_stage
        return offline_stage(self._player._dsp, path, rate)

    def _convert_pick(self):
        exts = " ".join(f"*{e}" for e in sorted(PLAYABLE_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Convert files", self._last_browse_dir,
            f"Audio/video ({exts});;All files (*)"
        )
        if paths:
            self._last_browse_dir = str(Path(paths[0]).parent)
            self._convert([Path(p) for p in paths])

    def open_link(self, url: str, play: bool = True):
        """A web link from the window, the command line or the desktop: the Online panel plays it."""
        self._online_dock.show()
        self._online_dock.raise_()
        self._online.open_link(url, play=play)

    def _on_online_saved(self, folder: Path):
        """A saved download is in the library's reach: its parent folder becomes a library folder."""
        root = folder.parent
        lib = self._library
        if not any(root == f or f in root.parents for f in lib._folders):
            lib._folders.append(root)
            lib._save_config()
        lib._refresh()

    def _on_devices_saved(self, folder):
        """Songs copied off a device into the Music folder join the library even when the library points elsewhere."""
        root, lib, music = Path(folder), self._library, plat_paths.music_dir()
        if (root == music or music in root.parents) and not any(root == f or f in root.parents for f in lib._folders):
            lib._folders.append(root)
            lib._save_config()
        lib._refresh()

    def _scan_folders_async(self, folders: list[Path]):
        """Walk dropped or chosen folders off the GUI thread: rglob over a big folder
        freezes the whole window."""
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
        if suspend:
            anim.finish_all()
        for w in (getattr(self, "_viz", None), getattr(self, "_osc", None),
                  getattr(self, "_eq_panel", None), getattr(self, "_milkdrop", None),
                  getattr(self, "_waterfall", None), getattr(self, "_dsp_panel", None)):
            if w is not None and hasattr(w, "set_suspended"):
                w.set_suspended(suspend)

    def _on_visual_visibility(self, _visible: bool):
        """For VIDEO only: decode the track for the analyzer while a visualizer is
        shown, free it when hidden. (Audio reads the engine buffer; .fur is live.)"""
        cur = self._player.current
        is_video = cur is not None and cur.path.suffix.lower() in VIDEO_EXTS
        if not is_video:
            return
        self._video_analyzer_source()

    def _video_analyzer_source(self):
        """The scope, spectrum and waterfall of a video: the processed sound from the player while it plays the video
        through our sink, otherwise (the switch is off, the sink failed) a decode of the file, only while a visualizer is shown."""
        cur = self._player.current
        if cur is None or cur.path.suffix.lower() not in VIDEO_EXTS:
            return
        if self._player.video_sound_tapped:
            self._analyzer.set_live(False)
            self._analyzer.set_engine_source(self._player.mono_window)
            self._analyzer_loaded_path = None
        elif self._visuals_shown():
            if self._analyzer_loaded_path != cur.path:
                self._analyzer.set_engine_source(None)
                self._analyzer.set_live(False)
                self._analyzer.load_track(cur.path)
                self._analyzer_loaded_path = cur.path
        else:
            self._analyzer.set_engine_source(None)
            self._analyzer.stop()
            self._analyzer_loaded_path = None

    def showEvent(self, e):
        super().showEvent(e)
        self._hook_all_tabs()
        if not self._state_restored:
            self._state_restored = True
            # the floating panels and the rest of the saved state, now that the window is on screen
            QTimer.singleShot(0, self._restore_ui_state)
            # after the layout is restored, so the overlays size to the real window
            QTimer.singleShot(0, self._start_fx)
            if sys.platform == "win32":
                QTimer.singleShot(5000, self._warm_converter)

    def _warm_converter(self):
        """The Convert window lists formats after one probe encode each, with ffmpeg started once per format. A virus
        scanner makes each start slow on Windows, so the first window would freeze for seconds; this does it early."""
        import threading
        import converter
        threading.Thread(target=converter.formats, daemon=True).start()

    def _start_fx(self):
        """Start the visual extras, each on its own so one cannot take the others.

        AUDIOCRATE_FX chooses which run. Unset is shine, "0" is none, and otherwise
        it is a comma separated list of shine and menus.
        Menus is off unless asked for: it animates every popup's geometry, which a
        Wayland popup answers with a reposition and a full relayout per frame, and
        it needs an application-wide Python event filter that sees every event.
        A crash in a paint or event path is not something a try block catches,
        so this exists to narrow one down without editing anything.
        """
        import os
        want = os.environ.get("AUDIOCRATE_FX", "").strip().lower()
        if want in ("0", "off", "none"):
            return
        on = {w.strip() for w in want.split(",") if w.strip()} or {"shine"}

        import fx
        if "shine" in on:
            try:
                self._shine = fx.Shine(self._status_bar)
            except Exception as e:
                print(f"fx: shine disabled ({e})")
        if "menus" in on:
            try:
                fx.install_menu_warp(QApplication.instance())
            except Exception as e:
                print(f"fx: menu warp disabled ({e})")

    def _show_status(self, msg: str, ms: int = 0):
        self._status_bar.showMessage(msg, ms)
        # the messages that end in an ellipsis are the ones still working
        shine = getattr(self, "_shine", None)
        if shine is not None:
            shine.start() if msg.rstrip().endswith("\u2026") else shine.stop()

    def _milkdrop_unavailable(self, why: str):
        QTimer.singleShot(0, self._milkdrop_dock.hide)
        self._show_status(why, 12000)

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
            # AY/YM/VGM/modules: rendered to WAV and played through the DSP engine, so
            # the scope/spectrum read the engine buffer (like plain audio). The waveform
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
            # Video plays on QMediaPlayer. Its processed sound comes from the player (a ring buffer keyed by media time)
            # while our sink plays it; otherwise it is decoded for the analyzer while a visualizer is shown (RAM gating).
            self._analyzer.set_live(False)
            self._video_analyzer_source()
        else:
            # Plain audio: scope/spectrum read the engine's decode directly, so there
            # is no second decode (no RAM, no track-change lag).
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
        """A chiptune/module finished rendering. Load its waveform bar from the WAV;
        the engine is already playing it."""
        try:
            self._controls._wf.load(Path(wav))
        except Exception:
            pass

    def _on_fur_pcm(self, frames_so_far, total_frames, rate, pcm_bytes):
        """Rendered .fur output (interleaved stereo f32), fed to the analyzer and the waveform."""
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
        """VGM/GME output (interleaved stereo int16), fed to the analyzer and the waveform."""
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
        """Scope popup-dialog edits are pushed into the Settings panel, so they persist
        (settings.json) and both UIs stay in sync."""
        s = self._settings
        s._osc_trigger.setChecked(d["osc_trigger"])
        s._osc_win_sl.setValue(d["osc_window_ms"])
        s._osc_gain_sl.setValue(d["osc_gain"])
        s._osc_smooth_sl.setValue(d["osc_smooth"])
        s._osc_lw_sl.setValue(d["osc_line_width"])
        if "osc_fps" in d:
            s._osc_fps_sl.setValue(d["osc_fps"])
        if "osc_vsync" in d:
            s._osc_vsync.setChecked(d["osc_vsync"])
        if "osc_bars" in d:
            s._osc_bars.setChecked(d["osc_bars"])
            s._osc_barw_sl.setValue(d["osc_bar_width"])
        s._apply_oscilloscope()

    def _on_queue_changed(self):
        if self._art_color_on:
            self._prefetch_timer.start()  # restarts the 300ms debounce window

    def _prefetch_all_palettes(self):
        # Only the current track and the next few in play order: prefetching the
        # whole queue means thousands of image decodes after adding a big folder.
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
        if not playing and self._player.enhance and self._player.queue:
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
        cur = self._player.current
        if cur is None or str(cur.path) != key:
            return                      # the user has already moved on; skip the art read
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

    def _restyle_panels(self, qss: str, palette: dict | None, on_done=None):
        """Switch the album-art theme without freezing the window.

        A QApplication-wide setStyleSheet re-polishes every widget in one call:
        about 200 ms here, all on the GUI thread, which also holds the GIL, so the
        audio feed thread stalls with it. A track change does that whenever the art
        colours change. Instead each top-level panel gets the sheet on its own, and
        a panel nobody can see waits until it is shown. The result matches the
        app-wide sheet pixel for pixel, except the window's own background, which
        event() draws below.

        Repaints are held until every panel has its sheet, then happen once, so
        the window never shows some panels in the old colours and some in the
        new. on_done runs at that moment. It is one pass and not a few panels per
        event-loop turn: nothing repaints in between anyway, and every turn waits
        behind the visualizers' frames, which stretched a 100 ms job to half a second."""
        self._panel_qss = qss
        self._panel_gen += 1
        gen = self._panel_gen
        self._art_bg = QColor(palette["bg"]) if palette else None
        panels = [c for c in self.children() if isinstance(c, QWidget)]
        self._stale_panels = {c for c in panels if not c.isVisible()}
        todo = iter([c for c in panels if c not in self._stale_panels])
        self.setUpdatesEnabled(False)

        def finish():
            if _qt_valid(self):
                self.setUpdatesEnabled(True)
                self.update()
                if on_done is not None:
                    on_done()

        def step():
            if gen != self._panel_gen or not _qt_valid(self):
                return                                    # a newer palette took over
            try:
                with self._quiet():
                    for c in todo:
                        if _qt_valid(c):
                            c.setStyleSheet(qss)
            except Exception:
                from applog import log
                log.exception("restyle failed")           # and never leave repaints off
            finish()

        step()

    @contextlib.contextmanager
    def _quiet(self):
        """Take this window's application-wide event filter out of the way for a
        burst of widget work. Restyling a panel sends thousands of events, every one
        of them goes through that Python filter, and each call has to win the
        interpreter lock back from whatever decode or network thread is running:
        that, not the styling, was most of a 100 ms stall."""
        app = QApplication.instance()
        app.removeEventFilter(self)
        try:
            yield
        finally:
            app.installEventFilter(self)

    def _set_app_qss(self, qss: str):
        """The synchronous path, for settings and theme changes. The per-panel
        sheets from _restyle_panels would shadow an app sheet, so drop them first."""
        if self._panel_qss is not None:
            self._panel_gen += 1
            self._panel_qss = None
            self._art_bg = None
            self._stale_panels = set()
            self.setUpdatesEnabled(True)         # a restyle may have been cut short with repaints held
            with self._quiet():
                for c in self.children():
                    if isinstance(c, QWidget) and _qt_valid(c):
                        c.setStyleSheet("")
            self.update()
        with self._quiet():
            QApplication.instance().setStyleSheet(qss)

    def event(self, e):
        # Under _restyle_panels the window itself keeps the old app sheet, so the
        # strips between docks are filled here. Before the base class paints, since
        # that is what draws the separators on top.
        if e.type() == QEvent.Type.Paint and self._art_bg is not None:
            p = QPainter(self)
            p.fillRect(e.rect(), self._art_bg)
            p.end()
        return super().event(e)

    def childEvent(self, e):
        # A dialog or menu made after the last palette change is not one of the
        # panels styled above, so it would show the stale app sheet.
        if e.type() == QEvent.Type.ChildPolished and self._panel_qss is not None:
            c = e.child()
            if isinstance(c, QWidget) and _qt_valid(c) and c.styleSheet() != self._panel_qss:
                c.setStyleSheet(self._panel_qss)
        super().childEvent(e)

    def _make_qss(self, palette=None) -> str:
        s = self._settings
        try:
            import titlebar
            titlebar.show_line = bool(s.get("panel_borders", True))
            titlebar.aero = s.get("theme_style", "default") == "aero"
            titlebar.shade = max(0, min(100, int(s.get("frame_shade", 0))))
        except ImportError:
            pass
        return make_qss(
            palette,
            text_font=s.get("text_font", "") or None,
            ui_size=s.get("ui_size", 13),
            accent=s.get("accent_color", "#8040c8"),
            bg=s.get("bg_color", "") or None,
            text=s.get("text_color", "") or None,
            frame_shade=s.get("frame_shade", 0),
            borders=bool(s.get("panel_borders", True)),
            style=s.get("theme_style", "default"),
            radius=int(s.get("corner_radius", 4)),
        )

    def _on_theme_style_changed(self, d: dict):
        acc = d.get("accent", "#8040c8")
        for v in (getattr(self, "_osc", None), getattr(self, "_viz", None),
                  getattr(self, "_eq_panel", None), getattr(self, "_dsp_panel", None), getattr(self, "_waterfall", None),
                  getattr(getattr(self, "_controls", None), "_wf", None)):
            if v is not None and hasattr(v, "set_default_accent"):
                v.set_default_accent(acc)
        self._refresh_idle_accents()
        if self._current_art_palette() is None:
            self._rgb.set_accent(None, acc)
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
        # does, so skip it when the palette didn't actually change (same album).
        if palette == getattr(self, "_last_art_palette", "unset"):
            return
        # And wait a moment before doing it. Skipping through songs delivers a
        # palette for each, and restyling for every one of them is what made the
        # window crawl and flash half old and half new colours.
        self._pending_art = (path, palette)
        self._art_timer.start()

    def _commit_art_palette(self):
        path, palette = self._pending_art
        cur = self._player.current
        if cur is None or str(cur.path) != str(path):
            return
        if palette == getattr(self, "_last_art_palette", "unset"):
            return
        self._last_art_palette = palette
        self._restyle_panels(self._make_qss(palette), palette,
                             lambda: self._canvas_accents(palette))

    def _canvas_accents(self, palette):
        """The widgets that draw their own colours switch in the same instant the
        restyled panels appear, not before (the EQ curve was turning the new colour
        while half the window still showed the old one)."""
        self._rgb.set_accent(palette, self._settings.get("accent_color", "#8040c8"))
        self._viz.set_accent(palette)
        self._osc.set_accent(palette)
        self._controls._wf.set_accent(palette)
        self._eq_panel.set_accent(palette)
        self._dsp_panel.set_accent(palette)
        self._waterfall.set_accent(palette)
        self._online.set_accent(palette)
        # built later than this handler is connected, so it may not exist yet
        np = getattr(self, "_np_widget", None)
        if np is not None:
            np.set_accent(palette)

    def _refresh_idle_accents(self):
        """Visualizers/waveform follow the OS highlight only on a *pure* system
        theme; with the app's own (purple) theme or art-derived colours, the
        no-art fallback is the app accent, so it matches what's on screen."""
        use_sys = self._system_theme and not self._art_color_on
        self._osc.set_system_accent(use_sys)
        self._waterfall.set_system_accent(use_sys)
        self._online.set_system_accent(use_sys)
        self._viz.set_system_accent(use_sys)
        self._controls._wf.set_system_accent(use_sys)
        eqp = getattr(self, "_eq_panel", None)
        if eqp is not None:
            eqp.set_system_accent(use_sys)
        dsp = getattr(self, "_dsp_panel", None)
        if dsp is not None:
            dsp.set_system_accent(use_sys)

    def _on_system_theme_changed(self, on: bool):
        self._system_theme = on
        app = QApplication.instance()
        if on:
            app.setStyle("")          # restore OS default style
            self._set_app_qss("")    # clear; rebuilt below once palette settles
        else:
            app.setStyle("Fusion")
        # Defer one tick so the palette swap has propagated before we rebuild.
        QTimer.singleShot(0, self._after_theme_change)

    def _after_theme_change(self):
        self._reapply_qss()
        self._refresh_idle_accents()

    def _on_font_changed(self, font_name: str):
        app = QApplication.instance()
        if app.styleSheet() or self._panel_qss is not None:
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
        if app.styleSheet() or self._panel_qss is not None:
            self._reapply_qss()

    def _reapply_qss(self):
        """Single source of truth for the active stylesheet.

        - album-art theme: full app QSS built from the art palette
        - system theme: keep the OS theme (no app QSS)
        - app (dark) theme: full app QSS (accent / background / text / shade)
        """
        app = QApplication.instance()
        palette = self._current_art_palette()
        self._last_art_palette = palette   # keep the no-op guard in sync
        if palette is not None:
            self._set_app_qss(self._make_qss(palette))
        elif self._system_theme and not self._art_color_on:
            self._set_app_qss("")
        else:
            self._set_app_qss(self._make_qss())
        self._restyle_toggle_buttons()

    def _restyle_toggle_buttons(self):
        """Make every checkable button visibly invert when active. The app/art QSS
        styles ``QPushButton:checked`` itself, but the system theme has no such rule,
        so under it the rule is set per button (only ``:checked``, so the native
        unchecked look is preserved) using the OS highlight colour. Covers Controls,
        Library mode buttons, EQ bypass, Milkdrop toggles, and any later toggle."""
        from PySide6.QtWidgets import QPushButton
        use_system = self._system_theme and not self._art_color_on
        if use_system:
            pal = self.palette()
            bg = pal.highlight().color().name()
            fg = pal.highlightedText().color().name()
            ss = ("QPushButton:checked{background:%s;border:1px solid %s;color:%s;}"
                  "QPushButton:checked:hover{background:%s;}" % (bg, bg, fg, bg))
        else:
            ss = ""   # app/art theme: the global QSS already styles :checked
        for b in self.findChildren(QPushButton):
            if b.isCheckable():
                b.setStyleSheet(ss)

    def _build_ui(self):
        self._viz   = VisualizerWidget(self._analyzer, self._player)

        self._video = make_video_widget()
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
            # The frame draws the panel's outline: a dock paints none of its own once it has a
            # custom title bar, and a border on the panel itself would sit under its children.
            frame = QFrame()
            frame.setObjectName("panel_frame")
            box = QVBoxLayout(frame)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(widget)
            d.setWidget(frame)
            d.setAllowedAreas(Qt.AllDockWidgetAreas)
            d.setFeatures(features)
            self.addDockWidget(area, d)
            _native_float(d, themed=self._titlebar is not None)
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

        self._online = OnlineWidget(self._player)
        self._online.add_to_playlist.connect(self._on_add)
        self._online.play_now.connect(self.open_files)
        self._online.play_next.connect(lambda paths: play_next_into(self._playlist, paths))
        self._online.library_changed.connect(self._on_online_saved)
        self._online.status.connect(self._show_status)
        self._online.set_keeper(lambda: [p for lst in self._playlist._lists() for p in lst.paths()])
        self._online_dock = _make_dock("Online", self._online, Qt.LeftDockWidgetArea)
        self.tabifyDockWidget(lib_dock, self._online_dock)
        self._online_dock.visibilityChanged.connect(lambda v: self._online.set_suspended(not v))
        self._online_dock.hide()

        self._devices = DevicesWidget(self._player)
        self._devices.add_to_playlist.connect(self._on_add)
        self._devices.play_now.connect(self.open_files)
        self._devices.library_changed.connect(self._on_devices_saved)
        self._devices.status.connect(self._show_status)
        self._devices.set_playlist_source(lambda: (self._playlist.source_name(), [t.path for t in self._player.queue]))
        self._playlist.send_to_device.connect(self._send_to_device)
        self._playlist.convert_requested.connect(self._convert)
        self._library.convert_requested.connect(self._convert)
        self._devices_dock = _make_dock("Devices", self._devices, Qt.LeftDockWidgetArea)
        self.tabifyDockWidget(lib_dock, self._devices_dock)
        self._devices_dock.hide()
        self._devices.watch()           # plugging something in is announced while the panel is closed

        self._upnext = UpNextWidget(self._player)
        self._playlist.source_changed.connect(self._upnext.set_source)
        self._upnext.set_source(self._playlist.source_name())
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

        self._waterfall = WaterfallWidget(self._analyzer, self._player)
        self._wf_dock = _make_dock("Waterfall", self._waterfall, Qt.BottomDockWidgetArea)
        self.tabifyDockWidget(ctrl_dock, self._wf_dock)
        self._wf_dock.hide()    # the first launch that has it adds it as a tab (see _restore_ui_state)

        self._eq_panel = EqWidget(self._player)
        self._eq_panel.status.connect(lambda m: self._show_status(m, 6000))
        eq_dock = _make_dock("Equalizer", self._eq_panel, Qt.BottomDockWidgetArea)
        self.tabifyDockWidget(ctrl_dock, eq_dock)
        eq_dock.hide()   # opt-in via the View menu, so it doesn't disturb saved layouts

        self._eq_dock = eq_dock
        self._dsp_panel = DspWidget(self._dsp_chain)
        self._dsp_dock = _make_dock("DSP", self._dsp_panel, Qt.BottomDockWidgetArea)
        self.tabifyDockWidget(ctrl_dock, self._dsp_dock)
        self._dsp_dock.hide()   # opt-in via the View menu, like the equalizer

        # Milkdrop (projectM) visualizer, docked inside the window; opt-in.
        try:
            from visualizer_window import VisualizerPanel
            self._milkdrop = VisualizerPanel(self._analyzer, self._player)
            self._milkdrop_dock = _make_dock("Milkdrop", self._milkdrop, Qt.BottomDockWidgetArea)
            self.tabifyDockWidget(ctrl_dock, self._milkdrop_dock)
            self._milkdrop_dock.hide()
            self._milkdrop.unavailable.connect(self._milkdrop_unavailable)
        except Exception as e:
            from applog import log
            log.warning("Milkdrop panel unavailable: %s", e)
            self._milkdrop = self._milkdrop_dock = None
        ctrl_dock.raise_()

        self._np_widget = NowPlayingWidget(self._player)
        self._player.set_video_output(self._np_widget.video_widget)
        np_dock = self._np_dock = _make_dock("Now Playing", self._np_widget, Qt.LeftDockWidgetArea)
        self.tabifyDockWidget(lib_dock, np_dock)
        lib_dock.raise_()

        self._settings = SettingsWidget()
        self._wire_settings()
        # Apply saved font/size over the base QSS loaded in main.py. With the system theme on there is no
        # app sheet to rebuild, and making one here put the purple theme back after _apply_settings had cleared it.
        _saved_tf = self._settings.get("text_font", "") or None
        _saved_sz = self._settings.get("ui_size", 13)
        if self._system_theme and not self._art_color_on:
            if _saved_tf:
                self._on_font_changed(_saved_tf)
            if _saved_sz != 13:
                self._on_ui_size_changed(_saved_sz)
        elif _saved_tf or _saved_sz != 13:
            self._set_app_qss(self._make_qss())
        self._settings_dock = _make_dock("Settings", self._settings, Qt.RightDockWidgetArea)
        self.tabifyDockWidget(meta_dock, self._settings_dock)
        pl_dock.raise_()

        self._viz.open_settings.connect(self._open_viz_settings)
        self._settings.tour_requested.connect(self.show_tour)

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
        s.waterfall_requested.connect(self._waterfall.open_settings)
        self._waterfall.config_changed.connect(s.set_waterfall)
        self._waterfall.presets_changed.connect(s.set_waterfall_presets)
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
        s.artist_split_changed.connect(self._on_artist_split_changed)
        s.layout_export.connect(self.export_layout)
        s.layout_import.connect(self.import_layout)
        s.config_export.connect(self.export_all_config)
        s.config_import.connect(self.import_all_config)
        s.audio_device_changed.connect(self._player.set_audio_device)
        s.keep_output_open_changed.connect(self._player.set_keep_output_open)
        s.discord_changed.connect(self._on_discord_changed)
        s.crossfade_changed.connect(self._player.set_crossfade)
        s.video_dsp_changed.connect(self._player.set_video_dsp)
        s.video_delay_changed.connect(self._player.set_video_delay)
        s.set_video_lag_source(self._player.video_sound_offset_ms)
        s.theme_style_changed.connect(self._on_theme_style_changed)
        s.rgb_changed.connect(self._rgb.configure)
        self._rgb.status_changed.connect(s.set_rgb_status)
        self._apply_settings()

    def _apply_settings(self):
        """Puts the saved settings into the panels and the engine. A config import runs it again, so it connects nothing."""
        s = self._settings
        self._waterfall.set_presets(s.get("waterfall_presets") or {})
        self._waterfall.apply_config(s.get("waterfall") or {})
        configure_artists(**s.artist_options())
        self._rgb.set_accent(None, s.get("accent_color", "#8040c8"))
        self._rgb.configure({
            "enabled":         s.get("rgb_enabled", False),
            "style":           s.get("rgb_style", "Spectrum"),
            "keyboard":        s.get("rgb_keyboard", True),
            "emblem":          s.get("rgb_emblem", True),
            "mode_led":        s.get("rgb_mode_led", False),
            "openrgb":         s.get("rgb_openrgb", False),
            "openrgb_host":    s.get("rgb_openrgb_host", "127.0.0.1:6742"),
            "openrgb_exclude": s.get("rgb_openrgb_exclude", "predator, acer"),
            "brightness":      s.get("rgb_brightness", 100),
            "fps":             s.get("rgb_fps", 12),
        })
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
            "vsync":      s.get("osc_vsync", False),
            "bars":       s.get("osc_bars", False),
            "bar_width":  s.get("osc_bar_width", 3),
        })
        self._lyrics.set_font_size(s.get("lyr_font_size", 18))
        self._art_color_on   = s.get("art_color_theme", False)
        self._system_theme   = s.get("system_theme", True)
        # Push the custom accent into the canvas widgets (they paint outside QSS).
        _acc = s.get("accent_color", "#8040c8")
        for v in (self._osc, self._viz, self._eq_panel, self._dsp_panel, self._controls._wf, self._waterfall):
            if hasattr(v, "set_default_accent"):
                v.set_default_accent(_acc)
        self._refresh_idle_accents()
        self._reapply_qss()   # also restyles all toggle buttons for the active theme
        self._apply_docking(s.get("docking_enabled", True))
        self._apply_viz_auto_suspend(s.get("viz_auto_suspend", True))
        self._play_stats.set_enabled(s.get("play_stats_enabled", False))
        self._player.set_crossfade(s.get("crossfade_ms", 0))
        self._player.set_keep_output_open(s.get("keep_output_open", False))
        self._player.set_video_dsp(s.get("video_dsp", True))
        self._player.set_video_delay(s.get("video_delay_ms", 0))
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
        # Restore saved audio output device. Logged either side, because this
        # is the first thing at startup to touch the audio backend and a crash
        # in there leaves no Python frame of its own to point at.
        saved_id = s.get("audio_device_id", "")
        if saved_id:
            from PySide6.QtMultimedia import QMediaDevices
            from player import device_key
            import logs
            log = logs.get("audio")
            for dev in QMediaDevices.audioOutputs():
                if device_key(dev) == saved_id:
                    log.debug("restoring saved output %s", dev.description())
                    self._player.set_audio_device(dev)
                    log.debug("output restored")
                    break
            else:
                log.debug("saved output is not plugged in, staying on the default")

    def _on_artist_split_changed(self):
        """Work out the artists of everything on screen again, from the tag cache. No file is read."""
        import library_cache
        by_path = {t.path: t for t in self._player.queue}
        for path, info in library_cache.get_many(list(by_path), read_missing=False).items():
            by_path[path].fill(info)
        self._library.regroup()
        self._playlist.reread_artists()

    def _on_appearance_changed(self, d: dict):
        self._art_color_on = d.get("art_color_theme", False)
        if not self._art_color_on:
            self._rgb.set_accent(None, self._settings.get("accent_color", "#8040c8"))
            self._viz.set_accent(None)
            self._osc.set_accent(None)
            self._controls._wf.set_accent(None)
            self._eq_panel.set_accent(None)
            self._dsp_panel.set_accent(None)
            np = getattr(self, "_np_widget", None)
            if np is not None:
                np.set_accent(None)
            if self._system_theme:
                QApplication.instance().setStyle("")
                self._set_app_qss("")
            else:
                self._set_app_qss(self._make_qss())
            self._restyle_toggle_buttons()
        else:
            self._reapply_qss()     # the art theme is the app's own sheet, with or without a palette yet
            if self._player.current:
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
        # Nesting splits any area between panels. (Not grouped dragging: it moves a whole tab
        # group into a window of Qt's own, which broke the mouse under Wayland.)
        opt = QMainWindow.DockOption
        opts = opt.AnimatedDocks | opt.AllowTabbedDocks
        if enabled:
            opts |= opt.AllowNestedDocks
        self.setDockOptions(opts)
        for dock in self.findChildren(QDockWidget):
            dock.setAllowedAreas(
                Qt.AllDockWidgetAreas if enabled else Qt.NoDockWidgetArea
            )

    def _apply_viz_auto_suspend(self, enabled: bool):
        self._viz_auto_suspend = enabled
        self._update_visual_suspend()

    def _open_viz_settings(self):
        self._settings_dock.setVisible(True)
        self._settings.show_section("Visuals")
        QTimer.singleShot(0, self._settings_dock.raise_)

    @staticmethod
    def _wants_titlebar() -> bool:
        import os
        if os.environ.get("AUDIOCRATE_TITLEBAR", "").strip() in ("0", "off", "no"):
            return False
        try:
            cfg = plat_paths.config_dir() / "settings.json"
            return bool(json.loads(cfg.read_text(encoding="utf-8")).get("custom_titlebar", True))
        except Exception:
            return True

    def _build_menu(self):
        # menuBar() would swap the title bar out for a plain menu bar
        mb = self._titlebar.menubar if self._titlebar is not None else self.menuBar()

        fm = mb.addMenu("File")
        fm.addAction("Add files…",  self._add_files)
        fm.addAction("Add folder…", self._add_folder)
        fm.addAction("Convert files…", self._convert_pick)
        fm.addSeparator()
        fm.addAction("Quit", self.close)

        pm = mb.addMenu("Playback")
        pm.addAction("Play / pause", self._player.toggle)
        pm.addAction("Stop",         self._player.stop)
        pm.addAction("Next",         self._player.next)
        pm.addAction("Previous",     self._player.prev)
        pm.addSeparator()
        self._build_sleep_menu(pm)

        vm = _CollapsibleMenu("View", self)
        mb.addMenu(vm)
        self._build_view_menu(vm)

        hm = mb.addMenu("Help")
        hm.addAction("Keyboard shortcuts", self._show_shortcuts)
        hm.addAction("Show the tour",      self.show_tour)
        hm.addAction("Diagnostics…",       self._show_diagnostics)
        hm.addAction("About AudioCrate",   self._show_about)

    def _show_shortcuts(self):
        from PySide6.QtWidgets import QMessageBox
        rows = [
            ("Space", "Play / pause"),
            ("S", "Stop"),
            ("N or Ctrl+→", "Next track"),
            ("P or Ctrl+←", "Previous track"),
            ("→ / ← or . / ,", "Seek ± 5 seconds"),
            ("↑ / ↓", "Volume ± 5%"),
            ("M", "Mute"),
            ("R", "Cycle play mode (SEQ / REP / RP1)"),
            ("E", "Toggle Enhance"),
            ("Ctrl+F", "Focus the playlist filter"),
            ("Del", "Remove selected playlist tracks"),
            ("Enter", "Play the selected playlist track"),
            ("Ctrl+Z / C / V", "Playlist undo / copy / paste"),
            ("Media keys", "Play / pause / stop / next / prev"),
            ("Drag &amp; drop", "Add files or folders to the queue"),
        ]
        body = "<table cellspacing=0 cellpadding=5>" + "".join(
            f"<tr><td><b>{k}</b></td><td>&nbsp;&nbsp;{v}</td></tr>" for k, v in rows
        ) + "</table>"
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard shortcuts")
        box.setTextFormat(Qt.RichText)
        box.setText(body)
        box.exec()

    def start_tour(self, skip: bool = False):
        """First start: show the picture tour once, a moment after the window is up.

        main.py calls this in the program that owns the window, with skip set when the launch
        was given files or a link to play (the tour then waits for the next start). The settings
        key tour_seen holds the version of the tour that has been shown, so a later tour can show
        again. AUDIOCRATE_NO_TOUR=1 turns it off, which the tests and the video harness use."""
        import os
        from applog import log
        from tour_dialog import TOUR_VERSION
        why = ("the launch has something to play" if skip
               else "AUDIOCRATE_NO_TOUR is set" if os.environ.get("AUDIOCRATE_NO_TOUR") == "1"
               else "it has been shown" if self._tour_seen() >= TOUR_VERSION else "")
        if why:
            log.info("tour not shown: %s", why)
            return
        QTimer.singleShot(700, self._first_tour)

    def _tour_seen(self) -> int:
        try:
            return int(self._settings.get("tour_seen", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _first_tour(self):
        from applog import log
        if not self.isVisible() or QApplication.activeModalWidget() is not None:
            log.info("tour not shown: the window is not ready or a dialog is open")
            return
        if self.show_tour():
            log.info("tour shown: first start")

    def show_tour(self) -> bool:
        """Open the picture tour (first start, Settings, Help menu). Playback goes on while it is open.
        False when the slides are not installed."""
        if self._tour is not None:
            self._tour.raise_()
            self._tour.activateWindow()
            return True
        from applog import log
        from tour_dialog import TourDialog, default_slides_dir
        dlg = TourDialog(self, default_slides_dir())
        if not dlg.slides:
            log.warning("the tour slides are missing from %s", default_slides_dir())
            dlg.deleteLater()
            self._show_status("The tour is not installed", 4000)
            return False
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.finished.connect(self._tour_closed)
        self._tour = dlg
        dlg.show()
        return True

    def _tour_closed(self, _result):
        """Done, Skip, Escape and the close button all count as having seen it."""
        from tour_dialog import TOUR_VERSION
        self._tour = None
        if self._tour_seen() < TOUR_VERSION:
            self._settings.set("tour_seen", TOUR_VERSION)

    def _show_diagnostics(self):
        """The Diagnostics window: not modal, one at a time, built when asked for and deleted when closed."""
        dlg = getattr(self, "_diag_dlg", None)
        if dlg is None or not _qt_valid(dlg):
            from diagnostics_dialog import DiagnosticsDialog
            dlg = self._diag_dlg = DiagnosticsDialog(self)
            dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

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
        "Waterfall":    "\U000f1849",   # waterfall
        "Milkdrop":     "\U000f02f8",   # image-filter-vintage
        "Now Playing":  "\U000f0387",   # music
        "Info":         "\U000f02fd",   # information-outline
        "Lyrics":       "\U000f09ed",   # text-box-outline
        "Up Next":      "\U000f0411",   # playlist-play
        "Controls":     "\U000f040b",   # play-box-outline
        "Equalizer":    "\U000f0ea2",   # equalizer
        "DSP":          "\U000f1542",   # tune-variant
        "Video":        "\U000f0567",   # video
        "Library":      "\U000f125f",   # bookshelf
        "Online":       "\U000f0337",   # link
        "Devices":      "\U000f129e",   # usb-flash-drive
        "Playlist":     "\U000f0cb8",   # playlist-music
        "Stats":        "\U000f012a",   # chart-line
        "Settings":     "\U000f0493",   # cog
    }
    _VIEW_CATEGORIES = [
        ("Visualizers", ["Spectrum", "Oscilloscope", "Waterfall", "Milkdrop"]),
        ("Now Playing", ["Now Playing", "Info", "Lyrics", "Up Next"]),
        ("Playback",    ["Controls", "Equalizer", "DSP", "Video"]),
        ("Library",     ["Library", "Online", "Playlist", "Stats", "Devices"]),
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
        self._sleep_menu = parent_menu.addMenu("Sleep timer")
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
        mins, ok = QInputDialog.getInt(self, "Sleep timer",
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
        self._show_status("Sleep timer elapsed, playback paused", 4000)

    def _set_sleep_label(self, suffix):
        self._sleep_menu.setTitle("Sleep timer" if not suffix else f"Sleep timer ({suffix})")

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
        if not any(t.path == target for t in self._player.queue):
            self._playlist.adopt_current()      # playing from another playlist: this one takes over
        q = self._player.queue
        for i in range(len(q) - 1, -1, -1):   # newest copy of the opened file
            if q[i].path == target:
                self._player.play_track(i)
                break

    def _add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(PLAYABLE_EXTS))
        # All files is offered as well, because the extension filter hides a file
        # whose name only looks like it has a known suffix. The playlist says so
        # when it refuses one, which is more use than the file never appearing.
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add files", self._last_browse_dir,
            f"Audio/video ({exts});;All files (*)"
        )
        if paths:
            self._last_browse_dir = str(Path(paths[0]).parent)
            self._playlist.add_paths([Path(p) for p in paths])

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder", self._last_browse_dir)
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
        self._restore_dock_layout(d)
        self._restore_floating(d)

    def _restore_layout(self):
        """Read the saved state and put the docks back, before the window is first shown.

        On a window that is already on screen restoreState lays the docks out again as it
        shows each one, which overwrites the saved sizes (a column came back with its middle
        panel stretched and its neighbours at their minimum), and under Wayland the window is
        still small when the first event loop turn runs and grows to its maximized size after.
        Qt looks after a hidden window itself."""
        try:
            self._saved = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._saved = _first_layout()  # no saved state, use the baked default
        try:
            self._restore_dock_layout(self._saved)
            self._place_new_panels(self._saved)
        except Exception:
            import traceback; traceback.print_exc()

    def _place_new_panels(self, d: dict):
        """Panels this build has and the saved layout never heard of. Qt parks such a panel as a
        tab in whichever group it likes, so one that is shown or raised later would change a
        group's current tab. It stays hidden for someone who already has a layout (the View menu
        has it, and the status bar says so), and a fresh install, which has no layout to disturb,
        gets the waterfall and Online (Devices is for people who plug something in)."""
        state = QByteArray.fromBase64((d.get("dock_state") or "").encode()).data()
        self._new_panels = []
        for dock in (self._wf_dock, self._online_dock, self._devices_dock, self._dsp_dock):
            if not state or dock.objectName().encode("utf-16-be") not in state:
                self._new_panels.append(dock.windowTitle())
                dock.setVisible(_baked(d) and dock not in (self._devices_dock, self._dsp_dock))
        if self._dsp_dock.windowTitle() in self._new_panels and not self._eq_dock.isFloating():
            self.tabifyDockWidget(self._eq_dock, self._dsp_dock)     # opens next to the equalizer, not in a column of its own
        if d is _COMPACT_LAYOUT:
            for dock in self.findChildren(QDockWidget):
                if dock.windowTitle() == "Playlist":
                    dock.raise_()       # the compact layout was saved on its Settings tab; a new install opens on the songs

    def _restore_dock_layout(self, d: dict):
        """The window geometry and the docked panels. Floating ones are set up by _restore_floating."""
        if geo := d.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        if state := d.get("dock_state"):
            self.restoreState(QByteArray.fromBase64(state.encode()))
        if not self.isVisible():
            for dock in self.findChildren(QDockWidget):
                if dock.isFloating():
                    dock.hide()     # restoreState showed it, as a window with no parent: _restore_floating does it again

    def _restore_floating(self, d: dict):
        # restoreState handles docked positions and visibility; a floating dock gets
        # its window flags, then its visibility, then its geometry.
        dock_data = d.get("docks", {})
        for dock in self.findChildren(QDockWidget):
            info = dock_data.get(dock.windowTitle(), {})
            if dock.isFloating():
                # Flags go on before restoreGeometry, because setWindowFlags resets geometry.
                _make_floating(dock, self._titlebar is not None)
                # Shown before its size is restored: a panel whose rows wrap (the equalizer) reports
                # a minimum height for the width it was last laid out at, and a hidden window keeps
                # the one from the narrow window it started as, which would stretch this one.
                if info.get("visible", True):
                    dock.show()
                else:
                    dock.hide()
                if geo_b64 := info.get("geo"):
                    dock.restoreGeometry(QByteArray.fromBase64(geo_b64.encode()))
            else:
                dock.setTitleBarWidget(_DockTitleBar(dock))

    def _save_ui_state(self):
        if not self._ui_restored:
            return      # still on its way back from the saved state
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
                return   # nothing changed since the last 10 s tick, skip the disk write
            self._last_saved_state = payload
            tmp = _STATE_FILE.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            plat_fs.replace(tmp, _STATE_FILE)
        except Exception:
            import traceback; traceback.print_exc()

    def _restore_ui_state(self):
        d = self._saved     # read by _restore_layout
        try:
            self._restore_floating(d)
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
                # player queue to that tab and load the track as current (not playing).
                # A missing file is in no tab, so nothing is loaded.
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
            if self._new_panels and not _baked(d):
                self._show_status("New panels in the View menu: " + ", ".join(self._new_panels), 10000)
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
        self._ui_restored = True

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
            cfg = dict(_DEFAULTS, tour_seen=self._tour_seen())     # a bundle without it must not bring the tour back
            cfg.update({k: v for k, v in bundle["settings"].items() if k in cfg})
            self._settings._cfg = cfg
            self._settings.save()
            self._apply_settings()
            self._settings.show_artist_options()
            self._settings.sync_theme()
            self._on_artist_split_changed()
            self._set_app_qss(self._make_qss())
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

        try: self._rgb.shutdown()          # hands the lights back before the process goes
        except Exception: pass
        try: self._online.shutdown()       # cancels downloads and ends the yt-dlp processes
        except Exception: pass
        try: self._devices.shutdown()
        except Exception: pass
        try:
            if self._convert_dlg is not None and _qt_valid(self._convert_dlg):
                self._convert_dlg.shutdown()   # cancels a running conversion and keeps its settings
        except Exception: pass

        # Stop all Qt timers before anything else
        for obj in (self._save_timer, self._prefetch_timer, self._art_timer, self._player._fade_timer):
            try: obj.stop()
            except Exception: pass
        try: self._palette_pool.shutdown(wait=False, cancel_futures=True)
        except Exception: pass
        try: self._viz._tick_timer.stop()
        except Exception: pass
        try: self._osc._tick_timer.stop()
        except Exception: pass
        try: self._waterfall._timer.stop()
        except Exception: pass
        try: self._waterfall.flush()           # a zoom or a setting still waiting to be saved
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

        # Give the media session back; MPRIS unregisters its D-Bus service before Qt destroys adaptors
        try: self._mpris.shutdown()
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

        try: self._player._media.stop()
        except Exception: pass
        try: self._player._vaudio.shutdown()
        except Exception: pass
        try: self._player._media.setSource(QUrl())
        except Exception: pass

        # Block dock signals during destruction
        for dock in self.findChildren(QDockWidget):
            try: dock.blockSignals(True)
            except Exception: pass

        try: self._save_ui_state()
        except Exception: pass
        try: self._playlist.save_session()
        except Exception: pass
        try: self._settings.save()
        except Exception: pass
        try: self._eq_panel._save_state()   # flush EQ state even if debounce pending
        except Exception: pass
        try:
            self._dsp_save.stop()
            self._dsp_chain.shutdown()      # writes dsp.json if anything changed since it was last written
        except Exception: pass
