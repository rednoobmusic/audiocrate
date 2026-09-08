#!/usr/bin/env bash
set -e

APP_NAME="PySide6 Player" # feoihfoirefiodflkjghldfjh v2
INSTALL_DIR="$HOME/.local/share/pyside6player"
DESKTOP_FILE="$HOME/.local/share/applications/pyside6player.desktop"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info()  { echo "  -> $*"; }
ok()    { echo "  [ok] $*"; }
die()   { echo "  [error] $*" >&2; exit 1; }

echo ""
echo "  $APP_NAME - setup"
echo "  ────────────────────────────────────"

PYTHON=$(command -v python3 || command -v python || true)
[ -z "$PYTHON" ] && die "Python 3 not found. Install it first."
PY_VER=$("$PYTHON" -c "import sys; print(sys.version_info[:2])")
info "Python: $("$PYTHON" --version)"

if ! command -v ffmpeg &>/dev/null; then
    echo ""
    echo "  ffmpeg not found. Attempting install..."
    if   command -v pacman  &>/dev/null; then sudo pacman -S --noconfirm ffmpeg
    elif command -v apt-get &>/dev/null; then sudo apt-get install -y ffmpeg
    elif command -v dnf     &>/dev/null; then sudo dnf install -y ffmpeg
    elif command -v brew    &>/dev/null; then brew install ffmpeg
    else die "Cannot auto-install ffmpeg. Install it manually then rerun."
    fi
fi
ok "ffmpeg: $(ffmpeg -version 2>&1 | head -1)"

info "Copying files to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"

info "Creating virtual environment"
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
info "Installing Python dependencies"
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Dependencies installed"

LAUNCHER="$INSTALL_DIR/launch.sh"
cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
cd "$INSTALL_DIR"
exec "$VENV_DIR/bin/python" main.py "\$@"
LAUNCH
chmod +x "$LAUNCHER"

mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=$APP_NAME
Comment=Music and video player
Exec=$LAUNCHER %U
Icon=multimedia-player
Terminal=false
Categories=AudioVideo;Audio;Player;
MimeType=audio/mpeg;audio/flac;audio/ogg;audio/x-wav;video/mp4;video/x-matroska;
StartupNotify=false
DESKTOP

command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

ok "Desktop shortcut created"

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
ln -sf "$LAUNCHER" "$BIN_DIR/pyside6player"
ok "Command 'pyside6player' available (ensure $BIN_DIR is in your PATH)"

echo ""
echo "  Done. Launch from your application menu or run: pyside6player"
echo ""
