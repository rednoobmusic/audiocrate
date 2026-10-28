#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/audiocrate"
BIN="$HOME/.local/bin"
DESK="$HOME/.local/share/applications"
ICON="$HOME/.local/share/icons/hicolor/256x256/apps"

PYBIN="$(command -v python3 || command -v python || true)"
[ -z "$PYBIN" ] && { echo "Python 3 is required but was not found."; exit 1; }
PYNAME="$(basename "$PYBIN")"

# Dependencies: PySide6, numpy, scipy, mutagen, pillow, ffmpeg.
miss=""
"$PYBIN" - <<'PY' 2>/dev/null || miss="python modules"
import importlib.util as u, sys
sys.exit(0 if all(u.find_spec(m) for m in ("PySide6","numpy","scipy","mutagen","PIL")) else 1)
PY
command -v ffmpeg >/dev/null 2>&1 || miss="${miss:+$miss, }ffmpeg"

if [ -n "$miss" ]; then
    echo "Missing dependencies: $miss"
    if   command -v pacman  >/dev/null; then PM="sudo pacman -S --needed"; PKGS="pyside6 python-numpy python-scipy python-mutagen python-pillow ffmpeg"; OPT="libsidplayfp libgme libopenmpt"
    elif command -v apt-get >/dev/null; then PM="sudo apt-get install -y"; PKGS="python3-pyside6 python3-numpy python3-scipy python3-mutagen python3-pil ffmpeg"; OPT="libsidplayfp6 libgme0 openmpt123"
    elif command -v dnf     >/dev/null; then PM="sudo dnf install -y";     PKGS="python3-pyside6 python3-numpy python3-scipy python3-mutagen python3-pillow ffmpeg"; OPT="libsidplayfp libgme openmpt123"
    elif command -v zypper  >/dev/null; then PM="sudo zypper install -y";  PKGS="python3-PySide6 python3-numpy python3-scipy python3-mutagen python3-Pillow ffmpeg"; OPT="libsidplayfp libgme openmpt123"
    else PM=""; fi

    if [ -n "$PM" ]; then
        echo "  $PM $PKGS"
        read -rp "Install them now? [Y/n] " a || a=Y
        [[ "${a:-Y}" =~ ^[Nn] ]] || $PM $PKGS || echo "Auto-install failed. Install the packages above, then run install.sh again."
    else
        echo "Install these with your package manager (or: pip install --user PySide6 numpy scipy mutagen pillow + ffmpeg)."
    fi
fi

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
    echo "  (libsidplayfp = C64 SID, libgme = NSF/SPC/VGM, openmpt123 = tracker modules)"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Add ~/.local/bin to PATH to launch from a terminal.";; esac
