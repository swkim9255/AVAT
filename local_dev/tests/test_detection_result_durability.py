"""Detection checkpoints survive reopening without saving unrelated edits."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from investigator.detection_controller import _JobContext
from investigator.detection_process import DetectionRequest
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_TRIGGER, add_manual_object
from investigator.project_tabs_state import ProjectTabsStore
from investigator.storage import CaseRepository, WorkspaceError


@pytest.fixture()
def checkpoint_window(qtbot, tmp_path, monkeypatch):
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *args, **kwargs: None))
    window = MainWindow(
        tmp_path / "project", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"),
    )
    def before_close(widget):
        # pytest-qt closes registered windows before fixture finalizers run.
        widget._commit_in_progress = False
        widget._workspace_load_failed = True

    qtbot.addWidget(window, before_close_func=before_close)
    cases = [
        InvestigationCase(
            values={"institution": "samsung", "accident_classification": f"{index:013d}"},
            videos=[str(tmp_path / f"clip-{index}.mp4")],
        )
        for index in (1, 2)
    ]
    window.repository.save(cases, active_case_uid=cases[0].uid)
    window.cases = cases
    window.current_uid = cases[0].uid
    window._mark_saved(cases[0].uid)
    yield window
    # Do not turn a checkpoint test into an implicit close/draft-save test.
    window._workspace_load_failed = True
    window.close()


def _add_box(window, case):
    objects, item = add_manual_object(
        case.video_objects(), 0, (10.0, 20.0, 30.0, 40.0), ROLE_TRIGGER, None,
    )
    case.set_video_objects(objects)
    window._mark_case_dirty(case)
    return item


def _reloaded(window):
    repository = CaseRepository(window.repository.workspace)
    cases = repository.load()
    return repository, cases, {case.uid: case for case in cases}


def test_checkpoint_saves_only_target_without_selection_or_order_rewrite(checkpoint_window):
    window = checkpoint_window
    first, second = window.cases
    item = _add_box(window, first)
    second.values["remarks"] = "unsaved unrelated edit"
    window._mark_case_dirty(second)
    window.cases.reverse()
    window.current_uid = second.uid
    window._order_dirty = True

    assert window._save_detection_case(first)

    repository, saved_cases, saved = _reloaded(window)
    assert saved[first.uid].video_objects().find(item.uid) is not None
    assert saved[first.uid].status == "draft"
    assert "remarks" not in saved[second.uid].values
    assert [case.uid for case in saved_cases] == [first.uid, second.uid]
    assert repository.active_case_uid() == first.uid
    assert window.current_uid == second.uid
    assert window._saved_active_uid == first.uid
    assert first.uid not in window._dirty_cases
    assert second.uid in window._dirty_cases
    assert window._order_dirty and window._dirty


def test_checkpoint_does_not_clear_or_replace_edits_during_commit(checkpoint_window, monkeypatch):
    window = checkpoint_window
    case = window.current_case
    _add_box(window, case)
    original = window._disk_operation

    def commit_then_edit(operation, *args, **kwargs):
        result = original(operation, *args, **kwargs)
        case.values["remarks"] = "newer live edit"
        window._mark_case_dirty(case)
        return result

    monkeypatch.setattr(window, "_disk_operation", commit_then_edit)
    assert window._save_detection_case(case)

    _, _, saved = _reloaded(window)
    assert "remarks" not in saved[case.uid].values
    assert case.values["remarks"] == "newer live edit"
    assert window.current_case is case
    assert case.uid in window._dirty_cases
    assert window._dirty


@pytest.mark.parametrize("error_type", [WorkspaceError, OSError])
def test_checkpoint_failure_preserves_memory_dirty_state_and_disk(
    checkpoint_window, monkeypatch, caplog, error_type,
):
    window = checkpoint_window
    case = window.current_case
    item = _add_box(window, case)

    def fail(*args, **kwargs):
        raise error_type("private case path must not be logged")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    assert not window._save_detection_case(case)

    _, _, saved = _reloaded(window)
    assert saved[case.uid].video_objects().objects == []
    assert case.video_objects().find(item.uid) is not None
    assert case.uid in window._dirty_cases
    assert window._dirty
    assert "임시저장" in window.statusBar().currentMessage()
    assert "detection_result_save_failed" in caplog.text
    assert "private case path" not in caplog.text


@pytest.mark.parametrize("blocked", ["commit", "load", "stale"])
def test_checkpoint_rejects_unavailable_or_stale_context(
    checkpoint_window, monkeypatch, blocked,
):
    window = checkpoint_window
    case = window.current_case
    if blocked == "commit":
        window._commit_in_progress = True
    elif blocked == "load":
        window._workspace_load_failed = True
    else:
        case = InvestigationCase.from_dict(case.to_dict())

    def unexpected(*args, **kwargs):
        pytest.fail("an unavailable target must not start a storage operation")

    monkeypatch.setattr(window, "_disk_operation", unexpected)
    assert not window._save_detection_case(case)
    window._commit_in_progress = False


def test_checkpoint_new_target_adds_only_its_persisted_uid(checkpoint_window):
    window = checkpoint_window
    other = InvestigationCase(
        values={"institution": "samsung", "accident_classification": "0000000000003"},
        videos=[str(window.repository.workspace / "third.mp4")],
    )
    window.cases.append(other)
    item = _add_box(window, other)

    assert other.uid not in window._persisted_uids
    assert window._save_detection_case(other)

    repository, cases, saved = _reloaded(window)
    assert saved[other.uid].video_objects().find(item.uid) is not None
    assert cases[-1].uid == other.uid
    assert other.uid in window._persisted_uids
    assert repository.active_case_uid() == window.cases[0].uid


@pytest.mark.parametrize("change_selection", [False, True])
def test_finished_detection_is_durable_without_draft_save(
    checkpoint_window, change_selection,
):
    window = checkpoint_window
    target = window.current_case
    other = window.cases[1]
    request = DetectionRequest(
        video_path=Path(target.active_video), start_frame=0, end_frame=4,
        frame_step=1, classes=["passenger"], role=ROLE_TRIGGER,
    )
    window._detection_context = _JobContext(
        window.repository.workspace, target.uid, target.active_video, request,
    )
    if change_selection:
        window.current_uid = other.uid
        other.values["remarks"] = "unrelated live edit"
        window._mark_case_dirty(other)
    window._detection_finished({
        "detections": [{
            "frame_index": 0, "class_key": "passenger", "object_id": "track-1",
            "bbox_xywh": [10.0, 20.0, 30.0, 40.0], "score": 0.9,
        }],
    })

    repository, _, saved = _reloaded(window)
    assert len(saved[target.uid].video_objects().objects) == 1
    assert saved[target.uid].video_objects().runs[0].object_count == 1
    assert saved[other.uid].video_objects().objects == []
    assert repository.active_case_uid() == target.uid
    assert "검출 완료" in window.detection_panel.status_label.text()
    assert target.uid not in window._dirty_cases
    if change_selection:
        assert "remarks" not in saved[other.uid].values
        assert window.current_uid == other.uid
        assert other.uid in window._dirty_cases


def test_checkpoint_does_not_touch_replaced_project_state(checkpoint_window, monkeypatch, tmp_path):
    window = checkpoint_window
    original_repository = window.repository
    case = window.current_case
    _add_box(window, case)
    original = window._disk_operation
    new_case = InvestigationCase(values={"remarks": "new project state"})
    new_repository = CaseRepository(tmp_path / "another-project")

    def commit_then_switch(operation, *args, **kwargs):
        result = original(operation, *args, **kwargs)
        window.repository = new_repository
        window.cases = [new_case]
        window.current_uid = new_case.uid
        window._persisted_uids = set()
        window._dirty_cases = {new_case.uid}
        window._dirty = True
        return result

    monkeypatch.setattr(window, "_disk_operation", commit_then_switch)
    assert window._save_detection_case(case)
    assert len(CaseRepository(original_repository.workspace).load()[0].video_objects().objects) == 1
    assert window._persisted_uids == set()
    assert window._dirty_cases == {new_case.uid}
    assert window._dirty


def _context(window, target):
    request = DetectionRequest(
        video_path=Path(target.active_video), start_frame=0, end_frame=4,
        frame_step=1, classes=["passenger"], role=ROLE_TRIGGER,
    )
    return _JobContext(window.repository.workspace, target.uid, target.active_video, request)


def test_finished_result_waits_for_in_progress_commit(checkpoint_window, qtbot):
    window = checkpoint_window
    target = window.current_case
    context = _context(window, target)
    window._detection_context = context
    window._commit_in_progress = True

    window._detection_finished({"detections": []})
    assert window._detection_context is context
    assert target.video_objects().runs == []

    window._commit_in_progress = False
    qtbot.waitUntil(lambda: bool(target.video_objects().runs), timeout=2000)
    _, _, saved = _reloaded(window)
    assert saved[target.uid].video_objects().runs[0].object_count == 0
    assert window._detection_context is None
    assert target.uid not in window._dirty_cases


def test_deferred_result_cannot_consume_newer_job_context(checkpoint_window, qtbot):
    window = checkpoint_window
    first, second = window.cases
    first_context = _context(window, first)
    new_context = _context(window, second)
    window._detection_context = first_context
    window._commit_in_progress = True
    window._detection_finished({"detections": []})
    # Cancellation/restart/project navigation invalidates the queued delivery.
    window._detection_context = new_context
    window._commit_in_progress = False
    qtbot.wait(120)

    assert window._detection_context is new_context
    assert first.video_objects().runs == []
    assert second.video_objects().runs == []
    _, _, saved = _reloaded(window)
    assert all(case.video_objects().runs == [] for case in saved.values())


def test_finished_result_announces_failed_checkpoint_and_retains_boxes(
    checkpoint_window, monkeypatch,
):
    window = checkpoint_window
    target = window.current_case
    window._detection_context = _context(window, target)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args[1:])),
    )

    def fail(*args, **kwargs):
        raise WorkspaceError("test-only storage failure")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    window._detection_finished({
        "detections": [{
            "frame_index": 0, "class_key": "passenger", "object_id": "track-1",
            "bbox_xywh": [10.0, 20.0, 30.0, 40.0], "score": 0.9,
        }],
    })

    assert len(target.video_objects().objects) == 1
    assert target.uid in window._dirty_cases and window._dirty
    assert "자동 저장 실패" in window.detection_panel.status_label.text()
    assert warnings and warnings[0][0] == "검출 결과 자동 저장 실패"
    _, _, saved = _reloaded(window)
    assert saved[target.uid].video_objects().objects == []
