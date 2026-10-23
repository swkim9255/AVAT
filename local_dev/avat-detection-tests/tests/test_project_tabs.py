from __future__ import annotations

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QTabBar

from investigator.form_definition import FormDefinition
from investigator.main_window import MainWindow
from investigator.storage import CaseRepository, WorkspaceError
from investigator.theme import apply_theme
from tests.test_ui_smoke import _Capture


@pytest.fixture
def projects(qtbot, tmp_path):
    first, second, third = (tmp_path / name for name in ("project_A", "project_B", "project_C"))
    for path in (first, second, third):
        path.mkdir()
    window = MainWindow(first)
    qtbot.addWidget(window)
    return window, first, second, third


def test_projects_store_cases_and_forms_independently(projects):
    window, first, second, _ = projects
    window.create_case()
    first_uid = window.current_uid
    window._field_changed("remarks", "first project")
    definition = FormDefinition.from_dict(window.repository.form_definition.to_dict())
    definition.tabs[1]["title"] = "첫 프로젝트 운전자"
    assert window.apply_form_definition(definition)

    assert window.open_project(second)
    assert window.project_tabs.count() == 2
    assert window.cases == []
    assert window.repository.form_definition.tabs[1]["title"] != "첫 프로젝트 운전자"
    window.create_case()
    second_uid = window.current_uid
    window._field_changed("remarks", "second project")
    assert window.switch_project(0)
    assert window.current_uid == first_uid
    assert window.current_case.values["remarks"] == "first project"
    assert window.repository.form_definition.tabs[1]["title"] == "첫 프로젝트 운전자"
    assert window.project_tabs.tabToolTip(window.project_tabs.currentIndex()) == str(first.resolve())
    assert window.switch_project(1)
    assert window.current_uid == second_uid
    assert window.current_case.values["remarks"] == "second project"
    assert window.project_tabs.tabToolTip(window.project_tabs.currentIndex()) == str(second.resolve())
    assert [case.uid for case in CaseRepository(first).load()] == [first_uid]
    assert [case.uid for case in CaseRepository(second).load()] == [second_uid]


def test_existing_folder_activates_one_tab_and_replace_keeps_other_projects(projects):
    window, first, second, third = projects
    window.create_case()
    uid = window.current_uid
    assert window.open_project(second)
    assert window.open_project(first / ".", new_tab=False)
    assert window.project_tabs.count() == 2
    assert window.current_uid == uid
    assert window.project_tabs.currentIndex() == 0
    assert window.switch_project(1)
    assert window.open_project(third, new_tab=False)
    assert window.project_tabs.count() == 2
    assert [project.repository.workspace for project in window._projects] == [first, third]
    assert window.switch_project(0)
    assert window.current_uid == uid


def test_project_switch_restores_list_form_rotation_and_paused_frame(projects):
    window, first, second, _ = projects
    video = first / "manual.mp4"
    video.write_bytes(b"fake")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=100)
    window.attach_videos([str(video)])
    first_uid = window.current_uid
    assert window.video_controller.seek(25)
    window.rotate_video_clockwise()
    window.investigation.tabs.setCurrentIndex(3)
    window.case_list.search.setText(window.current_case.display_id)
    window.case_list.tree.topLevelItem(0).setExpanded(False)
    assert window.open_project(second)
    assert not window.video_controller.is_open
    assert window.case_list.search.text() == ""
    assert window.switch_project(0)
    assert window.current_uid == first_uid
    assert window.video_controller.current_frame == 25
    assert not window.video_controller.is_playing
    assert window.current_case.video_rotation == 90
    assert window.video_player.canvas.rotation_degrees == 90
    assert window.investigation.tabs.currentIndex() == 3
    assert window.case_list.search.text() == window.current_case.display_id
    assert not window.case_list.tree.topLevelItem(0).isExpanded()


def test_project_switch_preserves_pending_navigation_target(projects):
    window, first, second, _ = projects
    video = first / "manual.mp4"
    video.write_bytes(b"fake")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=100)
    window.attach_videos([str(video)])
    assert window.video_controller.seek(25)
    # Represent a request accepted while the decoder still displays frame 25.
    # Switching tabs must retain the user's target, not the stale image frame.
    window.video_controller._seek_target = 75
    window.video_controller.seek_requested_position.emit(75)
    assert window.video_controller.current_frame == 25
    assert window.playback.frame_input.value() == 76
    assert window.open_project(second)
    assert window._projects[0].video_frame == 75
    assert window.switch_project(0)
    assert window.video_controller.current_frame == 75


@pytest.mark.parametrize("new_tab", [True, False])
def test_corrupt_project_open_preserves_source_tab(projects, monkeypatch, new_tab):
    window, first, second, _ = projects
    window.create_case()
    uid = window.current_uid
    window._field_changed("remarks", "keep")
    (second / "investigations.db").write_bytes(b"corrupt")
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    assert not window.open_project(second, new_tab=new_tab)
    assert window.project_tabs.count() == 1
    assert window.repository.workspace == first
    assert window.current_uid == uid
    assert window.current_case.values["remarks"] == "keep"
    assert window.investigation.isEnabled()


def test_save_failure_cancels_switch_and_close_project(projects, monkeypatch):
    window, first, second, _ = projects
    window.create_case()
    assert window.open_project(second)
    window.create_case()
    window._field_changed("remarks", "unsaved")
    original = window.repository.save_changes

    def fail(*args, **kwargs):
        raise WorkspaceError("disk full")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    window.project_tabs.setCurrentIndex(0)
    assert window.project_tabs.currentIndex() == 1
    assert window.repository.workspace == second
    assert window._dirty
    assert not window.close_project(1)
    assert window.project_tabs.count() == 2
    assert window.current_case.values["remarks"] == "unsaved"
    monkeypatch.setattr(window.repository, "save_changes", original)
    assert window.close_project(1)
    assert window.repository.workspace == first
    assert window.project_tabs.count() == 1
    assert not window.close_project(0)
    assert CaseRepository(second).load()[0].values["remarks"] == "unsaved"


def test_close_inactive_tab_does_not_switch_or_discard_active_edits(projects):
    window, _, second, third = projects
    assert window.open_project(second)
    assert window.open_project(third)
    window.create_case()
    window._field_changed("remarks", "active")
    assert window.close_project(0)
    assert window.repository.workspace == third
    assert window.project_tabs.currentIndex() == 1
    assert window._dirty
    assert window.current_case.values["remarks"] == "active"


@pytest.mark.parametrize("mode", ["current", "new", None])
def test_open_dialog_routes_choice_and_cancel(projects, monkeypatch, mode):
    window, first, second, _ = projects
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(second))
    monkeypatch.setattr(window, "_choose_project_open_mode", lambda folder: mode)
    window.choose_workspace()
    assert window.repository.workspace == (first if mode is None else second)
    assert window.project_tabs.count() == (2 if mode == "new" else 1)


def test_open_mode_dialog_defaults_to_new_tab(projects):
    window, _, second, _ = projects
    seen = []

    def accept_default():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        assert "현재 탭으로 불러오기" in [button.text() for button in dialog.buttons()]
        seen.append(dialog.defaultButton().text())
        dialog.defaultButton().click()

    QTimer.singleShot(0, accept_default)
    assert window._choose_project_open_mode(second) == "new"
    assert seen == ["새 탭에서 열기"]


def test_same_folder_names_have_distinct_tooltips(projects, tmp_path):
    window, _, _, _ = projects
    folders = [tmp_path / parent / "same" for parent in ("one", "two")]
    for path in folders:
        path.mkdir(parents=True)
        assert window.open_project(path)
    assert window.project_tabs.tabText(1) != window.project_tabs.tabText(2)
    assert [window.project_tabs.tabToolTip(i) for i in (1, 2)] == [str(p) for p in folders]


def test_video_folder_import_only_adds_to_active_project(projects, tmp_path):
    window, first, second, _ = projects
    media = tmp_path / "media"
    media.mkdir()
    (media / "1234567890123.mp4").write_bytes(b"fake")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=100)
    window.create_case()
    first_uid = window.current_uid
    assert window.open_project(second)
    assert window.import_video_folder(media)
    second_uid = window.current_uid
    assert window.current_case.values["accident_classification"] == "1234567890123"
    assert window.switch_project(0)
    assert [case.uid for case in window.cases] == [first_uid]
    assert window.current_case.videos == []
    assert [case.uid for case in CaseRepository(first).load()] == [first_uid]
    assert [case.uid for case in CaseRepository(second).load()] == [second_uid]


def test_old_project_drop_cannot_reorder_copied_project(projects, qtbot):
    window, _, second, _ = projects
    window.show()
    for _ in range(3):
        window.create_case()
    original_order = [case.uid for case in window.cases]
    # A copied project deliberately contains the same internal UIDs.
    CaseRepository(second).save([window._copy_case(case) for case in window.cases])
    assert window.open_project(second)
    assert window.switch_project(0)
    tree = window.case_list.tree
    tree.setCurrentItem(tree.topLevelItem(2))
    rect = tree.visualItemRect(tree.topLevelItem(0))
    drop = QDropEvent(
        QPointF(rect.center().x(), rect.top() + 1), Qt.DropAction.MoveAction,
        QMimeData(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    tree.dropEvent(drop)
    assert drop.isAccepted()
    assert window.switch_project(1)
    qtbot.wait(10)
    assert [case.uid for case in window.cases] == original_order
    assert not window._order_dirty


def test_same_path_choose_activates_existing_without_open_mode_prompt(projects, monkeypatch):
    window, first, second, _ = projects
    assert window.open_project(second)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(first))
    monkeypatch.setattr(window, "_choose_project_open_mode", lambda path: pytest.fail("duplicate prompt"))
    window.choose_workspace()
    assert window.repository.workspace == first
    assert window.project_tabs.count() == 2


def assert_project_tab_order(window, expected):
    assert [project.repository.workspace for project in window._projects] == expected
    assert [window.project_tabs.tabToolTip(i) for i in range(len(expected))] == [str(path) for path in expected]
    assert window.project_tabs.currentIndex() == window._active_project_index
    assert window._projects[window._active_project_index].repository is window.repository


@pytest.mark.parametrize("active_index", [0, 1, 2])
def test_reorder_tabs_tracks_active_identity_without_saving_or_reopening(projects, monkeypatch, active_index):
    window, first, second, third = projects
    assert window.open_project(second)
    assert window.open_project(third)
    assert window.switch_project(active_index)
    window.create_case()
    window._field_changed("remarks", "keep unsaved edits while moving tabs")
    window.investigation.tabs.setCurrentIndex(3)
    window.case_list.search.setText(window.current_case.display_id)
    active = window._projects[active_index]
    case = window.current_case
    cases = window.cases
    dirty = set(window._dirty_cases)
    monkeypatch.setattr(window, "_leave_project", lambda: pytest.fail("reorder saved or reopened a project"))
    assert window.project_tabs.isMovable()
    expected = [first, second, third]
    for source, target in [(0, 2), (2, 0), (1, 2), (2, 1), (1, 0), (0, 1), (1, 1)]:
        expected.insert(target, expected.pop(source))
        window.project_tabs.moveTab(source, target)
        assert_project_tab_order(window, expected)
        assert window._projects[window._active_project_index] is active
        assert window.cases is cases
        assert window.current_case is case
        assert window._dirty and window._dirty_cases == dirty
        assert case.values["remarks"] == "keep unsaved edits while moving tabs"
        assert window.investigation.tabs.currentIndex() == 3
        assert window.case_list.search.text() == case.display_id
        window._sync_project_tabs()
        assert_project_tab_order(window, expected)


def drag_project_tab(qtbot, tabs, source, destination):
    start, end = tabs.tabRect(source).center(), tabs.tabRect(destination).center()
    qtbot.mousePress(tabs, Qt.MouseButton.LeftButton, pos=start)
    for step in range(1, 13):
        point = QPoint(start.x() + (end.x() - start.x()) * step // 12, start.y())
        qtbot.mouseMove(tabs, pos=point, delay=5)
    qtbot.mouseRelease(tabs, Qt.MouseButton.LeftButton, pos=end)


@pytest.mark.parametrize("width", [1180, 1660])
def test_real_pointer_drag_moves_tabs_in_both_directions(projects, qtbot, qapp, width):
    window, first, second, third = projects
    apply_theme(qapp)
    window.resize(width, 760)
    window.show()
    assert window.open_project(second)
    assert window.open_project(third)
    qtbot.wait(20)
    spy = QSignalSpy(window.project_tabs.tabMoved)
    drag_project_tab(qtbot, window.project_tabs, 0, 2)
    assert spy.count() >= 2
    assert_project_tab_order(window, [second, third, first])
    assert window.repository.workspace == first
    drag_project_tab(qtbot, window.project_tabs, 2, 0)
    qtbot.wait(20)
    assert_project_tab_order(window, [first, second, third])
    assert window.repository.workspace == first
    for index in range(window.project_tabs.count()):
        close = window.project_tabs.tabButton(index, QTabBar.ButtonPosition.RightSide)
        assert close.isEnabled()
        assert close.geometry() == window.project_tabs._item_rects(index)[2]
    assert not window.project_tabs._painting_drag
    assert window.project_tabs._reorder_press is None


@pytest.mark.parametrize("busy_flag", ["_commit_in_progress", "_switching_project", "_video_import_in_progress"])
def test_busy_reorder_is_rolled_back_without_switching_or_losing_alignment(projects, monkeypatch, busy_flag):
    window, first, second, third = projects
    assert window.open_project(second)
    assert window.open_project(third)
    monkeypatch.setattr(window, "_leave_project", lambda: pytest.fail("busy move switched projects"))
    setattr(window, busy_flag, True)
    try:
        for source, destination in [(0, 2), (2, 0), (0, 1)]:
            window.project_tabs.moveTab(source, destination)
            assert_project_tab_order(window, [first, second, third])
            assert window.repository.workspace == third
    finally:
        setattr(window, busy_flag, False)


def test_reordered_tabs_close_correct_project_and_reopen_existing_folder_once(projects, qtbot):
    window, first, second, third = projects
    window.show()
    window.create_case()
    first_uid = window.current_uid
    assert window.open_project(second)
    window.create_case()
    second_uid = window.current_uid
    assert window.open_project(third)
    window.create_case()
    third_uid = window.current_uid
    window._field_changed("remarks", "third project active")
    window.project_tabs.moveTab(2, 0)
    assert_project_tab_order(window, [third, first, second])
    assert window.current_uid == third_uid
    # Close an inactive tab after its index changes, without losing active edits.
    close = window.project_tabs.tabButton(1, QTabBar.ButtonPosition.RightSide)
    qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
    assert_project_tab_order(window, [third, second])
    assert window._dirty and window.current_uid == third_uid
    assert window.open_project(second)
    assert window.project_tabs.count() == 2
    assert window._active_project_index == 1
    assert window.current_uid == second_uid
    close = window.project_tabs.tabButton(1, QTabBar.ButtonPosition.RightSide)
    qtbot.mouseClick(close, Qt.MouseButton.LeftButton)
    assert_project_tab_order(window, [third])
    assert window.current_uid == third_uid
    assert not window.project_tabs.tabButton(0, QTabBar.ButtonPosition.RightSide).isEnabled()
    assert CaseRepository(first).load()[0].uid == first_uid
    assert CaseRepository(third).load()[0].values["remarks"] == "third project active"


def test_moving_active_video_tab_keeps_decoder_frame_rotation_and_playback(projects, monkeypatch):
    window, first, second, third = projects
    assert window.open_project(second)
    assert window.open_project(third)
    video = third / "manual.mp4"
    video.write_bytes(b"fake")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=100)
    window.attach_videos([str(video)])
    assert window.video_controller.seek(25)
    window.rotate_video_clockwise()
    window.video_controller.play()
    assert window.video_controller.is_playing
    capture = window.video_controller._capture
    metadata = window.video_controller.metadata
    monkeypatch.setattr(window, "_leave_project", lambda: pytest.fail("reorder stopped the video"))
    window.project_tabs.moveTab(2, 0)
    assert_project_tab_order(window, [third, first, second])
    assert window.video_controller.is_playing
    assert window.video_controller._capture is capture
    assert window.video_controller.metadata is metadata
    assert window.video_controller.current_frame == 25
    assert window.video_player.canvas.rotation_degrees == 90
    window.video_controller.pause()


def test_reordered_inactive_session_preserves_search_form_and_project_data(projects):
    window, first, second, third = projects
    window.create_case()
    uid = window.current_uid
    window._field_changed("remarks", "first project")
    window.case_list.search.setText(window.current_case.display_id)
    window.investigation.tabs.setCurrentIndex(3)
    assert window.open_project(second)
    assert window.open_project(third)
    window.project_tabs.moveTab(0, 2)
    assert_project_tab_order(window, [second, third, first])
    assert window.open_project(first)
    assert window.project_tabs.count() == 3
    assert window.current_uid == uid
    assert window.current_case.values["remarks"] == "first project"
    assert window.case_list.search.text() == window.current_case.display_id
    assert window.investigation.tabs.currentIndex() == 3
    # Replacing the moved tab must replace this project, not its old index.
    fourth = third.parent / "project_D"
    fourth.mkdir()
    assert window.open_project(fourth, new_tab=False)
    assert_project_tab_order(window, [second, third, fourth])
    assert CaseRepository(first).load()[0].uid == uid


def test_drag_does_not_cross_wire_projects_when_click_to_switch_cannot_save(projects, qtbot, monkeypatch):
    window, first, second, third = projects
    window.show()
    assert window.open_project(second)
    assert window.open_project(third)
    window.create_case()
    uid = window.current_uid
    window._field_changed("remarks", "cannot save yet")
    with monkeypatch.context() as patch:
        patch.setattr(window, "_save", lambda **kwargs: False)
        drag_project_tab(qtbot, window.project_tabs, 0, 2)
        expected = [project.repository.workspace for project in window._projects]
        assert set(expected) == {first, second, third}
        assert_project_tab_order(window, expected)
        assert window.repository.workspace == third
        assert window.current_uid == uid
        assert window.current_case.values["remarks"] == "cannot save yet"
        assert window._dirty
