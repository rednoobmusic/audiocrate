"""Milkdrop (projectM) visualizer, a dockable panel inside AudioCrate.

projectM renders its final composite to framebuffer 0, so it must draw to a real
window surface; a QOpenGLWidget (which composites its own offscreen FBO) shows
nothing. The view is therefore a QOpenGLWindow, embedded in the dock with
QWidget.createWindowContainer so that it lives inside the main window.

Below the view is a control bar mirroring the site's butterchurn visualizer:
previous / next preset, shuffle toggle, lock toggle, and the current preset name.
Live mono PCM is pulled from the same analyzer that drives the spectrum/oscilloscope.
"""
from __future__ import annotations

import os
import time
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat, QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PySide6.QtOpenGL import QOpenGLWindow

import power
import projectm_engine as pe
from applog import log

_PCM_SAMPLES = 512                  # mono samples fed per frame
_RESIZE_SETTLE = 0.15               # seconds the window must hold its size before projectM is told
_BLEND = 3.2                        # a soft cut lasts 3 s (see ProjectMEngine); a resize waits it out
_IC_PREV    = "\U000f0141"          # chevron-left
_IC_NEXT    = "\U000f0142"          # chevron-right
_IC_SHUFFLE = "\U000f049d"          # shuffle
_IC_LOCK    = "\U000f033e"          # lock


class _GLViz(QOpenGLWindow):
    def __init__(self, pcm_callback):
        super().__init__()
        self._pcm = pcm_callback
        self._engine: pe.ProjectMEngine | None = None
        self._last_size = (0, 0)
        self._pending_preset = ""   # restore this preset once the engine exists
        self._locked = False        # and these: the engine is made on first show, which can be
        self._shuffle = True        # long after the saved toggles were restored
        self._want = (0, 0)         # the size the window has now and since when, see _size_ready
        self._changed_at = 0.0
        self._held = False          # automatic preset changes are held while the size is moving
        self._queued = None         # a preset button pressed meanwhile, done once it settles
        self._last_change = 0.0     # when the preset last changed, to know if a blend may be running
        self._last_pos = -1

        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        fmt.setSwapInterval(1)
        self.setFormat(fmt)

        self._timer = QTimer(self)
        self._timer.setInterval(16)   # ~60 fps, slower when a frame costs more (see _paced)
        self._timer.timeout.connect(self.update)
        self._cost = 0.0
        self._t0 = 0.0
        self.frameSwapped.connect(self._paced)

    def _fb_size(self):
        dpr = self.devicePixelRatio()
        return max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))

    # GL lifecycle (context current in these callbacks)
    def initializeGL(self):
        try:
            w, h = self._fb_size()
            self._engine = pe.ProjectMEngine(w, h, pe.resolve_preset_dir())
            self._last_size = self._want = (w, h)
            log.info("projectM %s ready: %d presets", pe.version(), self._engine.preset_count)
            self._engine.set_shuffle(self._shuffle)
            self._engine.set_locked(self._locked)
            self.apply_pending_preset()
            self._timer.start()
        except Exception as e:
            log.warning("projectM init failed: %s", e)

    def _size_ready(self) -> bool:
        """projectM is told the window's size only once it has held still, never on every step of a
        drag: each call rebuilds its render targets, and doing that while a preset blend or a preset
        load is under way is what crashed the driver. While the size moves nothing is drawn and the
        presets stay where they are; a blend still running is ended first."""
        now = time.perf_counter()
        size = self._fb_size()
        if size != self._want:
            self._want, self._changed_at = size, now
        pos = self._engine.position()
        if pos != self._last_pos:
            self._last_pos, self._last_change = pos, now          # a change the playlist made itself
        stale = size != self._last_size
        moving = stale and now - self._changed_at < _RESIZE_SETTLE
        self._hold(moving)
        if stale and not moving:
            if now - self._last_change < _BLEND:
                self._engine.hard_cut_current()
            self._engine.resize(*size)
            self._last_size = size
            stale = False
        return not stale

    def _hold(self, on: bool):
        if on == self._held:
            return
        self._held = on
        self._engine.set_locked(True if on else self._locked)
        if not on and self._queued:
            step, self._queued = self._queued, None
            self._step(step)

    def _step(self, step: str):
        self._last_change = time.perf_counter()
        (self._engine.next_preset if step == "next" else self._engine.prev_preset)(False)

    def paintGL(self):
        if not self._engine:
            return
        try:
            if not self._size_ready():
                f = self.context().functions()
                f.glClearColor(0.0, 0.0, 0.0, 1.0)
                f.glClear(0x4000)                  # GL_COLOR_BUFFER_BIT
                return
            self._t0 = time.thread_time()
            self._engine.add_pcm_mono(self._pcm())
            self._engine.render()
        except Exception as e:
            log.warning("projectM render error: %s", e)
            self._timer.stop()

    def _paced(self):
        """Keep a frame's share of the GUI thread to about a third. The cost is the CPU time
        from the start of the paint to the end of the swap (a driver that spins waiting for
        the display burns it there), so the scope, the restyle after a song change and every
        click are not queued behind a visualizer that wants the whole thread."""
        c = time.thread_time() - self._t0
        if not 0 < c < 0.5:
            return
        self._cost = self._cost * 0.8 + c * 0.2
        ms = max(power.frame_ms(16), min(100, int(self._cost * 3000)))
        if ms != self._timer.interval():
            self._timer.setInterval(ms)

    # controls (forwarded from the panel)
    def next_preset(self):
        if self._engine:                      # soft/blended, like the site's next arrow
            self._queued = "next" if self._held else None
            if not self._held:
                self._step("next")

    def prev_preset(self):
        if self._engine:
            self._queued = "prev" if self._held else None
            if not self._held:
                self._step("prev")

    def set_shuffle(self, on: bool):
        self._shuffle = on
        if self._engine:
            self._engine.set_shuffle(on)

    def set_locked(self, on: bool):
        self._locked = on
        if self._engine and not self._held:        # while held it is locked anyway, and restored after
            self._engine.set_locked(on)

    def preset_name(self) -> str:
        return self._engine.current_preset_name() if self._engine else ""

    def preset_path(self) -> str:
        return self._engine.current_preset_path() if self._engine else ""

    def apply_pending_preset(self):
        if self._engine and self._pending_preset and os.path.exists(self._pending_preset):
            self._engine.select_preset_path(self._pending_preset)

    def set_running(self, run: bool):
        if run and self._engine and not self._timer.isActive():
            self._timer.start()
        elif not run and self._timer.isActive():
            self._timer.stop()

    def shutdown(self):
        self._timer.stop()
        if self._engine:
            self.makeCurrent()
            try:
                self._engine.destroy()
            finally:
                self.doneCurrent()
            self._engine = None


class VisualizerPanel(QWidget):
    """Dockable Milkdrop panel: a projectM view + a butterchurn-style control bar."""

    def __init__(self, analyzer, player, parent=None):
        super().__init__(parent)
        self._analyzer = analyzer
        self._player   = player
        self._pending_preset = ""

        self._gl = _GLViz(self._pull_pcm)
        self._container = QWidget.createWindowContainer(self._gl, self)
        self._container.setFocusPolicy(Qt.StrongFocus)

        bar = self._build_controls()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._container, 1)
        lay.addLayout(bar)
        self.setMinimumSize(160, 140)

        # poll the active preset name (catches auto-advance, not just button presses)
        self._name_timer = QTimer(self)
        self._name_timer.setInterval(700)
        self._name_timer.timeout.connect(self._refresh_name)
        self._suspended = False   # set by MainWindow when the app is minimized/unfocused

    def _build_controls(self) -> QHBoxLayout:
        gf = QFont(); gf.setPointSize(12)
        def _btn(glyph, tip, checkable=False):
            b = QPushButton(glyph)
            b.setFont(gf)
            b.setFixedSize(30, 24)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            return b

        self._b_prev = _btn(_IC_PREV, "Previous preset")
        self._b_next = _btn(_IC_NEXT, "Next preset")
        self._b_shuffle = _btn(_IC_SHUFFLE, "Shuffle presets", checkable=True)
        self._b_lock = _btn(_IC_LOCK, "Lock current preset (stop auto-advance)", checkable=True)
        self._b_shuffle.setChecked(True)   # engine starts shuffled

        self._name = QLabel("")
        self._name.setObjectName("track_sub")
        nf = QFont(); nf.setPointSize(9)
        self._name.setFont(nf)
        self._name.setStyleSheet("color: rgba(255,255,255,0.45);")

        self._b_prev.clicked.connect(self._gl.prev_preset)
        self._b_next.clicked.connect(self._gl.next_preset)
        self._b_prev.clicked.connect(self._refresh_name)
        self._b_next.clicked.connect(self._refresh_name)
        self._b_shuffle.toggled.connect(self._gl.set_shuffle)
        self._b_lock.toggled.connect(self._gl.set_locked)
        # checked-state inversion is applied centrally by MainWindow._restyle_toggle_buttons

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 3, 8, 3)
        bar.setSpacing(4)
        for b in (self._b_prev, self._b_next, self._b_shuffle, self._b_lock):
            bar.addWidget(b)
        bar.addWidget(self._name, 1)
        return bar

    def set_pending_preset(self, path: str):
        """Preset to restore. Applied now if the engine already exists (e.g. the dock
        was restored visible and initialised before this ran), else when it inits."""
        self._pending_preset = path or ""
        self._gl._pending_preset = self._pending_preset
        self._gl.apply_pending_preset()

    # persisted toggle state (saved in ui_state.json)
    @property
    def locked(self) -> bool:
        return self._b_lock.isChecked()

    @property
    def shuffle_on(self) -> bool:
        return self._b_shuffle.isChecked()

    def restore_toggles(self, locked: bool, shuffle: bool):
        """Apply saved lock/shuffle. Restoring lock also stops auto-advance from
        immediately replacing the preset we just restored."""
        self._b_shuffle.setChecked(bool(shuffle))
        self._gl.set_shuffle(bool(shuffle))
        self._b_lock.setChecked(bool(locked))
        self._gl.set_locked(bool(locked))

    def current_preset_path(self) -> str:
        """Live preset path if running, else the last known one (so saving the session
        doesn't wipe the remembered preset when the visualizer was never opened)."""
        p = self._gl.preset_path()
        if p:
            self._pending_preset = p
        return p or self._pending_preset

    def _pull_pcm(self) -> np.ndarray | None:
        try:
            return self._analyzer.get_audio_window(self._player.position_ms, _PCM_SAMPLES)
        except Exception:
            return None

    def _refresh_name(self):
        name = self._gl.preset_name()
        if name:
            fm = self._name.fontMetrics()
            self._name.setText(fm.elidedText(name, Qt.ElideRight, max(40, self._name.width())))
            self._name.setToolTip(name)

    # Render only while on-screen and the app is focused: projectM is the heaviest
    # thing in the app, so it stops when hidden, tabbed away, minimized or unfocused.
    def _sync_running(self):
        run = self.isVisible() and not self._suspended
        self._gl.set_running(run)
        self._name_timer.start() if run else self._name_timer.stop()

    def set_suspended(self, suspended: bool):
        """Called by MainWindow when the app is minimized or loses focus."""
        self._suspended = bool(suspended)
        self._sync_running()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_running()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._sync_running()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Right, Qt.Key_N):
            self._gl.next_preset(); self._refresh_name()
        elif k in (Qt.Key_Left, Qt.Key_P):
            self._gl.prev_preset(); self._refresh_name()
        elif k in (Qt.Key_F, Qt.Key_F11):
            w = self.window()
            w.showNormal() if w.isFullScreen() else w.showFullScreen()
        elif k == Qt.Key_Escape and self.window().isFullScreen():
            self.window().showNormal()
        else:
            super().keyPressEvent(e)

    def shutdown(self):
        self._gl.shutdown()
