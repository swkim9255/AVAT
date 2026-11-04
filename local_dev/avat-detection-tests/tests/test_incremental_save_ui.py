from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFileDialog, QMessageBox

from investigator.main_window import MainWindow
from investigator.storage import CaseRepository, WorkspaceError


def test_one_field_saves_only_one_case_without_rebuilding(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    for _ in range(3):
        window.create_case()
    items = [window.case_list.tree.topLevelItem(i) for i in range(3)]
    calls = []
    original = window.repository.save_changes

    def capture(changes, **kwargs):
        calls.append([case.uid for case in changes])
        return original(changes, **kwargs)

    monkeypatch.setattr(window.repository, "save_changes", capture)
    monkeypatch.setattr(window.case_list, "set_cases", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("full rebuild")))
    window._field_changed("remarks", "changed")
    window.save_draft()
    assert calls == [[window.current_uid]]
    assert [window.case_list.tree.topLevelItem(i) for i in range(3)] == items
    assert CaseRepository(tmp_path).load()[-1].values["remarks"] == "changed"
    calls.clear()
    window.save_draft()
    assert calls == []


def test_edits_during_draft_commit_are_not_marked_saved(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    window._field_changed("remarks", "snapshot")
    original = window.repository.save_changes
    entered = threading.Event()
    release = threading.Event()

    def delayed(changes, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(changes, **kwargs)

    monkeypatch.setattr(window.repository, "save_changes", delayed)
    timer = QTimer()

    def edit():
        if not entered.is_set():
            return
        timer.stop()
        window._field_changed("remarks", "newer edit")
        assert not window.investigation.complete_button.isEnabled()
        release.set()

    timer.timeout.connect(edit)
    timer.start(5)
    window.save_draft()
    assert window._dirty
    assert window.current_case.values["remarks"] == "newer edit"
    assert CaseRepository(tmp_path).load()[0].values["remarks"] == "snapshot"
    monkeypatch.setattr(window.repository, "save_changes", original)
    window.save_draft()
    assert not window._dirty
    assert CaseRepository(tmp_path).load()[0].values["remarks"] == "newer edit"


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("fail_first", [False, True])
def test_deferred_drag_during_save_remains_dirty_until_next_save(
    qtbot, tmp_path, monkeypatch, group, fail_first
):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    for _ in range(4):
        window.create_case()
    original_order = [case.uid for case in window.cases]
    original_items = [window.case_list.tree.topLevelItem(i) for i in range(4)]
    window._field_changed("remarks", "pending save")
    original_save = window.repository.save_changes
    entered = threading.Event()
    release = threading.Event()

    def delayed(changes, **kwargs):
        entered.set()
        assert release.wait(5)
        if fail_first:
            raise WorkspaceError("simulated write failure")
        return original_save(changes, **kwargs)

    monkeypatch.setattr(window.repository, "save_changes", delayed)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    timer = QTimer()
    observed_order = []

    def deliver_drop():
        if not entered.is_set():
            return
        timer.stop()
        if group:
            window.reorder_cases(original_order[2:], 0)
        else:
            window.reorder_case(original_order[-1], 0)
        observed_order.extend(case.uid for case in window.cases)
        release.set()

    timer.timeout.connect(deliver_drop)
    timer.start(5)
    window.save_draft()
    assert observed_order == original_order  # No structural mutation during the commit.
    expected_indices = (2, 3, 0, 1) if group else (3, 0, 1, 2)
    expected_order = [original_order[i] for i in expected_indices]
    qtbot.waitUntil(lambda: [case.uid for case in window.cases] == expected_order)
    assert window._dirty and window._order_dirty
    assert [window.case_list.tree.topLevelItem(i) for i in range(4)] == [
        original_items[i] for i in expected_indices
    ]
    assert [case.uid for case in CaseRepository(tmp_path).load()] == original_order
    monkeypatch.setattr(window.repository, "save_changes", original_save)
    window.save_draft()
    assert not window._dirty and not window._order_dirty
    assert [case.uid for case in CaseRepository(tmp_path).load()] == expected_order


@pytest.mark.parametrize("boundary", ["workspace", "close"])
@pytest.mark.parametrize("fail_reorder_commit", [False, True])
def test_deferred_drag_is_committed_before_workspace_change_or_close(
    qtbot, tmp_path, monkeypatch, boundary, fail_reorder_commit
):
    workspace = tmp_path / "original"
    window = MainWindow(workspace)
    qtbot.addWidget(window)
    for _ in range(3):
        window.create_case()
    original_order = [case.uid for case in window.cases]
    expected_order = [original_order[-1], *original_order[:-1]]
    window._field_changed("remarks", "pending save")
    original_save = window.repository.save_changes
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def delayed(changes, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        if calls == 2 and fail_reorder_commit:
            raise WorkspaceError("reorder commit failed")
        return original_save(changes, **kwargs)

    monkeypatch.setattr(window.repository, "save_changes", delayed)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path / "next"))
    (tmp_path / "next").mkdir(exist_ok=True)
    monkeypatch.setattr(window, "_choose_project_open_mode", lambda folder: "current")
    timer = QTimer()

    def deliver_drop():
        if not entered.is_set():
            return
        timer.stop()
        window.reorder_case(original_order[-1], 0)
        release.set()

    timer.timeout.connect(deliver_drop)
    timer.start(5)
    close_event = QCloseEvent()
    if boundary == "workspace":
        window.choose_workspace()
    else:
        window.closeEvent(close_event)
    assert calls == 2  # Initial fields and deferred order both reach a commit boundary.
    if fail_reorder_commit:
        assert window.repository.workspace == workspace.resolve()
        if boundary == "close":
            assert not close_event.isAccepted()
        assert window._dirty and window._order_dirty
        assert [case.uid for case in window.cases] == expected_order
        assert [case.uid for case in CaseRepository(workspace).load()] == original_order
        monkeypatch.setattr(window.repository, "save_changes", original_save)
        window.save_draft()
    else:
        if boundary == "workspace":
            assert window.repository.workspace == (tmp_path / "next").resolve()
        else:
            assert close_event.isAccepted()
    assert [case.uid for case in CaseRepository(workspace).load()] == expected_order


def test_save_failure_retains_changes_and_cancels_close(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    window._field_changed("remarks", "unsaved")
    original = window.repository.save_changes

    def fail(*args, **kwargs):
        raise WorkspaceError("disk full")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert window._dirty
    assert "remarks" not in CaseRepository(tmp_path).load()[0].values
    monkeypatch.setattr(window.repository, "save_changes", original)
    window.save_draft()
    assert not window._dirty
