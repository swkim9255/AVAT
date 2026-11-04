"""Main-window navigation against the real isolated, parallel decoder."""

from __future__ import annotations

import os

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, QProcess, Qt
from PySide6.QtTest import QTest

from investigator.main_window import MainWindow


@pytest.fixture
def real_window(qtbot, tmp_path, monkeypatch):
    if not cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG):
        pytest.skip("This OpenCV build does not include FFmpeg.")
    monkeypatch.setenv("AVAT_DISABLE_AUDIO", "1")
    media = tmp_path / "media"
    media.mkdir()
    video = media / "1234567890123.mp4"
    writer = cv2.VideoWriter(
        str(video), cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"mp4v"), 25.0, (96, 64),
    )
    if not writer.isOpened():
        writer.release()
        pytest.skip("This FFmpeg build cannot create the MPEG-4 test video.")
    try:
        for number in range(120):
            frame = np.full((64, 96, 3), number * 2, dtype=np.uint8)
            cv2.putText(frame, str(number), (10, 45), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (255, 255, 255), 1)
            writer.write(frame)
    finally:
        writer.release()

    window = MainWindow(tmp_path / "project")
    qtbot.addWidget(window)
    try:
        window.show()
        window.attach_videos([str(video)])
        assert window.current_case is not None
        assert window.video_controller.is_open
        assert window.video_controller.decoder_pid != os.getpid()
        assert window.video_controller.decoder_pid is not None
        assert window.video_controller.total_frames == 120
        yield window
    finally:
        # keyClick(..., ControlModifier) sends modified key events, not a
        # physical Ctrl release. Clear Qt's cached modifier state so subsequent
        # selection/drag tests do not inherit an apparent held Ctrl key.
        QTest.keyRelease(window, Qt.Key.Key_Control)
        decoder = window.video_controller._decoder
        process = decoder.process if decoder is not None else None
        assert window.close()
        assert not window.video_controller.is_open
        if process is not None:
            assert process.state() is QProcess.ProcessState.NotRunning


def assert_decoded_frame(window, frame, qtbot):
    controller = window.video_controller
    qtbot.waitUntil(
        lambda: controller.current_frame == frame and not controller.is_seeking,
        timeout=3000,
    )
    assert window.playback.timeline.value() == frame
    assert window.playback.frame_input.value() == frame + 1
    image = window.video_player.canvas._source_image
    assert image is not None
    # The corner encodes frame identity separately from the visible number.
    color = image.pixelColor(4, 4)
    assert color.red() == pytest.approx(frame * 2, abs=5)
    assert color.green() == pytest.approx(frame * 2, abs=5)
    assert color.blue() == pytest.approx(frame * 2, abs=5)


@pytest.mark.parametrize("press_frame", [0, 55])
def test_real_pointer_drag_updates_requested_controls_then_exact_image(real_window, qtbot, press_frame):
    window = real_window
    controller = window.video_controller
    slider = window.playback.timeline
    observations = []
    controller.seek_requested_position.connect(
        lambda frame: observations.append(
            (frame, controller.current_frame, slider.value(), window.playback.frame_input.value())
        )
    )
    qtbot.mousePress(
        slider, Qt.MouseButton.LeftButton,
        pos=QPoint(slider._x_for_value(press_frame), slider.height() // 2),
    )
    for frame in (20, 40, 65, 85, 110):
        QTest.mouseMove(slider, QPoint(slider._x_for_value(frame), slider.height() // 2), 1)
    qtbot.mouseRelease(
        slider, Qt.MouseButton.LeftButton,
        pos=QPoint(slider._x_for_value(110), slider.height() // 2),
    )
    assert len(observations) >= 3
    assert all(target == thumb and target + 1 == value
               for target, _decoded, thumb, value in observations)
    assert any(target != decoded for target, decoded, _thumb, _value in observations)
    target = observations[-1][0]
    assert target == pytest.approx(110, abs=1)
    assert_decoded_frame(window, target, qtbot)

    qtbot.mouseClick(window.playback.trigger_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.playback.collision_button, Qt.MouseButton.LeftButton)
    assert window.current_case.trigger_frame == target
    assert window.current_case.collision_frame == target
    for intermediate, marker_y in ((10, 14), (20, slider.height() - 13)):
        assert controller.seek(intermediate)
        assert_decoded_frame(window, intermediate, qtbot)
        qtbot.mouseClick(
            slider, Qt.MouseButton.LeftButton,
            pos=QPoint(slider._x_for_value(target), marker_y),
        )
        assert controller.requested_frame == target
        assert_decoded_frame(window, target, qtbot)


def test_real_keyboard_burst_accumulates_pending_target(real_window, qtbot):
    window = real_window
    controller = window.video_controller
    window.video_player.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    window.video_player.canvas.setFocus()
    for _ in range(30):
        QTest.keyClick(window, Qt.Key.Key_Right)
    assert controller.requested_frame == 30
    assert window.playback.timeline.value() == 30
    assert window.playback.frame_input.value() == 31
    assert_decoded_frame(window, 30, qtbot)

    for _ in range(3):
        QTest.keyClick(window, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert controller.requested_frame == 60
    assert_decoded_frame(window, 60, qtbot)
