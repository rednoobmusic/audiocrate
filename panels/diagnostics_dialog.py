"""Help, Diagnostics: one read-only page with the program's state, the stacks of its threads and the end of the log.

Refresh reads it all again; nothing updates by itself, so nothing runs while the window is open or closed. Save report
writes a zip of the logs, the newest dumps, the versions and the settings with every secret removed (core/diag_report.py)
and does it on a worker thread.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase, QPalette
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

import diag
import diag_report
from applog import tail

_HIDE = ("log_tail", "stalls")       # shown in their own sections


def _outline(d: dict, depth: int = 0) -> list[str]:
    out = []
    for k, v in d.items():
        pad = "  " * depth
        label = str(k).replace("_", " ")
        if isinstance(v, dict) and v:
            out += [f"{pad}{label}:"] + _outline(v, depth + 1)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            out.append(f"{pad}{label}:")
            for item in v:
                out.append(f"{pad}  " + ", ".join(f"{a} {b}" for a, b in item.items()))
        else:
            out.append(f"{pad}{label}: {v}")
    return out


def page_text() -> str:
    snap = diag.snapshot()
    stalls = snap.get("stalls") or []
    out = ["Overview"] + ["  " + ln for ln in _outline({k: v for k, v in snap.items() if k not in _HIDE})]
    out += ["", f"UI stalls (the window did not answer for more than half a second), {len(stalls)} recorded"]
    out += [f"  {s['time']}  {s['seconds']:.1f} s  in {s['where']}" for s in stalls[-10:]] or ["  none"]
    out += ["", "Threads", diag.stacks_text(), "Log, the last 200 lines"]
    return diag_report.scrub_text("\n".join(out + tail.last(200)))      # what is shown is what gets pasted into a bug report


class DiagnosticsDialog(QDialog):
    _saved = Signal(str)

    def __init__(self, window=None):
        super().__init__(window)
        self.setWindowTitle("Diagnostics")
        self.setMinimumSize(520, 460)
        self.resize(640, 560)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        if self.font().pixelSize() > 0:
            font.setPixelSize(self.font().pixelSize())
        self._text.setFont(font)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setForegroundRole(QPalette.PlaceholderText)
        refresh, save, close = QPushButton("Refresh"), QPushButton("Save report…"), QPushButton("Close")
        refresh.clicked.connect(self.refresh)
        save.clicked.connect(self._save)
        close.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addWidget(self._status, 1)
        for b in (refresh, save, close):
            row.addWidget(b)
        col = QVBoxLayout(self)
        col.addWidget(self._text, 1)
        col.addLayout(row)
        self._saved.connect(self._status.setText)
        self.refresh()

    def refresh(self):
        bar = self._text.verticalScrollBar()
        keep = bar.value()
        self._text.setPlainText(page_text())
        bar.setValue(keep)

    def _save(self):
        default = str(Path.home() / time.strftime("audiocrate-report-%Y%m%d-%H%M%S.zip"))
        path, _ = QFileDialog.getSaveFileName(self, "Save report", default, "Zip files (*.zip)")
        if not path:
            return
        self._status.setText("Saving the report...")
        threading.Thread(target=self._write, args=(path,), name="diag-report", daemon=True).start()

    def _write(self, path: str):
        try:
            diag_report.build_report(path, diag.snapshot("report"), diag.stacks_text("report"), diag.CONFIG_DIR, diag.DEBUG_DIR)
            msg = f"Saved to {path}. Tokens, secrets and cookies were removed."
        except Exception as e:
            msg = f"The report could not be saved: {e}"
        try:
            self._saved.emit(msg)
        except RuntimeError:
            pass                                    # the window was closed meanwhile
