"""Present AudioCrate to PulseAudio / PipeWire as its own application.

Every stream the app opens is labelled "python3" in pavucontrol, helvum and the
GNOME/KDE volume panels. Generic icon, and no way to tell it apart from any
other Python program. That is not Qt's doing. libpulse fills `application.name`
from the process binary (`/proc/self/exe`, which is the interpreter) whenever
the application has not set it itself, and Qt Multimedia never sets it. Per-app
volume is remembered against that name too, so the level you leave AudioCrate
at is inherited by the next Python program that plays anything.

Two channels are set, because which one is live depends on the Qt build:

    PIPEWIRE_PROPS       read by pw_stream_connect() in this process
                         (pipewire src/pipewire/stream.c). Live whenever Qt
                         uses its native PipeWire audio sink, which exists from
                         Qt 6.10 and is preferred over libpulse when the server
                         announces itself as PipeWire. On such a system this is
                         the channel that does the work, and it is applied
                         after Qt's own properties and after stream.rules, so
                         it wins.
    PULSE_PROP_OVERRIDE  read by libpulse in pa_init_proplist() and applied
                         with PA_UPDATE_REPLACE. Only reached when Qt falls
                         back to QPulseAudioSink, which is every Qt before 6.10
                         and any build without the pipewire feature. Inert
                         otherwise, since libpulse never builds a context.

Verified live rather than assumed: `application.version` appears on the stream,
and no Qt backend and no libpulse code path ever writes it. Only PIPEWIRE_PROPS
can put it there.

The two strings are built separately because the conventions differ. PulseAudio
quotes values and spells keys with underscores. PipeWire takes them bare, and
spells the icon key with a hyphen (PW_KEY_APP_ICON_NAME is
"application.icon-name"), which pipewire-pulse remaps to the underscore form on
the way out to pulse clients. That is why pactl shows an underscore for a key
set here with a hyphen.

Do not remove `application.id`. WirePlumber keys saved volume on it:
`Output/Audio:application.id:audiocrate` in
~/.local/state/wireplumber/stream-properties. Rename it and every saved level
is orphaned silently, since a missing entry falls back to a default rather than
erroring.

`media.role` is deliberately not set here, but not because it would do harm.
Qt's PipeWire sink sets it to "Music" on every stream it opens, so leaving it
out of the environment does not remove it. It also does not affect volume
restore: WirePlumber's formKey() only takes the media.role branch for the exact
string "Notification", and otherwise uses application.id. The
`module-stream-restore.id = sink-input-by-media-role:music` that pactl reports
is manufactured by pipewire-pulse at serialization time for any playback node
and is never read by WirePlumber.

Both variables are read when the client library initialises, on the first
QAudioSink/QAudioOutput, so apply() has to run before PySide6.QtMultimedia is
imported. main.py calls it first thing.

RESOURCE_NAME closes the same gap on X11: Qt fills the instance half of
WM_CLASS from argv[0] ("python3") unless that variable is set. The class half
already comes from applicationName, which is why StartupWMClass=AudioCrate in
audiocrate.desktop matches today. That half is left alone.

The app has five separate output paths (QMediaPlayer for video, and a QAudioSink
each for the DSP engine, Furnace, VGM and SID), so each one used to appear as
its own anonymous "python3" entry. Setting this at the process level names all
of them at once, without touching five call sites.
"""
from __future__ import annotations

import os

from _version import VERSION

APP_NAME = "AudioCrate"
APP_ID   = "audiocrate"   # matches audiocrate.desktop and the installed icon name

# Quoted values, underscore spelling. PulseAudio's conventions.
_PULSE_PROPS = " ".join((
    f"application.name='{APP_NAME}'",
    f"application.id='{APP_ID}'",
    f"application.icon_name='{APP_ID}'",
    f"application.version='{VERSION}'",
    # Factually this process is the interpreter, but the installed entry point
    # is `audiocrate` and this is the field mixers fall back to for a label.
    f"application.process.binary='{APP_ID}'",
))

# Bare values, hyphen spelling. PipeWire's conventions. This is the live channel
# on a Qt 6.10+ PipeWire build, so everything the mixer reads has to be here and
# not only in the PulseAudio string above.
_PIPEWIRE_PROPS = " ".join((
    f"application.name={APP_NAME}",
    f"application.id={APP_ID}",          # WirePlumber's volume-restore key
    f"application.version={VERSION}",
    # Hyphen, per PW_KEY_APP_ICON_NAME. pipewire-pulse remaps it to the
    # underscore form for pulse clients, so pactl shows application.icon_name.
    # Setting the underscore form here would pass through unremapped and be
    # invisible to anything reading the native graph.
    f"application.icon-name={APP_ID}",
    # PipeWire fills this from the process name (python3.x) only when absent.
    f"application.process.binary={APP_ID}",
))

_applied = False


def _prepend(var: str, value: str) -> None:
    """Put *value* at the front of env var *var*, keeping what is already there.

    Both proplist parsers apply assignments left to right and let the last one
    win, so putting ours first means anything the user set in their own
    environment still overrides it.
    """
    existing = os.environ.get(var, "").strip()
    os.environ[var] = f"{value} {existing}" if existing else value


def _merge_pipewire(value: str) -> str:
    """Combine *value* with an existing PIPEWIRE_PROPS inside one set of braces.

    PIPEWIRE_PROPS takes either a bare `key=value` list or a JSON object; two
    adjacent objects would not parse, so an existing braced value is unwrapped
    and folded in rather than concatenated. Ours goes first, so the user's
    entries win (see _prepend).
    """
    existing = os.environ.get("PIPEWIRE_PROPS", "").strip()
    if existing.startswith("{") and existing.endswith("}"):
        existing = existing[1:-1].strip()
    return "{ " + (f"{value} {existing}" if existing else value) + " }"


def apply() -> None:
    """Name this process to the audio server. Must run before QtMultimedia loads."""
    global _applied
    if _applied:          # prepending twice would just duplicate the entries
        return
    _applied = True

    _prepend("PULSE_PROP_OVERRIDE", _PULSE_PROPS)
    os.environ["PIPEWIRE_PROPS"] = _merge_pipewire(_PIPEWIRE_PROPS)
    os.environ.setdefault("RESOURCE_NAME", APP_ID)


def apply_to_qt(app) -> None:
    """Identity that only Qt can set, once a QApplication exists.

    setDesktopFileName is what ties the process to audiocrate.desktop: Wayland
    uses it as the app_id, and XDG portals and the desktop's own app matching
    read it to find the icon and name. It does not affect X11 WM_CLASS (Qt
    builds that from applicationName), so StartupWMClass keeps matching.
    """
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setApplicationVersion(VERSION)


if __name__ == "__main__":      # self-check: python3 core/audio_identity.py
    import re

    def _run(env):
        global _applied
        for _k in ("PULSE_PROP_OVERRIDE", "PIPEWIRE_PROPS", "RESOURCE_NAME"):
            os.environ.pop(_k, None)
        os.environ.update(env)
        _applied = False
        apply()

    # Clean environment: the keys mixers read, and no trace of the interpreter.
    _run({})
    _props = dict(re.findall(r"([\w.]+)='([^']*)'", os.environ["PULSE_PROP_OVERRIDE"]))
    assert _props["application.name"] == APP_NAME, _props
    assert _props["application.id"] == APP_ID, _props
    assert _props["application.icon_name"] == APP_ID, _props
    # Qt sets media.role itself, so ours would be redundant rather than wrong.
    # See the module docstring for why omitting it changes nothing either way.
    assert "media.role" not in _props, _props

    # The PipeWire string is the live channel on a Qt 6.10+ build, so the keys a
    # mixer reads have to be present there and not only in the PulseAudio one.
    _pw = os.environ["PIPEWIRE_PROPS"]
    for _k in ("application.name", "application.id", "application.version",
               "application.icon-name", "application.process.binary"):
        assert f"{_k}={APP_ID}" in _pw or f"{_k}=" in _pw, (_k, _pw)
    # Hyphen only. The underscore spelling is the pulse wire format and would
    # pass through the native graph unremapped.
    assert "application.icon_name" not in _pw, _pw
    assert "python" not in os.environ["PULSE_PROP_OVERRIDE"].lower()
    assert os.environ["RESOURCE_NAME"] == APP_ID

    # Calling apply() twice must not duplicate the entries.
    _before = os.environ["PULSE_PROP_OVERRIDE"]
    apply()
    assert os.environ["PULSE_PROP_OVERRIDE"] == _before

    # Anything the user set survives and still wins. Both parsers take the last
    # assignment, so ours going first is what makes theirs override it.
    _run({"PULSE_PROP_OVERRIDE": "application.name='Mine'",
          "PIPEWIRE_PROPS": "{ node.latency=256/48000 }"})
    _pulse = os.environ["PULSE_PROP_OVERRIDE"]
    assert _pulse.rindex("'Mine'") > _pulse.rindex(f"'{APP_NAME}'"), _pulse
    _pw = os.environ["PIPEWIRE_PROPS"]
    assert "node.latency=256/48000" in _pw, _pw
    assert _pw.count("{") == 1 and _pw.count("}") == 1, _pw   # two objects would not parse

    # PIPEWIRE_PROPS also takes a bare key=value list. Fold that in as well.
    _run({"PIPEWIRE_PROPS": "node.latency=256/48000"})
    assert os.environ["PIPEWIRE_PROPS"].startswith("{ application.name=")
    assert "node.latency=256/48000" in os.environ["PIPEWIRE_PROPS"]

    # RESOURCE_NAME is a plain default. A user value is left alone.
    _run({"RESOURCE_NAME": "custom"})
    assert os.environ["RESOURCE_NAME"] == "custom"

    print("audio_identity self-check OK")
