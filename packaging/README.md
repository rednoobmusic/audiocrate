# Installing AudioCrate on Linux

AudioCrate is pure Python + PySide6, so the cleanest install uses your system
Python/Qt (no 600 MB bundle). Three options, smallest/simplest first.

## 1. User-local (no root) — recommended for your own machine
Installs into `~/.local`, adds a launcher + menu entry. ~16 MB.

```bash
# deps (once)
sudo pacman -S --needed pyside6 python-numpy python-scipy python-mutagen python-pillow ffmpeg
# optional: libgme (VGM/VGZ)  python-pypresence (Discord)

bash packaging/install.sh      # → "AudioCrate" appears in the app menu
bash packaging/uninstall.sh    # to remove
```

## 2. Arch package (pacman-managed, system-wide)
Proper distro integration; remove later with `pacman -R audiocrate`.

```bash
makepkg -si -p packaging/PKGBUILD     # run from the project root
```

## 3. Self-contained bundle (no Python/Qt needed on the target)
For machines that DON'T have the deps. Larger (~143 MB single file) and uses
the system `ffmpeg` at runtime.

```bash
pyinstaller --noconfirm audiocrate-onefile.spec   # → dist/audiocrate (one file)
# or audiocrate.spec for a onedir build, which is easy to wrap in an AppImage.
```

## Source protection
Both the user-local install and the PKGBUILD ship **optimised bytecode only**:
the install compiles with `python -OO` (drops docstrings; comments never enter
bytecode) and deletes every `.py`, so the installed app has no readable source
or comments — the launcher runs `main.pyc`. The PyInstaller bundles likewise
ship `.pyc` inside the archive. (Bytecode is not encryption — it can still be
decompiled — but no source/comments are exposed. It's tied to the Python
version, so re-install after a major Python upgrade.)

## Notes
- Source layout: `core/ panels/ integrations/ ui/` with flat imports via
  `_bootstrap.py` — the launcher runs `python main.pyc`, no env setup needed.
- Config lives in `~/.config/audiocrate/`; EQ presets in `~/Documents/EQPresets`.
- `.fur` playback needs `libfurnace_wrap.so` (shipped); `.vgm/.vgz` need `libgme`.
