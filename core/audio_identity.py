"""Present AudioCrate to PulseAudio and PipeWire as its own application.

Without this every stream the app opens is labelled "python3" in pavucontrol, helvum and the GNOME and KDE
volume panels, with a generic icon. libpulse fills `application.name` from the process binary
(`/proc/self/exe`, the interpreter) when the application has not set it, and Qt Multimedia never sets it.
Per-app volume is remembered against that name too, so the level left on AudioCrate would be inherited by the
next Python program that plays anything.

Two channels are set, because which one is live depends on the Qt build:

    PIPEWIRE_PROPS       read by pw_stream_connect() in this process. Live when Qt uses its native PipeWire
                         audio sink (Qt 6.10 and later, preferred over libpulse when the server is PipeWire).
                         It is applied after Qt's own properties and after stream.rules, so it wins.
    PULSE_PROP_OVERRIDE  read by libpulse in pa_init_proplist() and applied with PA_UPDATE_REPLACE. Only
                         reached when Qt falls back to QPulseAudioSink (every Qt before 6.10, and builds
                         without the pipewire feature). Inert otherwise.

`application.version` shows on the stream, and no Qt backend or libpulse code path writes it, so only
PIPEWIRE_PROPS can have put it there.

The two strings are built separately because the conventions differ. PulseAudio quotes values and spells keys
with underscores. PipeWire takes them bare and spells the icon key with a hyphen (PW_KEY_APP_ICON_NAME is
"application.icon-name"); pipewire-pulse remaps it to the underscore form for pulse clients, which is why
pactl shows an underscore for a key set here with a hyphen.

Do not remove `application.id`. WirePlumber keys saved volume on it (`Output/Audio:application.id:audiocrate`
in ~/.local/state/wireplumber/stream-properties). Renaming it orphans every saved level without an error,
since a missing entry falls back to a default.

`media.role` is not set here. Qt's PipeWire sink sets it to "Music" on every stream anyway, and it does not
affect volume restore: WirePlumber's formKey() only takes the media.role branch for the exact string
"Notification" and otherwise uses application.id. The `module-stream-restore.id =
sink-input-by-media-role:music` that pactl reports is made up by pipewire-pulse when it serialises any playback
node, and WirePlumber never reads it.

Both variables are read when the client library initialises, on the first QAudioSink or QAudioOutput, so
apply() has to run before PySide6.QtMultimedia is imported. main.py calls it first thing.

RESOURCE_NAME does the same job on X11: Qt fills the instance half of WM_CLASS from argv[0] ("python3") unless
that variable is set. The class half comes from applicationName, which is why StartupWMClass=AudioCrate in
audiocrate.desktop matches. That half is left alone.

The app has five output paths (QMediaPlayer for video, and a QAudioSink each for the DSP engine, Furnace, VGM
and SID). Setting the names at process level covers all of them without touching five call sites.
"""
from __future__ import annotations

import os

from _version import VERSION

APP_NAME = "AudioCrate"
APP_ID   = "audiocrate"   # matches audiocrate.desktop and the installed icon name

# PulseAudio style: quoted values, underscore spelling.
_PULSE_PROPS = " ".join((
    f"application.name='{APP_NAME}'",
    f"application.id='{APP_ID}'",
    f"application.icon_name='{APP_ID}'",
    f"application.version='{VERSION}'",
    # The process is really the interpreter, but mixers fall back to this field
    # for a label and the installed entry point is `audiocrate`.
    f"application.process.binary='{APP_ID}'",
))

# PipeWire style: bare values, hyphen spelling. This is the live channel on a Qt 6.10+
# PipeWire build, so everything the mixer reads has to be here too.
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
    """Put *value* in front of env var *var*, keeping what is already there. Both proplist parsers let the
    last assignment win, so anything the user set themselves still overrides ours."""
    existing = os.environ.get(var, "").strip()
    os.environ[var] = f"{value} {existing}" if existing else value


def _merge_pipewire(value: str) -> str:
    """Combine *value* with an existing PIPEWIRE_PROPS inside one set of braces. It takes either a bare
    `key=value` list or a JSON object and two adjacent objects would not parse, so a braced value is unwrapped
    and folded in. Ours goes first, so the user's entries win (see _prepend)."""
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
    """The identity only Qt can set, once a QApplication exists. setDesktopFileName ties the process to
    audiocrate.desktop: Wayland uses it as the app_id, and XDG portals and the desktop's app matching read it for
    the icon and name. It does not touch X11 WM_CLASS (built from applicationName), so StartupWMClass still matches."""
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

    # clean environment: the keys mixers read, and no trace of the interpreter
    _run({})
    _props = dict(re.findall(r"([\w.]+)='([^']*)'", os.environ["PULSE_PROP_OVERRIDE"]))
    assert _props["application.name"] == APP_NAME, _props
    assert _props["application.id"] == APP_ID, _props
    assert _props["application.icon_name"] == APP_ID, _props
    # Qt sets media.role itself (see the module docstring)
    assert "media.role" not in _props, _props

    # the PipeWire string is the live channel on Qt 6.10+, so the mixer keys must be there too
    _pw = os.environ["PIPEWIRE_PROPS"]
    for _k in ("application.name", "application.id", "application.version",
               "application.icon-name", "application.process.binary"):
        assert f"{_k}={APP_ID}" in _pw or f"{_k}=" in _pw, (_k, _pw)
    # hyphen only: the underscore spelling would pass through the native graph unremapped
    assert "application.icon_name" not in _pw, _pw
    assert "python" not in os.environ["PULSE_PROP_OVERRIDE"].lower()
    assert os.environ["RESOURCE_NAME"] == APP_ID

    # a second apply() must not duplicate the entries
    _before = os.environ["PULSE_PROP_OVERRIDE"]
    apply()
    assert os.environ["PULSE_PROP_OVERRIDE"] == _before

    # a user value survives and still wins, because the parsers take the last assignment
    _run({"PULSE_PROP_OVERRIDE": "application.name='Mine'",
          "PIPEWIRE_PROPS": "{ node.latency=256/48000 }"})
    _pulse = os.environ["PULSE_PROP_OVERRIDE"]
    assert _pulse.rindex("'Mine'") > _pulse.rindex(f"'{APP_NAME}'"), _pulse
    _pw = os.environ["PIPEWIRE_PROPS"]
    assert "node.latency=256/48000" in _pw, _pw
    assert _pw.count("{") == 1 and _pw.count("}") == 1, _pw   # two objects would not parse

    # a bare key=value list is folded in as well
    _run({"PIPEWIRE_PROPS": "node.latency=256/48000"})
    assert os.environ["PIPEWIRE_PROPS"].startswith("{ application.name=")
    assert "node.latency=256/48000" in os.environ["PIPEWIRE_PROPS"]

    # RESOURCE_NAME is only a default
    _run({"RESOURCE_NAME": "custom"})
    assert os.environ["RESOURCE_NAME"] == "custom"

    print("audio_identity self-check OK")
