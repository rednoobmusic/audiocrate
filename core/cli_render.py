"""Render chiptune and tracker-module files to WAV with external command line players, so they
play through the normal DSP engine like any other file.

- zxtune123: AY/YM and the native ZX formats, plus AY/X1 VGM that libgme can't emulate.
- openmpt123: tracker modules (MOD, XM, IT, S3M and so on).

Multi-subsong chip formats (NSF, SPC, GBS, SAP, KSS, HES, GYM) stay on libgme (VgmPlayer) so the
subsong navigator keeps working; this module doesn't claim them.
"""
from __future__ import annotations
import shutil
import hashlib
import subprocess
import sys
from pathlib import Path

import plat_fs
import plat_paths
import plat_proc

_CACHE = plat_paths.cache_dir() / "chiprender"

# AY/YM, native ZX formats and VGM (zxtune renders these better than libgme)
_ZXTUNE_EXTS = {
    ".ay", ".ym", ".vtx", ".pt1", ".pt2", ".pt3", ".stc", ".st1", ".st3",
    ".psg", ".asc", ".ftc", ".gtr", ".psc", ".sqt", ".chi", ".tfc", ".tfd",
    ".tfe", ".sqd", ".str", ".ayc", ".vgm", ".vgz",
}
# tracker modules, via libopenmpt's command line player
_OPENMPT_EXTS = {
    ".mod", ".xm", ".it", ".s3m", ".mptm", ".mtm", ".669", ".far", ".ult",
    ".okt", ".med", ".ptm", ".dbm", ".digi", ".amf", ".dsm", ".mt2", ".stm",
    ".imf", ".psm", ".ams", ".j2b", ".gdm", ".umx", ".oct",
}
EXTS = _ZXTUNE_EXTS | _OPENMPT_EXTS


def tool_for(ext: str) -> str | None:
    ext = ext.lower()
    if ext in _ZXTUNE_EXTS and plat_proc.which("zxtune123"):
        return "zxtune"
    if ext in _OPENMPT_EXTS and plat_proc.which("openmpt123"):
        return "openmpt"
    return None


def supported(ext: str) -> bool:
    return tool_for(ext) is not None


def _exe(name: str) -> str:
    """The program to start. Windows gets the full path, because the tools folder is not on PATH; elsewhere the name does."""
    return (plat_proc.which(name) or name) if sys.platform == "win32" else name


def missing_hint() -> str:
    """What to check when a module or a ZX file that has a renderer still comes out empty."""
    if sys.platform == "win32":
        return "are openmpt123.exe and zxtune123.exe in the tools folder?"
    return "is openmpt123/zxtune123 installed?"


def _cache_path(src: Path) -> Path:
    try:
        st = src.stat()
        key = f"{src}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = str(src)
    return _CACHE / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".wav")


def render(src, timeout: int = 180) -> str | None:
    """Render ``src`` to a cached WAV and return its path, or None on failure. Blocks, so call it off the UI thread."""
    src = Path(src)
    tool = tool_for(src.suffix)
    if tool is None:
        return None
    out = _cache_path(src)
    if out.exists() and out.stat().st_size > 1024:
        return str(out)
    _CACHE.mkdir(parents=True, exist_ok=True)
    produced: Path | None = None
    link: Path | None = None
    try:
        if tool == "zxtune":
            produced = out.with_name(out.stem + ".part.wav")
            cmd = [_exe("zxtune123"), "--wav", f"filename={produced}", str(src)]
        else:
            # openmpt123 --render writes "<input>.wav" next to the input; stage a
            # link in the cache dir so the output lands there and the source folder
            # stays clean, then move it to the cache name.
            link = _CACHE / (out.stem + src.suffix.lower())
            try:
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(src.resolve())
            except OSError:
                shutil.copy2(src, link)
            produced = Path(str(link) + ".wav")
            cmd = [_exe("openmpt123"), "--render", "--output-type", "wav", str(link)]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, **plat_proc.popen_kwargs(group=False))
        if produced.exists() and produced.stat().st_size > 1024:
            plat_fs.replace(produced, out)
            from native_lock import prune_cache
            prune_cache(_CACHE)
            return str(out)
        return None
    except Exception:
        return None
    finally:
        try:
            if link is not None and (link.exists() or link.is_symlink()):
                link.unlink()
        except OSError:
            pass
        try:
            if produced is not None and produced.exists():
                produced.unlink()
        except OSError:
            pass
