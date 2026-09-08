#!/usr/bin/env bash
# Build a distributable Linux release into dist/release/:
#   packaging/make_release.sh [version]
#
# The app is compiled to optimised bytecode (no source/comments), compressed to
# app.tar.xz, and bundled with the installer into a zip. The installer extracts
# the compressed app on the target — so the download stays small.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
# Version: explicit arg wins, else read the single source of truth core/_version.py
VER="${1:-$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "$SRC/core/_version.py" 2>/dev/null || echo 0.0.0)}"
STAGE="$(mktemp -d)"
PKG="$STAGE/audiocrate"            # becomes the top-level dir inside the zip
APPBUILD="$STAGE/appbuild"         # scratch dir for bytecode compilation
mkdir -p "$PKG" "$APPBUILD"

echo "Staging app…"
cp -r "$SRC"/main.py "$SRC"/_bootstrap.py \
      "$SRC"/core "$SRC"/integrations "$SRC"/ui \
      "$SRC"/JetBrainsMono "$SRC"/icon.png "$APPBUILD/"
# panels/ holds local working trees alongside the modules (e.g. panels/myfurnace,
# 5+ GB of private forks) — stage ONLY the panel modules themselves.
mkdir -p "$APPBUILD/panels"
cp "$SRC"/panels/*.py "$APPBUILD/panels/"
for _so in libfurnace_wrap.so libsid_wrap.so; do
    [ -f "$SRC/$_so" ] && cp "$SRC/$_so" "$APPBUILD/"
done

# Drop bytecode carried over from the working tree: a stale .pyc for a module
# that has since been renamed or deleted would otherwise ship (and still be
# importable) even though its source is long gone.
find "$APPBUILD" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# Hard guard: never let anything private into a release, no matter what the
# staging rules above become later.
if find "$APPBUILD" -iname '*PRIVATE*' -o -iname '*DoNOTPush*' | grep -q .; then
    echo "ERROR: PRIVATE/DoNOTPush files reached the release stage — aborting." >&2
    exit 1
fi

# GPL-2.0-or-later: AudioCrate links Furnace's engine, so the distributed work
# is GPL and MUST come with its complete source. Ship the .py sources (plus the
# C wrapper source and the licence) rather than bytecode-only.
echo "Staging source + licence (GPL compliance)…"
cp "$SRC/LICENSE" "$APPBUILD/" 2>/dev/null || {
    echo "ERROR: LICENSE missing — refusing to build a GPL release without it." >&2
    exit 1; }
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$APPBUILD/"
# Wrapper sources ship too: both .so files are linked against GPL libraries
# (Furnace, libsidplayfp), so their corresponding source must accompany them.
for _src in _furnace_wrapper_src _sid_wrapper_src; do
    if [ -d "$SRC/$_src" ]; then
        mkdir -p "$APPBUILD/$_src"
        cp -r "$SRC/$_src/." "$APPBUILD/$_src/"
    fi
done
# Precompile for fast first start, but KEEP the .py files next to the bytecode.
python -m compileall -q "$APPBUILD" >/dev/null 2>&1 || true

echo "Compressing app → app.tar.xz…"
raw_kb=$(du -sk "$APPBUILD" | cut -f1)
tar -cJf "$PKG/app.tar.xz" -C "$APPBUILD" .
xz_kb=$(du -sk "$PKG/app.tar.xz" | cut -f1)

# Installer + extras
[ -f "$SRC/CHANGELOG.md" ] && cp "$SRC/CHANGELOG.md" "$PKG/"
cp "$SRC/packaging/release_install.sh" "$PKG/install.sh"; chmod +x "$PKG/install.sh"
cp "$SRC/packaging/uninstall.sh"       "$PKG/uninstall.sh"; chmod +x "$PKG/uninstall.sh"
cp "$SRC/packaging/audiocrate.desktop" "$PKG/"
cat > "$PKG/README.txt" <<EOF
AudioCrate $VER — Linux

  bash install.sh      install to ~/.local (adds an app-menu entry)
  bash uninstall.sh    remove it

The installer checks for and offers to install the dependencies:
PySide6, numpy, scipy, mutagen, pillow, ffmpeg.
Optional:
  libsidplayfp  C64 SID playback (.sid/.psid/.rsid) with subsongs
  libgme        NSF/SPC/GBS/VGM/AY/GYM/HES/KSS/SAP playback
  openmpt123    tracker modules (MOD/XM/IT/S3M and ~40 more)
  zxtune123     AY/YM + native ZX Spectrum formats
  libprojectm   Milkdrop visualiser
  pypresence    Discord Rich Presence

SID song lengths: drop an HVSC Songlengths.md5 at
  ~/.config/audiocrate/Songlengths.md5
for exact per-subsong lengths. Without it, lengths are detected from the audio.
EOF

echo "Zipping…"
OUT="$SRC/dist/release"
mkdir -p "$OUT"
ZIP="$OUT/audiocrate-$VER-linux.zip"
rm -f "$ZIP"
# Zip via Python (no external 'zip' dependency); preserve unix exec bits on scripts.
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
