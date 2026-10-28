"""The built in DSP presets, as data: each one is a whole chain in the form DspChain.load_dict reads.

Setting keys are the Param keys of the module (see the module files); a key left out keeps the module's default.
"""
from __future__ import annotations

import copy


def _chain(*modules) -> dict:
    return {"version": 1, "modules": [{"id": i, "enabled": True, "settings": dict(s)} for i, s in modules]}


BUILTIN: dict[str, dict] = {
    "Headphone crossfeed": _chain(("crossfeed", {"strength": "Medium"})),
    "Late night": _chain(
        ("compressor", {"threshold": -26.0, "ratio": 3.5, "knee": 10.0, "attack": 8.0, "release": 250.0, "makeup": 5.0}),
        ("loudness", {"source": "Player volume", "reference": 85.0, "strength": 100.0, "max_lift": 8.0})),
    "Concert hall": _chain(
        ("reverb", {"room": 80.0, "damp": 40.0, "width": 100.0, "predelay": 25.0, "wet": 22.0, "dry": 100.0}),
        ("stereo_width", {"width": 125.0})),
    "Mono": _chain(("channel_mixer", {"mode": "Mono"})),
    "Bass and clarity": _chain(
        ("graphic_eq", {"bands": "10 band", "preset": "Bass boost"}),
        ("tone", {"bass_gain": 0.0, "treble_gain": 3.0})),
    "Gentle leveler": _chain(("leveler", {"strength": 40.0, "target": -18.0, "boost": 6.0, "release": 3.0})),
    "Radio": _chain(
        ("compressor", {"threshold": -24.0, "ratio": 6.0, "knee": 4.0, "attack": 3.0, "release": 120.0, "makeup": 6.0}),
        ("tone", {"bass_freq": 200.0, "bass_gain": -3.0, "treble_freq": 5000.0, "treble_gain": 3.0})),
    "Fade tracks": _chain(("fade", {"fade_in": 2.0, "fade_out": 4.0, "shape": "Cosine"})),
    "Flat": _chain(),
}


def names() -> list[str]:
    return list(BUILTIN)


def get(name: str) -> dict | None:
    """A copy of the preset's chain dict, or None."""
    d = BUILTIN.get(name)
    return copy.deepcopy(d) if d is not None else None
