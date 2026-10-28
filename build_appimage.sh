#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

APP=audiocrate
APPDIR=dist/AppDir

pyinstaller --clean audiocrate.spec

# ffmpeg_util looks for a copy next to the executable before the system one
FFMPEG_BIN=$(which ffmpeg 2>/dev/null || echo "")
if [ -z "$FFMPEG_BIN" ]; then
    echo "WARNING: ffmpeg not found on PATH, so it is not bundled"
else
    cp "$FFMPEG_BIN" dist/audiocrate/ffmpeg
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp -r dist/audiocrate/* "$APPDIR/usr/bin/"

# AppRun starts the executable under this name
mv "$APPDIR/usr/bin/audiocrate" "$APPDIR/usr/bin/audiocrate.bin"

cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/usr/bin:$LD_LIBRARY_PATH"
export QT_PLUGIN_PATH="$HERE/usr/bin/PySide6/Qt/plugins"
export QML_IMPORT_PATH="$HERE/usr/bin/PySide6/Qt/qml"
exec "$HERE/usr/bin/audiocrate.bin" "$@"
EOF
chmod +x "$APPDIR/AppRun"

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

if [ -f "icon.png" ]; then
    cp icon.png "$APPDIR/audiocrate.png"
    cp icon.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/audiocrate.png"
else
    # no icon.png in the tree: draw a plain one so appimagetool has something
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

if ! command -v appimagetool &>/dev/null; then
    echo "Downloading appimagetool..."
    curl -Lo /tmp/appimagetool "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x /tmp/appimagetool
    APPIMAGETOOL=/tmp/appimagetool
else
    APPIMAGETOOL=appimagetool
fi

ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "AudioCrate-x86_64.AppImage"

echo "Built AudioCrate-x86_64.AppImage"
