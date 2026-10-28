# Installing AudioCrate on Linux

AudioCrate is written in Python with PySide6, so the cleanest install uses your
system Python and Qt, with no 600 MB bundle. There are three options, the
smallest and simplest first.

## 1. User-local (no root), recommended for your own machine
Installs into `~/.local` and adds a launcher and a menu entry. About 16 MB.

```bash
# deps (once)
sudo pacman -S --needed pyside6 python-numpy python-scipy python-mutagen python-pillow ffmpeg
# optional: libgme (VGM/VGZ)  python-pypresence (Discord)

bash packaging/install.sh      # → "AudioCrate" appears in the app menu
bash packaging/uninstall.sh    # to remove
```

## 2. Arch package (pacman-managed, system-wide)
Installed and removed through pacman. Remove it later with `pacman -R audiocrate`.

```bash
makepkg -si -p packaging/PKGBUILD     # run from the project root
```

## 3. Self-contained bundle (no Python/Qt needed on the target)
For machines that do not have the dependencies. It is larger (about 143 MB as a
single file) and uses the system `ffmpeg` at runtime.

```bash
pyinstaller --noconfirm audiocrate-onefile.spec   # → dist/audiocrate (one file)
# or audiocrate.spec for a onedir build, which is easy to wrap in an AppImage.
```

## Source protection
The user-local install and the PKGBUILD both install bytecode only. The install
compiles with `python -OO`, which drops docstrings, and comments never enter
bytecode at all. It then deletes every `.py`, so the installed app has no
readable source or comments and the launcher runs `main.pyc`. The PyInstaller
bundles also ship `.pyc` inside the archive. Bytecode is not encryption and can
still be decompiled, but no source or comments are exposed. It is tied to the
Python version, so install again after a major Python upgrade.

## Notes
- Source layout: `core/ panels/ integrations/ ui/` with flat imports via
  `_bootstrap.py`. The launcher runs `python main.pyc`, no env setup needed.
- Config lives in `~/.config/audiocrate/`. EQ presets live in
  `~/Documents/EQPresets`.
- `.fur` playback needs `libfurnace_wrap.so` (shipped). `.vgm/.vgz` need `libgme`.
