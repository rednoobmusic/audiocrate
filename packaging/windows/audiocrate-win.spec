# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 6.22 spec for the Windows build: one folder, no console window, no UPX.
#
#   run_win.sh pyinstaller --noconfirm --clean audiocrate-win.spec
#
# build.sh copies the program, this file, audiocrate.ico and audiocrate.manifest into one folder and runs it there.
# The native libraries (libs\) and the helper programs (tools\) are not part of the freeze: build.sh puts them
# beside AudioCrate.exe, where plat_native and plat_paths look for them.
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

HERE = Path(SPECPATH)
SRC_DIRS = ("core", "panels", "integrations", "ui")

_ver = (HERE / "core" / "_version.py").read_text(encoding="utf-8")
version = re.search(r'VERSION\s*=\s*"([^"]+)"', _ver).group(1)
build = int(re.search(r"BUILD\s*=\s*(\d+)", _ver).group(1))
numbers = (tuple(int(n) for n in re.findall(r"\d+", version)) + (0, 0, 0))[:3] + (build,)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                      date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "AudioCrate"),
            StringStruct("FileDescription", "AudioCrate music player"),
            StringStruct("FileVersion", f"{version}.{build}"),
            StringStruct("InternalName", "AudioCrate"),
            StringStruct("LegalCopyright", "GPL-2.0-or-later"),
            StringStruct("OriginalFilename", "AudioCrate.exe"),
            StringStruct("ProductName", "AudioCrate"),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# The program imports its own modules by flat name (_bootstrap puts the four folders on sys.path), and a few of
# them are only imported inside functions, so every module of the four folders is listed.
program_modules = sorted({p.stem for d in SRC_DIRS for p in (HERE / d).glob("*.py") if p.stem != "__init__"})

# Qt modules the program does not use. QtMultimedia (with Qt's FFmpeg backend, for video), QtMultimediaWidgets,
# QtOpenGL, QtDBus (mpris.py imports it), QtNetwork and QtSvg stay.
QT_EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2", "PySide6.QtQuick3D",
    "PySide6.QtQuickTest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebView", "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtHttpServer", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtTest", "PySide6.QtHelp",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtVirtualKeyboard", "PySide6.QtSql", "PySide6.QtSpatialAudio", "PySide6.QtUiTools",
    "PySide6.QtConcurrent", "PySide6.QtMultimediaQuick",
]

# Installed in the build Python for other jobs, never imported by the program.
OTHER_EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "PyQt5", "PyQt6", "PySide2", "IPython", "pytest", "yt_dlp", "gallery_dl",
    "requests", "urllib3", "certifi", "charset_normalizer", "idna", "fontTools",
    "PIL.AvifImagePlugin", "PIL._avif",           # AVIF pictures (4 MB of codec): covers and tour images are JPEG, PNG and WebP
]

a = Analysis(
    ["main.py"],
    pathex=[str(HERE), *(str(HERE / d) for d in SRC_DIRS)],
    binaries=[],
    datas=[
        ("JetBrainsMono", "JetBrainsMono"),
        ("icon.png", "."),
        ("ui/tour", "tour"),                    # tour_dialog looks for a folder called tour beside itself
        ("integrations/webui", "webui"),        # and webremote for webui
        ("LICENSE", "."),
    ],
    hiddenimports=[
        *program_modules,
        "pypresence", "shiboken6",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtDBus", "PySide6.QtNetwork",
        "PySide6.QtOpenGL", "PySide6.QtSvg",
        "scipy.signal", "scipy.ndimage", "scipy.linalg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=QT_EXCLUDES + OTHER_EXCLUDES,
    noarchive=False,
    optimize=2,                                   # no docstrings or asserts in the frozen modules: a smaller archive
)


# Qt files that only the on-screen keyboard plugin and the PDF image plugin need (the import tables of everything that
# stays were read: nothing else loads Qt6Qml, Qt6Quick or Qt6Pdf), and plugins for touch input and Direct2D rendering.
UNUSED_QT = ("qt6qml", "qt6quick", "qt6pdf", "qt6virtualkeyboard", "qpdf.dll", "qtvirtualkeyboardplugin",
             "qtuiotouchplugin", "qdirect2d.dll")


def keep(entry) -> bool:
    """Drop what a player that only speaks English and runs from a folder does not use: Qt's translations, the Qt
    files listed above and the test folders of numpy and scipy."""
    name = entry[0].replace("\\", "/").lower()
    if "/translations/" in name or name.endswith(".qm") or any(part in name for part in UNUSED_QT):
        return False
    return not ("/tests/" in name and name.startswith(("numpy/", "scipy/")))


a.datas = [d for d in a.datas if keep(d)]
a.binaries = [b for b in a.binaries if keep(b)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AudioCrate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(HERE / "audiocrate.ico"),
    version=version_info,
    manifest=str(HERE / "audiocrate.manifest"),
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AudioCrate",
)
