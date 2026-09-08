"""ctypes binding for projectM v4 (libprojectM-4) — a native Milkdrop visualizer.

projectM renders the same `.milk` presets Milkdrop/Butterchurn use. The v4 library
exposes a clean C API (no C++ shim needed). This wraps the core + playlist libs and
drives them from an OpenGL context owned by Qt (see ui/visualizer_window.py).

All GL calls (create / render / resize) must happen with the target GL context
current — the QOpenGLWidget guarantees that inside initializeGL/paintGL/resizeGL.
"""
from __future__ import annotations
import ctypes
import os
from ctypes.util import find_library

import numpy as np

PROJECTM_MONO   = 1
PROJECTM_STEREO = 2

_core = None
_pl   = None


def _load():
    global _core, _pl
    if _core is not None:
        return True
    try:
        _core = ctypes.CDLL(find_library("projectM-4") or "libprojectM-4.so")
        _pl   = ctypes.CDLL(find_library("projectM-4-playlist") or "libprojectM-4-playlist.so")
    except OSError:
        _core = _pl = None
        return False

    h = ctypes.c_void_p
    # ── core (libprojectM-4) ──────────────────────────────────────────────────
    _core.projectm_create.restype  = h
    _core.projectm_destroy.argtypes = [h]
    _core.projectm_set_window_size.argtypes = [h, ctypes.c_size_t, ctypes.c_size_t]
    _core.projectm_set_mesh_size.argtypes   = [h, ctypes.c_size_t, ctypes.c_size_t]
    _core.projectm_set_fps.argtypes = [h, ctypes.c_int32]
    _core.projectm_set_preset_duration.argtypes = [h, ctypes.c_double]
    _core.projectm_set_aspect_correction.argtypes = [h, ctypes.c_bool]
    _core.projectm_set_soft_cut_duration.argtypes = [h, ctypes.c_double]
    _core.projectm_set_preset_locked.argtypes = [h, ctypes.c_bool]
    _core.projectm_get_preset_locked.argtypes = [h]
    _core.projectm_get_preset_locked.restype  = ctypes.c_bool
    _core.projectm_load_preset_file.argtypes = [h, ctypes.c_char_p, ctypes.c_bool]
    _core.projectm_opengl_render_frame.argtypes = [h]
    _core.projectm_pcm_add_float.argtypes = [h, ctypes.POINTER(ctypes.c_float),
                                             ctypes.c_uint, ctypes.c_int]
    _core.projectm_pcm_get_max_samples.restype = ctypes.c_uint
    _core.projectm_get_version_string.restype  = ctypes.c_char_p

    # ── playlist (libprojectM-4-playlist) ─────────────────────────────────────
    ph = ctypes.c_void_p
    _pl.projectm_playlist_create.restype  = ph
    _pl.projectm_playlist_create.argtypes = [h]
    _pl.projectm_playlist_destroy.argtypes = [ph]
    _pl.projectm_playlist_add_path.argtypes = [ph, ctypes.c_char_p, ctypes.c_bool, ctypes.c_bool]
    _pl.projectm_playlist_add_path.restype  = ctypes.c_uint32
    _pl.projectm_playlist_size.argtypes = [ph]
    _pl.projectm_playlist_size.restype  = ctypes.c_uint32
    _pl.projectm_playlist_set_shuffle.argtypes = [ph, ctypes.c_bool]
    _pl.projectm_playlist_set_position.argtypes = [ph, ctypes.c_uint32, ctypes.c_bool]
    _pl.projectm_playlist_set_position.restype  = ctypes.c_uint32
    _pl.projectm_playlist_get_position.argtypes = [ph]
    _pl.projectm_playlist_get_position.restype  = ctypes.c_uint32
    _pl.projectm_playlist_play_next.argtypes = [ph, ctypes.c_bool]
    _pl.projectm_playlist_play_next.restype  = ctypes.c_uint32
    _pl.projectm_playlist_play_previous.argtypes = [ph, ctypes.c_bool]
    _pl.projectm_playlist_play_previous.restype  = ctypes.c_uint32
    _pl.projectm_playlist_item.argtypes = [ph, ctypes.c_uint32]
    _pl.projectm_playlist_item.restype  = ctypes.c_void_p   # owned char* → free it ourselves
    _pl.projectm_playlist_free_string.argtypes = [ctypes.c_void_p]
    return True


def available() -> bool:
    return _load()


def version() -> str:
    if not _load():
        return ""
    v = _core.projectm_get_version_string()
    return v.decode() if v else ""


def resolve_preset_dir() -> str | None:
    """First existing preset directory with .milk files. User config dir wins so the
    site's exact presets (copied there, kept out of the release) take priority over the
    system projectM pack."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.expanduser("~/.config/audiocrate/presets"),
        os.path.join(here, "..", "_presets_site"),
        "/usr/share/projectM/presets",
        "/usr/share/projectM",
    ]
    for d in cands:
        try:
            if os.path.isdir(d) and any(f.endswith(".milk") for f in os.listdir(d)):
                return os.path.abspath(d)
            # one level down (the system pack groups presets in subfolders)
            if os.path.isdir(d):
                for sub in os.listdir(d):
                    p = os.path.join(d, sub)
                    if os.path.isdir(p) and any(f.endswith(".milk") for f in os.listdir(p)):
                        return os.path.abspath(d)
        except OSError:
            continue
    return None


class ProjectMEngine:
    """Owns a projectM instance + its playlist. Construct/use only with a current GL
    context (i.e. from inside QOpenGLWidget's GL callbacks)."""

    def __init__(self, width: int, height: int, preset_dir: str | None = None,
                 preset_seconds: float = 22.0, shuffle: bool = True):
        if not _load():
            raise RuntimeError("libprojectM-4 not available")
        self._pm = _core.projectm_create()
        if not self._pm:
            raise RuntimeError("projectm_create() failed")
        _core.projectm_set_window_size(self._pm, max(1, width), max(1, height))
        _core.projectm_set_fps(self._pm, 60)
        _core.projectm_set_mesh_size(self._pm, 48, 32)
        _core.projectm_set_aspect_correction(self._pm, True)
        _core.projectm_set_preset_duration(self._pm, float(preset_seconds))
        _core.projectm_set_soft_cut_duration(self._pm, 3.0)
        self._max_samples = int(_core.projectm_pcm_get_max_samples()) or 512

        self._pl = _pl.projectm_playlist_create(self._pm)
        self._count = 0
        if preset_dir:
            self._count = int(_pl.projectm_playlist_add_path(
                self._pl, preset_dir.encode(), True, False))
            _pl.projectm_playlist_set_shuffle(self._pl, shuffle)
            if self._count:
                _pl.projectm_playlist_set_position(self._pl, 0, True)

    @property
    def preset_count(self) -> int:
        return self._count

    # destroy() nulls _pm, and a paint can still land after it — Qt repaints on
    # expose and resize, not only on the visualizer's frame timer, so the window
    # between destroy() and the widget dropping its engine reference is live.
    # Passing that NULL to projectM segfaults, so every entry point checks.

    def resize(self, w: int, h: int):
        if not self._pm:
            return
        _core.projectm_set_window_size(self._pm, max(1, w), max(1, h))

    def add_pcm_mono(self, samples: np.ndarray):
        if not self._pm or samples is None or len(samples) == 0:
            return
        a = np.ascontiguousarray(samples, dtype=np.float32)
        n = min(len(a), self._max_samples)
        _core.projectm_pcm_add_float(
            self._pm, a.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            ctypes.c_uint(n), PROJECTM_MONO)

    def render(self):
        if not self._pm:
            return
        _core.projectm_opengl_render_frame(self._pm)

    def next_preset(self, hard: bool = True):
        if self._pl:
            _pl.projectm_playlist_play_next(self._pl, hard)

    def prev_preset(self, hard: bool = True):
        if self._pl:
            _pl.projectm_playlist_play_previous(self._pl, hard)

    def set_preset_duration(self, seconds: float):
        if not self._pm:
            return
        _core.projectm_set_preset_duration(self._pm, float(seconds))

    def set_shuffle(self, on: bool):
        if self._pl:
            _pl.projectm_playlist_set_shuffle(self._pl, bool(on))

    def set_locked(self, on: bool):
        if not self._pm:
            return
        _core.projectm_set_preset_locked(self._pm, bool(on))

    def is_locked(self) -> bool:
        return bool(_core.projectm_get_preset_locked(self._pm)) if self._pm else False

    def current_preset_path(self) -> str:
        """Full path of the active preset's .milk file, or ''."""
        if not self._pl or not self._count:
            return ""
        pos = int(_pl.projectm_playlist_get_position(self._pl))
        ptr = _pl.projectm_playlist_item(self._pl, pos)
        if not ptr:
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", "replace")
        finally:
            _pl.projectm_playlist_free_string(ptr)

    def current_preset_name(self) -> str:
        """Display name (filename without extension) of the active preset, or ''."""
        name = os.path.basename(self.current_preset_path())
        if name.lower().endswith(".milk"):
            name = name[:-5]
        return name

    def select_preset_path(self, path: str) -> bool:
        """Jump the playlist to the preset with this exact path (keeps next/prev
        coherent). Returns True if found. O(n) scan — runs once on restore."""
        if not self._pl or not path:
            return False
        n = int(_pl.projectm_playlist_size(self._pl))
        for i in range(n):
            ptr = _pl.projectm_playlist_item(self._pl, i)
            if not ptr:
                continue
            s = ctypes.string_at(ptr).decode("utf-8", "replace")
            _pl.projectm_playlist_free_string(ptr)
            if s == path:
                _pl.projectm_playlist_set_position(self._pl, i, True)
                return True
        return False

    def destroy(self):
        if self._pl:
            _pl.projectm_playlist_destroy(self._pl)
            self._pl = None
        if self._pm:
            _core.projectm_destroy(self._pm)
            self._pm = None
