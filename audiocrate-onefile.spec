# -*- mode: python ; coding: utf-8 -*-
# Trimmed single-file test build:
#   /tmp/acbuild/bin/pyinstaller --noconfirm audiocrate-onefile.spec
# Produces dist/audiocrate, one self-contained executable.
#
# What keeps it small:
#   - no collect_dynamic_libs/collect_data_files('PySide6'), which pulled in all
#     ~640 MB; PyInstaller's PySide6 hook and the excludes below do the job
#   - the heavy Qt modules the app does not use are excluded (WebEngine,
#     QML/Quick, Charts, 3D, ...)
#   - symbols are stripped and the binaries are UPX-compressed, except the Qt and
#     Python core libs
block_cipher = None

_QT_EXCLUDES = [
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
    'PySide6.QtQuickControls2', 'PySide6.QtQuick3D', 'PySide6.QtQuickTest',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngineQuick', 'PySide6.QtWebView', 'PySide6.QtWebChannel',
    'PySide6.QtWebSockets', 'PySide6.QtHttpServer',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DExtras',
    'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtDesigner',
    'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
    'PySide6.QtLocation', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
    'PySide6.QtSerialBus', 'PySide6.QtTest', 'PySide6.QtHelp',
    'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtStateMachine',
    'PySide6.QtTextToSpeech', 'PySide6.QtVirtualKeyboard', 'PySide6.QtSql',
    'PySide6.QtSpatialAudio', 'PySide6.QtUiTools', 'PySide6.QtConcurrent',
    'PySide6.QtMultimediaQuick',
]

a = Analysis(
    ['main.py'],
    pathex=['.', 'core', 'panels', 'integrations', 'ui'],
    binaries=[
        ('/usr/lib/libgme.so.0', '.'),
        ('libfurnace_wrap.so', '.'),
    ],
    datas=[
        ('JetBrainsMono', 'JetBrainsMono'),
        ('icon.png', '.'),
        ('ui/tour', 'tour'),    # the tour dialog looks for it beside itself
    ],
    hiddenimports=[
        '_bootstrap', 'audio_identity',
        'vgm_player', 'fur_player', 'dsp_engine', 'dsp_chain', 'panel_eq',
        'dsp_base', 'dsp_user_chain', 'dsp_presets', 'dsp_offline', 'dsp_fx_dynamics',
        'dsp_fx_space', 'dsp_fx_filters', 'dsp_fx_loudness', 'dsp_fx_modulation',
        'video_audio', 'panel_dsp', 'converter', 'audio_files', 'convert_dialog',
        'diag', 'diag_cli', 'diag_report', 'diagnostics_dialog',
        'discord_rpc', 'pypresence', 'scipy', 'scipy.signal', 'ffmpeg_util',
        'library_cache', 'lyrics_fetch', 'sleep_timer', 'applog',
        'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtDBus',
        'PySide6.QtNetwork', 'PySide6.QtOpenGL', 'PySide6.QtSvg', 'shiboken6',
        'mutagen', 'mutagen.mp3', 'mutagen.flac', 'mutagen.mp4', 'mutagen.id3',
        'mutagen.ogg', 'mutagen.oggvorbis', 'mutagen.wave',
        'numpy', 'PIL', 'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'pandas', 'PyQt5', 'PyQt6', 'PySide2',
        'IPython', 'pytest',
        # scipy submodules stay: scipy.signal imports scipy.fft, scipy.special,
        # scipy.linalg and others when it loads
    ] + _QT_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Drop the FFmpeg media stack (the video and decode backend of QtMultimedia's
# QMediaPlayer). Audio playback feeds raw PCM from the system ffmpeg binary to a
# QAudioSink and chiptunes use their own libs, so neither needs these (~40 MB
# saved). Video files do not play in this trimmed build, on purpose.
_VIDEO_DROP = (
    'libavcodec', 'libavformat', 'libavutil', 'libavfilter', 'libavdevice',
    'libswscale', 'libswresample', 'libpostproc', 'ffmpegmediaplugin',
)
# Also drop bits the app never uses:
#  - GTK platform theme (libgtk-3 / libqgtk3): Qt falls back to its default theme
#  - AVIF/HEIF image codecs (aom/avif/heif/dav1d/de265): cover art is jpg/png/webp/bmp
_EXTRA_DROP = (
    'libgtk-3', 'libgtk-x11', 'libqgtk3', 'libgdk-3', 'libgdk_pixbuf',
    'libaom', 'libavif', 'libheif', 'libdav1d', 'libde265', 'libx265',
)
a.binaries = [b for b in a.binaries
              if not any(k in b[0].lower() for k in _VIDEO_DROP + _EXTRA_DROP)]
# Drop Qt translations (.qm); the UI is English only.
a.datas = [d for d in a.datas
           if 'translations' not in d[0].lower() and not d[0].lower().endswith('.qm')]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    a.zipfiles,
    [],
    name='audiocrate',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[
        'libQt6Core.so*', 'libQt6Gui.so*', 'libQt6Widgets.so*',
        'libpython3*.so*', 'libgme.so*', 'libfurnace_wrap.so',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
