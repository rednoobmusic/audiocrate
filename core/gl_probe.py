"""Whether this computer can give the program the OpenGL it asks for, found out before a window that needs it is shown.

A computer with only the basic display driver (a remote desktop, a virtual machine, a driver that was never installed) may
answer a request for a core profile context of OpenGL 3.3 with an older context or with none. Qt then shows a window that
never draws. The probe makes the context on a surface that is never shown, reads what it really is, and says in a sentence
what is missing, so the panel that needs it can step aside.
"""
from __future__ import annotations

import re

from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat

_GL_RENDERER, _GL_VERSION = 0x1F01, 0x1F02


def _text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def probe(fmt: QSurfaceFormat) -> tuple[bool, str]:
    """(True, what the context is) when a context of the requested version and profile can be made current, otherwise
    (False, the reason in plain words). Call it on the GUI thread, after the application exists."""
    want = (fmt.majorVersion(), fmt.minorVersion())
    name = "OpenGL %d.%d" % want
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    if not surface.isValid():
        return False, "%s could not be started: the system gave no surface to draw on" % name
    context = QOpenGLContext()
    context.setFormat(fmt)
    if not context.create():
        return False, "%s could not be started: the display driver does not offer it" % name
    if not context.makeCurrent(surface):
        return False, "%s could not be started: the driver refused to use it" % name
    try:
        got = context.format()
        have = (got.majorVersion(), got.minorVersion())
        compat = got.profile() == QSurfaceFormat.CompatibilityProfile
        core = got.profile() == QSurfaceFormat.CoreProfile
        renderer = ""
        try:
            f = context.functions()
            renderer = _text(f.glGetString(_GL_RENDERER))
            version = _text(f.glGetString(_GL_VERSION))
            numbers = re.match(r"\s*(\d+)\.(\d+)", version)
            if numbers:                         # what the driver says, not what the platform plugin was asked for
                have = (int(numbers[1]), int(numbers[2]))
                compat = compat or "compatibility" in version.lower()
                core = core and not compat
        except Exception:
            pass
        if have < want or (fmt.profile() == QSurfaceFormat.CoreProfile and compat):
            return False, "%s is not available: the display driver gives %d.%d%s" % (
                name, have[0], have[1], " without the core profile" if compat else "")
        return True, "OpenGL %d.%d%s%s" % (have[0], have[1], " core" if core else "", ", " + renderer if renderer else "")
    finally:
        context.doneCurrent()
