"""Stand-in for ``python -m investigator.detection_worker`` with a fake SAM 3 backend.

Used by GUI tests as the configured "검출용 Python" via a small shell wrapper.
"""

from __future__ import annotations

import sys

import numpy as np

from investigator.detection_worker import __main__ as cli
from investigator.detection_worker.backend import RawInstance


class FakeBackend:
    """'car' moves right 2 px per call; 'tree' stays; 'wall' overlaps 'building wall'."""

    device = "cpu"
    model_id = "fake/sam3"

    def __init__(self, on_call=None):  # type: ignore[no-untyped-def]
        self.calls = 0
        self.on_call = on_call

    @staticmethod
    def _mask(shape, x, y, w, h):  # type: ignore[no-untyped-def]
        mask = np.zeros(shape[:2], dtype=bool)
        mask[y : y + h, x : x + w] = True
        return mask

    def predict(self, frame_rgb, prompts, threshold, mask_threshold):  # type: ignore[no-untyped-def]
        shift = self.calls * 2
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        shape = frame_rgb.shape
        out = {prompt: [] for prompt in prompts}
        if "car" in out:
            out["car"] = [RawInstance(self._mask(shape, 20 + shift, 40, 60, 40), 0.9)]
        if "tree" in out:
            out["tree"] = [RawInstance(self._mask(shape, 200, 20, 30, 150), 0.8)]
        if "building wall" in out:
            out["building wall"] = [RawInstance(self._mask(shape, 240, 20, 70, 150), 0.8)]
        if "wall" in out:
            out["wall"] = [RawInstance(self._mask(shape, 240, 20, 70, 150), 0.6)]
        if "guardrail" in out:
            out["guardrail"] = [RawInstance(self._mask(shape, 0, 200, 300, 20), 0.1)]
        return out


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:2] == ["-m", "investigator.detection_worker"]:
        argv = argv[2:]
    sys.exit(cli.main(argv, backend=FakeBackend()))
