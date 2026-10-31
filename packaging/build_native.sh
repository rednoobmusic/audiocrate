#!/usr/bin/env bash
# Builds the native libraries of the Linux release (libfurnace_wrap.so, libsid_wrap.so and projectM 4) so that they load on
# Debian, Ubuntu and Fedora and not only on the system they were built on. A library built on a current rolling distribution
# asks for a newer glibc than those have, so this runs inside a Debian 12 container, whose glibc is older than theirs:
#
#   docker run --rm -v "$PWD":/src:ro -v "$PWD/dist/native":/out debian:12 bash /src/packaging/build_native.sh
#
# The result is in dist/native: libfurnace_wrap.so and libsid_wrap.so (the libstdc++ and libgcc they need are inside them,
# and libsidplayfp and the Furnace engine are linked in, so only SDL2, libsndfile, zlib and ALSA are taken from the system),
# and libs/ with libprojectM-4 and its playlist library, which the player uses when the system has no projectM 4.
set -euo pipefail

FURNACE_TAG=dev250
SIDPLAYFP_TAG=v3.1.1
RESIDFP_TAG=v1.2.3
PROJECTM_TAG=v4.1.6
JOBS="${JOBS:-4}"
SRC=/src
OUT=/out
W=/tmp/native
mkdir -p "$W" "$OUT/libs"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential cmake git pkg-config ninja-build autoconf automake libtool \
    libsdl2-dev libsndfile1-dev libasound2-dev zlib1g-dev libgl-dev libglu1-mesa-dev xa65 >/dev/null

want() { [ -z "${ONLY:-}" ] || [ "$ONLY" = "$1" ]; }

# Furnace engine, as described in the README
want furnace && {
cd "$W"
[ -d furnace ] || git clone -q --depth 1 --branch "$FURNACE_TAG" --recurse-submodules --shallow-submodules https://github.com/tildearrow/furnace furnace
cd furnace
git apply --check "$SRC/_furnace_wrapper_src/furnace-dev250-build-fixes.patch" 2>/dev/null && git apply "$SRC/_furnace_wrapper_src/furnace-dev250-build-fixes.patch"
cmake -B build-lib -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF -DWITH_PORTAUDIO=OFF -DWITH_JACK=OFF \
      -DCMAKE_CXX_FLAGS="-fPIC" -DCMAKE_C_FLAGS="-fPIC" >/dev/null
cmake --build build-lib -j"$JOBS" || true          # the cli target may fail to link; the engine objects are what is needed
(cd build-lib && find CMakeFiles/furnace.dir -name '*.o' ! -name 'main.cpp.o' ! -path '*src/cli/*' | xargs ar rcs libfurnace_lib.a)
F=$PWD
B=$F/build-lib
g++ -shared -fPIC -std=c++23 -O2 -static-libstdc++ -static-libgcc "$SRC/_furnace_wrapper_src/desktop/furnace_wrap.cpp" \
    -I"$F/src" -I"$F/extern/blip_buf" -I"$F/extern/fmt/include" -I"$F/extern/fftw/api" -I"$F/extern/fftw" \
    -I"$F/extern/vgsound_emu-modified" -I"$F/extern/zlib" -I"$F/extern/IconFontCppHeaders" -I"$F/src/icon" \
    -I"$B/extern/zlib" -I"$B/extern/fftw" -DHAVE_SNDFILE -DHAVE_DIRENT_TYPE \
    -Wl,--whole-archive "$B/libfurnace_lib.a" -Wl,--no-whole-archive \
    "$B/extern/fmt/libfmt.a" "$B/extern/fftw/libfftw3.a" "$B/extern/rtmidi/librtmidi.a" \
    $(pkg-config --libs sndfile zlib sdl2) -lasound -lpthread -ldl -lm \
    -o "$OUT/libfurnace_wrap.so"
}

# libsidplayfp and its libresidfp, linked in statically: Debian, Ubuntu and Fedora ship other sonames than Arch does
want sid && {
cd "$W"
[ -d residfp ] || git clone -q --depth 1 --branch "$RESIDFP_TAG" https://github.com/libsidplayfp/libresidfp residfp
cd residfp
autoreconf -fi
./configure --disable-shared --enable-static --with-pic --prefix="$W/sid-root"
make -j"$JOBS"
make install
cd "$W"
[ -d sidplayfp ] || git clone -q --depth 1 --branch "$SIDPLAYFP_TAG" --recurse-submodules --shallow-submodules https://github.com/libsidplayfp/libsidplayfp sidplayfp
cd sidplayfp
autoreconf -fi
PKG_CONFIG_PATH="$W/sid-root/lib/pkgconfig" ./configure --disable-shared --enable-static --with-pic --without-exsid --without-usbsid \
    --prefix="$W/sid-root"
make -j"$JOBS"
make install
g++ -shared -fPIC -std=c++17 -O2 -static-libstdc++ -static-libgcc "$SRC/_sid_wrapper_src/sid_wrap.cpp" \
    -I"$W/sid-root/include" "$W/sid-root/lib/libsidplayfp.a" "$W/sid-root/lib/libresidfp.a" -o "$OUT/libsid_wrap.so"
}

# projectM 4 for systems that only have 3 (Debian and Ubuntu do)
want projectm && {
cd "$W"
[ -d projectm ] || git clone -q --depth 1 --branch "$PROJECTM_TAG" --recurse-submodules --shallow-submodules https://github.com/projectM-visualizer/projectm projectm
cmake -S projectm -B projectm/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DENABLE_PLAYLIST=ON -DENABLE_SDL_UI=OFF \
      -DBUILD_TESTING=OFF -DCMAKE_INSTALL_PREFIX="$W/pm-root" -DCMAKE_INSTALL_RPATH='$ORIGIN' \
      -DCMAKE_SHARED_LINKER_FLAGS="-static-libstdc++ -static-libgcc" >/dev/null
cmake --build projectm/build -j"$JOBS" >/dev/null
cmake --install projectm/build >/dev/null
cp -a "$W"/pm-root/lib*/libprojectM-4.so* "$W"/pm-root/lib*/libprojectM-4-playlist.so* "$OUT/libs/"
}

# what the libraries ask of the system, for the log
for f in "$OUT"/libfurnace_wrap.so "$OUT"/libsid_wrap.so "$OUT"/libs/libprojectM-4.so.4; do
    [ -f "$f" ] || continue
    echo "== $f"
    readelf -V "$f" | grep -o 'GLIBC_[0-9.]*\|GLIBCXX_[0-9.]*' | sort -uV | tail -2
    readelf -d "$f" | grep NEEDED
done
