# AudioCrate Changelog

## Unreleased

### Fixed
- **Crashes when switching chiptunes quickly, and when closing the visualizer.**
  Three separate use-after-free paths, all the same shape — native memory freed
  on one thread while another was still using it:
  - libgme's emulator was deleted without holding the lock that exists precisely
    to serialise access to it, so a track change could free the emulator while
    the decode thread was still rendering from it. Every call now takes the lock,
    deletes included, and `tools/check_emu_lock.py` fails the build if a new one
    forgets.
  - Stopping a chiptune tore down the audio sink after a 0.5 s wait, whether or
    not the decode thread had actually finished — the same expired wait that let
    the emulator above be freed mid-render. The wait is now long enough to be
    conclusive, and teardown is skipped entirely if the thread is somehow still
    running.
  - Closing the Milkdrop visualizer could render one more frame through a
    destroyed projectM instance. Every projectM entry point now checks first.
- Pausing and resuming a chiptune left a decode thread running for every
  pause/resume — twelve of them after twelve rounds, all feeding one audio
  device. The old one is now retired before a new one starts.
- Resuming a chiptune whose audio device had been torn down did nothing at all.
- **AudioCrate now appears as itself in your volume mixer**, with its own name,
  icon and version, instead of an anonymous "python3". This affects pavucontrol,
  helvum, and the GNOME and KDE volume panels, on both PipeWire and PulseAudio.
  Two consequences beyond the label. Your saved volume for AudioCrate is now
  kept against AudioCrate rather than against the Python interpreter, because
  WirePlumber keys it on `application.id`. And the five output paths (video, DSP
  engine, Furnace, VGM, SID) are all identified, where each used to show up as
  its own nameless entry. A routing rule that wants this app specifically can
  match `application.id = audiocrate`.
- **The mixer entry now carries the AudioCrate icon**, and reports `audiocrate`
  rather than `python3.14` as the process. Both keys were only ever set for the
  PulseAudio path, which turns out not to be the one in use.
- The app is properly associated with `audiocrate.desktop`, so Wayland
  compositors and XDG portals find its icon and name rather than a generic one.
- On X11, the WM_CLASS instance name is `audiocrate` instead of `python3`.

## 1.4.0 — 2026-07-30

### Added
- **Furnace `.fur` modules start almost instantly** (well under half a second,
  typically ~35 ms) — they stream while the rest renders underneath, so DSP,
  visualizers and seeking work from the first sample even on long modules.
- **Commodore 64 SID playback** (`.sid` / `.psid` / `.rsid`) via libsidplayfp,
  with a **subsong picker** next to the existing prev/next controls.
- **SID tune lengths are worked out before playback starts**, so the seek bar
  and time display are right from the first frame. Exact per-subsong lengths
  come from an HVSC `Songlengths.md5` if you have one (drop it in
  `~/.config/audiocrate/`); otherwise the length is detected from the audio by
  finding where the tune stops or loops — which also covers your own exports
  that no database knows about.
- Oscilloscope smoothing can now be set to **0** (raw, unfiltered trace).

### Fixed
- **A rare crash on chiptune playback traced to a one-word bug in the Furnace
  engine itself**: the engine's constructor zeroed only half of an internal
  pointer array (`sizeof(float)` where `sizeof(float*)` was meant), leaving
  garbage pointers that were later freed. Whether it crashed depended on
  whatever happened to be in memory — which is why it struck at random. Fixed
  in our engine build; the patch ships in `_furnace_wrapper_src/` and is being
  reported upstream. The embedded engine also no longer inherits your desktop
  Furnace tracker's settings (render thread pool, MIDI input).
- **Video files were silently ignored** when dropped on the window, added from
  a folder, chosen in the file dialog, or scanned into the library — every path
  filtered on audio extensions only. Video has always been playable; it just
  could not be added.
- **A crash when switching tracks quickly.** Furnace's engine cannot have two
  instances in one process (it keeps chip definitions in shared state but
  registers them per instance), and the app was creating a second one. This was
  behind the intermittent "free(): invalid pointer" aborts.
- Skipping through a playlist no longer queues up renders you have already
  skipped past — abandoned renders are cancelled, so the track you actually
  picked starts immediately instead of waiting its turn.
- SID subsongs played about a semitone sharp and too fast (they were rendered
  at 44.1 kHz and played at the device's rate without resampling).
- The waveform seek bar now updates per subsong instead of showing the first
  subsong's shape for all of them.
- Choosing a subsong sticks: a short subsong ending no longer silently jumps
  back to subsong 1.
- The waveform bar no longer decodes every file a second time — it reads the
  playback engine's own buffer, appearing in ~0.16 s and filling as it decodes.
- Rendered-chiptune caches are now capped (512 MB, least-recently-used first);
  previously they grew without limit.

## 1.3.0 — 2026-07-29

### Added
- **Massive format expansion**: 249 extensions (190 audio, 59 video), including
  VLC-parity aliases `.a52 .adt .adts .amb .mpga .qcp .xa`. Everything ffmpeg can demux —
  WavPack, Musepack, TTA, TAK, DSD (DSF/DFF), AC3/E-AC3/DTS/TrueHD/MLP, ALAC,
  Opus/Speex, QOA, plus dozens of game-console and broadcast formats — and all
  common video containers. `.nsfe` chiptunes now play too.
- **Furnace engine updated to dev250** (from dev245), so newer `.fur` modules
  and chip revisions play correctly.
- **Oscilloscope frame rate is configurable** (15–120 fps, default 60) in both
  Settings and the scope's own right-click dialog.
- Milkdrop visualizer now remembers its **lock and shuffle toggles** as well as
  the current preset.

### Fixed
- **Enhance no longer clips.** Normalize gain was applied *after* the safety
  limiter, so normalized + enhanced tracks clipped; gain now runs before the
  DSP chain, the limiter release is the intended 80 ms (was 5 ms, which itself
  distorted), and the wet mix gets 4.5 dB of headroom.
- **`.fur` files now get the DSP, visualizers and seeking from the first
  sample.** They render up-front (well under a second, cached) and play through
  the normal engine, instead of starting on the native player and switching
  over a moment later — which is why Enhance audibly "kicked in" late.
- Playback position and duration for `.fur` are now accurate and seekable.
- **Oscilloscope frame pacing**: the render timer used Qt's coarse timer, which
  batched wakeups and made the trace visibly speed up and slow down. Both the
  scope and the spectrum analyzer now use precise timers.
- A saved oscilloscope frame rate is applied at startup.

## 1.2.0 — 2026-07-26

### Performance
- Audio engine is now multithreaded: the DSP feed loop (EQ, Enhance, crossfade
  mixing) runs on a dedicated audio thread, so heavy UI moments can no longer
  cause audible dropouts.
- Track changes are instant: tag/duration/ReplayGain probing moved off the UI
  thread (previously could block for seconds on slow files).
- Album art is decoded once per track instead of 3–4 times, and folder-cover
  lookups are cached per album folder.
- Tag database reads/writes are batched — adding a large folder or restoring a
  big session does one query instead of thousands.
- Waveform seekbar renders from a cached image; moving the playhead no longer
  redraws the whole bar. Spectrum analyzer and waveform math vectorized.
- Visualizer caches scaled album art and per-bar colors (was rescaling
  multi-megapixel art every frame).
- Album-art theme prefetch is limited to the next few tracks in play order
  (was decoding art for the entire queue).
- Library search is debounced and filters in place — selection and scroll
  position survive typing and folder rescans.

### Fixed
- Playlists no longer lose tracks whose files are temporarily unavailable
  (unplugged drive, renamed folder). Missing entries are kept greyed-out,
  reported once in the status bar, skipped automatically during playback, and
  only removed via the new right-click "Remove Missing Tracks" action.
- Lyrics panel now tells "no lyrics found" apart from "couldn't reach LRCLIB"
  (offline), shows a "Searching…" state while looking, and has a right-click
  Retry — and a slow fetch can no longer show up on the wrong track.
- Drag-reordering a playlist now actually reorders what plays (rows and the
  play queue could get out of sync, playing the wrong track).
- Broken or unrenderable tracks show a status-bar message and skip ahead
  instead of failing silently (including missing openmpt123/zxtune123).
- The "Rendering…" status is cleared when a chiptune render finishes or fails.
- Deleting the currently-playing file from disk stops/advances playback first.
- Oscilloscope trace no longer jitters (playback position could momentarily
  read backwards between audio buffer updates).
- Adding a large folder (menu or drag-drop) no longer freezes the window while
  it scans.
- Missing library folders are reported instead of silently skipped; first
  library scans show live progress.

### Changed
- One keyboard map everywhere — keys no longer change meaning depending on
  which panel has focus. New/changed: `S` stop, `R` cycle play mode (was `M`
  in the controls bar; `M` is always mute), `E` toggle Enhance, `.`/`,` seek.
  Help → Keyboard Shortcuts now lists everything.
- Playlist: `Del` removes selected tracks, `Enter` plays the selection, the
  filter box keeps its text when switching tabs.
- File dialogs remember their last folder across sessions.
