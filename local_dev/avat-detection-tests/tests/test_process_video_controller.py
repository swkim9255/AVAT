from __future__ import annotations

import os
from collections import deque

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QColor, QImage

import investigator.video_controller as controller_module
from investigator.video_controller import PlaybackState, VideoController


def frame_image(frame: int, width: int = 12, height: int = 8) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(QColor(frame % 256, 0, 0))
    return image


class ControlledDecoder(QObject):
    """A transport that completes only when the test explicitly allows it."""

    frame_decoded = Signal(QImage, int, int, float)
    end_of_stream = Signal(int)
    request_failed = Signal(int, str)
    failed = Signal(str)
    instances: list[ControlledDecoder] = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.requests = []
        self.pending = deque()
        self.closed = False
        self.threads = None
        self.pid = 90000 + len(self.instances)
        self.instances.append(self)

    def open(self, path, threads=0):
        self.path = path
        self.threads = threads
        return {
            "reported_fps": 25.0, "fps": 25.0, "total_frames": 200,
            "width": 12, "height": 8, "threads": 4 if threads == 0 else threads,
        }, frame_image(0)

    def request(self, request_id, frame, *, playback=False):
        assert not self.closed
        item = request_id, frame, playback
        self.requests.append(item)
        self.pending.append(item)
        return True

    def complete(self):
        request, frame, _ = self.pending.popleft()
        self.frame_decoded.emit(frame_image(frame), request, frame, 20.0)

    def close(self):
        self.closed = True


@pytest.fixture
def controlled_controller(tmp_path, monkeypatch, qtbot):
    monkeypatch.setattr(controller_module, "ProcessDecoder", ControlledDecoder)
    monkeypatch.setattr(ControlledDecoder, "instances", [])
    path = tmp_path / "controlled.mp4"
    path.touch()
    controller = VideoController()
    assert controller.open_video(path)
    try:
        yield controller, ControlledDecoder.instances[0], path
    finally:
        controller.close_video()


def test_burst_of_100_drag_targets_retains_only_latest_pending(controlled_controller):
    controller, decoder, _ = controlled_controller
    published = []
    controller.frame_ready.connect(lambda _image, frame, _time: published.append(frame))

    for frame in range(1, 101):
        assert controller.seek(frame)
    assert [request[1] for request in decoder.requests] == [1]
    assert controller.requested_frame == 100
    assert controller.is_seeking

    decoder.complete()
    assert [request[1] for request in decoder.requests] == [1, 100]
    decoder.complete()
    assert controller.current_frame == 100
    assert not controller.is_seeking
    assert len(published) <= 2
    assert len(decoder.requests) == 2


def test_relative_keyboard_requests_accumulate_against_pending_target(controlled_controller):
    controller, decoder, _ = controlled_controller
    for _ in range(12):
        controller.next_frame()
    assert controller.requested_frame == 12
    assert len(decoder.requests) == 1
    decoder.complete()
    assert decoder.requests[-1][1] == 12
    decoder.complete()
    assert controller.current_frame == 12


def test_cache_hit_is_immediate_and_older_native_reply_cannot_roll_it_back(controlled_controller):
    controller, decoder, _ = controlled_controller
    controller.seek(10)
    decoder.complete()
    assert controller.current_frame == 10

    controller.seek(80)
    assert controller.is_seeking
    controller.seek(10)
    assert controller.current_frame == 10
    assert not controller.is_seeking
    assert len(decoder.requests) == 2
    decoder.complete()
    assert controller.current_frame == 10
    assert controller.current_image().pixelColor(0, 0).red() == 10
    assert 80 in controller._cache


def test_cached_target_discards_pending_drag_but_not_shared_buffer_handshake(controlled_controller):
    controller, decoder, _ = controlled_controller
    controller.seek(50)
    controller.seek(90)
    controller.seek(0)
    assert controller.current_frame == 0
    assert not controller.is_seeking
    decoder.complete()
    assert controller.current_frame == 0
    assert len(decoder.requests) == 1
    assert controller._inflight is None


def test_retained_frames_are_bounded_by_64_mib_even_for_large_images(controlled_controller):
    controller, _, _ = controlled_controller
    # Five 16 MiB frames exceed the cache's retained-byte budget. QImage shares
    # pixels here; this tests eviction without allocating five giant arrays.
    large = QImage(2048, 2048, QImage.Format.Format_RGB32)
    large.fill(QColor("black"))
    for frame in range(10, 15):
        controller._remember_frame(frame, large)
    assert controller._cache_bytes <= 64 * 1024 * 1024
    assert sum(image.sizeInBytes() for image in controller._cache.values()) == controller._cache_bytes
    assert len(controller._cache) == 4
    assert list(controller._cache) == [11, 12, 13, 14]


def test_single_oversized_frame_is_not_retained(controlled_controller):
    controller, _, _ = controlled_controller
    oversized = QImage(4097, 4097, QImage.Format.Format_RGB32)
    controller._remember_frame(10, oversized)
    assert 10 not in controller._cache
    assert controller._cache_bytes <= 64 * 1024 * 1024


def test_seek_requests_leave_event_loop_responsive_while_decode_is_pending(controlled_controller, qtbot):
    controller, decoder, _ = controlled_controller
    ticks = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(controller.current_frame))
    timer.start()
    try:
        for frame in range(1, 101):
            controller.seek(frame)
        qtbot.waitUntil(lambda: len(ticks) >= 5, timeout=1000)
        assert controller.current_frame == 0
        assert len(decoder.requests) == 1
        assert controller.is_seeking
    finally:
        timer.stop()


def test_pause_freezes_display_when_playback_frame_is_in_flight(controlled_controller):
    controller, decoder, _ = controlled_controller
    controller.play()
    controller._next_for_playback()
    assert decoder.requests[-1][1:] == (1, True)
    controller.pause()
    decoder.complete()
    assert controller.current_frame == 0
    assert controller.state is PlaybackState.PAUSED
    assert not controller._timer.isActive()


def test_play_waits_for_pending_manual_seek_then_resumes_at_requested_frame(controlled_controller):
    controller, decoder, _ = controlled_controller
    controller.seek(40)
    controller.play()
    assert not controller._timer.isActive()
    decoder.complete()
    assert controller.current_frame == 40
    assert controller.is_playing
    assert controller._timer.isActive()


def test_play_from_last_frame_does_not_overwrite_pending_seek_with_rewind(controlled_controller):
    controller, decoder, _ = controlled_controller
    controller.seek(199)
    decoder.complete()
    controller.seek(60)
    controller.play()
    assert controller.requested_frame == 60
    assert [item[1] for item in decoder.requests] == [199, 60]
    decoder.complete()
    assert controller.current_frame == 60
    assert controller.is_playing
    assert controller._timer.isActive()


def test_close_during_recovery_still_emits_video_closed(controlled_controller, qtbot):
    controller, decoder, _ = controlled_controller
    closed = []
    controller.video_closed.connect(lambda: closed.append(True))
    decoder.failed.emit("simulated crash")
    assert controller._decoder is None
    controller.close_video()
    qtbot.wait(10)
    assert closed == [True]
    assert controller._metadata is None
    assert not controller.is_open


def test_close_ignores_inflight_reply_and_clears_transient_state(controlled_controller):
    controller, decoder, _ = controlled_controller
    controller.seek(40)
    request_id = decoder.requests[-1][0]
    controller.close_video()
    # Test the guard directly: the QObject can already be queued for deletion,
    # but a queued transport callback must still be harmless.
    controller._process_frame(decoder, frame_image(40), request_id, 40)
    assert controller.current_frame == -1
    assert not controller.is_open
    assert not controller.is_seeking
    assert controller._inflight is None
    assert controller._pending_seek is None
    assert controller._cache_bytes == 0
    assert decoder.closed


def test_video_switch_cannot_publish_a_reply_from_previous_decoder(controlled_controller, tmp_path):
    controller, decoder, _ = controlled_controller
    controller.seek(70)
    request_id = decoder.requests[-1][0]
    replacement = tmp_path / "replacement.mp4"
    replacement.touch()
    assert controller.open_video(replacement)
    assert controller._decoder is not decoder
    controller._process_frame(decoder, frame_image(70), request_id, 70)
    assert controller.metadata.path == replacement.resolve()
    assert controller.current_frame == 0
    assert controller.current_image().pixelColor(0, 0).red() == 0


def test_recovery_callback_cannot_reopen_closed_or_replaced_video(controlled_controller, qtbot):
    controller, decoder, _ = controlled_controller
    decoder.failed.emit("simulated crash")
    controller.close_video()
    qtbot.wait(20)
    assert not controller.is_open
    assert len(ControlledDecoder.instances) == 1


def test_repeated_crashes_retry_parallel_then_single_thread_then_stop(controlled_controller, qtbot):
    controller, decoder, path = controlled_controller
    errors = []
    controller.error_occurred.connect(errors.append)
    assert decoder.threads == 0

    decoder.failed.emit("first crash")
    qtbot.waitUntil(lambda: controller._decoder is not None, timeout=1000)
    second = controller._decoder
    assert second is not decoder
    assert second.threads == 0

    second.failed.emit("second crash")
    qtbot.waitUntil(lambda: controller._decoder is not None, timeout=1000)
    third = controller._decoder
    assert third.threads == 1
    assert path.resolve() in controller._safe_files

    third.failed.emit("third crash")
    qtbot.wait(20)
    assert controller._decoder is None
    assert len(ControlledDecoder.instances) == 3
    assert controller._crash_counts[path.resolve()] == 3
    assert controller.state is PlaybackState.PAUSED
    assert errors


@pytest.fixture
def real_video(tmp_path):
    if not cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG):
        pytest.skip("This OpenCV build does not include FFmpeg.")
    path = tmp_path / "process-controller.mp4"
    writer = cv2.VideoWriter(
        str(path), cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"mp4v"), 25.0, (64, 48),
    )
    if not writer.isOpened():
        writer.release()
        pytest.skip("This FFmpeg build cannot create the MPEG-4 test video.")
    try:
        for frame in range(120):
            writer.write(np.full((48, 64, 3), frame * 2, dtype=np.uint8))
    finally:
        writer.release()
    return path


def test_real_parallel_decoder_lives_in_child_and_publishes_exact_seek_frames(real_video, qtbot):
    controller = VideoController()
    try:
        assert controller.open_video(real_video)
        assert controller.decoder_pid and controller.decoder_pid != os.getpid()
        assert controller.decoder_threads >= 1
        if (os.cpu_count() or 1) > 1:
            assert controller.decoder_threads > 1
        assert controller.metadata.total_frames == 120
        for frame in [119, 0, 60, 14, 15, 97, 5]:
            assert controller.seek(frame)
            qtbot.waitUntil(lambda target=frame: controller.current_frame == target and not controller.is_seeking, timeout=5000)
            color = controller.current_image().pixelColor(0, 0)
            # MPEG-4's RGB/YUV conversion is lossy, unlike protocol frame IDs.
            assert color.red() == pytest.approx(frame * 2, abs=5)
            assert color.green() == pytest.approx(frame * 2, abs=5)
            assert color.blue() == pytest.approx(frame * 2, abs=5)
    finally:
        controller.close_video()


def test_real_native_process_termination_is_bounded_and_does_not_kill_parent(real_video, qtbot):
    controller = VideoController()
    try:
        assert controller.open_video(real_video)
        original_pid = controller.decoder_pid
        controller._decoder.process.kill()
        qtbot.waitUntil(lambda: controller.decoder_pid is not None and controller.decoder_pid != original_pid, timeout=7000)
        assert controller.decoder_threads >= 1
        if (os.cpu_count() or 1) > 1:
            assert controller.decoder_threads > 1

        second_pid = controller.decoder_pid
        controller._decoder.process.kill()
        qtbot.waitUntil(lambda: controller.decoder_pid is not None and controller.decoder_pid != second_pid, timeout=7000)
        assert controller.decoder_threads == 1

        controller._decoder.process.kill()
        qtbot.waitUntil(lambda: controller._decoder is None, timeout=3000)
        qtbot.wait(30)
        assert controller._crash_counts[real_video.resolve()] == 3
        assert controller.state is PlaybackState.PAUSED
        assert controller.current_frame == 0
    finally:
        controller.close_video()
