# Windows build files

The Windows zip is a PyInstaller one-folder build of this source tree. These are the files it is made from.

| File | What it is |
| --- | --- |
| `audiocrate-win.spec` | The PyInstaller spec: one folder, no console window, no UPX, and a version resource taken from `core/_version.py` |
| `audiocrate.manifest` | The application manifest: runs as the user, Windows 10 and 11, long paths, UTF-8 |
| `make_ico.py` | Makes `audiocrate.ico` (16 to 256 px) from `icon.png` |
| `collect_licenses.py` | Collects the licence files of the bundled Python packages into a `LICENSES` folder |
| `THIRD-PARTY.txt`, `README-win.txt` | The texts that go into the zip. `@VERSION@` is replaced by the version |

To build by hand, copy the program tree, the spec, `audiocrate.ico` and the manifest into one folder and run `pyinstaller --noconfirm --clean audiocrate-win.spec` there, with Python 3.14 and `requirements.txt` installed.

The native libraries and helper programs are not part of the freeze. They sit beside `AudioCrate.exe`:

    libs\     libgme, libfurnace_wrap, libsid_wrap, projectM-4 and its playlist library
    tools\    ffmpeg.exe and ffprobe.exe with their DLLs, openmpt123.exe, zxtune123\

`libfurnace_wrap` and `libsid_wrap` are built with mingw-w64 from the sources in `_furnace_wrapper_src` and `_sid_wrapper_src`.

The release zips were made this way under Wine, and that is how the Windows version has been tested so far.
