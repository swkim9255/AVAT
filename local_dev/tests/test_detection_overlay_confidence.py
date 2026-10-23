"""Confidence affects presentation and selected-frame navigation, never saved boxes."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from tests.detection_fixture import video as video

from investigator.main_window import MainWindow
from investigator.object_detection import (
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    DetectedObject,
    ObjectBox,
    VideoObjects,
)
from investigator.project_tabs_state import ProjectTabsStore


@pytest.fixture
def window(qtbot, tmp_path, video):
    win = MainWindow(tmp_path / "ws", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"))
    qtbot.addWidget(win)
    win.show()
    win.attach_videos([str(video)])
    qtbot.waitUntil(lambda: win.video_controller.is_open, timeout=5000)
    win.playback.detection_panel_button.setChecked(True)
    yield win
    win.video_controller.close_video()


def test_overlay_filters_each_box_and_preserves_raw_data(window, qtbot):
    mixed = DetectedObject("mixed", "V001", ROLE_TRIGGER, None, [
        ObjectBox(0, (10, 10, 40, 40), .4), ObjectBox(1, (10, 10, 40, 40), .9)
    ], source=SOURCE_AUTOMATIC)
    high = DetectedObject("high", "V002", ROLE_TRIGGER, None, [
        ObjectBox(0, (100, 10, 40, 40), .8)
    ], source=SOURCE_AUTOMATIC)
    manual = DetectedObject("manual", "U001", ROLE_TRIGGER, None, [
        ObjectBox(0, (200, 10, 40, 40))
    ])
    window._store_objects(window.current_case, VideoObjects(objects=[mixed, high, manual]),
                          window.current_case.active_video)
    original = window.current_case.video_objects().to_dict()
    panel = window.detection_panel
    panel.confidence_spin.setValue(.5)
    window._detection_frame_changed(0)
    assert {box.uid for box in window.video_player.canvas.overlay} == {"high", "manual"}
    assert panel.table.rowCount() == 3  # Mixed track qualifies at frame 1.
    panel.select_uids(["mixed"])
    qtbot.waitUntil(lambda: window._overlay_frame == 1, timeout=5000)
    assert {box.uid for box in window.video_player.canvas.overlay} == {"mixed"}
    panel.confidence_spin.setValue(.95)
    assert not window.video_player.canvas.overlay
    window._seek_video(0)
    qtbot.waitUntil(lambda: window._overlay_frame == 0, timeout=5000)
    assert {box.uid for box in window.video_player.canvas.overlay} == {"manual"}
    panel.confidence_spin.setValue(0)
    assert len(window.video_player.canvas.overlay) == 3
    assert window.current_case.video_objects().to_dict() == original


def test_selection_and_frame_updates_use_cached_overlay_labels(window, monkeypatch):
    item = DetectedObject("a", "U001", ROLE_TRIGGER, None, [ObjectBox(0, (10, 10, 40, 40))])
    window._store_objects(window.current_case, VideoObjects(objects=[item]),
                          window.current_case.active_video)
    monkeypatch.setattr("investigator.detection_controller.object_display_text",
                        lambda *_args: pytest.fail("Selection must not rebuild label schema"))
    window.detection_panel.select_uids(["a"])
    for _ in range(10):
        window._detection_frame_changed(0)
    assert window.video_player.canvas.overlay[0].selected


def test_real_canvas_click_with_2255_objects_never_rebuilds_table(window, qtbot, monkeypatch):
    objects = VideoObjects(objects=[
        DetectedObject(
            f"uid-{index}", f"V{index:04d}", ROLE_TRIGGER, None,
            [ObjectBox(index % 30, (10 + ((index // 30) % 20) * 12,
                                  10 + ((index // 600) % 10) * 16, 8, 8), .9)],
            source=SOURCE_AUTOMATIC,
        )
        for index in range(2255)
    ])
    window._store_objects(window.current_case, objects, window.current_case.active_video)
    panel = window.detection_panel
    assert panel.table.rowCount() == 2255
    assert panel.table.height() < 3200
    first_cell = panel.table.item(0, 0)
    canvas = window.video_player.canvas
    point = canvas.source_to_widget(14, 14)
    assert point is not None
    with monkeypatch.context() as click_guard:
        click_guard.setattr(panel, "_rebuild_table", lambda *_args: pytest.fail("Canvas click rebuilt 2255 rows"))
        with qtbot.waitSignal(canvas.box_clicked, timeout=1000):
            qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point.toPoint())
        assert panel.selected_uids() == ["uid-0"]
        assert panel.table.item(0, 0) is first_cell
        assert any(box.uid == "uid-0" and box.selected for box in canvas.overlay)
