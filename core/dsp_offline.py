"""The player's sound stage for work that runs beside playback, such as the converter.

A stage here is a private copy: its own ParametricEQ, its own Enhancer and its own DspChain with fresh module
instances, all built from the engine's settings at the moment a file starts. Nothing is shared with the engine,
so it is safe on a worker thread while music plays, and several can run at once.
"""
from __future__ import annotations

import numpy as np

from dsp_base import Context
from dsp_chain import Enhancer, ParametricEQ, high_ratio
from dsp_user_chain import DspChain


class OfflineStage:
    """process(block) takes a (n, 2) float32 block of consecutive audio at fs and returns the same. The volume
    slider is not applied. `ratio` is the whole-track measurement Enhance adapts to, as the engine takes it from the
    first minute of a decoded track; without one Enhance uses its middle brightness setting."""

    def __init__(self, engine, fs: int = 44100, total: int = 0, ratio: float | None = None):
        self.fs, self.total, self.pos = int(fs), int(total), 0
        self.eq = ParametricEQ(self.fs)
        try:
            self.eq.load_dict(engine.user_eq.to_dict())
        except Exception:
            self.eq = ParametricEQ(self.fs)
        self.enh = Enhancer(self.fs)
        self.enh.set_enabled(engine.enhance_enabled)
        self.enh.set_adaptive(0.4 if ratio is None else ratio)
        self.chain = None
        src = getattr(engine, "dsp_chain", None)
        if src is not None:
            try:
                self.chain = DspChain(fs=self.fs)
                self.chain.load_dict(src.to_dict())
                self.chain._clock = lambda: 0.0    # the overload guard watches real time, a conversion is not in real time
                self.chain.reset()                 # a stream that starts after a reset starts at full effect, as in the engine
            except Exception:
                self.chain = None

    def process(self, block: np.ndarray) -> np.ndarray:
        x = np.asarray(block, dtype=np.float32)
        y = self.eq.process(x)
        ch = self.chain
        if ch is not None and ch.active:
            y = ch.process(y, Context(self.fs, self.pos, self.total, 1.0))
        self.pos += len(x)
        y = self.enh.process(y)
        return np.clip(y, -1.0, 1.0).astype(np.float32, copy=False)

    def finish(self) -> np.ndarray:
        """What the chain still holds when the file has ended (echoes ringing out, look-ahead), run through Enhance, as
        one (n, 2) block. Empty when the chain has nothing to give."""
        ch = self.chain
        flush = getattr(ch, "flush", None) if ch is not None and ch.active else None
        tail = np.asarray(flush() if flush else [], dtype=np.float32).reshape(-1, 2)
        if not len(tail):
            return tail
        return np.clip(self.enh.process(tail), -1.0, 1.0).astype(np.float32, copy=False)


def offline_stage(engine, path, fs: int = 44100) -> OfflineStage:
    """A stage for converting the file at path, which is decoded at fs. The length comes from ffprobe so a fade module
    knows where the end is; a file that cannot be probed gets 0, which leaves position based modules alone. Enhance is
    set from the first minute of the file, the way the engine does it for a track it decodes."""
    total, ratio = 0, None
    try:
        import converter
        pr = converter.probe(path)
        total = int(float(pr["duration"]) * fs) if pr and pr.get("duration") else 0
        if engine.enhance_enabled:
            head = converter.head_audio(path, fs, 60)
            if len(head):
                ratio = high_ratio((head[:, 0] + head[:, 1]) * 0.5)
    except Exception:
        pass
    return OfflineStage(engine, fs, total, ratio)
