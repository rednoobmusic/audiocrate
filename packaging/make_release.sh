#!/usr/bin/env bash
# Build a Linux release into dist/release/:
#   packaging/make_release.sh [version]
#
# The app is staged with its .py files, precompiled for a fast first start,
# compressed to app.tar.xz and zipped together with the installer. The installer
# unpacks app.tar.xz on the target, which keeps the download small.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
# the version is the first argument, else core/_version.py
VER="${1:-$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "$SRC/core/_version.py" 2>/dev/null || echo 0.0.0)}"
STAGE="$(mktemp -d)"
PKG="$STAGE/audiocrate"            # becomes the top-level dir inside the zip
APPBUILD="$STAGE/appbuild"         # the app as it will be packed
mkdir -p "$PKG" "$APPBUILD"

echo "Staging app..."
cp -r "$SRC"/main.py "$SRC"/_bootstrap.py \
      "$SRC"/core "$SRC"/integrations "$SRC"/ui \
      "$SRC"/JetBrainsMono "$SRC"/icon.png "$APPBUILD/"
# panels/ holds local working trees next to the modules (panels/myfurnace is
# 5+ GB of private forks), so only the .py files are staged
mkdir -p "$APPBUILD/panels"
cp "$SRC"/panels/*.py "$APPBUILD/panels/"
for _so in libfurnace_wrap.so libsid_wrap.so; do
    [ -f "$SRC/$_so" ] && cp "$SRC/$_so" "$APPBUILD/"
done
# projectM 4 for systems that only have projectM 3, built by packaging/build_native.sh
[ -d "$SRC/linuxlibs" ] && cp -r "$SRC/linuxlibs" "$APPBUILD/"

# Drop bytecode carried over from the working tree: a stale .pyc for a module
# that has since been renamed or deleted would otherwise ship (and still be
# importable) even though its source is long gone.
find "$APPBUILD" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# last guard against private files reaching a release
if find "$APPBUILD" -iname '*PRIVATE*' -o -iname '*DoNOTPush*' | grep -q .; then
    echo "ERROR: PRIVATE/DoNOTPush files reached the release stage, aborting." >&2
    exit 1
fi

# AudioCrate links Furnace's engine, so the released work is GPL-2.0-or-later and
# has to come with its complete source: the .py files, the C wrapper source and
# the license, not bytecode alone
echo "Staging source and license..."
cp "$SRC/LICENSE" "$APPBUILD/" 2>/dev/null || {
    echo "ERROR: LICENSE missing, refusing to build a GPL release without it." >&2
    exit 1; }
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$APPBUILD/"
# both .so files are linked against GPL libraries (Furnace, libsidplayfp), so
# their wrapper sources go in too
for _src in _furnace_wrapper_src _sid_wrapper_src; do
    if [ -d "$SRC/$_src" ]; then
        mkdir -p "$APPBUILD/$_src"
        cp -r "$SRC/$_src/." "$APPBUILD/$_src/"
    fi
done
# precompile for a fast first start; the .py files stay next to the bytecode
python -m compileall -q "$APPBUILD" >/dev/null 2>&1 || true

echo "Compressing app to app.tar.xz..."
raw_kb=$(du -sk "$APPBUILD" | cut -f1)
tar -cJf "$PKG/app.tar.xz" -C "$APPBUILD" .
xz_kb=$(du -sk "$PKG/app.tar.xz" | cut -f1)

[ -f "$SRC/CHANGELOG.md" ] && cp "$SRC/CHANGELOG.md" "$PKG/"
cp "$SRC/packaging/release_install.sh" "$PKG/install.sh"; chmod +x "$PKG/install.sh"
cp "$SRC/packaging/uninstall.sh"       "$PKG/uninstall.sh"; chmod +x "$PKG/uninstall.sh"
cp "$SRC/packaging/audiocrate.desktop" "$PKG/"
cat > "$PKG/README.txt" <<EOF
AudioCrate $VER for Linux

  bash install.sh      install to ~/.local (adds an app-menu entry)
  bash uninstall.sh    remove it

The installer checks for and offers to install the dependencies:
PySide6, numpy, scipy, mutagen, pillow, ffmpeg.
Optional:
  libgme        NSF/SPC/GBS/VGM/AY/GYM/HES/KSS/SAP playback
  openmpt123    tracker modules (MOD/XM/IT/S3M and ~40 more)
  zxtune123     AY/YM + native ZX Spectrum formats
  pypresence    Discord Rich Presence
C64 SID playback, Furnace (.fur) files and the Milkdrop visualizer come with the release.
Furnace files also use SDL2, libsndfile and ALSA, which most desktops have.

SID song lengths: drop an HVSC Songlengths.md5 at
  ~/.config/audiocrate/Songlengths.md5
for exact per-subsong lengths. Without it, lengths are detected from the audio.
EOF

echo "Zipping..."
OUT="$SRC/dist/release"
mkdir -p "$OUT"
ZIP="$OUT/audiocrate-$VER-linux.zip"
rm -f "$ZIP"
# zip through Python so there is no dependency on a zip binary; the unix exec bits of the scripts are kept
python - "$STAGE" "$ZIP" <<'PY'
import sys, os, zipfile
stage, out = sys.argv[1], sys.argv[2]
base = os.path.join(stage, "audiocrate")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(base):
        for f in sorted(files):
            fp = os.path.join(root, f)
            zi = zipfile.ZipInfo(os.path.relpath(fp, stage))
            zi.external_attr = (os.stat(fp).st_mode & 0xFFFF) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(fp, "rb") as fh:
                z.writestr(zi, fh.read())
PY
zip_kb=$(du -sk "$ZIP" | cut -f1)
rm -rf "$STAGE"

echo ""
echo "Release: $ZIP"
printf "  app (uncompressed): %s MB\n" "$(awk "BEGIN{printf \"%.1f\", $raw_kb/1024}")"
printf "  app.tar.xz:         %s MB\n" "$(awk "BEGIN{printf \"%.1f\", $xz_kb/1024}")"
printf "  final zip:          %s MB\n" "$(awk "BEGIN{printf \"%.1f\", $zip_kb/1024}")"
