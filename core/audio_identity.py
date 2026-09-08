"""Present AudioCrate to PulseAudio / PipeWire as its own application.

Every stream the app opens is labelled "python3" in pavucontrol, helvum and the
GNOME/KDE volume panels — generic icon, no way to tell it apart from any other
Python program. That is not Qt's doing. libpulse fills `application.name` from
the process binary (`/proc/self/exe` → the interpreter) whenever the
application has not set it itself, and Qt Multimedia never sets it. Per-app
volume is remembered against that name too, so the level you leave AudioCrate
at is inherited by the next Python program that plays anything.

Two channels have to be told, because Qt can reach the server either way:

    PULSE_PROP_OVERRIDE  read by libpulse in pa_init_proplist() and applied
                         with PA_UPDATE_REPLACE, so it wins whatever else sets
                         the same key. This is the path that matters in
                         practice: Qt Multimedia uses its PulseAudio backend on
                         Linux, talking either to real PulseAudio or to
                         pipewire-pulse.
    PIPEWIRE_PROPS       merged into the property dict of every stream by
                         pw_stream_new(), covering a native libpipewire path.

The two strings are built separately rather than shared because the conventions
differ: PulseAudio quotes values and spells keys with underscores, PipeWire
takes them bare and hyphenated.

Deliberately absent from both: `media.role`. Setting it looks right for a music
player, but PulseAudio's stream-restore keys saved volume on the role before it
looks at the application, so every app tagged `music` — Chromium among them —
would share one remembered volume with us. Leaving it off keys volume on
`application.id` instead, which is the whole point of naming ourselves. A
routing rule that wants this app can match `application.id = audiocrate`, which
is more specific than a role anyway.

Both variables are read when the client library initialises, on the first
QAudioSink/QAudioOutput — so apply() has to run before PySide6.QtMultimedia is
imported. main.py calls it first thing.

RESOURCE_NAME closes the same gap on X11: Qt fills the instance half of
WM_CLASS from argv[0] ("python3") unless that variable is set. The class half
already comes from applicationName, which is why StartupWMClass=AudioCrate in
audiocrate.desktop matches today — that half is left alone.

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

# Quoted values, underscore spelling — PulseAudio's conventions.
_PULSE_PROPS = " ".join((
    f"application.name='{APP_NAME}'",
    f"application.id='{APP_ID}'",
    f"application.icon_name='{APP_ID}'",
    f"application.version='{VERSION}'",
    # Factually this process is the interpreter, but the installed entry point
    # is `audiocrate` and this is the field mixers fall back to for a label.
    f"application.process.binary='{APP_ID}'",
))

# Bare values, hyphen spelling — PipeWire's conventions.
# No icon key here: PipeWire documents `media.icon-name` for media objects but
# nothing equivalent for applications, and pipewire-pulse already carries the
# PulseAudio one above, which is the path Qt actually takes.
_PIPEWIRE_PROPS = " ".join((
    f"application.name={APP_NAME}",
    f"application.id={APP_ID}",
    f"application.version={VERSION}",
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
    # media.role must stay off: stream-restore keys saved volume on the role
    # ahead of the application, so setting it shares our volume with every other
    # app tagged the same way. See the module docstring.
    assert "media.role" not in _props, _props
    assert "python" not in os.environ["PULSE_PROP_OVERRIDE"].lower()
    assert os.environ["RESOURCE_NAME"] == APP_ID

    # Calling apply() twice must not duplicate the entries.
    _before = os.environ["PULSE_PROP_OVERRIDE"]
    apply()
    assert os.environ["PULSE_PROP_OVERRIDE"] == _before

    # Anything the user set survives and still wins — both parsers take the last
    # assignment, so ours going first is what makes theirs override it.
    _run({"PULSE_PROP_OVERRIDE": "application.name='Mine'",
          "PIPEWIRE_PROPS": "{ node.latency=256/48000 }"})
    _pulse = os.environ["PULSE_PROP_OVERRIDE"]
    assert _pulse.rindex("'Mine'") > _pulse.rindex(f"'{APP_NAME}'"), _pulse
    _pw = os.environ["PIPEWIRE_PROPS"]
    assert "node.latency=256/48000" in _pw, _pw
    assert _pw.count("{") == 1 and _pw.count("}") == 1, _pw   # two objects would not parse

    # PIPEWIRE_PROPS also takes a bare key=value list; fold that in as well.
    _run({"PIPEWIRE_PROPS": "node.latency=256/48000"})
    assert os.environ["PIPEWIRE_PROPS"].startswith("{ application.name=")
    assert "node.latency=256/48000" in os.environ["PIPEWIRE_PROPS"]

    # RESOURCE_NAME is a plain default — a user value is left alone.
    _run({"RESOURCE_NAME": "custom"})
    assert os.environ["RESOURCE_NAME"] == "custom"

    print("audio_identity self-check OK")
