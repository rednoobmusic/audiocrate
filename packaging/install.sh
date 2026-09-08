#!/usr/bin/env bash
# User-local install (no root): copies AudioCrate into ~/.local and registers a
# launcher + menu entry. Uses the system Python/Qt — install the deps first:
#   sudo pacman -S --needed pyside6 python-numpy python-scipy python-mutagen \
#                           python-pillow ffmpeg            # (optional: libgme python-pypresence)
# Uninstall with packaging/uninstall.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"          # project root
APP="$HOME/.local/share/audiocrate"
BIN="$HOME/.local/bin"
DESK="$HOME/.local/share/applications"
ICON="$HOME/.local/share/icons/hicolor/256x256/apps"

echo "Installing AudioCrate from $SRC → $APP"
rm -rf "$APP"
mkdir -p "$APP" "$BIN" "$DESK" "$ICON"

# Runtime source (the folders that _bootstrap puts on sys.path + entry point).
# panels/ is staged as *.py only — it also holds local working trees
# (panels/myfurnace) that must never be installed or shipped.
cp -r "$SRC/main.py" "$SRC/_bootstrap.py" \
      "$SRC/core" "$SRC/integrations" "$SRC/ui" \
      "$SRC/JetBrainsMono" "$SRC/icon.png" "$APP/"
mkdir -p "$APP/panels"
cp "$SRC"/panels/*.py "$APP/panels/"
for _so in libfurnace_wrap.so libsid_wrap.so; do
    [ -f "$SRC/$_so" ] && cp "$SRC/$_so" "$APP/"
done

# Ship optimised bytecode only: -OO drops docstrings, compiling drops comments,
# then the .py sources are removed so no readable source/comments are installed.
# (Bytecode is tied to this Python version — re-run install.sh after a major
#  Python upgrade.)
python -OO -m compileall -b -q "$APP" >/dev/null 2>&1 || \
    python -m compileall -b -q "$APP" >/dev/null 2>&1 || true
find "$APP" -name '*.py' -delete
find "$APP" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

# Launcher (runs the compiled entry point)
cat > "$BIN/audiocrate" <<EOF
#!/usr/bin/env bash
exec python "$APP/main.pyc" "\$@"
EOF
chmod +x "$BIN/audiocrate"

# Icon + menu entry (absolute Exec so it works even if ~/.local/bin isn't on PATH)
cp "$SRC/icon.png" "$ICON/audiocrate.png"
sed "s|^Exec=audiocrate|Exec=$BIN/audiocrate|" \
    "$SRC/packaging/audiocrate.desktop" > "$DESK/audiocrate.desktop"
update-desktop-database "$DESK" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo "Done. 'AudioCrate' is now in your app menu."
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "NOTE: add ~/.local/bin to PATH so the launcher resolves, e.g.:"
     echo '      echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc' ;;
esac
