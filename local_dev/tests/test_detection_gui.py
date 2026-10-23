"""Offscreen GUI flow: range selection → detection (fake worker) → labels → form."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal
from PySide6.QtWidgets import QMessageBox
from tests.detection_fixture import video as video

from investigator.detection_process import DetectionJob, DetectionSettings
from investigator.object_detection import ROLE_BOTH, ROLE_COLLISION, ROLE_TRIGGER
from investigator.project_tabs_state import ProjectTabsStore
from investigator.widgets.playback_panel import MarkerSlider
from investigator.widgets.video_player import OverlayBox, VideoCanvas

HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    QCoreApplication.setOrganizationName("AVAT-test")
    QCoreApplication.setApplicationName("detection-test")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "ini"))
    QSettings().clear()
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    yield


def _fake_python(tmp_path: Path) -> str:
    return sys.executable


@pytest.fixture(autouse=True)
def portable_fake_worker(monkeypatch):
    original = DetectionJob.arguments

    def arguments(job):
        args = original(job)
        if job.settings.model == "fake/sam3":
            return [str(HERE / "fake_detection_worker.py"), *args]
        return args

    monkeypatch.setattr(DetectionJob, "arguments", arguments)


class _SilentAudio(QObject):
    """QtMultimedia needs an audio backend that headless containers lack."""

    error_occurred = Signal(str)

    def __init__(self, parent=None):  # type: ignore[no-untyped-def]
        super().__init__(parent)

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return lambda *args, **kwargs: None


@pytest.fixture()
def window(qtbot, tmp_path, video, monkeypatch):  # type: ignore[no-untyped-def]
    from investigator import video_controller
    from investigator.main_window import MainWindow

    monkeypatch.setattr(video_controller, "AudioPlayback", _SilentAudio)

    win = MainWindow(tmp_path / "ws", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"))
    qtbot.addWidget(win)
    win.show()
    win.attach_videos([str(video)])
    qtbot.waitUntil(lambda: win.video_controller.is_open, timeout=5000)
    yield win
    win.video_controller.close_video()


def test_timeline_shift_drag_emits_range(qtbot):  # type: ignore[no-untyped-def]
    slider = MarkerSlider()
    slider.resize(400, 40)
    slider.setRange(0, 99)
    qtbot.addWidget(slider)
    slider.show()
    from PySide6.QtCore import QPoint, Qt

    with qtbot.waitSignal(slider.range_selected) as blocker:
        qtbot.mousePress(
            slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, QPoint(60, 20)
        )
        qtbot.mouseMove(slider, QPoint(300, 20))
        qtbot.mouseRelease(
            slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, QPoint(300, 20)
        )
    start, end = blocker.args
    assert 0 < start < end < 99


def test_canvas_maps_boxes_through_rotation(qtbot):  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QImage

    canvas = VideoCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(640, 480)
    image = QImage(320, 240, QImage.Format.Format_RGB888)
    canvas.set_frame(image)
    for degrees in (0, 90, 180, 270):
        canvas.set_rotation(degrees)
        point = canvas.source_to_widget(100.0, 50.0)
        back = canvas.widget_to_source(point)
        assert abs(back.x() - 100.0) < 1 and abs(back.y() - 50.0) < 1
    # 90°: the source's top-left corner lands at the widget image's top-right.
    canvas.set_rotation(90)
    corner = canvas.source_to_widget(0.0, 0.0)
    other = canvas.source_to_widget(320.0, 240.0)
    assert corner.x() > other.x() and corner.y() < other.y()
    canvas.set_rotation(0)
    canvas.set_overlay([OverlayBox("a", (10, 10, 50, 50), "#1F5FBF", "V001")])
    assert canvas.box_at(canvas.source_to_widget(30.0, 30.0)) == "a"


def test_range_detection_labels_and_form(window, qtbot, tmp_path):  # type: ignore[no-untyped-def]
    DetectionSettings(python=_fake_python(tmp_path), model="fake/sam3", device="cpu", backend="sam3").save()
    window.playback.detection_panel_button.setChecked(True)
    panel = window.detection_panel
    assert window.detection_scroll.isVisible()

    # Timeline Shift+drag sets the same range and timeline band, without a mode switch.
    window.playback.range_selected.emit(4, 12)
    assert panel.frame_range == (4, 12)
    assert window.playback.timeline.selection_range == (4, 12)

    # Typed range + step.
    panel.end_spin.setValue(10)  # 1-based → frame 9
    panel.step_spin.setValue(2)
    assert panel.frame_range == (4, 9) and panel.processed_frame_count() == 3
    panel._class_keys = ["vehicle", "streettree", "building_wall", "boundary_wall"]

    panel.run_button.click()
    qtbot.waitUntil(lambda: window._detection_job is None, timeout=30000)
    case = window.current_case
    objects = case.video_objects()
    assert {item.class_key for item in objects.objects} == {
        "vehicle",
        "streettree",
        "building_wall",
    }
    assert all(item.role == ROLE_BOTH for item in objects.objects)
    car = next(item for item in objects.objects if item.class_key == "vehicle")
    assert car.frames == [4, 6, 8]
    assert panel.table.rowCount() == 3
    assert "검출 완료" in panel.status_label.text()

    # Review a common object, then apply all common objects to either tab.
    tree = next(item for item in objects.objects if item.class_key == "streettree")
    window._change_object_subtype([tree.uid], "streettree", None, None)
    window._change_object_reviewed([tree.uid], True)
    tree = case.video_objects().find(tree.uid)
    assert tree.role == ROLE_BOTH and tree.reviewed

    window.apply_objects_to_form(ROLE_COLLISION)
    window.apply_objects_to_form(ROLE_TRIGGER)
    values = window.repository.form_definition.canonical_values(case.values)
    assert set(values["collision_targets"]) == {
        "vehicle.passenger", "building.wall", "building.streettree",
    }
    assert values["obstacle_exists"] == "유"
    assert values["obstacle_vehicle_types"] == ["승용"]
    assert set(values["obstacle_building_landscape"]) == {"건물 벽", "화단·흙더미·가로수(수목)"}

    # Overlay shows boxes of the current frame only while the panel is open.
    window._seek_video(4)
    qtbot.waitUntil(lambda: window._overlay_frame == 4, timeout=5000)
    assert {box.uid for box in window.video_player.canvas.overlay} >= {car.uid}
    window.playback.detection_panel_button.setChecked(False)
    assert window.video_player.canvas.overlay == []

    # Saved and reloaded with the case.
    window.save_draft()
    from investigator.storage import CaseRepository

    reloaded = CaseRepository(window.repository.workspace).load()
    saved = next(item for item in reloaded if item.uid == case.uid)
    assert len(saved.video_objects().objects) == 3
    assert saved.video_objects().find(tree.uid).role == ROLE_BOTH


def test_manual_box_and_failed_worker(window, qtbot, tmp_path):  # type: ignore[no-untyped-def]
    window.playback.detection_panel_button.setChecked(True)
    window._canvas_box_drawn((10.0, 10.0, 40.0, 30.0))
    objects = window.current_case.video_objects().objects
    assert len(objects) == 1 and objects[0].object_id == "U001" and objects[0].label is None
    assert window.detection_panel.selected_uids() == [objects[0].uid]

    DetectionSettings(python=str(tmp_path / "missing-python"), device="cpu").save()
    window.detection_panel.run_button.click()
    qtbot.waitUntil(lambda: window._detection_job is None, timeout=10000)
    assert "검출용 Python" in window.detection_panel.status_label.text()
    assert len(window.current_case.video_objects().objects) == 1


def test_speed_and_detection_panels_replace_each_other(window):  # type: ignore[no-untyped-def]
    playback = window.playback
    playback.detection_panel_button.setChecked(True)
    assert window.detection_scroll.isVisible()
    playback.speed_panel_button.setChecked(True)
    assert window.speed_scroll.isVisible() and not window.detection_scroll.isVisible()
    assert not playback.detection_panel_button.isChecked()
    assert window.video_player.canvas.overlay == []
    playback.detection_panel_button.setChecked(True)
    assert window.detection_scroll.isVisible() and not window.speed_scroll.isVisible()
    assert not playback.speed_panel_button.isChecked()
    # Adding a speed point (M) brings the speed panel back.
    window.toggle_speed_point()
    assert window.speed_scroll.isVisible() and not window.detection_scroll.isVisible()


def test_hidden_detection_has_no_per_frame_ui_or_deserialization(window, monkeypatch):
    case = window.current_case
    original = type(case).video_objects
    reads = []

    def video_objects(item):
        reads.append(item.uid)
        return original(item)

    monkeypatch.setattr(type(case), "video_objects", video_objects)
    frames = []
    monkeypatch.setattr(window.detection_panel, "set_current_frame", frames.append)
    for frame in range(30):
        window._detection_frame_changed(frame)
    assert reads == [] and frames == []


def test_reopening_panel_keeps_range_independent_of_current_displayed_frame(window, qtbot, monkeypatch):
    window._seek_video(7)
    qtbot.waitUntil(lambda: window._overlay_frame == 7)
    window.playback.detection_panel_button.setChecked(True)
    assert window.detection_panel.frame_range == (0, 0)
    assert window.detection_panel._current_frame == 7
    window.detection_panel.set_selected_range(3, 9)
    window._canvas_box_drawn((10.0, 10.0, 40.0, 30.0))
    original = type(window.current_case).video_objects
    reads = []

    def video_objects(item):
        reads.append(item.uid)
        return original(item)

    monkeypatch.setattr(type(window.current_case), "video_objects", video_objects)
    for frame in range(10):
        window._detection_frame_changed(frame)
    assert reads == []
    assert window.detection_panel.frame_range == (3, 9)


def test_manual_drawing_pauses_playback(window):
    window.playback.detection_panel_button.setChecked(True)
    window.video_controller.play()
    assert window.video_controller.is_playing
    window.detection_panel.draw_button.setChecked(True)
    assert not window.video_controller.is_playing


def test_optional_object_edit_keeps_completed_case_presentation(window):
    case = window.current_case
    case.status = "completed"
    window._refresh_case_presentation(rebuild_list=True)
    assert not window.investigation.complete_button.isEnabled()
    window.playback.detection_panel_button.setChecked(True)
    assert case.status == "completed"
    window._canvas_box_drawn((10.0, 10.0, 40.0, 30.0))
    assert case.status == "completed"
    assert not window.investigation.complete_button.isEnabled()
    # The fixture deliberately did not fill all required investigation values.
    # Restore its synthetic status before the normal close/save cleanup.
    case.status = "draft"


def test_result_stays_with_requested_case_after_selection_changes(window, qtbot, tmp_path):
    case = window.current_case
    DetectionSettings(python=sys.executable, model="fake/sam3", device="cpu", backend="sam3").save()
    window.playback.detection_panel_button.setChecked(True)
    window.detection_panel._class_keys = ["vehicle"]
    window.run_detection()
    window.create_case()
    next_case = window.current_case
    assert next_case.uid != case.uid
    qtbot.waitUntil(lambda: window._detection_job is None, timeout=15000)
    assert window.current_case.uid == next_case.uid
    assert len(case.video_objects().objects) == 1
    assert next_case.objects_by_video == {}


def test_shutdown_stops_active_detection_without_saving_partial_result(window):
    DetectionSettings(python=sys.executable, model="fake/sam3", device="cpu", backend="sam3").save()
    window.playback.detection_panel_button.setChecked(True)
    window.run_detection()
    job = window._detection_job
    assert job is not None
    window._shutdown_detection()
    assert window._detection_job is None and not job.is_running
    assert window.current_case.objects_by_video == {}


def test_overlay_never_uses_cached_boxes_for_a_different_open_video(window, monkeypatch):
    window.playback.detection_panel_button.setChecked(True)
    window._canvas_box_drawn((10.0, 10.0, 40.0, 30.0))
    assert window.video_player.canvas.overlay
    monkeypatch.setattr(window, "_detection_case", lambda: (None, False))
    window._update_overlay()
    assert window.video_player.canvas.overlay == []
