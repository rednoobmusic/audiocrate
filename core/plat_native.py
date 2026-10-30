"""Loading the native libraries the program uses (libgme, the Furnace and SID wrappers, projectM)."""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def _folders() -> list[Path]:
    """Where a DLL may be, in the order they are tried. AC_LIBS names one more folder, tried last."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        found = [exe_dir, exe_dir / "libs"] + ([Path(sys._MEIPASS)] if hasattr(sys, "_MEIPASS") else [])
    else:
        here = Path(__file__).resolve().parent
        found = [here, here.parent, here.parent / "libs"]
    extra = os.environ.get("AC_LIBS")
    return found + [Path(extra)] if extra else found


def load(linux_name, windows_name: str, with_folder: bool = False):
    """The library as a ctypes object, or None when it is not there. Linux asks the system loader for linux_name, or
    for each of a list of names and paths in turn when the caller keeps a search of its own. Windows loads the file
    from the program's own folders by absolute path, so a copy of the same DLL somewhere else on PATH is never picked
    up in its place. with_folder is for a DLL that imports another one lying beside it: the folder is added to the
    places Windows looks in for imports."""
    if sys.platform != "win32":
        names = [linux_name] if isinstance(linux_name, (str, os.PathLike)) else linux_name
        for name in names:
            try:
                return ctypes.CDLL(str(name))
            except OSError:
                pass
        return None
    for folder in _folders():
        path = folder / windows_name
        if path.is_file():
            try:
                if with_folder:
                    os.add_dll_directory(str(folder))
                return ctypes.CDLL(str(path))
            except OSError:
                pass
    return None
