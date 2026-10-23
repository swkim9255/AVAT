from pathlib import Path

import cv2
import numpy as np
import pytest


@pytest.fixture()
def video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (320, 240))
    assert writer.isOpened()
    for index in range(30):
        writer.write(np.full((240, 320, 3), index * 5 % 255, dtype=np.uint8))
    writer.release()
    return path
