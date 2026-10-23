from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

W, H = 320, 240


def write_video(path: Path, frames: int = 30, fps: float = 10.0) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    for index in range(frames):
        writer.write(np.full((H, W, 3), index * 5 % 255, dtype=np.uint8))
    writer.release()
    return path


@pytest.fixture()
def video(tmp_path: Path) -> Path:
    return write_video(tmp_path / "clip.avi")
