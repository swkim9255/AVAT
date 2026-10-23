from __future__ import annotations

import cv2
import numpy as np
import pytest

from investigator.video_capture import open_video_capture

# 열기 옵션: FFmpeg 디코더 1스레드 + 하드웨어(GPU) 디코딩 사용 안 함
SINGLE_THREAD_SOFTWARE = [
    cv2.CAP_PROP_N_THREADS, 1, cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE,
]


class Capture:
    def __init__(self, opened: bool | Exception = True) -> None:
        self.opened = opened
        self.release_count = 0

    def isOpened(self) -> bool:
        if isinstance(self.opened, Exception):
            raise self.opened
        return self.opened

    def release(self) -> None:
        self.release_count += 1


def test_open_requests_ffmpeg_with_one_thread_at_construction(monkeypatch):
    capture = Capture()
    requested = []
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: requested.append(args) or capture)

    assert open_video_capture("video.mp4") is capture

    assert requested == [("video.mp4", cv2.CAP_FFMPEG, SINGLE_THREAD_SOFTWARE)]
    assert capture.release_count == 0


@pytest.mark.parametrize("opened", [False, RuntimeError("open failed"), ValueError("bad state")])
def test_unsuccessful_capture_is_released_without_unsafe_retry(monkeypatch, opened):
    capture = Capture(opened)
    requested = []
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: requested.append(args) or capture)

    with pytest.raises((RuntimeError, ValueError)):
        open_video_capture("broken.mp4")

    assert len(requested) == 1
    assert capture.release_count == 1


def test_unsupported_constructor_is_not_retried_without_parameters(monkeypatch):
    requested = []

    def construct(*args):
        requested.append(args)
        raise cv2.error("unsupported N_THREADS")

    monkeypatch.setattr(cv2, "VideoCapture", construct)

    with pytest.raises(cv2.error, match="unsupported N_THREADS"):
        open_video_capture("video.mp4")

    assert requested == [("video.mp4", cv2.CAP_FFMPEG, SINGLE_THREAD_SOFTWARE)]


def test_missing_thread_option_fails_before_opening(monkeypatch):
    requested = []
    monkeypatch.delattr(cv2, "CAP_PROP_N_THREADS")
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: requested.append(args))

    with pytest.raises(RuntimeError, match="single-threaded FFmpeg"):
        open_video_capture("video.mp4")

    assert not requested


def test_real_ffmpeg_capture_keeps_one_thread_during_repeated_seek(tmp_path):
    if not cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG):
        pytest.skip("This OpenCV build does not include FFmpeg.")
    path = tmp_path / "single-thread.avi"
    writer = cv2.VideoWriter(
        str(path), cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"MJPG"), 25.0, (64, 48),
    )
    if not writer.isOpened():
        writer.release()
        pytest.skip("This FFmpeg build cannot create the MJPEG test video.")
    try:
        for frame in range(20):
            writer.write(np.full((48, 64, 3), frame * 10, dtype=np.uint8))
    finally:
        writer.release()

    capture = open_video_capture(str(path))
    try:
        assert capture.getBackendName() == "FFMPEG"
        assert capture.get(cv2.CAP_PROP_N_THREADS) == 1
        for frame in [19, 0, 10, 4, 17, 1, 9, 3] * 8:
            assert capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, image = capture.read()
            assert ok and image is not None
            assert image.mean() == pytest.approx(frame * 10, abs=3)
    finally:
        capture.release()
    assert not capture.isOpened()
