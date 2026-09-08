# AudioCrate

A desktop music player that treats **chiptunes as first-class music** — Furnace
modules, VGM, NSF, SPC, tracker modules and AY/YM formats play natively, with
the same waveform, seeking, EQ and visualizers as any MP3 or FLAC.

Built with Python + PySide6 (Qt 6). Linux; Windows/macOS are untested.

## Features

**Formats** — 240+ extensions. Everything ffmpeg can decode (MP3, FLAC, Opus,
ALAC/M4A, WavPack, Musepack, Monkey's Audio, TTA, TAK, DSD, AC3/DTS/TrueHD,
plus dozens of game-console and broadcast formats), all common video containers,
and the chiptune set below.

**Chiptunes** — Furnace `.fur` (via libfurnace, streamed so it starts in
milliseconds), Commodore 64 SID (via libsidplayfp), VGM/VGZ, NSF/NSFe, SPC,
GBS, AY, GYM, HES, KSS, SAP (via libgme), tracker modules MOD/XM/IT/S3M and
friends (via openmpt123), and ZX/AY native formats (via zxtune123).
Multi-subsong navigation — prev/next plus a direct picker — for the formats
that have it.

**Chiptune lengths.** SID files carry no duration at all, and many chip formats
lie about it. Every tune is measured before it plays, so the seek bar is right
from the first frame: an HVSC songlength database is used when available,
otherwise the length is detected from the rendered audio by finding where the
tune stops or starts repeating.

**Playback** — gapless, crossfade, ReplayGain-aware normalization, silence trim,
and a live "Enhance" DSP chain (multiband EQ, exciters, stereo widening, safety
limiter) that toggles instantly without re-decoding. Audio runs on a dedicated
thread, so UI activity never interrupts sound.

**Visuals** — spectrum analyzer, oscilloscope with zero-crossing trigger and
configurable frame rate, Milkdrop/projectM visualizer, waveform seek bar, and
optional album-art color theming for the whole UI.

**Library & extras** — folder library with tag search, playlists with undo and
drag reordering, synced lyrics (LRCLIB), MPRIS media keys, Discord Rich
Presence, Last.fm scrobbling, play statistics, sleep timer.

## Install (Linux)

From a release zip:

```bash
unzip audiocrate-*-linux.zip && cd audiocrate
bash install.sh          # installs to ~/.local, adds an app-menu entry
bash uninstall.sh        # to remove
```

From the source tree:

```bash
bash packaging/install.sh              # user-local install
makepkg -si -p packaging/PKGBUILD      # Arch package
python main.py                         # or just run it in place
```

### Dependencies

Required: `python3`, `PySide6`, `numpy`, `scipy`, `mutagen`, `pillow`, `ffmpeg`.
The installer detects your package manager and offers to install them.

Optional: `libsidplayfp` (C64 SID), `libgme` (VGM/NSF/SPC playback),
`openmpt123` (tracker modules), `zxtune123` (AY/YM/ZX formats),
`libprojectm` (Milkdrop visualizer), `pypresence` (Discord Rich Presence).

For exact SID song lengths, put an HVSC `Songlengths.md5` at
`~/.config/audiocrate/Songlengths.md5` (also found automatically at
`/usr/share/hvsc/DOCUMENTS/` or `~/HVSC/DOCUMENTS/`). Without it, lengths are
detected from the audio.

## Building the Furnace player library

`.fur` support uses `libfurnace_wrap.so`, a small C wrapper (in
`_furnace_wrapper_src/desktop/`) around upstream Furnace's engine. To rebuild it:

```bash
git clone --depth 1 --branch dev250 --recurse-submodules \
    https://github.com/tildearrow/furnace
cd furnace
cmake -B build-lib -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF \
      -DCMAKE_CXX_FLAGS="-fPIC" -DCMAKE_C_FLAGS="-fPIC"
cmake --build build-lib -j$(nproc)     # the GUI target fails; the engine objects are what we need
cd build-lib && find CMakeFiles/furnace.dir -name '*.o' \
    ! -name 'main.cpp.o' ! -path '*src/cli/*' | xargs ar rcs libfurnace_lib.a
```

Then link the wrapper against it — **use system shared libsndfile/zlib/SDL2**,
not the vendored static copies, or you get a double-allocator crash at runtime:

```bash
g++ -shared -fPIC -std=c++23 -O2 _furnace_wrapper_src/desktop/furnace_wrap.cpp \
  -I<furnace>/src -I<furnace>/extern/blip_buf -I<furnace>/extern/fmt/include \
  -I<furnace>/extern/fftw/api -I<furnace>/extern/fftw \
  -I<furnace>/extern/vgsound_emu-modified -I<furnace>/extern/zlib \
  -I<furnace>/extern/IconFontCppHeaders -I<furnace>/src/icon \
  -I<build>/extern/zlib -I<build>/extern/fftw -DHAVE_SNDFILE -DHAVE_DIRENT_TYPE \
  -Wl,--whole-archive <build>/libfurnace_lib.a -Wl,--no-whole-archive \
  <build>/extern/fmt/libfmt.a <build>/extern/fftw/libfftw3.a \
  <build>/extern/rtmidi/librtmidi.a \
  $(pkg-config --libs sndfile zlib sdl2) -lasound -lpthread -ldl -lm \
  -o libfurnace_wrap.so
```

Two upstream files need a missing include added (`<cstring>` in
`src/fileutils/cfile.cpp`, `<climits>` in `src/engine/filePlayer.h`) to compile
with recent GCC.

## Building the SID player library

`.sid` support uses `libsid_wrap.so`, a small C wrapper (in `_sid_wrapper_src/`)
around the system libsidplayfp:

```bash
g++ -shared -fPIC -std=c++17 -O2 _sid_wrapper_src/sid_wrap.cpp \
    $(pkg-config --cflags --libs libsidplayfp) -o libsid_wrap.so
```

## Making a release

```bash
packaging/make_release.sh          # → dist/release/audiocrate-<version>-linux.zip
```

Version lives in `core/_version.py` and is the single source of truth.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE). AudioCrate links Furnace's engine,
which is GPL-2.0-or-later, so the combined work is GPL. Release zips include
the full source (`source/` inside the app archive) to satisfy that.
