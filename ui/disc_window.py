"""The window of the disc player: a DVD or Blu-ray in a drive, a disc image, or the folder of a disc.

The picture and the strip over it are VideoStage, the same as for a video in Now Playing. What a disc adds is its
menu, its titles and its chapters, and the keys that steer a menu. The window is not part of the main window, so a
film can be watched while the library is used, and closing it stops the disc.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from disc_player import DiscPlayer
from panel_controls import _slider_to_vol, _vol_to_slider
from video_stage import VideoStage

_SEEK = 10                      # seconds an arrow key moves
_NAV_KEYS = {Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
             Qt.Key_Return: "activate", Qt.Key_Enter: "activate"}


def _clock(seconds: int) -> str:
    h, rest = divmod(max(int(seconds), 0), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


class DiscWindow(QWidget):
    closed = Signal()

    def __init__(self, args=()):
        super().__init__(None, Qt.Window)
        self.setWindowTitle("AudioCrate disc")
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, Qt.black)
        self.setPalette(pal)
        self.resize(960, 540)
        self.disc = DiscPlayer(self, args)
        self.stack = QStackedWidget(self)
        self.stage = VideoStage(self.disc)
        self.stack.addWidget(self.stage)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.stack)

        disc, stage = self.disc, self.stage
        disc.frame.connect(stage.video_widget.show_image)
        disc.ratio_changed.connect(self._set_ratio)
        disc.error.connect(stage.notice)
        disc.frame.connect(lambda _: setattr(self, "_shown", True))
        disc.ended.connect(self._on_ended)
        self._shown = False                 # has a picture come out of this disc yet
        stage.volume_set.connect(lambda v: setattr(disc, "volume", _slider_to_vol(v)))
        stage.volume_nudged.connect(self._nudge_volume)
        stage.mute_toggled.connect(self._toggle_mute)
        stage.extra_menu = self._disc_menu
        stage.on_click = lambda: None if disc.in_menu else disc.toggle()      # a click cannot press a menu button, and a menu does not need a pause
        self._b_menu = stage.add_button("Menu", "The menu of the disc", disc.go_menu)
        self._b_titles = stage.add_button("Titles", "Titles of the disc", lambda: stage._popup(self._titles_menu(), self._b_titles))
        self._b_chapters = stage.add_button("Chapters", "Chapters of this title",
                                            lambda: stage._popup(self._chapters_menu(), self._b_chapters))
        # a menu of the disc can be steered with the buttons, as with the arrow keys and Enter (libvlc has no way to
        # hand it a click on the picture)
        self._pad = [stage.add_button(text, tip, lambda _=False, w=where: disc.navigate(w), 30)
                     for text, tip, where in (("\u25b2", "Up in the menu", "up"), ("\u25bc", "Down in the menu", "down"),
                                              ("\u25c0", "Left in the menu", "left"), ("\u25b6", "Right in the menu", "right"),
                                              ("OK", "Choose the highlighted button", "activate"))]
        for b in self._pad:
            b.hide()
        disc.disc_changed.connect(self._sync_pad)
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------ opening and closing

    def open(self, source: str) -> str:
        """Play a disc, an image or a folder. Returns '' or a sentence that says why not."""
        self._shown = False
        why = self.disc.open(source)
        if why:
            return why
        name = Path(source).stem if not str(source).startswith("/dev/") else "Disc"
        self.setWindowTitle(f"AudioCrate: {name}")
        self.show()
        self.raise_()
        self.activateWindow()
        return ""

    def closeEvent(self, e):
        self.stage.set_fullscreen(False)
        self.disc.close()
        QApplication.instance().removeEventFilter(self)
        self.closed.emit()
        super().closeEvent(e)

    def _sync_pad(self):
        menu = self.disc.in_menu
        for b in self._pad:
            b.setVisible(menu)
        self.stage.refit()
        if menu:
            self.stage.poke()

    def _on_ended(self):
        if not self._shown:
            QMessageBox.information(self, "Play a disc", "Nothing came out of this disc. A disc with copy protection needs libdvdcss "
                                    "to be read, and a scratched one may not read at all.")

    def _set_ratio(self, ratio: float):
        self.stage.video_widget.native_ratio = ratio
        self.stage.video_widget.update()

    # ------------------------------------------------------------------ menus

    def _titles_menu(self, parent=None) -> QMenu:
        m = QMenu("Titles", parent or self)
        titles, cur = self.disc.titles(), self.disc.title_index
        if not titles:
            m.addAction("This disc lists no titles").setEnabled(False)
        for i, (name, secs, menu) in enumerate(titles):
            label = "Menu" if menu and not name else (name or f"Title {i}")
            a = m.addAction(label + (f"   {_clock(secs)}" if secs else ""), lambda _=False, i=i: self.disc.set_title(i))
            a.setCheckable(True)
            a.setChecked(i == cur)
        return m

    def _chapters_menu(self, parent=None) -> QMenu:
        menu = self.disc.in_menu
        m = QMenu("Disc menus" if menu else "Chapters", parent or self)       # a menu's "chapters" are the other menus
        chapters, cur = self.disc.chapters(), self.disc.chapter_index
        if not chapters:
            m.addAction("This title has no chapters").setEnabled(False)
        for i, (name, secs) in enumerate(chapters):
            a = m.addAction(name if menu else f"{name}   {_clock(secs)}", lambda _=False, i=i: self.disc.set_chapter(i))
            a.setCheckable(True)
            a.setChecked(i == cur)
        return m

    def _disc_menu(self, menu: QMenu):
        d = self.disc
        sub = menu.addMenu("Disc")
        sub.addAction("Go to the menu of the disc", d.go_menu)
        sub.addMenu(self._titles_menu(sub))
        sub.addMenu(self._chapters_menu(sub))
        sub.addSeparator()
        for text, where in (("Up", "up"), ("Down", "down"), ("Left", "left"), ("Right", "right"), ("Select", "activate")):
            sub.addAction(text + "\t" + {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "activate": "Enter"}[where]
                          + " in a menu", lambda _=False, w=where: d.navigate(w))
        menu.addAction("Next chapter\tPage down", d.next)
        menu.addAction("Previous chapter\tPage up", d.prev)

    # ------------------------------------------------------------------ keys and volume

    def _nudge_volume(self, step: int):
        if self.disc.in_menu:                       # the wheel and the up and down keys move through a menu
            self.disc.navigate("up" if step > 0 else "down")
            return
        self.disc.volume = _slider_to_vol(_vol_to_slider(self.disc.volume) + step)

    def _toggle_mute(self):
        self.disc.toggle_mute()
        self.stage.notice("Muted" if self.disc.muted else "Sound on")

    def eventFilter(self, obj, ev):
        # every key press of the program comes by here first, and while this window is the active one the keys are its
        # own: the main window would otherwise play or skip the library on a space or an N
        if (ev.type() == QEvent.KeyPress and self.isVisible() and QApplication.activeWindow() is self.stage.window()
                and QApplication.activePopupWidget() is None and QApplication.activeModalWidget() is None):
            if not ev.isAutoRepeat() or ev.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
                self._key(ev)
            return True
        return False

    def _key(self, ev):
        K, d = Qt.Key, self.disc
        k = ev.key()
        if self.stage.handle_key(ev):
            return
        if d.in_menu and k in _NAV_KEYS:
            d.navigate(_NAV_KEYS[k])
        elif k in (K.Key_Space, K.Key_MediaTogglePlayPause, K.Key_MediaPlay, K.Key_MediaPause):
            d.toggle()
        elif k == K.Key_Left:
            d.seek_delta(-_SEEK)
        elif k == K.Key_Right:
            d.seek_delta(_SEEK)
        elif k == K.Key_Up:
            self._nudge_volume(5)
        elif k == K.Key_Down:
            self._nudge_volume(-5)
        elif k in (K.Key_PageDown, K.Key_N, K.Key_MediaNext):
            d.next()
        elif k in (K.Key_PageUp, K.Key_P, K.Key_MediaPrevious):
            d.prev()
        elif k in (K.Key_M, K.Key_VolumeMute):
            self._toggle_mute()
        elif k == K.Key_Menu or k == K.Key_T:
            d.go_menu()
