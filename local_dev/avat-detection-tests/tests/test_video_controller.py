from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from investigator.video_controller import DEFAULT_FPS, PlaybackState, VideoController, VideoMetadata


class FakeCapture:
    def __init__(self, frames: list[np.ndarray], fps: float = 20.0) -> None:
        self.frames = frames
        self.fps = fps
        self.position = 0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802
        return True

    def read(self):  # type: ignore[no-untyped-def]
        if self.position >= len(self.frames):
            return False, None
        frame = self.frames[self.position]
        self.position += 1
        return True, frame.copy()

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(len(self.frames))
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop != cv2.CAP_PROP_POS_FRAMES:
            return False
        self.position = int(value)
        return True

    def release(self) -> None:
        self.released = True


def test_open_seek_and_relative_seconds_are_frame_exact(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"fake")
    frames = [np.full((4, 6, 3), index, dtype=np.uint8) for index in range(120)]
    capture = FakeCapture(frames, fps=20.0)
    controller = VideoController(capture_factory=lambda _: capture)

    with qtbot.waitSignal(controller.video_opened):
        assert controller.open_video(path)
    assert controller.current_frame == 0
    assert controller.total_frames == 120
    assert controller.seek_relative(10)
    assert controller.current_frame == 10
    assert controller.seek_seconds(5)
    assert controller.current_frame == 110
    assert controller.seek_seconds(5)
    assert controller.current_frame == 119
    assert controller.seek_relative(-500)
    assert controller.current_frame == 0
    controller.close_video()
    assert capture.released
    assert controller.state is PlaybackState.EMPTY


def test_failed_open_keeps_existing_video(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    good = tmp_path / "good.mp4"
    good.write_bytes(b"good")
    capture = FakeCapture([np.zeros((2, 3, 3), dtype=np.uint8)])
    controller = VideoController(capture_factory=lambda _: capture)
    assert controller.open_video(good)
    assert not controller.open_video(tmp_path / "missing.mp4")
    assert controller.is_open
    assert controller.metadata and controller.metadata.path == good


@pytest.mark.parametrize("reported_fps", [0.0, -1.0, float("nan"), float("inf")])
def test_missing_fps_preserves_playback_fallback_without_reporting_it_as_actual(
    qtbot, tmp_path: Path, reported_fps: float
) -> None:
    path = tmp_path / "unknown-fps.mp4"
    path.write_bytes(b"test")
    capture = FakeCapture([np.zeros((2, 3, 3), dtype=np.uint8)], fps=reported_fps)
    controller = VideoController(capture_factory=lambda _: capture)
    try:
        assert controller.open_video(path)
        assert controller.metadata is not None
        assert controller.fps == DEFAULT_FPS
        assert controller.metadata.fps == DEFAULT_FPS
        assert controller.metadata.reported_fps == 0.0
        assert controller.metadata.duration_seconds == 1 / DEFAULT_FPS
        assert controller._timer.interval() == round(1000 / DEFAULT_FPS)
    finally:
        controller.close_video()
    assert capture.released


def test_reported_fps_matches_actual_metadata_and_survives_observed_length(
    qtbot, tmp_path: Path
) -> None:
    class UnknownLengthCapture(FakeCapture):
        def get(self, prop: int) -> float:
            return 0.0 if prop == cv2.CAP_PROP_FRAME_COUNT else super().get(prop)

    path = tmp_path / "unknown-length.mp4"
    path.write_bytes(b"test")
    capture = UnknownLengthCapture([np.zeros((2, 3, 3), dtype=np.uint8)], fps=29.97)
    controller = VideoController(capture_factory=lambda _: capture)
    try:
        assert controller.open_video(path)
        assert controller.metadata is not None
        assert controller.metadata.reported_fps == 29.97
        assert controller.metadata.fps == 29.97
        assert controller.metadata.total_frames == 0
        assert controller.play()
        controller._next_for_playback()
        assert controller.metadata.total_frames == 1
        assert controller.metadata.reported_fps == 29.97
        assert controller.metadata.fps == 29.97
    finally:
        controller.close_video()


def test_video_metadata_retains_five_argument_constructor_compatibility() -> None:
    metadata = VideoMetadata(Path("legacy.mp4"), 24, 48, 1920, 1080)
    assert metadata.duration_seconds == 2.0
    assert metadata.reported_fps == 0.0


class RecordingCapture(FakeCapture):
    def __init__(self, frames):
        super().__init__(frames)
        self.seeks = []
        self.reads = 0

    def set(self, prop, value):
        self.seeks.append(int(value))
        return super().set(prop, value)

    def read(self):
        self.reads += 1
        return super().read()


def numbered_frames(count=30):
    return [np.full((4, 6, 3), index, dtype=np.uint8) for index in range(count)]


def test_injected_capture_keeps_synchronous_open_and_frame_exact_seek(qtbot, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    capture = RecordingCapture(numbered_frames())
    requested = []
    def factory(path):
        requested.append(path)
        return capture

    controller = VideoController(capture_factory=factory)
    assert controller.open_video(video)
    assert requested == [str(video.resolve())]
    assert controller.current_frame == 0
    assert controller.seek(7)
    assert controller.current_frame == 7
    assert capture.seeks == [7]
    controller.close_video()
    assert capture.released


def test_forward_one_frame_reads_sequentially_without_repeated_seek_flushes(qtbot, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    capture = RecordingCapture(numbered_frames())
    controller = VideoController(capture_factory=lambda _: capture)
    try:
        assert controller.open_video(video)
        for frame in range(1, 10):
            assert controller.next_frame()
            assert controller.current_frame == frame
            assert controller.current_image().pixelColor(0, 0).red() == frame
        assert capture.seeks == []
        assert controller.seek(20)
        assert controller.seek(17)
        assert controller.seek(17)
        assert controller.seek(18)
        assert capture.seeks == [20, 17]
        assert controller.current_image().pixelColor(0, 0).red() == 18
    finally:
        controller.close_video()


def test_failed_seek_read_invalidates_position_and_realigns_before_resuming(qtbot, tmp_path):
    class BrokenRead(RecordingCapture):
        def read(self):
            if self.position == 20:
                self.position = 25
                raise RuntimeError("decoder moved before failing")
            return super().read()

    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    capture = BrokenRead(numbered_frames())
    controller = VideoController(capture_factory=lambda _: capture)
    errors = []
    controller.error_occurred.connect(lambda message: errors.append((controller._timer.isActive(), controller.state)))
    try:
        assert controller.open_video(video)
        assert controller.seek(2)
        assert controller.play()
        assert not controller.seek(20)
        assert errors == [(False, PlaybackState.PAUSED)]
        assert controller.current_frame == 2
        assert controller._next_decode_frame is None
        assert controller.play()
        controller._next_for_playback()
        assert capture.seeks == [2, 20, 3]
        assert controller.current_frame == 3
        assert controller.current_image().pixelColor(0, 0).red() == 3
    finally:
        controller.close_video()


def test_failed_native_set_does_not_leave_a_trusted_decoder_position(qtbot, tmp_path):
    class BrokenSeek(RecordingCapture):
        def set(self, prop, value):
            if value == 20:
                self.position = 25
                return False
            return super().set(prop, value)

    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    capture = BrokenSeek(numbered_frames())
    controller = VideoController(capture_factory=lambda _: capture)
    try:
        assert controller.open_video(video)
        assert controller.seek(2)
        assert not controller.seek(20)
        assert controller.seek(2)
        assert capture.seeks == [2, 2]
        assert controller.next_frame()
        assert controller.current_image().pixelColor(0, 0).red() == 3
    finally:
        controller.close_video()


def test_playback_error_stops_timer_before_error_callbacks_can_reenter(qtbot, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    frames = [numbered_frames(1)[0], np.zeros((4, 6, 2), dtype=np.uint8), numbered_frames(1)[0]]
    capture = RecordingCapture(frames)
    controller = VideoController(capture_factory=lambda _: capture)
    seen = []

    def on_error(message):
        seen.append((controller._timer.isActive(), controller.state))
        controller._next_for_playback()

    controller.error_occurred.connect(on_error)
    try:
        assert controller.open_video(video)
        assert controller.play()
        controller._next_for_playback()
        assert seen == [(False, PlaybackState.STOPPED)]
        assert capture.reads == 2
    finally:
        controller.close_video()


@pytest.mark.parametrize("disabled", ["1", "true", "yes", "on"])
def test_audio_diagnostic_switch_prevents_creating_second_decoder(qtbot, tmp_path, monkeypatch, disabled):
    monkeypatch.setenv("AVAT_DISABLE_AUDIO", disabled)
    monkeypatch.setattr("investigator.video_controller.AudioPlayback", lambda *args: pytest.fail("Qt decoder created with audio disabled"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    capture = RecordingCapture(numbered_frames())
    controller = VideoController(capture_factory=lambda _: capture)
    try:
        assert controller.open_video(video)
        assert controller._audio is None
        assert controller.play()
        assert controller.seek(10)
        assert controller.pause()
    finally:
        controller.close_video()
