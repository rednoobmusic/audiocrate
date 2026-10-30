AudioCrate @VERSION@ for Windows (preview)

This is the first Windows version of AudioCrate. It runs from any folder and installs nothing. It needs Windows 10 (22H2)
or Windows 11, 64 bit. It was built and tested on a Windows stand-in (Wine) and not yet on many real PCs, so some things
may not work. The list below says which, and a report of anything else is welcome.

Starting it

1. Unpack the zip somewhere you can write to (the Desktop or a USB stick). Keep the folders together.
2. Double click AudioCrate.exe.

The program is not signed, so Windows may show "Windows protected your PC". Choose "More info", then "Run anyway". A
virus scanner may look at the folder the first time and make the first start slow.

You can also start it with files, a folder or a web link after the name (AudioCrate.exe song.mp3), or drop files on
AudioCrate.exe. A second start while one is running hands its files to the first window.

Where your files go

portable.txt beside AudioCrate.exe makes the program keep everything in the data folder next to it: settings, the library,
playlists, logs and caches. Delete portable.txt to use %APPDATA%\AudioCrate and
%LOCALAPPDATA%\AudioCrate instead. Furnace modules (.fur) are played by the Furnace engine, which keeps its own settings and
log in %APPDATA%\furnace in both cases.

Milkdrop presets are not included. Put .milk files in data\presets, or in a folder called presets beside AudioCrate.exe.

What is in the folder

  AudioCrate.exe, _internal    the program with Python and Qt; leave them together
  libs                         the libraries for chiptune formats and the Milkdrop visualizer
  tools                        ffmpeg and ffprobe (decoding, video, converting), openmpt123 (MOD, XM, IT, S3M), zxtune123
  LICENSES                     licences of everything in the folder, and THIRD-PARTY.txt with versions and sources
  source                       the program's source code and the patches applied to the libraries

Sound plays through the Windows default output device.

Not finished for Windows yet

The Devices panel (USB drives, iPod, iPhone) and the Online panel (links and search; yt-dlp is not included) may show an
error or stay empty. The media keys and the lock screen controls are in, but nobody has tried them on a real PC yet. File
associations and the Start menu entry come with an installer later.

Reporting a problem

Help, Diagnostics, "Save report..." makes a zip with the log and the state of the program, with passwords removed. From a
Command Prompt in this folder, AudioCrate.exe --debug-dump (with the program running) writes the same state to data\debug and
prints where the files are; the text may appear after the prompt has come back. The log is data\audiocrate.log. If the program
stops at start, crash.log in the same folder may say why.
