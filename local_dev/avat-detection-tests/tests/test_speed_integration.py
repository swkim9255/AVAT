from __future__ import annotations

import csv
import json

import cv2
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QDoubleSpinBox, QMessageBox

from investigator.form_definition import FormDefinition
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.speed_estimation import SpeedPoint, estimate_speed
from investigator.storage import CaseRepository
from investigator.theme import apply_theme
from investigator.widgets.playback_panel import MarkerSlider
from investigator.widgets.speed_panel import SpeedPanel
from tests.test_ui_smoke import _Capture


class SpeedCapture(_Capture):
    def __init__(self, fps=30.0):
        super().__init__(opened=True, total_frames=300)
        self.actual_fps = fps

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return self.actual_fps
        return super().get(prop)


@pytest.fixture
def speed_window(qtbot, qapp, monkeypatch, tmp_path):
    apply_theme(qapp)
    window = MainWindow(tmp_path / 'project')
    # Synthetic frames are supplied explicitly; subprocess decoders cannot
    # inherit an in-process OpenCV monkeypatch or decode this placeholder file.
    window.video_controller._capture_factory = lambda *_: SpeedCapture()
    qtbot.addWidget(window)
    window.show()
    video = tmp_path / '2026100200003.mp4'
    video.write_bytes(b'fake video supplied by capture fixture')
    window.attach_videos([str(video)])
    return window


def measure(window):
    for frame in (30, 55, 82, 111, 142):
        window.video_controller.seek(frame)
        window.toggle_speed_point()
        if frame != 30:
            window.set_speed_distance(frame, 8)
    window.video_controller.seek(126)
    window.set_trigger_marker()


def test_reference_measurement_shows_table_chart_and_manual_apply(speed_window):
    window = speed_window
    measure(window)
    assert window.speed_panel.isVisible()
    assert window.playback.timeline.speed_frames == [30, 55, 82, 111, 142]
    assert window.speed_panel.table.rowCount() == 5
    assert window.speed_panel.table.item(0, 1).text() == '31'
    assert window.speed_panel._estimate.kph == pytest.approx(27.8309316187)
    assert '28 km/h' in window.speed_panel.result_label.text()
    assert window.current_case.values.get('trigger_vehicle_speed') is None
    window.speed_panel.apply_button.click()
    assert window.current_case.values['trigger_vehicle_speed'] == '저속(10-30kph)'
    assert window.current_case.values['trigger_vehicle_speed_source'] == '사고영상'


def test_typing_distance_updates_model_and_speed_without_enter(speed_window, qtbot):
    window = speed_window
    for frame in (30, 55):
        window.video_controller.seek(frame)
        window.toggle_speed_point()
    window.video_controller.seek(40)
    window.set_trigger_marker()
    editor = window.speed_panel.table.cellWidget(1, 3)
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, '8')

    assert window.current_case.speed_points == [SpeedPoint(30), SpeedPoint(55, 8)]
    assert window.speed_panel.table.cellWidget(1, 3) is editor
    assert editor.hasFocus()
    assert editor.hasFrame()  # The reference frame must not overwrite Qt's bool property.
    assert window.speed_panel._estimate.kph == pytest.approx(34.56)
    assert window.speed_panel.table.item(1, 4).text() != '-'
    assert window.speed_panel.chart._estimate.kph == pytest.approx(34.56)
    assert window.speed_panel.apply_button.isEnabled()

    editor.selectAll()
    qtbot.keyClicks(editor, '12.5')
    assert window.current_case.speed_points[1].distance_m == 12.5
    assert window.speed_panel._estimate.kph == pytest.approx(54)
    editor.selectAll()
    qtbot.keyClicks(editor, '0')
    assert window.current_case.speed_points[1].distance_m is None
    assert window.speed_panel._estimate.kph is None


def test_adding_points_keeps_typed_distances_and_persists_them(speed_window, qtbot):
    window = speed_window
    for frame in (30, 55):
        window.video_controller.seek(frame)
        window.toggle_speed_point()
    editor = window.speed_panel.table.cellWidget(1, 3)
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, '8.5')
    window.video_controller.seek(82)
    qtbot.mouseClick(window.speed_panel.add_button, Qt.MouseButton.LeftButton)

    assert window.current_case.speed_points == [
        SpeedPoint(30), SpeedPoint(55, 8.5), SpeedPoint(82, 8.5),
    ]
    assert window.speed_panel.table.cellWidget(1, 3).value() == 8.5
    editor = window.speed_panel.table.cellWidget(2, 3)
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, '12')
    window.video_controller.seek(111)
    qtbot.mouseClick(window.speed_panel.add_button, Qt.MouseButton.LeftButton)
    expected = [SpeedPoint(30), SpeedPoint(55, 8.5), SpeedPoint(82, 12), SpeedPoint(111, 12)]
    assert window.current_case.speed_points == expected
    assert window._save(show_message=False)
    assert CaseRepository(window.repository.workspace).load()[0].speed_points == expected


def test_removed_distance_editor_cannot_update_the_remaining_point(speed_window):
    window = speed_window
    measure(window)
    old_editor = window.speed_panel.table.cellWidget(1, 3)
    window.remove_speed_point(55)
    before = window.current_case.speed_points
    window.speed_panel._distance_edited(old_editor, 99)
    assert window.current_case.speed_points == before


def test_inserting_point_only_invalidates_the_split_segment(speed_window):
    window = speed_window
    measure(window)
    window.video_controller.seek(95)
    window.toggle_speed_point()
    assert window.current_case.speed_points == [
        SpeedPoint(30), SpeedPoint(55, 8), SpeedPoint(82, 8),
        SpeedPoint(95, 8), SpeedPoint(111), SpeedPoint(142, 8),
    ]
    assert '5번' in window.statusBar().currentMessage()
    assert '다시 입력' in window.statusBar().currentMessage()


def test_speed_points_persist_per_video_and_across_projects(speed_window, tmp_path):
    window = speed_window
    measure(window)
    first = window.current_case.speed_points
    first_workspace = window.repository.workspace
    second_video = tmp_path / 'another.mp4'
    second_video.write_bytes(b'fake')
    window.attach_videos([str(second_video)])
    window.select_video(window.current_uid, 1)
    assert window.current_case.speed_points == []
    window.video_controller.seek(20)
    window.toggle_speed_point()
    window.video_controller.seek(50)
    window.toggle_speed_point()
    window.set_speed_distance(50, 5)
    window.select_video(window.current_uid, 0)
    assert window.current_case.speed_points == first
    assert window._save(show_message=False)
    restored = CaseRepository(first_workspace).load()[0]
    assert restored.speed_points == first
    other_project = tmp_path / 'other_project'
    other_project.mkdir()
    assert window.open_project(other_project)
    assert window.speed_panel.table.rowCount() == 0
    assert window.switch_project(0)
    assert window.current_case.speed_points == first
    assert window.speed_panel.table.rowCount() == 5


def test_real_fps_is_required_not_playback_fallback(speed_window):
    window = speed_window
    path = window.current_case.active_video
    window.video_controller._capture_factory = lambda *_: SpeedCapture(0)
    assert window.video_controller.open_video(path)
    assert window.video_controller.fps == 30
    measure(window)
    assert window.speed_panel._estimate.kph is None
    assert not window.speed_panel.apply_button.isEnabled()
    assert 'FPS' in window.speed_panel.warning_label.text()


def test_applying_over_edr_requires_confirmation_and_corrects_source(speed_window, monkeypatch):
    window = speed_window
    measure(window)
    window._field_changed('trigger_vehicle_speed_source', 'EDR')
    window._field_changed('trigger_vehicle_speed', '중속(30-60kph)')
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.No)
    window.speed_panel.apply_button.click()
    assert window.current_case.values['trigger_vehicle_speed_source'] == 'EDR'
    assert window.current_case.values['trigger_vehicle_speed'] == '중속(30-60kph)'
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Yes)
    window.speed_panel.apply_button.click()
    assert window.current_case.values['trigger_vehicle_speed_source'] == '사고영상'
    assert window.current_case.values['trigger_vehicle_speed'] == '저속(10-30kph)'


@pytest.mark.parametrize('trigger_frame,has_warnings', [(126, False), (0, True)])
def test_apply_dialog_only_mentions_warnings_when_present(
    speed_window, monkeypatch, trigger_frame, has_warnings,
):
    window = speed_window
    measure(window)
    if window.current_case.trigger_frame != trigger_frame:
        window.video_controller.seek(trigger_frame)
        window.set_trigger_marker()
    assert bool(window.speed_panel._estimate.warnings) is has_warnings
    window._field_changed('trigger_vehicle_speed_source', 'EDR')
    messages = []

    def question(_parent, _title, message, *_args):
        messages.append(message)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, 'question', question)
    window.speed_panel.apply_button.click()
    assert len(messages) == 1
    assert ('경고가 있습니다.' in messages[0]) is has_warnings
    assert ('측정 지점을 먼저 확인' in messages[0]) is has_warnings
    assert '경계' not in messages[0]
    assert window.current_case.values['trigger_vehicle_speed_source'] == 'EDR'


def test_apply_uses_customized_stored_choice_values(speed_window):
    window = speed_window
    definition = FormDefinition.from_dict(window.repository.form_definition.to_dict())
    replacements = {
        'trigger_vehicle_speed': ('저속(10-30kph)', 'video_low'),
        'trigger_vehicle_speed_source': ('사고영상', 'video_source'),
    }
    for record in definition.records():
        if record['key'] in replacements:
            old, new = replacements[record['key']]
            for choice in record['choices']:
                if choice['value'] == old:
                    choice['value'] = new
    assert window.apply_form_definition(definition)
    measure(window)
    window.speed_panel.apply_button.click()
    assert window.current_case.values['trigger_vehicle_speed'] == 'video_low'
    assert window.current_case.values['trigger_vehicle_speed_source'] == 'video_source'


def test_distance_editor_focus_does_not_leak_between_video_contexts(qtbot):
    panel = SpeedPanel()
    qtbot.addWidget(panel)
    first = [SpeedPoint(0), SpeedPoint(30, 8)]
    panel.set_data(first, estimate_speed(first, 30, 15), 30, context_key=('a', 'video'))
    panel.show()
    old_editor = panel.table.cellWidget(1, 3)
    old_editor.setFocus()
    qtbot.wait(10)
    second = [SpeedPoint(0), SpeedPoint(30, 3)]
    panel.set_data(second, estimate_speed(second, 30, 15), 30, context_key=('b', 'video'))
    assert panel.table.cellWidget(1, 3).value() == 3
    values = []
    panel.distance_changed.connect(lambda *args: values.append(args))
    panel._distance_edited(old_editor, 99)
    assert values == []


def test_marker_and_table_seek_keep_normal_timeline_clicks(speed_window, qtbot):
    window = speed_window
    measure(window)
    slider = window.playback.timeline
    point = QPoint(slider._x_for_value(82), 6)
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=point)
    assert window.video_controller.current_frame == 82
    window.speed_panel.table.cellClicked.emit(1, 1)
    assert window.video_controller.current_frame == 55
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() - 1, slider.height() // 2))
    assert window.video_controller.current_frame == 299


def test_m_shortcut_is_ignored_inside_distance_inputs_and_statistics(speed_window):
    window = speed_window
    measure(window)
    editor = window.speed_panel.table.cellWidget(1, 3)
    assert isinstance(editor, QDoubleSpinBox)
    editor.setFocus()
    count = len(window.current_case.speed_points)
    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_M, Qt.KeyboardModifier.NoModifier))
    assert len(window.current_case.speed_points) == count
    window.mode_tabs.setCurrentIndex(1)
    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_M, Qt.KeyboardModifier.NoModifier))
    assert len(window.current_case.speed_points) == count


@pytest.mark.parametrize('width,height', [(1180, 760), (1660, 1040)])
def test_speed_panel_responsive_without_expanding_window(speed_window, qtbot, width, height):
    window = speed_window
    measure(window)
    window.resize(width, height)
    qtbot.wait(20)
    assert window.size().toTuple() == (width, height)
    assert window.speed_scroll.horizontalScrollBar().maximum() == 0
    assert window.playback.height() < 300
    for button in (window.playback.trigger_button, window.playback.speed_point_button, window.playback.speed_panel_button):
        point = button.mapTo(window.playback, button.rect().bottomRight())
        assert 0 <= point.x() < window.playback.width()
        assert 0 <= point.y() < window.playback.height()


def test_csv_includes_speed_only_markers_without_changing_headers(tmp_path):
    video = str(tmp_path / 'video.mp4')
    case = InvestigationCase(videos=[video])
    case.toggle_speed_point(10)
    case.toggle_speed_point(40)
    case.set_speed_distance(40, 8)
    repository = CaseRepository(tmp_path)
    repository.save([case])
    path = tmp_path / 'result.csv'
    repository.export_csv(path, [case])
    with path.open(encoding='utf-8-sig', newline='') as stream:
        row = next(csv.DictReader(stream))
    markers = json.loads(row['video_markers'])
    assert markers[video]['speed_points'] == [{'frame': 10, 'distance_m': None}, {'frame': 40, 'distance_m': 8.0}]
    assert not any('speed_point' in key for key in row)


def test_speed_marker_hover_and_nearby_trigger_label_dont_block_point(qtbot):
    slider = MarkerSlider()
    qtbot.addWidget(slider)
    slider.resize(450, 56)
    slider.setRange(0, 299)
    slider.set_markers(126, 170, [30, 55, 82, 111, 142])
    slider.show()
    target = QPoint(slider._x_for_value(142), 6)
    qtbot.mouseMove(slider, target)
    assert slider.hovered_marker == 'speed'
    assert '5' in slider.toolTip()
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=target)
    assert slider.value() == 142


def test_clear_confirmation_keeps_trigger_and_only_clears_speed_points(speed_window, monkeypatch):
    window = speed_window
    measure(window)
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.No)
    window.clear_speed_points()
    assert len(window.current_case.speed_points) == 5
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Yes)
    window.clear_speed_points()
    assert window.current_case.speed_points == []
    assert window.current_case.trigger_frame == 126
    assert window.playback.timeline.speed_frames == []
    assert window.speed_panel._estimate.kph is None
