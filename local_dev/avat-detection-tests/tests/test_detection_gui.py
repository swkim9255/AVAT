"""Offscreen GUI flow: range selection → detection (fake worker) → labels → form."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QSettings, Signal
from PySide6.QtWidgets import QMessageBox

from investigator.detection_process import DetectionSettings
from investigator.object_detection import ROLE_COLLISION, ROLE_TRIGGER, LabelRef
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
    wrapper = tmp_path / "fake-python.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_detection_worker.py"}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


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
    DetectionSettings(python=_fake_python(tmp_path), model="fake/sam3", device="cpu").save()
    window.playback.detection_panel_button.setChecked(True)
    panel = window.detection_panel
    assert window.detection_scroll.isVisible()

    # Timeline Shift+drag result → range mode with the same frames and a timeline band.
    window.playback.range_selected.emit(4, 12)
    assert panel.range_radio.isChecked() and panel.frame_range == (4, 12)
    assert window.playback.timeline.selection_range == (4, 12)

    # Typed range + step.
    panel.end_spin.setValue(10)  # 1-based → frame 9
    panel.step_spin.setValue(2)
    assert panel.frame_range == (4, 9) and panel.processed_frame_count() == 3
    panel._class_keys = ["passenger", "streettree", "building_wall", "boundary_wall"]
    panel.set_new_object_role(ROLE_COLLISION)

    panel.run_button.click()
    qtbot.waitUntil(lambda: window._detection_job is None, timeout=30000)
    case = window.current_case
    objects = case.video_objects()
    assert {item.class_key for item in objects.objects} == {
        "passenger",
        "streettree",
        "building_wall",
    }
    assert all(item.role == ROLE_COLLISION for item in objects.objects)
    car = next(item for item in objects.objects if item.class_key == "passenger")
    assert car.frames == [4, 6, 8]
    assert panel.table.rowCount() == 3
    assert "검출 완료" in panel.status_label.text()

    # Change the tree into a trigger object, relabel it, then apply both tabs.
    tree = next(item for item in objects.objects if item.class_key == "streettree")
    window._change_object_role([tree.uid], ROLE_TRIGGER)
    window._change_object_label([tree.uid], LabelRef("obstacle_road_facilities", "가드레일"))
    tree = case.video_objects().find(tree.uid)
    assert tree.role == ROLE_TRIGGER and tree.reviewed

    window.apply_objects_to_form(ROLE_COLLISION)
    window.apply_objects_to_form(ROLE_TRIGGER)
    values = window.repository.form_definition.canonical_values(case.values)
    assert values["collision_targets"] == ["vehicle.passenger", "building.wall"]
    assert values["obstacle_exists"] == "유"
    assert values["obstacle_road_facilities"] == ["가드레일"]

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
    assert saved.video_objects().find(tree.uid).role == ROLE_TRIGGER


def test_manual_box_and_failed_worker(window, qtbot, tmp_path):  # type: ignore[no-untyped-def]
    window.playback.detection_panel_button.setChecked(True)
    window.detection_panel.set_new_object_role(ROLE_TRIGGER)
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
