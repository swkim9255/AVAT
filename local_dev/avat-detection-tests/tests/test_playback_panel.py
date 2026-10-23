import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleFactory, QStyleOptionSlider

from investigator.theme import PRIMARY_HOVER
from investigator.widgets.playback_panel import (
    COLLISION_HOVER,
    MarkerSlider,
    PlaybackPanel,
    format_timestamp,
)


def rendered_color(slider: MarkerSlider, point: QPoint) -> QColor:
    slider.repaint()
    application = QApplication.instance()
    assert application is not None
    application.processEvents()
    return slider.grab().toImage().pixelColor(point)


def set_slider_style(slider: MarkerSlider, name: str) -> None:
    style = QStyleFactory.create(name)
    assert style is not None
    # QWidget does not own setStyle()'s argument. Give each test style the
    # slider's lifetime instead of relying on temporary Python wrapper refs.
    style.setParent(slider)
    slider.setStyle(style)


def test_timestamp_matches_wireframe_precision() -> None:
    assert format_timestamp(32.45) == "00:32.45"
    assert format_timestamp(84.20) == "01:24.20"
    assert format_timestamp(3661.239) == "01:01:01.24"


def test_time_buttons_request_one_second_in_each_direction(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(120, 30.0)
    requested: list[float] = []
    panel.relative_seconds_requested.connect(requested.append)
    assert panel.back_1s.toolTip() == "1초 뒤로 이동"
    assert panel.next_1s.toolTip() == "1초 앞으로 이동"
    panel.back_1s.click()
    panel.next_1s.click()
    assert requested == [-1.0, 1.0]


def test_drag_seeks_immediately_follow_every_position(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    qtbot.addWidget(slider)
    slider.setRange(0, 999)
    requested = []
    slider.seek_requested.connect(requested.append)
    for frame in range(100):
        slider.sliderMoved.emit(frame)
        assert requested[-1] == frame
    assert requested == list(range(100))
    for frame in (200, 300, 400):
        slider.sliderMoved.emit(frame)
        assert requested[-1] == frame
    slider.sliderReleased.emit()
    expected = [*range(100), 200, 300, 400]
    assert requested == expected
    qtbot.wait(100)
    assert requested == expected


def test_unknown_frame_count_grows_with_observed_position(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(0, 20.0)
    panel.set_position(42)
    assert panel.timeline.maximum() >= 42
    assert panel.frame_input.maximum() >= 43
    assert panel.frame_input.value() == 43
    assert panel.frame_total.text() == "/ ?"
    assert panel.time_label.text() == "00:02.10 / --:--.--"


def test_requested_position_updates_controls_before_decoding(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(1000, 25.0)
    panel.set_position(50)

    panel.set_requested_position(750)
    assert panel.timeline.value() == 750
    assert panel.frame_input.value() == 751
    assert panel.time_label.text() == "00:30.00 / 00:39.96"
    assert panel._frame == 50  # Requested position is not a decoded frame.

    panel.set_position(300)
    assert panel.timeline.value() == 750
    assert panel.frame_input.value() == 751
    assert panel.time_label.text() == "00:30.00 / 00:39.96"

    panel.set_position(750)
    panel.set_position(751)
    assert panel.timeline.value() == 751
    assert panel.frame_input.value() == 752


def test_newest_seek_target_stays_visible_when_old_result_arrives(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(1000, 25.0)
    panel.set_requested_position(300)
    panel.set_requested_position(800)
    panel.set_position(300)
    assert panel.timeline.value() == 800
    assert panel.frame_input.value() == 801
    panel.set_position(800)
    assert panel._requested_frame is None


def test_decoded_position_does_not_move_thumb_during_drag(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(1000, 25.0)
    panel.timeline.setSliderDown(True)
    panel.timeline.setSliderPosition(500)
    panel.set_position(100)
    assert panel.timeline.sliderPosition() == 500
    assert panel._frame == 100
    panel.timeline.setSliderDown(False)


def test_cancelled_seek_restores_last_decoded_position(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(1000, 25.0)
    panel.set_position(100)
    panel.set_requested_position(800)
    panel.clear_pending_seek()
    assert panel.timeline.value() == 100
    assert panel.frame_input.value() == 101
    assert panel._requested_frame is None


def test_changing_video_clears_pending_seek(qtbot) -> None:  # type: ignore[no-untyped-def]
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.set_video(1000, 25.0)
    panel.set_requested_position(800)
    panel.clear_video()
    assert panel._requested_frame is None
    assert panel.frame_input.value() == 1
    panel.set_video(500, 25.0)
    panel.set_requested_position(400)
    panel.set_video(100, 25.0)
    assert panel._requested_frame is None
    assert panel.timeline.value() == 0
    assert panel.frame_input.value() == 1


def test_timeline_click_at_right_edge_reaches_last_frame(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    slider.resize(300, 56)
    slider.setRange(0, 3599)
    qtbot.addWidget(slider)
    slider.show()
    qtbot.mouseClick(
        slider,
        Qt.MouseButton.LeftButton,
        pos=QPoint(slider.width() - 1, slider.height() // 2),
    )
    assert slider.value() == 3599


def test_groove_release_keeps_final_pointer_position_without_last_move(qtbot) -> None:
    slider = MarkerSlider()
    qtbot.addWidget(slider)
    slider.resize(600, 56)
    slider.setRange(0, 999)
    slider.show()
    requested = []
    slider.seek_requested.connect(requested.append)
    press = QPoint(slider._x_for_value(250), slider.height() // 2)
    release = QPoint(slider._x_for_value(750), slider.height() // 2)
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=press)
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=release)
    assert requested == [slider._value_at_position(press), slider._value_at_position(release)]
    assert slider.value() == requested[-1]
    assert not slider.isSliderDown()
    assert not slider._groove_dragging


def test_cancelled_groove_drag_does_not_resume_on_mouse_move(qtbot) -> None:
    slider = MarkerSlider()
    qtbot.addWidget(slider)
    slider.resize(600, 56)
    slider.setRange(0, 999)
    slider.show()
    requested = []
    slider.seek_requested.connect(requested.append)
    point = QPoint(slider._x_for_value(250), slider.height() // 2)
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=point)
    before = slider.value()
    slider.setSliderDown(False)
    QTest.mouseMove(slider, QPoint(slider._x_for_value(750), slider.height() // 2), 1)
    assert slider.value() == before
    assert requested == [before]
    assert not slider._groove_dragging
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point)


@pytest.mark.parametrize("inverted", [False, True])
@pytest.mark.parametrize("start_frame, targets", [(250, (450, 750)), (750, (550, 150))])
@pytest.mark.parametrize("style_name", QStyleFactory.keys())
def test_groove_press_immediately_starts_continuous_drag(
    qtbot, inverted: bool, start_frame: int, targets: tuple[int, int], style_name: str
) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    set_slider_style(slider, style_name)
    slider.resize(600, 56)
    slider.setRange(0, 999)
    slider.setInvertedAppearance(inverted)
    slider.setValue(0)
    requested: list[int] = []
    slider.seek_requested.connect(requested.append)
    qtbot.addWidget(slider)
    slider.show()

    def frame_point(frame: int) -> QPoint:
        return QPoint(slider._x_for_value(frame), slider.height() // 2)

    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=frame_point(start_frame))
    assert slider.isSliderDown()
    assert abs(requested[-1] - start_frame) <= 1
    for frame in targets:
        QTest.mouseMove(slider, frame_point(frame), 1)
        assert abs(slider.value() - frame) <= 1
        assert abs(requested[-1] - frame) <= 1
    final_target = requested[-1]
    before_release = requested.copy()
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=frame_point(targets[-1]))
    assert not slider.isSliderDown()
    assert requested == before_release
    assert requested.count(final_target) == 1
    assert len(requested) == 3


def test_right_edge_press_can_drag_back_without_second_click(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    set_slider_style(slider, "Fusion")
    slider.resize(600, 56)
    slider.setRange(0, 999)
    requested: list[int] = []
    slider.seek_requested.connect(requested.append)
    qtbot.addWidget(slider)
    slider.show()
    edge = QPoint(slider.width() - 1, slider.height() // 2)
    target = QPoint(slider._x_for_value(500), slider.height() // 2)

    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=edge)
    assert slider.value() == 999
    assert slider.isSliderDown()
    QTest.mouseMove(slider, target, 1)
    assert abs(slider.value() - 500) <= 1
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=target)
    assert len(requested) == 2
    assert not slider.isSliderDown()


def test_native_handle_drag_keeps_the_original_grab_offset(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    set_slider_style(slider, "Fusion")
    slider.resize(600, 56)
    slider.setRange(0, 999)
    slider.setValue(400)
    qtbot.addWidget(slider)
    slider.show()
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, slider
    )
    press = QPoint(handle.right() - 1, handle.center().y())
    target = press + QPoint(100, 0)
    requested: list[int] = []
    slider.seek_requested.connect(requested.append)

    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=press)
    assert slider.value() == 400
    assert slider.isSliderDown()
    QTest.mouseMove(slider, target, 1)
    _, span, upside_down = slider._slider_metrics()
    expected = QStyle.sliderValueFromPosition(
        slider.minimum(), slider.maximum(),
        QStyle.sliderPositionFromValue(0, 999, 400, span, upside_down) + 100,
        span, upside_down,
    )
    assert slider.value() == expected
    assert requested == [expected]
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=target)
    assert requested == [expected]


@pytest.mark.parametrize("kind", ["trigger", "collision", "speed"])
def test_marker_press_and_hold_remains_a_single_exact_seek(qtbot, kind: str) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    set_slider_style(slider, "Fusion")
    slider.resize(600, 56)
    slider.setRange(0, 999)
    slider.set_markers(200, 500, [800])
    qtbot.addWidget(slider)
    slider.show()
    frame = {"trigger": 200, "collision": 500, "speed": 800}[kind]
    y = {"trigger": 14, "collision": slider.height() - 13, "speed": 8}[kind]
    press = QPoint(slider._x_for_value(frame), y)
    target = QPoint(slider._x_for_value(950), slider.height() // 2)
    requested: list[int] = []
    slider.seek_requested.connect(requested.append)

    QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=press)
    assert not slider.isSliderDown()
    QTest.mouseMove(slider, target, 1)
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=target)
    assert requested == [frame]
    assert slider.value() == frame
    assert (slider.trigger_frame, slider.collision_frame, slider.speed_frames) == (200, 500, [800])


def test_timeline_marker_labels_seek_to_their_exact_frames(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    slider.resize(400, 56)
    slider.setRange(0, 999)
    slider.set_markers(137, 731)
    requested: list[int] = []
    slider.seek_requested.connect(requested.append)
    qtbot.addWidget(slider)
    slider.show()

    # Click inside each label, far enough from its exact x-coordinate that the
    # ordinary slider mapping would seek to a neighboring frame.
    qtbot.mouseClick(
        slider,
        Qt.MouseButton.LeftButton,
        pos=QPoint(slider._x_for_value(137) + 18, 8),
    )
    assert requested[-1] == 137
    assert slider.value() == 137

    qtbot.mouseClick(
        slider,
        Qt.MouseButton.LeftButton,
        pos=QPoint(slider._x_for_value(731) + 18, slider.height() - 14),
    )
    assert requested[-1] == 731
    assert slider.value() == 731


def test_marker_hover_changes_color_cursor_and_tooltip(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    slider.resize(400, 56)
    slider.setRange(0, 999)
    slider.set_markers(137, 731)
    qtbot.addWidget(slider)
    slider.show()

    trigger_point = QPoint(slider._x_for_value(137), 14)
    collision_point = QPoint(slider._x_for_value(731), slider.height() - 12)
    neutral_point = QPoint(slider.width() // 2, slider.height() // 2)

    normal_trigger_color = rendered_color(slider, trigger_point)
    QTest.mouseMove(slider, trigger_point, 1)
    qtbot.waitUntil(lambda: slider.hovered_marker == "trigger")
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert slider.toolTip() == "T 트리거 · 138번 프레임 (클릭하여 이동)"
    assert slider._marker_color("trigger") == QColor(PRIMARY_HOVER)
    assert rendered_color(slider, trigger_point) != normal_trigger_color

    normal_collision_color = slider._marker_color("collision")
    QTest.mouseMove(slider, collision_point, 1)
    qtbot.waitUntil(lambda: slider.hovered_marker == "collision")
    assert slider.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert slider.toolTip() == "C 충돌 · 732번 프레임 (클릭하여 이동)"
    assert slider._marker_color("collision") == QColor(COLLISION_HOVER)
    assert slider._marker_color("collision") != normal_collision_color

    QTest.mouseMove(slider, neutral_point, 1)
    qtbot.waitUntil(lambda: slider.hovered_marker is None)
    assert slider.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert slider.toolTip() == ""


def test_marker_interaction_shapes_are_square_and_centered(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    slider.resize(400, 56)
    slider.setRange(0, 999)
    slider.set_markers(137, 731)
    qtbot.addWidget(slider)
    slider.show()

    trigger_rect = slider._trigger_interaction_rect()
    collision_rect = slider._collision_interaction_rect()
    assert trigger_rect is not None
    assert collision_rect is not None
    assert trigger_rect.width() == trigger_rect.height() == 25
    assert collision_rect.width() == collision_rect.height() == 25
    assert trigger_rect.center() == QPoint(slider._x_for_value(137), 14)
    assert collision_rect.center() == QPoint(slider._x_for_value(731), slider.height() - 13)


def test_non_marker_click_keeps_normal_timeline_seek(qtbot) -> None:  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    slider.resize(400, 56)
    slider.setRange(0, 999)
    slider.set_markers(137, 731)
    requested: list[int] = []
    slider.seek_requested.connect(requested.append)
    qtbot.addWidget(slider)
    slider.show()

    point = QPoint(slider._x_for_value(137) + 18, slider.height() // 2)
    start, span, upside_down = slider._slider_metrics()
    slider_position = max(0, min(span, point.x() - start))
    expected = QStyle.sliderValueFromPosition(
        slider.minimum(), slider.maximum(), slider_position, span, upside_down
    )
    assert expected != 137

    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=point)
    assert requested == [expected]
    assert slider.value() == expected
