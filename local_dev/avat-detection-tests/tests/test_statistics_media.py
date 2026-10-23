from __future__ import annotations

import os
from threading import Event

import cv2
import pytest

from investigator.statistics_media import probe_video_fps

# 열기 옵션: FFmpeg 디코더 1스레드 + 하드웨어(GPU) 디코딩 사용 안 함
SINGLE_THREAD_SOFTWARE = [
    cv2.CAP_PROP_N_THREADS, 1, cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE,
]


class MetadataCapture:
    def __init__(self, fps: float | Exception, *, opened: bool = True) -> None:
        self.fps = fps
        self.opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def get(self, prop: int) -> float:
        assert prop == cv2.CAP_PROP_FPS
        if isinstance(self.fps, Exception):
            raise self.fps
        return self.fps

    def release(self) -> None:
        self.released = True


def test_probe_reads_only_metadata_releases_and_preserves_input(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test metadata")
    capture = MetadataCapture(29.97)
    requested = []
    monkeypatch.setattr(
        cv2, "VideoCapture", lambda *args: requested.append(args) or capture,
    )
    original = {"unrelated": (1, 2, 24.0)}

    result = probe_video_fps([str(video), str(video)], original)

    key = str(video.resolve())
    assert requested == [(key, cv2.CAP_FFMPEG, SINGLE_THREAD_SOFTWARE)]
    assert result[key] == (video.stat().st_size, video.stat().st_mtime_ns, 29.97)
    assert result["unrelated"] == original["unrelated"]
    assert original == {"unrelated": (1, 2, 24.0)}
    assert capture.released


@pytest.mark.parametrize("fps", [0.0, -1.0, float("nan"), float("inf"), ValueError("bad")])
def test_unavailable_fps_stays_zero_and_is_cached(tmp_path, monkeypatch, fps):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    capture = MetadataCapture(fps)
    requested = []
    monkeypatch.setattr(
        cv2, "VideoCapture", lambda *args: requested.append(args) or capture,
    )

    result = probe_video_fps([str(video)], {})
    again = probe_video_fps([str(video)], result)

    assert result[str(video.resolve())][2] == 0.0
    assert again == result
    assert len(requested) == 1
    assert capture.released


def test_changed_file_is_reprobed_even_when_size_unchanged(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    captures = [MetadataCapture(0), MetadataCapture(60)]
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args: captures.pop(0))
    result = probe_video_fps([str(video)], {})
    previous = video.stat()
    os.utime(video, ns=(previous.st_atime_ns, previous.st_mtime_ns + 10_000_000))

    refreshed = probe_video_fps([str(video)], result)

    assert refreshed[str(video.resolve())][2] == 60
    assert result[str(video.resolve())][2] == 0


def test_cancel_stops_between_files_and_returns_partial_cache(tmp_path, monkeypatch):
    videos = [tmp_path / f"video-{n}.mp4" for n in range(2)]
    for video in videos:
        video.write_bytes(b"test")
    cancel = Event()
    first = MetadataCapture(25)
    requested = []

    def capture(*args):
        requested.append(args)
        cancel.set()
        return first

    monkeypatch.setattr(cv2, "VideoCapture", capture)
    result = probe_video_fps([str(video) for video in videos], {}, cancel)

    assert requested == [
        (str(videos[0].resolve()), cv2.CAP_FFMPEG, SINGLE_THREAD_SOFTWARE),
    ]
    assert set(result) == {str(videos[0].resolve())}
    assert first.released
    assert probe_video_fps([str(videos[1])], {}, cancel) == {}


def test_missing_or_unopenable_files_do_not_get_assumed_fps(tmp_path, monkeypatch):
    missing = tmp_path / "missing.mp4"
    video = tmp_path / "invalid.mp4"
    video.write_bytes(b"invalid")
    capture = MetadataCapture(30, opened=False)
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args: capture)

    result = probe_video_fps([str(missing), str(video)], {})

    assert result[str(missing.resolve())] == (0, 0, 0.0)
    assert result[str(video.resolve())][2] == 0.0
    assert capture.released


def test_newly_created_missing_file_is_probed(tmp_path, monkeypatch):
    video = tmp_path / "later.mp4"
    result = probe_video_fps([str(video)], {})
    video.write_bytes(b"test")
    capture = MetadataCapture(24)
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args: capture)

    refreshed = probe_video_fps([str(video)], result)

    assert refreshed[str(video.resolve())][2] == 24
    assert capture.released


def test_unsupported_single_thread_decoder_is_cached_without_retry(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    requested = []

    def capture(*args):
        requested.append(args)
        raise cv2.error("unsupported N_THREADS")

    monkeypatch.setattr(cv2, "VideoCapture", capture)
    result = probe_video_fps([str(video)], {})

    assert result[str(video.resolve())][2] == 0.0
    assert probe_video_fps([str(video)], result) == result
    assert requested == [
        (str(video.resolve()), cv2.CAP_FFMPEG, SINGLE_THREAD_SOFTWARE),
    ]
