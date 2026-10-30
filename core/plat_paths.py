"""Where the program keeps its files.

Linux keeps them where the code always did: ~/.config/audiocrate, ~/.cache/audiocrate and ~/.local/share/audiocrate
(the Online service also reads XDG_CONFIG_HOME, XDG_CACHE_HOME and XDG_DATA_HOME, and asks for that with xdg=True).
Windows keeps settings in %APPDATA%\\AudioCrate, downloaded tools and databases in %LOCALAPPDATA%\\AudioCrate\\Data, caches
in %LOCALAPPDATA%\\AudioCrate\\Cache and files that only matter while the program runs in %LOCALAPPDATA%\\AudioCrate\\run.
A file called portable.txt beside the program moves all of that into a data folder beside it, so the program can run
from a stick.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_LINUX_NAME = "audiocrate"
_WINDOWS_NAME = "AudioCrate"

# Known folder ids (KNOWNFOLDERID in shlobj_core.h)
_MUSIC = "4BD8D571-6D19-48D3-BE97-422220080E43"
_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
_PROFILE = "5E6C858F-0E22-4760-9AFE-EA3317B67173"


def _program_dir() -> Path:
    """The folder the program runs from: beside the executable when frozen, the folder with main.py otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    """Whether a portable.txt sits beside the program. Only Windows builds are ever portable."""
    return sys.platform == "win32" and (_program_dir() / "portable.txt").is_file()


def _windows_base(variable: str, below_profile: tuple) -> Path:
    """The folder a Windows environment variable names, or the same place below the profile when it is not set."""
    return Path(os.environ.get(variable) or Path.home().joinpath(*below_profile))


def _xdg_base(variable: str, below_home: tuple, xdg: bool) -> Path:
    home = Path.home()
    return Path(os.environ.get(variable) or home.joinpath(*below_home)) if xdg else home.joinpath(*below_home)


def config_dir(xdg: bool = False) -> Path:
    """Settings, the library database, playlists, logs. xdg=True makes Linux honour XDG_CONFIG_HOME, which nothing
    but the Online service does."""
    if sys.platform != "win32":
        return _xdg_base("XDG_CONFIG_HOME", (".config",), xdg) / _LINUX_NAME
    if is_portable():
        return _program_dir() / "data"
    return _windows_base("APPDATA", ("AppData", "Roaming")) / _WINDOWS_NAME


def data_dir(xdg: bool = False) -> Path:
    """Files the program downloads or is given and keeps, such as its own yt-dlp. On Windows they stay on this
    machine (the local folder) so the profile does not carry them to others."""
    if sys.platform != "win32":
        return _xdg_base("XDG_DATA_HOME", (".local", "share"), xdg) / _LINUX_NAME
    if is_portable():
        return _program_dir() / "data"
    return _windows_base("LOCALAPPDATA", ("AppData", "Local")) / _WINDOWS_NAME / "Data"


def cache_dir(xdg: bool = False) -> Path:
    """Anything that can be thrown away and made again."""
    if sys.platform != "win32":
        return _xdg_base("XDG_CACHE_HOME", (".cache",), xdg) / _LINUX_NAME
    if is_portable():
        return _program_dir() / "data" / "cache"
    return _windows_base("LOCALAPPDATA", ("AppData", "Local")) / _WINDOWS_NAME / "Cache"


def runtime_dir() -> Path:
    """The pid file and other files that mean something only while the program runs. Linux uses XDG_RUNTIME_DIR
    when it is a folder and a per-user folder in the temp folder (made with mode 700 by the caller) when not."""
    if sys.platform == "win32":
        if is_portable():
            return _program_dir() / "data" / "run"
        return _windows_base("LOCALAPPDATA", ("AppData", "Local")) / _WINDOWS_NAME / "run"
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base)
    import tempfile
    return Path(tempfile.gettempdir()) / f"audiocrate-{os.getuid()}"


def _known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath for one KNOWNFOLDERID, None when Windows has no answer."""
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    path = ctypes.c_wchar_p()
    try:
        if ctypes.WinDLL("shell32").SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return None
        return Path(path.value) if path.value else None
    finally:
        ctypes.WinDLL("ole32").CoTaskMemFree(path)


def _user_folder(folder_id: str, name: str) -> Path:
    """A folder of the user's profile such as Music. Windows may have moved it (OneDrive does), so it is asked for.
    When USERPROFILE has been pointed somewhere else than the real profile, as the checks do, the folder is looked
    for below that profile instead, so nothing reaches the real one."""
    if sys.platform == "win32":
        real = _known_folder(_PROFILE)
        if real is not None and os.path.normcase(os.path.normpath(real)) == os.path.normcase(os.path.normpath(Path.home())):
            found = _known_folder(folder_id)
            if found is not None:
                return found
    return Path.home() / name


def music_dir() -> Path:
    return _user_folder(_MUSIC, "Music")


def documents_dir() -> Path:
    return _user_folder(_DOCUMENTS, "Documents")


def tools_dir() -> Path | None:
    """The folder beside the program that holds helper programs (ffmpeg, openmpt123 and the like), None on Linux,
    where they come from the system."""
    if sys.platform != "win32":
        return None
    return _program_dir() / "tools"


def breakable(text: str) -> str:
    """text with a place to break the line after every backslash on Windows. Qt breaks a line at spaces and slashes but not
    at a backslash, so a Windows path in a label that wraps keeps the whole path on one line and stretches the label
    (and the page it is on) to the width of the path. Linux text is returned as it is."""
    return text.replace("\\", "\\\u200b") if sys.platform == "win32" else text


def shown(path) -> str:
    """A folder as it is written in a message to the person using the program. Linux writes the home folder as ~,
    Windows writes the whole path."""
    p = Path(path)
    if sys.platform != "win32":
        try:
            return "~/" + p.relative_to(Path.home()).as_posix()
        except ValueError:
            pass
    return str(p)
