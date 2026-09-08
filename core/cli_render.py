"""Render chiptune & tracker-module files to WAV via external CLI players, so they
can be played through AudioCrate's normal DSP engine (full waveform / seek / DSP /
visualizers come for free). Repurposed from the AY-Machine bot's converter approach.

- zxtune123  → AY/YM + native ZX formats, and AY/X1 VGM that libgme can't emulate
               (fixes the slow/looping .ay and the silent VGM exports).
- openmpt123 → tracker modules (MOD/XM/IT/S3M/…).

Multi-subsong chip formats (NSF/SPC/GBS/SAP/KSS/HES/GYM) deliberately stay on libgme
(VgmPlayer) so the subsong navigator keeps working; this module doesn't claim them.
"""
from __future__ import annotations
import shutil
import hashlib
import subprocess
from pathlib import Path

_CACHE = Path.home() / ".cache" / "audiocrate" / "chiprender"

# AY/YM + native ZX engines + VGM (zxtune renders these far better than libgme)
_ZXTUNE_EXTS = {
    ".ay", ".ym", ".vtx", ".pt1", ".pt2", ".pt3", ".stc", ".st1", ".st3",
    ".psg", ".asc", ".ftc", ".gtr", ".psc", ".sqt", ".chi", ".tfc", ".tfd",
    ".tfe", ".sqd", ".str", ".ayc", ".vgm", ".vgz",
}
# Tracker modules via libopenmpt's CLI
_OPENMPT_EXTS = {
    ".mod", ".xm", ".it", ".s3m", ".mptm", ".mtm", ".669", ".far", ".ult",
    ".okt", ".med", ".ptm", ".dbm", ".digi", ".amf", ".dsm", ".mt2", ".stm",
    ".imf", ".psm", ".ams", ".j2b", ".gdm", ".umx", ".oct",
}
EXTS = _ZXTUNE_EXTS | _OPENMPT_EXTS


def tool_for(ext: str) -> str | None:
    ext = ext.lower()
    if ext in _ZXTUNE_EXTS and shutil.which("zxtune123"):
        return "zxtune"
    if ext in _OPENMPT_EXTS and shutil.which("openmpt123"):
        return "openmpt"
    return None


def supported(ext: str) -> bool:
    return tool_for(ext) is not None


def _cache_path(src: Path) -> Path:
    try:
        st = src.stat()
        key = f"{src}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        key = str(src)
    return _CACHE / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".wav")


def render(src, timeout: int = 180) -> str | None:
    """Render ``src`` to a cached WAV and return its path (or None on failure).
    Blocking — call off the UI thread. Re-uses the cache on repeat plays."""
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
            cmd = ["zxtune123", "--wav", f"filename={produced}", str(src)]
        else:
            # openmpt123 --render writes "<input>.wav" next to the input; stage a
            # link in the cache dir so the output lands there (and the source folder
            # stays clean), then move it to the canonical cache name.
            link = _CACHE / (out.stem + src.suffix.lower())
            try:
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(src.resolve())
            except OSError:
                shutil.copy2(src, link)
            produced = Path(str(link) + ".wav")
            cmd = ["openmpt123", "--render", "--output-type", "wav", str(link)]

        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
        if produced.exists() and produced.stat().st_size > 1024:
            produced.replace(out)
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
