"""Real key events for drawing undo/redo, with no model dependency."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from tests.detection_fixture import video as video

from investigator.detection_controller import _JobContext
from investigator.detection_process import DetectionRequest
from investigator.main_window import MainWindow
from investigator.object_detection import ROLE_BOTH, ObjectBox
from investigator.project_tabs_state import ProjectTabsStore


@pytest.fixture()
def editor_window(qtbot, qapp, tmp_path, video):
    win = MainWindow(tmp_path / "project", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"))
    qtbot.addWidget(win)
    win.show()
    win.attach_videos([str(video)])
    qtbot.waitUntil(lambda: win.video_controller.is_open, timeout=5000)
    win.playback.detection_panel_button.setChecked(True)
    win.activateWindow()
    win.video_player.canvas.setFocus()
    qapp.processEvents()
    yield win
    win.video_controller.close_video()


def draw(window, bbox=(10.0, 10.0, 40.0, 30.0)):
    window._canvas_box_drawn(bbox)
    return window.current_case.video_objects().objects[-1]


def key(qtbot, window, code, *, shift=False):
    canvas = window.video_player.canvas
    canvas.setFocus()
    modifiers = Qt.KeyboardModifier.ControlModifier
    if shift:
        modifiers |= Qt.KeyboardModifier.ShiftModifier
    qtbot.keyClick(canvas, code, modifiers)


def test_ctrl_z_ctrl_y_and_ctrl_shift_z_preserve_object_identity(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.current_case.video_objects().objects == []
    assert win.video_player.canvas.overlay == []
    key(qtbot, win, Qt.Key.Key_Y)
    assert win.current_case.video_objects().objects[0].to_dict() == item.to_dict()
    assert win.detection_panel.selected_uids() == [item.uid]
    key(qtbot, win, Qt.Key.Key_Z)
    key(qtbot, win, Qt.Key.Key_Z, shift=True)
    assert win.current_case.video_objects().objects[0].uid == item.uid


def test_new_drawing_after_undo_discards_redo(editor_window, qtbot):
    win = editor_window
    first = draw(win)
    key(qtbot, win, Qt.Key.Key_Z)
    second = draw(win, (30.0, 40.0, 20.0, 20.0))
    key(qtbot, win, Qt.Key.Key_Y)
    assert [item.uid for item in win.current_case.video_objects().objects] == [second.uid]
    assert second.uid != first.uid


def test_whole_object_delete_then_undo_redo_updates_table(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    assert not hasattr(win, "_delete_object_box")
    assert not hasattr(win.detection_panel, "delete_box_button")
    win.detection_panel.delete_button.click()
    assert win.detection_panel.table.rowCount() == 0
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.detection_panel.table.rowCount() == 1
    assert win.current_case.video_objects().find(item.uid).boxes == item.boxes
    key(qtbot, win, Qt.Key.Key_Y)
    assert win.detection_panel.table.rowCount() == 0


def test_subtype_edits_are_undoable_without_touching_investigation(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    win.current_case.values["remarks"] = "유지할 조사 내용"
    assert item.role == ROLE_BOTH
    assert not hasattr(win, "_change_object_role")
    win._change_object_subtype([item.uid], "passenger", None, None)
    assert win.current_case.video_objects().find(item.uid).subtype_key == "passenger"
    win._change_object_subtype([item.uid], "truck", None, None)
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.current_case.video_objects().find(item.uid).subtype_key == "passenger"
    assert win.current_case.video_objects().find(item.uid).role == ROLE_BOTH
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.current_case.video_objects().find(item.uid).label is None
    assert win.current_case.values["remarks"] == "유지할 조사 내용"


def test_noop_does_not_create_history_or_destroy_redo(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    win._change_object_reviewed([item.uid], True)  # Already reviewed; no new history entry.
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.current_case.video_objects().objects == []
    win._delete_objects(["does-not-exist"])
    key(qtbot, win, Qt.Key.Key_Y)
    assert win.current_case.video_objects().objects[0].uid == item.uid


def test_typing_in_remarks_uses_native_undo_not_box_history(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    notes = win.investigation.editors["remarks"].control
    win.investigation.tabs.setCurrentIndex(len(win.investigation.sections) - 1)
    notes.setFocus()
    qtbot.keyClicks(notes, "some notes")
    assert notes.toPlainText() == "some notes"
    qtbot.keyClick(notes, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert notes.toPlainText() == ""
    assert win.current_case.video_objects().find(item.uid) is not None


def test_hidden_panel_and_commit_do_not_apply_box_history(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    win._commit_in_progress = True
    key(qtbot, win, Qt.Key.Key_Z)
    win._commit_in_progress = False
    assert win.current_case.video_objects().find(item.uid) is not None
    win.playback.detection_panel_button.setChecked(False)
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.current_case.video_objects().find(item.uid) is not None


def test_unfinished_rectangle_is_cancelled_without_undoing_completed_box(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    canvas = win.video_player.canvas
    win.detection_panel.draw_button.setChecked(True)
    start = canvas.source_to_widget(60.0, 60.0).toPoint()
    end = canvas.source_to_widget(100.0, 100.0).toPoint()
    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    key(qtbot, win, Qt.Key.Key_Z)
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    assert [obj.uid for obj in win.current_case.video_objects().objects] == [item.uid]


def test_undo_returns_to_edited_frame_and_redoes_after_draft_save(editor_window, qtbot):
    win = editor_window
    win._seek_video(4)
    qtbot.waitUntil(lambda: win._overlay_frame == 4)
    item = draw(win)
    win.save_draft()
    win._seek_video(10)
    qtbot.waitUntil(lambda: win._overlay_frame == 10)
    key(qtbot, win, Qt.Key.Key_Z)
    qtbot.waitUntil(lambda: win._overlay_frame == 4)
    assert win.current_case.video_objects().objects == []
    key(qtbot, win, Qt.Key.Key_Y)
    assert win.current_case.video_objects().find(item.uid) is not None


def test_same_video_in_two_cases_keeps_separate_undo_redo(editor_window, qtbot):
    win = editor_window
    first_case = win.current_case
    first = draw(win)
    path = first_case.active_video
    win.create_case()
    win.attach_videos([path])
    qtbot.waitUntil(lambda: win.video_controller.is_open)
    second_case = win.current_case
    second = draw(win)
    key(qtbot, win, Qt.Key.Key_Z)
    assert second_case.video_objects().objects == []
    assert first_case.video_objects().find(first.uid) is not None
    win.select_case(first_case.uid)
    key(qtbot, win, Qt.Key.Key_Z)
    assert first_case.video_objects().objects == []
    win.select_case(second_case.uid)
    key(qtbot, win, Qt.Key.Key_Y)
    assert second_case.video_objects().find(second.uid) is not None
    assert first_case.video_objects().objects == []


def test_automatic_result_clears_only_its_requested_case_history(editor_window, qtbot):
    win = editor_window
    first_case = win.current_case
    first = draw(win)
    path = first_case.active_video
    request = DetectionRequest(
        video_path=Path(path),
        start_frame=0,
        end_frame=29,
        frame_step=1,
        classes=["passenger"],
        role=ROLE_BOTH,
    )
    context = _JobContext(win.repository.workspace, first_case.uid, path, request)
    win.create_case()
    win.attach_videos([path])
    qtbot.waitUntil(lambda: win.video_controller.is_open)
    second_case = win.current_case
    second = draw(win)
    win._detection_context = context
    win._detection_finished({"detections": []})
    assert win.current_case.uid == second_case.uid
    key(qtbot, win, Qt.Key.Key_Z)
    assert second_case.video_objects().objects == []
    win.select_case(first_case.uid)
    key(qtbot, win, Qt.Key.Key_Z)
    assert first_case.video_objects().find(first.uid) is not None
    assert second.uid != first.uid


def test_current_frame_split_keeps_other_frames_and_is_undoable(editor_window, qtbot):
    win = editor_window
    item = draw(win)
    objects = win.current_case.video_objects()
    objects.find(item.uid).boxes.append(ObjectBox(1, (20, 20, 30, 30)))
    win._edit_current_objects(lambda _objects: objects)
    win.detection_panel.select_uids([item.uid])
    original = win.current_case.video_objects().find(item.uid).to_dict()
    win.detection_panel.split_frame_button.click()
    split_objects = win.current_case.video_objects()
    assert len(split_objects.objects) == 2
    assert split_objects.find(item.uid).frames == [1]
    new = next(obj for obj in split_objects.objects if obj.uid != item.uid)
    assert new.frames == [0] and new.role == ROLE_BOTH
    assert new.boxes[0] == item.boxes[0]
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.current_case.video_objects().find(item.uid).to_dict() == original
    assert len(win.current_case.video_objects().objects) == 1
    key(qtbot, win, Qt.Key.Key_Y)
    assert win.current_case.video_objects().find(new.uid).to_dict() == new.to_dict()
    assert win.current_case.video_objects().find(item.uid).frames == [1]
