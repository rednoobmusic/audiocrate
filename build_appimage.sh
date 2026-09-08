#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

APP=audiocrate
APPDIR=dist/AppDir

# ── 1. PyInstaller ──────────────────────────────────────────────────────────
echo ">>> Running PyInstaller..."
pyinstaller --clean audiocrate.spec

# ── 1b. Bundle ffmpeg ────────────────────────────────────────────────────────
echo ">>> Bundling ffmpeg..."
FFMPEG_BIN=$(which ffmpeg 2>/dev/null || echo "")
if [ -z "$FFMPEG_BIN" ]; then
    echo "WARNING: ffmpeg not found on PATH — waveform and normalize will not work"
else
    cp "$FFMPEG_BIN" dist/audiocrate/ffmpeg
fi

# ── 2. Build AppDir structure ───────────────────────────────────────────────
echo ">>> Building AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output
cp -r dist/audiocrate/* "$APPDIR/usr/bin/"

# Move the actual executable up one level so AppRun can find it
mv "$APPDIR/usr/bin/audiocrate" "$APPDIR/usr/bin/audiocrate.bin"

# ── 3. AppRun ───────────────────────────────────────────────────────────────
cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/usr/bin:$LD_LIBRARY_PATH"
export QT_PLUGIN_PATH="$HERE/usr/bin/PySide6/Qt/plugins"
export QML_IMPORT_PATH="$HERE/usr/bin/PySide6/Qt/qml"
exec "$HERE/usr/bin/audiocrate.bin" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# ── 4. .desktop file ────────────────────────────────────────────────────────
cat > "$APPDIR/$APP.desktop" << EOF
[Desktop Entry]
Name=AudioCrate
Exec=audiocrate
Icon=audiocrate
Type=Application
Categories=AudioVideo;Audio;Player;
Comment=Music player
EOF
cp "$APPDIR/$APP.desktop" "$APPDIR/usr/share/applications/"

# ── 5. Icon ─────────────────────────────────────────────────────────────────
if [ -f "icon.png" ]; then
    cp icon.png "$APPDIR/audiocrate.png"
    cp icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/audiocrate.png"
else
    # Generate a placeholder icon if none exists
    python3 - << 'PYEOF'
from PIL import Image, ImageDraw
img = Image.new("RGBA", (256, 256), (14, 12, 20, 255))
d = ImageDraw.Draw(img)
d.ellipse([20, 20, 236, 236], fill=(128, 64, 200, 255))
d.polygon([(90,70),(90,186),(196,128)], fill=(14,12,20,255))
img.save("icon.png")
print("Generated placeholder icon.png")
PYEOF
    cp icon.png "$APPDIR/audiocrate.png"
    cp icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/audiocrate.png"
fi

# ── 6. Download appimagetool if not on PATH ──────────────────────────────────
if ! command -v appimagetool &>/dev/null; then
    echo ">>> Downloading appimagetool..."
    curl -Lo /tmp/appimagetool "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x /tmp/appimagetool
    APPIMAGETOOL=/tmp/appimagetool
else
    APPIMAGETOOL=appimagetool
fi

# ── 7. Package AppImage ──────────────────────────────────────────────────────
echo ">>> Packaging AppImage..."
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "AudioCrate-x86_64.AppImage"

echo ""
echo "✓ Done: AudioCrate-x86_64.AppImage"
