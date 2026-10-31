#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/audiocrate"
BIN="$HOME/.local/bin"
DESK="$HOME/.local/share/applications"
ICON="$HOME/.local/share/icons/hicolor/256x256/apps"

PYBIN="${AUDIOCRATE_PYTHON:-$(command -v python3 || command -v python || true)}"
[ -z "$PYBIN" ] && { echo "Python 3 is required but was not found."; exit 1; }
PYNAME="$(basename "$PYBIN")"
[ -n "${AUDIOCRATE_PYTHON:-}" ] && PYNAME="$PYBIN"      # a virtual environment's python is started by its full path

# What the player needs, one import at a time so the list says which part is missing.
bad="$("$PYBIN" - <<'PY' 2>/dev/null || true
import importlib
for m in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtMultimedia", "numpy", "scipy", "mutagen", "PIL"):
    try:
        importlib.import_module(m)
    except Exception:
        print(m)
PY
)"
extras="$("$PYBIN" - <<'PY' 2>/dev/null || true
import importlib
for m, what in (("PySide6.QtMultimediaWidgets", "video picture (works without)"), ("PySide6.QtDBus", "media keys"),
                ("PySide6.QtOpenGL", "Milkdrop")):
    try:
        importlib.import_module(m)
    except Exception:
        print(m.split(".")[1] + ": " + what)
PY
)"
miss=""
[ -n "$bad" ] && miss="python modules ($(echo $bad | tr ' ' ','))"
command -v ffmpeg >/dev/null 2>&1 || miss="${miss:+$miss, }ffmpeg"
command -v xz >/dev/null 2>&1 || miss="${miss:+$miss, }xz (the package xz-utils)"

if [ -n "$miss" ]; then
    echo "Missing dependencies: $miss"
    if   command -v pacman  >/dev/null; then PM="sudo pacman -S --needed"; PKGS="pyside6 python-numpy python-scipy python-mutagen python-pillow ffmpeg"; OPT="libgme libopenmpt"
    elif command -v apt-get >/dev/null; then PM="sudo apt-get install -y"; PKGS="python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets python3-pyside6.qtmultimedia python3-pyside6.qtmultimediawidgets python3-pyside6.qtopengl python3-pyside6.qtdbus python3-numpy python3-scipy python3-mutagen python3-pil ffmpeg"; OPT="libgme0 libsdl2-2.0-0 libsndfile1 libasound2 openmpt123"
    elif command -v dnf     >/dev/null; then PM="sudo dnf install -y";     PKGS="python3-pyside6 python3-numpy python3-scipy python3-mutagen python3-pillow ffmpeg-free"; OPT="game-music-emu SDL2 libsndfile alsa-lib openmpt123"
    elif command -v zypper  >/dev/null; then PM="sudo zypper install -y";  PKGS="python3-PySide6 python3-numpy python3-scipy python3-mutagen python3-Pillow ffmpeg"; OPT="libgme0 openmpt123"
    else PM=""; fi

    if [ -n "$PM" ]; then
        echo "  $PM $PKGS"
        read -rp "Install them now? [Y/n] " a || a=Y
        [[ "${a:-Y}" =~ ^[Nn] ]] || $PM $PKGS || echo "Auto-install failed (some systems have no PySide6 package: Ubuntu 24.04 has none, use pip in a virtual environment and run this script with AUDIOCRATE_PYTHON=/path/to/venv/bin/python). Install what is missing, then run install.sh again."
    else
        echo "Install these with your package manager (or: pip install --user PySide6 numpy scipy mutagen pillow + ffmpeg)."
    fi
fi
[ -n "$extras" ] && { echo "Optional parts of PySide6 that are not installed, so these features are off:"; echo "$extras" | sed 's/^/  /'; }

echo "Installing AudioCrate -> $APP"
rm -rf "$APP"
mkdir -p "$APP" "$BIN" "$DESK" "$ICON"
tar -xJf "$HERE/app.tar.xz" -C "$APP"

cat > "$BIN/audiocrate" <<EOF
#!/usr/bin/env bash
exec $PYNAME "$APP/main.py" "\$@"
EOF
chmod +x "$BIN/audiocrate"

cp "$APP/icon.png" "$ICON/audiocrate.png"
sed "s|^Exec=audiocrate|Exec=$BIN/audiocrate|" "$HERE/audiocrate.desktop" > "$DESK/audiocrate.desktop"
update-desktop-database "$DESK" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo "Done. 'AudioCrate' is in your application menu."
if [ -n "${OPT:-}" ]; then
    echo "Optional extras for more formats: $OPT"
    echo "  (libgme = NSF/SPC/VGM, openmpt123 = tracker modules; SDL2, libsndfile and alsa are what Furnace .fur files use)"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Add ~/.local/bin to PATH to launch from a terminal.";; esac
