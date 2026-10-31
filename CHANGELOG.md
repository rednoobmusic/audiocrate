# AudioCrate changelog

## 1.7.1 (2026-10-30)

### Added
- A theme picker at the top of Settings, Appearance: 22 themes as cards, each painted in its own colours. System follows your desktop, AudioCrate and Frutiger Aero are the two looks that were there, four more have a style and textures of their own (Terminal, Carbon, Chiptune, Parchment), and there are eleven dark and four light ones (Midnight, Black, Nord, Dracula, Gruvbox, Solarized, Tokyo Night, Rosé Pine, Catppuccin, Forest, Crimson, Light, Paper, Latte). The list is folded behind a card that shows the theme in use, and a click on the card opens it. A click on a theme applies it at once. Customize under it still sets the accent, background and text yourself, and has a new Corners setting for how round the buttons, fields and menus are. "Colours follow the album art" is its own box now.
- The oscilloscope can draw its wave as bars (Settings, Visuals, Oscilloscope, Draw as bars), with a bar width of 1 to 12 px.
- Checked boxes show a check mark in the themes of their own, and the logo in the title bar takes the text colour on a light theme.

### Changed
- The Waterfall's spectrum strip is off by default (Waterfall settings, Display, Spectrum strip turns it on). Rows per second go up to 1000 instead of 240, which costs about a quarter of a core at the top. The oscilloscope window goes up to 2000 ms instead of 500, and a window that long is drawn as the band the wave swings in.
- Settings is easier to take in. The long explanations under settings are behind a small ? next to the setting now, and a click on it opens the text. Sections that most people never need are folded until you open them: Oscilloscope, Colours, Artist names, Logs, Lighting, Backup and reset, Discord, and Last.fm and Libre.fm until you turn them on. The colour rows grey out while the system style is in use.

### Fixed
- With System chosen and "Colours follow the album art" on, a song without a cover brought back the colours of the theme chosen before System, and the title bars kept the style of that theme. System stays the system look now until a cover gives it colours.
- The Waterfall kept the colours of the album art after the art colours were turned off, so changing the theme left its colours as they were. It lets go of them now, and "Follow the app colours" is on by default, so a new install has the Waterfall in the colours of the theme.
- The links in the Discord help did not open for some people. The help bubble is an ordinary widget in the window now and not a popup window.
- The player did not start where PySide6 comes without QtMultimediaWidgets, which Debian ships as a package of its own. The picture of a video is now painted from Qt's video sink when that part is missing.
- The player did not start on Qt older than 6.8, which has no sound tap for video. Video then plays through Qt's own sound output, without the EQ and DSP chain.
- Without QtDBus the player no longer stops at start up. The media keys are off instead.
- The icons on the control buttons were empty boxes on Qt 6.6 and older, which do not find the glyphs of the bundled font by themselves.
- Furnace (`.fur`) and SID files did not play on Debian, Ubuntu and Fedora. The Furnace library asked for a newer glibc than those have, and the SID library for a libsidplayfp they do not ship. Both are built on Debian 12 now, with libsidplayfp linked in, and `packaging/build_native.sh` does it.
- `libgme` was not found on Debian, Ubuntu and Fedora, which only ship `libgme.so.0`, so VGM, NSF and SPC files did not play.
- The Milkdrop visualizer needs projectM 4, and Debian, Ubuntu and Fedora ship projectM 3. The Linux release now carries projectM 4 and uses it when the system has none. Where Milkdrop cannot start, the panel says why and the other visualizers go on working.
- The Linux installer named PySide6 packages that Debian does not have. It now lists the Qt parts, says which optional ones are missing, and takes a virtual environment through `AUDIOCRATE_PYTHON`.

## 1.7.0 (2026-10-30)

### Added
- CD and DVD drives show up in the Devices panel, with a disc in them or without one. With no disc, the page has a Close tray button and the eject button opens the tray. An audio CD opens through GVFS (the gvfs package on most systems) as one Track N.wav file per track. You can play the tracks, add them to the playlist, or copy them to a folder, which rips a track to a WAV file. A data CD or DVD shows up as a read only volume, and nothing can be copied onto it. This is new and has so far been tried only with test data and a stand-in drive, so reports of discs or drives that do not work are welcome.
- A blank CD-R or DVD+R can be written. The Devices panel shows how much room it has, and Copy music writes the songs into a Music folder in a single session with xorriso (the libisoburn package). A disc can only be written once, so the question says so, and a disc that already has something on it is left alone. This has been tested against a file and a stand-in drive, not yet on a real disc.
- The Folders view of a drive or disc is now a folder browser that reads one folder at a time as you open it. A drive of any size shows its top folders at once and nothing is indexed. Double click plays a song or opens a folder, and Play and Add to playlist take a song, several songs or a whole folder, up to 5,000 songs at a time. Songs, Albums and Artists still read the whole drive. On a drive over 32 GB they first ask which folder to read, and the drive's menu changes that choice or reads everything.

### Changed
- A new install opens with a different layout. The top left is one tab group with Oscilloscope, Milkdrop, Now Playing, Online, Devices and Video, then the equalizer and DSP, then the controls. The playlist, library, settings, stats, up next, lyrics and info are tabs at the top right with the waterfall under them, and the window starts maximized. A screen shorter than about 980 px, such as a 768 line laptop, gets the previous compact layout instead, because the new one needs that much height. A layout you have already saved is not touched.

## 1.6.1 (2026-10-29)

### Fixed
- Album art showed the wrong picture. A file with several pictures showed the first one, which is often the back cover. It now shows the front cover, then any picture with no type, and a back cover or booklet page only when nothing else is there. WMA files had no art at all and now show it, and so do Monkey's Audio and WavPack files with a "Cover Art (Front)" tag. A picture in the song's folder is used only if its name says it is a cover, such as cover, folder, front or album art, and the small thumbnails Windows Media Player leaves come last. Before, any picture in the folder counted, so a folder of downloads gave every song in it the same wallpaper.
- Skipping quickly through songs could leave the previous song's cover in Now Playing or the Spectrum panel, when its picture arrived after the next song's.
- CPU use with nothing playing. The Waterfall redrew itself sixty times a second, and the window and panel behind it with it, and the Spectrum panel kept drawing its resting bars. Both now draw only when something moves.
- The Online panel no longer opens a new install with a warning about the JavaScript runtime or an old yt-dlp. The notes appear when you first search or open a link.
- A new install opened on the Settings tab. It now opens on the Playlist.
- Memory. The next song was decoded as soon as the current one started, so the player held two decoded songs for nearly the whole of every song, and an hour long mix meant hundreds of megabytes more. It is now decoded a minute before the current song ends, or at once when the song is shorter than that or its length is not known yet. That roughly halves what playback holds. A seek or a pause moves the moment, and gapless playback is unchanged.
- Windows: the 1 ms timer tick and the opt-out from background throttling were held for as long as the program ran. They are now taken while a song plays and given back five seconds after it stops, so a player left paused in the background no longer keeps the whole machine on a fast timer. The memory that start-up leaves behind is handed back to the system once the window is up.
- Windows: turning on lighting started a thread that failed at once, because the Predator keyboard controller exists only on Linux. It is no longer started there, and the keyboard, lid emblem and mode LED settings are hidden.

## 1.6.0 (2026-10-27)

### Added
- Keep the output open (Settings, Output device). Playback used to close its stream in the sound server on every stop,
  skip, seek and output change, and a patchbay such as Helvum loses everything wired to a stream that closes. With
  this on, one stream is opened at once and stays open (suspended while nothing plays) until you quit or turn it off,
  so stopping, pausing, skipping and seeking keep what you wired. Choosing another output opens a new stream on it.
  A song you stop or skip plays on for a moment (about a tenth of a second), because what is queued in an open
  stream cannot be thrown away. Chiptunes played by the libgme player and the sound of videos still use streams of
  their own. Windows has no patchbay, so the setting is not shown there.
- DSP chain. The DSP panel (View, DSP; it starts hidden) is a list of effects that you add, drag
  into order and bypass one by one, like foobar2000's DSP manager. It runs after the equalizer and
  before Enhance. The modules are Amplify, Fade in and out, EBU R128 normalizer, Track normalizer,
  Compressor, Leveler, Graphic equalizer, Tone control, Loudness compensation, Reverb, Convolver,
  Echo, Chorus, Flanger, Phaser, Crossfeed, Stereo width, Channel mixer and Dither. Nine presets are
  built in, your own go to `~/.config/audiocrate/dsp_presets`, and the chain you left is kept in
  `~/.config/audiocrate/dsp.json`. Editing while a song plays does not cut the sound. A safety
  limiter ends the chain. It looks 1.5 ms ahead, so the chain delays the sound by that much while
  it is active, and the Leveler and a Compressor with look-ahead add up to 10 ms each. A chain that
  keeps using more than 85 percent of the time a block of audio lasts is switched off, and the
  panel offers Re-enable. Pitch or tempo change, VST hosting and a choice of resampler are not
  there yet.
- Convert files. File, Convert files and the Convert entry in the playlist and library right-click
  menus open a window that converts music to MP3, AAC in M4A, Opus, Ogg Vorbis, FLAC, Apple
  Lossless, WAV, AIFF or WavPack, as far as your ffmpeg can write them. Monkey's Audio is added when
  the `mac` program is installed. Add files or folders, or drop them on the window. Each format has
  its own quality choices. You can set the sample rate, the channels and the volume (ReplayGain
  from the tags, or a one-pass loudness leveling), trim silence at the ends, keep or drop tags and
  cover art, and name the files with a template where a slash starts a folder. If a file with that
  name exists, you choose whether to number the new one, skip it or replace it. Safe names for FAT
  drives switches on by itself for a destination on a FAT, exFAT or NTFS drive. Video files give
  their sound. Chip and tracker files are played through once to make audio, with a length for
  files that do not say how long they are, a fade out and a loop count. Use my equalizer, DSP chain
  and Enhance runs the sound through a copy of your equalizer, DSP chain and Enhance settings
  first. Each result is written under a hidden name, checked and then renamed, and the original is
  never changed. The window does not block, so the music keeps playing.
- Debugging a running AudioCrate. `kill -USR1` writes the stack of every Python thread and
  `kill -USR2` a state snapshot (memory, threads, audio engine, player, DSP chain, recent log
  lines), both to `~/.config/audiocrate/debug`, and both work while the window is frozen.
  `audiocrate --debug-dump` finds the running instance, asks for both and prints the paths with a
  summary; `--debug-stacks` asks for the stacks only. A watchdog logs every time the window did not
  answer for more than half a second, with the function it was in. Log lines carry the thread name,
  the log keeps three files of 2 MB, and Settings, System, Write detailed logs (or
  `AUDIOCRATE_LOG=debug`) turns on debug lines without a restart. Underruns, the audio device, slow
  track starts and slow level analyses are logged. After a run that did not end normally, the next
  start says where its crash log and last dump are. Help, Diagnostics shows all of it on one page
  and saves a report zip with tokens, secrets and cookies removed.
- Artists in the library. The Library panel has a fourth way to browse, next to Tracks, Albums and
  Folders. It lists every artist once, with how many albums and tracks they have, and a song with
  several artists is listed under each of them. Open an artist to see the albums it is on, and open
  an album to see its tracks there. Albums are filed under their album artist, or under the first
  artist when there is none, as in the Albums view. The search box filters the list, a double click
  or Enter adds the tracks, Right and Left open and close a row, and the right-click menu has the
  same entries as the other views. The library remembers which view you used. Where the panel is
  too narrow for four buttons, the switch becomes a menu button that shows the current view. The
  panel needs no more room than before.
- Settings, Artist names has Split at slashes, off by default. Older ID3v2.3 tags put a slash
  between artists, as in "A/B". With the box on, a tag that holds one string is also split at the
  slash. Names on the exception list and the album artist of the file stay whole. AC/DC and K/DA
  are on the default list. If you saved your own list before, add the names you need to it.

### Changed
- Importing a config and resetting the settings now update the Artist names boxes and regroup the
  library, the playlist and the queue at once. Importing a config used to leave the library grouped
  the old way until the next change.
- The small equalizer in the DSP panel draws in the same accent as the Equalizer panel. In the Aero
  look it used a slightly different colour.
- Chiptunes played by libgme go through the equalizer, the DSP chain and Enhance. NSF, NSFe, SPC,
  GBS, GYM, HES, KSS and SAP files, and VGM, VGZ and AY files when zxtune123 is not installed, had
  only the volume control before. libgme now renders at the output device's rate instead of
  44.1 kHz.
- The sound of video files goes through the equalizer, the DSP chain and Enhance. It runs about
  50 ms behind the picture and is kept in step by small corrections. If that fails, the video goes
  back to its own sound. Settings, Playback has a switch for it (on by default, it takes effect at
  once) and a delay slider from -200 to +200 ms to line the sound up on your own sound card, with a
  readout of how far behind the picture it runs. The scope, spectrum and waveform follow it too.
- Chiptunes follow the volume slider within 20 ms and ring out. The tail of a reverb or echo is no
  longer cut where a chiptune ends, and Enhance is set from the first 30 seconds of the tune.
- Menu items, dialog titles, buttons and tooltips are in sentence case: Add files, Show the tour,
  Play now, Remove duplicates, Library folders. Panel names such as Now Playing and Up Next keep
  their capitals, because saved layouts use them.
- The Info panel lists more of what it reads. Chiptune and tracker files show game, system,
  copyright, dumper, tracks, sub-songs and the chips and channels, and a file with a lyrics tag shows
  it. A row appears only when the file has a value for it.
- The equalizer says in the status bar when a preset cannot be saved, loaded or deleted. It used to
  fail without a word. The reason is in the log.

### Removed
- Normalize volume in Settings. The Track normalizer in the DSP chain does that job now. It
  measures each song whole, so the gain is right from the first sample instead of drifting, and in
  Peak mode the loudest sample lands on 0 dBFS. Add it in View, DSP. It works on songs the engine
  plays, so chiptunes played by libgme and videos are not leveled. The EBU R128 normalizer and the
  Leveler level as a song plays.

### Fixed
- Windows: the Convert files window no longer freezes the first time it opens. The list of formats it can write is
  now worked out in the background a few seconds after start.
- Windows: a conversion that fails now says why. The message for a full disk looked for a signal Windows does not have
  and raised an error instead of showing any message.
- Plainer wording in the Convert files window. The destination choices are Next to the original files and
  Folder, the choices for a file that already exists are Add a number, Skip and Replace, and the tips and
  messages say the same things in fewer words.
- Panels can be dragged again. Drag a panel by its bar and the window shows where it would dock; let go
  to drop it there, as a tab, beside another panel or at an edge. A floating panel docks the same way
  when you drag its bar back over the window. This was switched off to cure clicks stopping after a
  drag, which came from swapping the panel's bar in the middle of the press. The bar now waits until
  the button is up.
- Playing a video no longer opens or raises the Now Playing panel. The picture shows there when you have
  the panel open, and nothing moves on its own when you do not.
- Unplugging the sound output while a song played could leave playback frozen until you changed
  song or seeked. Some outputs say nothing when they go away. A sink that has taken no audio for
  two seconds, or that reports an error, now pauses playback as if you had pressed Pause. Play, or
  starting a track, then moves to the output you chose, or to the default if that one is gone, and
  says so. If no output exists, nothing starts. Chiptunes are paused and moved the same way, and a
  paused chiptune now follows an output change in Settings too.
- After a config import, changes in Settings ran twice, because the import connected every setting
  to its handler a second time. The import now applies the imported values and connects nothing.
- Finding ffprobe next to ffmpeg changed every "ffmpeg" in the path, so a folder such as
  `/opt/ffmpeg-7/bin` gave a path that does not exist. Only the file name changes now.
- Mic Codec Lab ended any ffmpeg process whose command line named its virtual device. It now ends
  only the pipelines it started.

## 1.5.0 (2026-10-04)

### Added
- Online. Paste a YouTube or X link, or search YouTube, and it plays through AudioCrate's own
  engine, so the EQ, Enhance, normalization, scrobbling and the visualizers work on it. Playlists
  and channels open as lists, and anything else yt-dlp reads works too (SoundCloud, Bandcamp, Vimeo,
  Mixcloud, the Internet Archive, direct audio links). You can play now, play next, add to a
  playlist, or save to your library with tags and cover art in the file. Fetches show progress, can
  be cancelled, and are kept in a cache you can cap and clear. Signing in is optional and uses your
  browser's cookies or a cookies file, never a password. Errors say what happened and what to do.
  Settings shows the yt-dlp version with an Update button, because sites change and yt-dlp has to
  follow. A notice on the first run explains what the sites' terms say. Drop a link on the window to
  play it. Spotify, Apple Music and other copy-protected services are not supported. The panel
  starts hidden if you already have a layout (View, Library, Online).
- Devices. USB sticks, SD cards, external drives, Rockbox players and iPods appear in a Devices
  panel with their size and free space. You can browse them, play from them, copy music onto them
  from a playlist or a folder (converted only when the device cannot play it, with tags, cover art
  and file names that FAT accepts) and copy music off them. An iPod in disk mode shows its real
  library instead of the scrambled file names, and on models whose database can be written safely,
  added songs show up in the iPod's own menus. The database is backed up first and can be restored.
  iPhones and iPads can be browsed and copied from. Eject unmounts, flushes and powers off, and
  refuses while a copy is running.
- Title bars in the app's own theme. The main window and every popout (floating panels, dialogs,
  message boxes) draw their own bar instead of the compositor's. Moving, resizing, snapping and
  tiling still go through the compositor. Settings has "Use AudioCrate title bars" to turn it off
  (restart to apply).
- Lights that follow the music. The Predator keyboard, lid emblem and mode LED, and any OpenRGB
  device, take the album color and move with the spectrum (Spectrum, Pulse or Steady). The color
  fades from song to song, anything that overwrites the lights (Predator Sense, the resume service)
  is corrected within a second, and your saved lighting comes back when you turn it off or quit.
  OpenRGB's server is started for you if it is not already running.
- Listening over time. The Stats panel has an Over time tab: cumulative lines for your top artists
  or tracks, by plays or listening time, from 7 days to all time. History is kept from this
  version on, so earlier plays have no dates.
- A new playlist. Group by album, artist or folder with cover headers, sort presets and multi-key
  header sorting with a one-click way back to your own order, drag to reorder with an insertion
  line, move to top or bottom, remove duplicates, a column chooser, and a compact two-line layout
  for narrow panels. Next and previous follow the order on screen.
- A new library. Tracks, Albums (cover grid or list) and Folders views with covers, counts, empty
  and scanning states, and a right-click menu.
- Settings in five sections instead of nine tabs, with the controls grouped and aligned.
- A waterfall spectrogram (View, Visualizers, Waterfall; it starts hidden if you already have a
  layout). Frequency runs across and time flows away from the newest row, so a sweep is a diagonal
  and a drum is a streak, with a live spectrum and peak hold strip above it. You can set everything
  from its right-click menu or Settings, Visuals: FFT size, window (Hann, Hamming, Blackman,
  Blackman-Harris, Nuttall, flat top, Kaiser, Gaussian, rectangular), zero padding, rows per
  second, a linear, log, mel, Bark or ERB axis with its range, level floor and ceiling, gamma,
  brightness, auto range, spectral tilt, time and frequency smoothing, twelve color maps plus album
  art, accent and your own (or follow the app colors whatever map is chosen), the direction the
  history goes, labels, grid, legend, the spectrum strip above it (in the theme's colors, and it
  can be switched off), the frame rate and V-Sync like the oscilloscope's, and a hover readout.
  Click to freeze, use the wheel to zoom the frequency axis (what is already on screen zooms with
  it, because the picture is drawn again from the history), Shift and wheel to pan, and double
  click to reset it. Named presets can be saved and loaded. Labels slide inside the panel, skip
  each other and give way to the legend and the strip as the panel gets small, so nothing is cut
  off or overlaps.
- A Frutiger Aero style (Settings, Appearance, Style): glass title bars, menus, tabs and buttons
  with a bright rim, sky blue accents and a shine across the title bar. It comes from the early
  AudioCrate and is brought up to date for the current panels. It follows the album art colors and
  your accent, background and text colors like the default style does, and the font you pick still
  applies.
- Borders around every panel, its title bar included, floating panels too, in the theme's border
  color. Settings, Borders between panels turns them off for a flatter look.
- A title bar menu on panels. Right-click a docked panel's bar for Float, Move to Left, Right, Top
  or Bottom and Close, and a floating panel's bar for Dock and Dock to Left, Right, Top or Bottom.
  Double-click either bar to float or dock the panel.
- The UI font is chosen in the system font dialog (KDE's own where it is available) instead of a
  long drop-down list.
- The oscilloscope frame rate now goes up to 240 fps and then Unlimited, and there is a V-Sync
  option that draws one frame per display refresh.
- A short picture tour. Eight slides, each a picture of the real window with a few lines of text,
  show where things are. It opens by itself the first time AudioCrate starts (not when the launch
  was given a song or a link to play, then it waits for the next start) and again from Settings,
  System or Help, Show the Tour. The arrow keys, Escape and the dots work, and Skip, Done or
  closing it counts as seen.
- Channel and playlist pages in Online. Click the uploader of a YouTube result, or paste a channel
  or playlist link or an @handle, and it opens as a page with the channel's name, subscriber count
  and description, tabs for Videos, Shorts, Live and Playlists, a search box for that channel, and
  Latest or Popular order. Back and Forward work like a browser's. SoundCloud and Bandcamp pages
  open as one list.
- Sign in with your browser. The Accounts page in the Online settings has a button that opens the
  site's sign-in page in your default browser. When you are done there, press Done and AudioCrate
  reads that browser's cookies for the site. It never sees your password.
- A Files tab for iPhones. An iPhone opens as a file browser: its media folders and the folders of
  the apps that share files. You can copy files off it and copy songs into an app's folder.
- Float and move panels from their tabs. Right-click a docked panel's tab for Float, Move to Left,
  Right, Top or Bottom, and Close, or double-click the tab to float the panel.
- Send to Device in the playlist menu copies the selected songs to a device from the Devices panel.
- Links from the command line and by drop. `audiocrate https://...` and a link dropped on the
  window go to the Online panel. A video that starts brings the Now Playing panel forward.

### Removed
- The cursor particle trail. Nothing draws over the window while the mouse moves any more.

### Fixed
- A slow disk service no longer freezes the program. The Devices panel read drives through Qt's
  D-Bus calls, which hold Python's lock while they wait, so a busy `udisksd` could freeze the window
  and the audio for as long as it took to answer. Drives are now read with `busctl`, which can be
  stopped. Quitting while a device tool hangs closes at once and leaves nothing running, and a
  hidden Devices panel no longer reads the songs on mounted sticks.
- Online no longer reads the clipboard by itself. On X11 that could freeze the window for seconds.
  The Paste card does it when you ask. Online also says when its cache folder cannot be written,
  instead of claiming yt-dlp is missing.
- The View menu showed the wrong pictures for eight entries, such as a bold B for Milkdrop, a taco
  for Stats and a sprout for Controls. They match the icon font now, and `tools/check_icons.py`
  compares every named icon with its glyph.
- A layout saved by an older build no longer changes the window size on the first launch when the
  app's own theme is on and the window is not maximized.
- Songs copied off a device into ~/Music join the library, even when the library points somewhere
  else.
- SIGTERM and SIGHUP close the window the normal way, so the layout and settings are saved. Folders
  and file://localhost addresses on the command line open.
- Floating and docking a panel no longer leaves a title bar behind each time.
- Saving from Online no longer drops library folders on a drive that is not plugged in. The library
  list keeps them and writes them back.
- Frame shading works again. The setting drew its gradient on a title bar style that no panel uses
  any more, because every panel has a title bar of its own, so moving the slider changed nothing.
  The gradient now goes on the panel bars and on the floating and main window bars: lighter at the
  top, darker below, flat at 0.
- Panels keep their sizes when you relaunch. The saved layout was put back after the window was
  already on screen and still growing to its maximized size, and Qt laid the panels out again for
  every one it showed. The Oscilloscope came back at its minimum height with the Equalizer
  stretched, panels beside each other moved a pixel on every launch, and a window that was not
  maximized came back taller. The layout is now restored before the window is first shown, a
  floating panel is shown before its size is applied, and a window closed before its saved state is
  back leaves the file alone.
- The window stopped taking clicks after panels were floated and docked again. Dragging a docked
  panel by its bar made Qt grab the mouse for its own dock drag, and when the panel turned into a
  window halfway through, the button release went to the old window, so the grab never ended and
  every click went to the dock. A docked bar no longer starts Qt's drag (the float button, a double
  click and the right-click menu do it instead), a floating panel is no longer given new window
  flags, floating and docking from a title bar happen after the click has finished, and a click
  while a dock still holds the mouse lets it go.
- Freezes when you change song. Discord presence used to connect, read and resize the cover and
  wait for Discord's answer on the window's own thread (an 800 ms freeze with a slow client in the
  test). It now runs on its own thread, and a change that arrives meanwhile is sent after. Clicking
  through songs no longer starts a full decode of the next track for every song you pass: it waits
  until you have stayed on one for a second, and a decode nobody wants any more stops at once. The
  window recolors in one pass instead of a few panels per turn of the event loop, each turn waiting
  behind the visualizers (a 100 ms job took half a second), colors are not read for a song you
  already left, and the prefetch of the next covers waits until after the recolor. MilkDrop now
  paces itself by what a frame costs on the window's thread, so it cannot keep the rest of the
  window waiting.
- The collapsed title bar tab sits flush in the title bar color, and its notch is as dim as the
  title bar buttons until you hover it.
- Playlists stay separate. What is playing comes from one playlist, and only that playlist shows
  the playing row (a track in several playlists used to be marked in all of them). Up Next lists
  that playlist's upcoming tracks and says which playlist it is. Adding to, removing from or
  clearing another playlist no longer changes the queue that is playing.
- MilkDrop's lock did nothing after a restart. The button showed locked, but the visualizer is only
  built when it is first shown, after the saved lock had been applied to nothing, so it changed
  preset every 22 seconds anyway. Each change compiles new shaders on the window's own thread,
  which froze the app for up to a second at a time. The saved lock and shuffle are now applied once
  it exists.
- UI size applies when you change it. It used to apply only when you let go of the slider, so the
  arrow keys, the mouse wheel and a click on the track did nothing. It now applies a moment after
  the last change, and the size you pick in the font dialog sets the UI size too (it was thrown
  away, only the font name was kept).
- The oscilloscope shows the time you set. The scope counted its window in samples at 44.1 kHz, but
  the engine plays at the output device's rate, so a 500 ms window was 459 ms on a 48 kHz system.
  It now uses the real rate.
- A shelf's slope can be set in the EQ. The Slope box was only enabled for highpass and lowpass. A
  low or high shelf now takes 12, 24, 36 or 48 dB per octave. Steeper ones are a cascade of
  sections with the Butterworth pole Qs, each carrying its share of the gain, so the plateau stays
  at the gain you set, with no overshoot. 12 is the old shelf exactly, and Q still scales the
  shape.
- The font you pick is used. The style sheet listed the bundled Nerd Font first and your choice
  after it, so the first one always won and the UI never changed. Your font now comes first, and
  the Nerd Font behind it still draws the icons.
- The Now Playing seek bar follows the theme like the one in Controls, in the album art colors, the
  app colors and the system theme alike.
- Song changes no longer wait on swap. On a machine short of memory the pages a song change touches
  had often been swapped out, and each one is a wait: the running app had 85 percent of its memory
  in swap and took over a hundred page faults a second. AudioCrate now keeps its memory resident as
  it uses it (mlockall with on-fault locking, so nothing already swapped is pulled in and empty
  address space is not filled). Set AUDIOCRATE_NO_MLOCK=1 to turn it off.
- The oscilloscope no longer steps back or loses its trace. The position it was drawn at came from
  two numbers the audio thread updated at different moments, so a read in between jumped a whole
  block ahead and back. It is now published as one number. The scope reads the audio without
  waiting for a lock (a missed read decayed the trace or drew it unaligned), and the trigger never
  locks to a crossing earlier than the last frame's.
- Group by artist gathers all of an artist's tracks under one header. It only joined neighboring
  tracks, so the same artist showed up twice, and it now treats Mylta, mylta, a name with a zero
  width space and full width or bold look-alike letters as one artist. Group by album and by folder
  gather the same way, and the order you arranged is still what gets saved.
- Freezes and cut-outs under load. The audio thread now runs at real-time priority and keeps a
  deeper queue while the machine is busy (250 ms normally, 600 ms after a late tick). Several
  things that held the app's locks across waits are gone: the decode thread no longer holds the
  engine lock while copying, the scope, spectrum and waveform never wait on it, cover art is
  decoded without holding the interpreter lock, the per-track analysis is vectorized, and adding a
  folder no longer reads tags on the GUI thread (a few thousand new files took seconds). Garbage
  collection no longer walks the startup heap.
- Color changes between songs. The window used to show some panels in the old album's colors and
  some in the new for a moment, and each song change did the work again. Colors now wait a moment
  for the song to settle and change in one repaint, and the visuals that draw their own colors
  start fading at the same instant (and fade by time rather than by frame count).
- Opening a menu no longer animates the popup, which cost a relayout per frame.
- Visuals under a power saving profile drop to 30 fps, and the spectrum, EQ curve and oscilloscope
  are cheaper per frame (cached layers, fewer allocations, a thick trace drawn without Qt's slow
  stroker).
- The EQ editor's controls wrap instead of squeezing, so labels are no longer cut off ("emov",
  "n"), and they keep the height they need in a narrow panel.
- Oscilloscope smoothing 0 no longer shimmers: the trace is averaged per pixel rather than sampled.
- A native crash in the sound server's library during startup is retried automatically instead of
  ending the app.
- Dashes and bullet separators removed from interface text, including the web remote.
- Crashes when switching chiptunes quickly, and when closing the visualizer. These were three
  separate use-after-free paths of the same shape, native memory freed on one thread while another
  was still using it:
  - libgme's emulator was deleted without holding the lock that exists to serialize access to it,
    so a track change could free the emulator while the decode thread was still rendering from it.
    Every call now takes the lock, deletes included, and `tools/check_emu_lock.py` fails the build
    if a new one forgets.
  - Stopping a chiptune tore down the audio sink after a 0.5 s wait, whether or not the decode
    thread had finished. It was the same expired wait that let the emulator above be freed
    mid-render. The wait is now long enough to be conclusive, and teardown is skipped entirely if
    the thread is somehow still running.
  - Closing the Milkdrop visualizer could render one more frame through a destroyed projectM
    instance. Every projectM entry point now checks first.
- Pausing and resuming a chiptune left a decode thread running for every pause/resume, twelve of
  them after twelve rounds, all feeding one audio device. The old one is now retired before a new
  one starts.
- Resuming a chiptune whose audio device had been torn down did nothing at all.
- AudioCrate now appears as itself in your volume mixer, with its own name, icon and version,
  instead of an anonymous "python3". This affects pavucontrol, helvum, and the GNOME and KDE volume
  panels, on both PipeWire and PulseAudio. Your saved volume for AudioCrate is now kept against
  AudioCrate rather than against the Python interpreter, because WirePlumber keys it on
  `application.id`. The five output paths (video, DSP engine, Furnace, VGM, SID) are all
  identified too; each used to show up as its own nameless entry. A routing rule that wants this
  app specifically can match `application.id = audiocrate`.
- The mixer entry now carries the AudioCrate icon, and reports `audiocrate` rather than
  `python3.14` as the process. Both keys were only ever set for the PulseAudio path, which turns
  out not to be the one in use.
- The app is properly associated with `audiocrate.desktop`, so Wayland compositors and XDG portals
  find its icon and name rather than a generic one.
- On X11, the WM_CLASS instance name is `audiocrate` instead of `python3`.
- The media keys, playerctl and the media controls in the Plasma and GNOME desktops work now. The
  MPRIS object was on the bus but had none of the MPRIS interfaces, so nothing could control the
  player.

## 1.4.0 (2026-07-30)

### Added
- Furnace `.fur` modules start almost instantly (well under half a second, typically ~35 ms). They
  stream while the rest renders underneath, so DSP, visualizers and seeking work from the first
  sample even on long modules.
- Commodore 64 SID playback (`.sid` / `.psid` / `.rsid`) via libsidplayfp, with a subsong picker
  next to the existing prev/next controls.
- SID tune lengths are worked out before playback starts, so the seek bar and time display are
  right from the first frame. Exact per-subsong lengths come from an HVSC `Songlengths.md5` if you
  have one (drop it in `~/.config/audiocrate/`). Otherwise the length is detected from the audio by
  finding where the tune stops or loops, which also covers your own exports that no database
  knows about.
- Oscilloscope smoothing can now be set to 0 (raw, unfiltered trace).

### Fixed
- A rare crash on chiptune playback came from a one-word bug in the Furnace engine itself: the
  engine's constructor zeroed only half of an internal pointer array (`sizeof(float)` where
  `sizeof(float*)` was meant), leaving garbage pointers that were later freed. Whether it crashed
  depended on whatever happened to be in memory, which is why it struck at random. Fixed in our
  engine build. The patch ships in `_furnace_wrapper_src/` and is being reported upstream. The
  embedded engine also no longer inherits your desktop Furnace tracker's settings (render thread
  pool, MIDI input).
- Video files were silently ignored when dropped on the window, added from a folder, chosen in the
  file dialog, or scanned into the library, because every path filtered on audio extensions only.
  Video has always been playable but could not be added.
- A crash when switching tracks quickly. Furnace's engine cannot have two instances in one process
  (it keeps chip definitions in shared state but registers them per instance), and the app was
  creating a second one. This was behind the intermittent "free(): invalid pointer" aborts.
- Skipping through a playlist no longer queues up renders you have already skipped past. Abandoned
  renders are cancelled, so the track you picked starts immediately instead of waiting its turn.
- SID subsongs played about a semitone sharp and too fast (they were rendered at 44.1 kHz and
  played at the device's rate without resampling).
- The waveform seek bar now updates per subsong instead of showing the first subsong's shape for
  all of them.
- Choosing a subsong sticks: a short subsong ending no longer silently jumps back to subsong 1.
- The waveform bar no longer decodes every file a second time. It reads the playback engine's own
  buffer, appearing in ~0.16 s and filling as it decodes.
- Rendered-chiptune caches are now capped (512 MB, least-recently-used first). They used to grow
  without limit.

## 1.3.0 (2026-07-29)

### Added
- Format support grew to 249 extensions (190 audio, 59 video), including the VLC aliases
  `.a52 .adt .adts .amb .mpga .qcp .xa`. It covers everything ffmpeg can demux: WavPack, Musepack,
  TTA, TAK, DSD (DSF/DFF), AC3/E-AC3/DTS/TrueHD/MLP, ALAC, Opus/Speex, QOA, dozens of game-console
  and broadcast formats, and all common video containers. `.nsfe` chiptunes now play too.
- The Furnace engine is updated to dev250 (from dev245), so newer `.fur` modules and chip revisions
  play correctly.
- The oscilloscope frame rate is configurable (15-120 fps, default 60) in both Settings and the
  scope's own right-click dialog.
- Milkdrop now remembers its lock and shuffle toggles as well as the current preset.

### Fixed
- Enhance no longer clips. Normalize gain was applied after the safety limiter, so normalized and
  enhanced tracks clipped. Gain now runs before the DSP chain, the limiter release is the intended
  80 ms (it was 5 ms, which itself distorted), and the wet mix gets 4.5 dB of headroom.
- `.fur` files now get the DSP, visualizers and seeking from the first sample. They render up front
  (well under a second, cached) and play through the normal engine, instead of starting on the
  native player and switching over a moment later, which is why Enhance audibly kicked in late.
- Playback position and duration for `.fur` are now accurate and seekable.
- Oscilloscope frame pacing was uneven. The render timer used Qt's coarse timer, which batched
  wakeups and made the trace visibly speed up and slow down. The scope and the spectrum analyzer
  now both use precise timers.
- A saved oscilloscope frame rate is applied at startup.

## 1.2.0 (2026-07-26)

### Performance
- The audio engine is now multithreaded: the DSP feed loop (EQ, Enhance, crossfade mixing) runs on
  a dedicated audio thread, so heavy UI moments can no longer cause audible dropouts.
- Track changes are instant: tag, duration and ReplayGain probing moved off the UI thread (it
  could block for seconds on slow files).
- Album art is decoded once per track instead of 3-4 times, and folder-cover lookups are cached
  per album folder.
- Tag database reads and writes are batched. Adding a large folder or restoring a big session does
  one query instead of thousands.
- The waveform seek bar renders from a cached image, so moving the playhead no longer redraws the
  whole bar. The spectrum analyzer and waveform math are vectorized.
- The visualizer caches scaled album art and per-bar colors (it used to rescale multi-megapixel
  art every frame).
- Album-art theme prefetch is limited to the next few tracks in play order (it used to decode art
  for the entire queue).
- Library search is debounced and filters in place, so selection and scroll position survive
  typing and folder rescans.

### Fixed
- Playlists no longer lose tracks whose files are temporarily unavailable (unplugged drive,
  renamed folder). Missing entries are kept grayed out, reported once in the status bar, skipped
  automatically during playback, and only removed with the new right-click "Remove Missing Tracks"
  action.
- The Lyrics panel now tells "no lyrics found" apart from "couldn't reach LRCLIB" (offline), shows
  a "Searching…" state while looking, and has a right-click Retry. A slow fetch can no longer show
  up on the wrong track.
- Drag-reordering a playlist now reorders what plays (rows and the play queue could get out of
  sync, playing the wrong track).
- Broken or unrenderable tracks show a status-bar message and skip ahead instead of failing
  silently (including missing openmpt123/zxtune123).
- The "Rendering…" status is cleared when a chiptune render finishes or fails.
- Deleting the currently playing file from disk stops or advances playback first.
- The oscilloscope trace no longer jitters (the playback position could momentarily read backwards
  between audio buffer updates).
- Adding a large folder (menu or drag-drop) no longer freezes the window while it scans.
- Missing library folders are reported instead of silently skipped. First library scans show live
  progress.

### Changed
- One keyboard map everywhere, so keys no longer change meaning depending on which panel has
  focus. New or changed: `S` stop, `R` cycle play mode (was `M` in the controls bar, and `M` is
  always mute), `E` toggle Enhance, `.` and `,` seek. Help, Keyboard Shortcuts now lists
  everything.
- Playlist: `Del` removes selected tracks, `Enter` plays the selection, and the filter box keeps its
  text when switching tabs.
- File dialogs remember their last folder across sessions.
