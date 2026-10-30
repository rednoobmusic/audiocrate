# AudioCrate

A desktop music player that plays chiptune formats natively. Furnace modules,
VGM, NSF, SPC, tracker modules and AY/YM files get the same waveform, seeking,
EQ and visualizers as an MP3 or FLAC.

Written in Python with PySide6 (Qt 6). Linux only. Windows and macOS are untested.

## Features

### Formats

240+ extensions. Everything ffmpeg can decode (MP3, FLAC, Opus, ALAC/M4A,
WavPack, Musepack, Monkey's Audio, TTA, TAK, DSD, AC3/DTS/TrueHD and dozens of
game-console and broadcast formats), all common video containers, and the
chiptune formats below.

### Chiptunes

Furnace `.fur` (through libfurnace, streamed so it starts in milliseconds),
Commodore 64 SID (libsidplayfp), VGM/VGZ, NSF/NSFe, SPC, GBS, AY, GYM, HES, KSS
and SAP (libgme), tracker modules such as MOD, XM, IT and S3M (openmpt123), and
ZX/AY native formats (zxtune123). Formats with several subsongs get prev/next
buttons and a picker.

On Windows the same formats play through files that sit in the program's own
folders. `libs\` beside `main.py` holds `libgme.dll` (VGM, VGZ, NSF, NSFe, SPC,
GBS, AY, GYM, HES, KSS and SAP), `libfurnace_wrap.dll` (Furnace), `libsid_wrap.dll`
(SID) and `projectM-4.dll` with `projectM-4-playlist.dll` (the Milkdrop visualizer),
with the licence texts in `libs\LICENSES`. `tools\` holds `openmpt123.exe` (tracker
modules) and, in a folder of its own with the DLLs that belong to it,
`tools\zxtune123\zxtune123.exe` (the ZX formats: PT3, STC, PSG and the rest). The
release build adds zxtune123, the official build of the same revision as the Linux
package; a source checkout has no zxtune123 until it is unpacked there, and until
then those formats are not offered and `.vgm`, `.vgz` and `.ay` play through libgme.
The DLLs import system DLLs only. The song length database goes in
`%LOCALAPPDATA%\AudioCrate\Data\Songlengths.md5`, and Milkdrop presets in
`%LOCALAPPDATA%\AudioCrate\Data\presets` or in a `presets` folder beside the program.
No presets ship with the program; with none, the Milkdrop panel says where to put them. On a
computer that cannot give it OpenGL 3.3 (a remote desktop, a graphics driver that is not
installed) the panel stays off and says so, and the other visualizers work as usual.

### Chiptune lengths

SID files carry no duration, and many chip formats report a wrong one. Every
tune is measured before it plays, so the seek bar is right from the first frame.
If an HVSC songlength database is available it is used. Otherwise the length is
detected from the rendered audio, by finding where the tune stops or starts
repeating.

### Playback

Gapless playback, crossfade, silence trim, an equalizer, a DSP chain you build
yourself (see below) and Enhance (multiband EQ, exciters, stereo widening and a
safety limiter). Enhance switches on and off at once, without decoding the track
again. The sound goes through the equalizer, the DSP chain and Enhance in that
order. All three work on every format, including chiptunes played by libgme
(NSF/NSFe, SPC, GBS, GYM, HES, KSS, SAP) and the sound of video files. A video's
sound runs about 50 ms behind its picture. If it cannot be kept in step, the
video plays with its own sound instead. Settings, Playback has a switch for this
and a delay slider. Audio runs on its own thread, so UI activity does not
interrupt the sound.

### DSP chain

The DSP panel (View, DSP) is a list of effects that you stack and reorder, as in
foobar2000. Add opens a menu grouped as Level, Dynamics, Tone, Space, Time,
Channels and Utility. Drag a row, or use Up and Down, to change the order, and
untick a row to bypass it. The controls of the selected module are next to the
list or under it.

The modules are Amplify, Fade in and out, EBU R128 normalizer and Track
normalizer (Level); Compressor and Leveler (Dynamics); Graphic equalizer, Tone
control and Loudness compensation (Tone); Reverb and Convolver (Space); Echo,
Chorus, Flanger and Phaser (Time); Crossfeed, Stereo width and Channel mixer
(Channels); and Dither (Utility). The Convolver loads an impulse response from a
WAV or FLAC file.

The Track normalizer sets the level of each song before the other effects. It
measures the whole song first, so its gain is right from the first sample and
does not drift. Peak mode puts the loudest sample at the target (0 dBFS by
default). Loudness mode puts the song's integrated loudness at a LUFS target.
Maximum boost stops a silent or very quiet song from being turned up into noise.
A song starts a moment later while it is measured, well under a second for a
normal one. The normalizer works on songs the engine plays, so chiptunes played
by libgme and videos are not leveled. The EBU R128 normalizer measures as the
song plays and needs a moment to settle.

The chain runs after the equalizer and before Enhance, and holds up to 32
modules. Presets lists nine built-in chains (Headphone crossfeed, Late night,
Concert hall, Mono, Bass and clarity, Gentle leveler, Radio, Fade tracks, Flat),
and Save as keeps your own in `~/.config/audiocrate/dsp_presets`. The chain you
built is saved in `~/.config/audiocrate/dsp.json` and is back at the next start.
Adding, removing, moving or bypassing a module while a song plays does not cut
the sound, and the tail of a reverb or echo dies away instead of stopping.

A safety limiter ends the chain. It is on by default and keeps the output at or
below full scale (0 dBFS). It looks 1.5 ms ahead, so the sound is delayed by that
much while the chain is active. The Leveler and a Compressor with look-ahead
each delay it by up to 10 ms more. If the chain keeps using more than 85 percent
of the time a block of audio lasts, AudioCrate switches it off and the panel
offers Re-enable. A module that raises an error is bypassed and marked in the
list. Pitch or tempo change, VST hosting and a choice of resampler are not there
yet.

### Visuals

Spectrum analyzer, waterfall spectrogram, oscilloscope with a zero-crossing
trigger and a configurable frame rate, Milkdrop/projectM visualizer, waveform
seek bar, and optional album-art color theming for the whole UI.

### Online

Paste a YouTube or X link, or search YouTube, and the audio plays in
AudioCrate's own engine. The EQ, Enhance, the DSP chain, loop and scrobbling
apply to it, the visualizers draw it, and you can add it to a playlist or save
it to your library with its tags and cover art. No browser tab and no account
are involved. In video mode the sound goes through the EQ, the DSP chain and
Enhance too, as it does for video files.

Links from SoundCloud, Bandcamp, Vimeo, Mixcloud, the Internet Archive and
anything else yt-dlp can read work the same way, and so do direct links to audio
files. A YouTube channel or playlist opens as a page, with tabs for videos,
shorts, live streams and playlists and a search box for that channel. If
something needs you signed in, such as a private playlist, the Accounts page in
the Online settings opens your own browser for it, and AudioCrate then reads that
browser's cookies for the site. AudioCrate never asks for a password.

yt-dlp does the fetching, and AudioCrate runs it as a separate program. Sites
change often and yt-dlp has to catch up, so the Online settings show its version
and can update it. The first time you open Online, AudioCrate shows what the
sites' terms say about this. Spotify, Apple Music and other copy-protected
services do not work.

### Devices

Plug in a USB stick, SD card, external drive, Rockbox player or iPod and it
appears in the Devices panel with its name, size and free space. You can browse
it, play from it, copy music onto it from a playlist or a folder, copy music off
it, and eject it safely. Files the device cannot play are converted on the way,
tags and cover art come along, and file names are made safe for FAT.

An iPod in disk mode shows its real library, with titles, artists, albums and
playlists, instead of the scrambled file names on the disk. On models whose
database can be written safely, songs you add appear in the iPod's own menus.
AudioCrate backs up the database first and can restore it. Models that cannot be
written get plain file copy and a sentence that says why. An iPhone or iPad has
a Files tab that shows its media folders and the folders of apps that share
files, and you can copy files off it or songs into an app's folder. iOS does not
let other programs add to the Music app, so that direction is not offered.

### Converting files

File, Convert files opens a window where you add files or folders (or drop them
on it), pick a format and press Convert. The playlist and the library have
Convert in their right-click menus and send the selected tracks, albums or
folders to the same window. The window does not block, so the music keeps
playing.

The formats are MP3, AAC in M4A, Opus, Ogg Vorbis, FLAC, Apple Lossless, WAV,
AIFF and WavPack, as far as your ffmpeg has an encoder for them. Monkey's Audio
is added when the `mac` program is installed. Each format has its own quality
choices, such as V0, V2 or a fixed bitrate for MP3 and compression levels 0 to 12
for FLAC. You can keep or change the sample rate (44.1, 48 or 96 kHz; a rate the
format cannot write becomes the nearest one it can) and the channels, set the
volume from the file's ReplayGain tags or with a one-pass loudness leveling, trim
silence at the start and end, and choose whether tags and cover art are kept.
WAV keeps them in an ID3 block and WavPack in an APE block. The check box Use my
equalizer, DSP chain and Enhance runs the sound through a copy of your equalizer,
DSP chain and Enhance settings before it is encoded. That path decodes to stereo
at the file's own sample rate (a rate below 44.1 kHz or above 96 kHz is first
brought to a standard one), does not apply the volume slider, and sets Enhance
from the first minute of the file, as playback does.

File names come from a template with the fields `{artist}`, `{album}`,
`{albumartist}`, `{title}`, `{track}`, `{disc}`, `{year}`, `{genre}` and
`{filename}`, and a slash starts a folder. Results go next to the originals or
into one folder. If the file already exists, the new one is numbered, skipped or
replaces it, as you choose, but a result never replaces
its own original or another file in the list. The Safe names for FAT drives
option replaces the characters FAT cannot hold. It switches on by itself when
the destination is on a FAT, exFAT or NTFS drive or under `/run/media` or
`/media`.

Video files give their sound. Chip and tracker files (Furnace, SID, VGM, NSF,
SPC, tracker modules and the other chiptune formats) are played through once to
make audio first, with the same players as playback. For those you set a length
for files that do not say how long they are (5 to 3600 seconds), a fade out and
how many times a looping tune plays (the loop count). Originals are never written. Each result is
encoded under a hidden temporary name, checked for sound and length with
ffprobe and then renamed, so a cancelled or failed file leaves no partial
result. Up to four files convert at once, at low priority, and Cancel keeps the
files that are already done.

### Library and extras

Folder library that you browse by tracks, albums, artists or folders, with tag
search, playlists with undo and drag reordering, synced lyrics (LRCLIB), MPRIS
(media keys, playerctl and the media controls of Plasma and GNOME), Discord Rich
Presence, Last.fm scrobbling, play statistics, sleep timer.

On Windows the media keys, the volume card and the lock screen are Windows' own system
media controls: they show the title, artist, album, cover and position, and their buttons
(play, pause, next, previous, seek, shuffle, repeat) control the player. When Windows will
not give the program a media session, the four media keys are taken as hotkeys instead,
which keeps the keys working but leaves the volume card and lock screen out. The log says
which of the two happened.

A song with several artists is listed under each of them in the Artists view, with
the albums and tracks under every name. Settings, Artist names says how a tag with
one string is split: at commas, at ampersands and, if you turn it on, at slashes
(older ID3v2.3 tags put a slash between artists). A list of exceptions keeps names
such as AC/DC and Earth, Wind & Fire whole.

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

On Windows the media controls need the `winrt-runtime`, `winrt-Windows.Foundation`,
`winrt-Windows.Foundation.Collections`, `winrt-Windows.Media`, `winrt-Windows.Media.Playback`,
`winrt-Windows.Storage` and `winrt-Windows.Storage.Streams` packages (all version 3.2.1, MIT
licence; they pull in `typing_extensions`, PSF-2.0). `requirements.txt` lists them for Windows only.

For Online: `yt-dlp` and a JavaScript runtime (`deno`; yt-dlp also accepts `node` 22 or newer), and
`gallery-dl` if you want X profiles. For Devices: `udisks2` with `busctl`, which comes with systemd (finding drives,
mounting and eject), `libgpod` (iPod databases), `libimobiledevice` and `gvfs` (iPhone and iPad).

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

Then link the wrapper against it. Use the system shared libsndfile, zlib and
SDL2, not the vendored static copies, or you get a double-allocator crash at
runtime:

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

## Debugging a running AudioCrate

A running or frozen AudioCrate can report what it is doing, without a debugger.
The files go to `~/.config/audiocrate/debug/` and the newest ten of each kind
are kept.

- `kill -USR1 PID` writes the stack of every Python thread to `stacks-PID-TIME.txt`.
- `kill -USR2 PID` writes a state snapshot to `snapshot-PID-TIME.json`: versions, memory, threads, the
  audio engine (device, queue, underruns, feed timing), the player, the DSP chain, panels and the last
  300 log lines. Both signals work while the window is frozen.
- `python main.py --debug-dump` (or `audiocrate --debug-dump`) finds the running instance through its pid
  file in `$XDG_RUNTIME_DIR`, sends it both signals and prints where the files are with a short summary.
  `--debug-stacks` asks for the stacks only. It signals only an AudioCrate that is yours. When none is
  running it says so and exits with 1, and it exits with 2 if the instance did not answer within five seconds.
- When the window does not answer for more than half a second, the log gets a line with the function the
  window thread was in: `UI stalled 1.4 s in function_name (file.py:123), stack: ...`. The last fifty
  are in the snapshot.
- Help, Diagnostics shows the state, the thread stacks and the end of the log on one page. Save report
  writes a zip with the logs, the newest dumps, the versions and your settings, with tokens, secrets and
  cookies removed.
- The log is `~/.config/audiocrate/audiocrate.log`, three files of 2 MB, with the thread name on every
  line. Settings, System, Write detailed logs adds the debug lines, and `AUDIOCRATE_LOG=debug` does the
  same from the start.
- After a run that did not end normally, the next start writes to the log where the crash log and the
  last dump are.

## Making a release

```bash
packaging/make_release.sh          # → dist/release/audiocrate-<version>-linux.zip
```

The version is set in `core/_version.py`.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE). AudioCrate links two GPL libraries,
Furnace's engine for `.fur` playback and libsidplayfp for SID, and both are
GPL-2.0-or-later, so the combined work is GPL. Either one on its own would be
enough for that. Release zips carry the `.py` files and the wrapper sources
inside `app.tar.xz`, which is how the source requirement is met.

Nothing else it uses forces the license. libgme, projectM and Qt through PySide6
are LGPL, and ffmpeg, zxtune123 and openmpt123 run as separate processes. The
grant has to be GPL-2.0-or-later rather than plain GPL-2 because PySide6 is
LGPL-3, which GPL-2 alone cannot absorb.
