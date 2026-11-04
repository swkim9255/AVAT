"""Explicit discovery replaces manual accident creation without losing work."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import get_ident

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMessageBox, QPushButton

import investigator.main_window as main_module
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.project_tabs_state import ProjectTabsStore
from investigator.schema import ALL_FIELDS, FieldKind
from investigator.storage import CaseRepository, WorkspaceError
from investigator.video_import import MANAGED_VIDEO_DIRECTORY


def _video(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture; video decoding is deliberately stubbed")
    return str(path.resolve())


def _completed_values(number: str) -> dict:
    values = {}
    for field in ALL_FIELDS:
        if not field.required:
            continue
        if field.kind in {FieldKind.SINGLE, FieldKind.MULTI}:
            choice = field.choices[0].value
            values[field.key] = [choice] if field.kind is FieldKind.MULTI else choice
        elif field.kind is FieldKind.DECIMAL:
            values[field.key] = "10"
        elif field.kind is FieldKind.DIGITS:
            values[field.key] = "1"
        else:
            values[field.key] = "테스트"
    values.update(
        institution="samsung", accident_classification=number,
        vehicle_model_year="2023", incident_time="0930", incident_date="20210828",
        vehicle_manufacturer="kia", vehicle_model_name="테스트 차량",
        obstacle_exists="무", incident_damage_unknown="확인불가", remarks="완료 조사 내용 유지",
    )
    return values


@pytest.fixture
def refresh_window(qtbot, tmp_path, monkeypatch):
    windows = []
    opened = []
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(MainWindow, "_watch_active_project", lambda self: None)

    def open_video(self, *, case=None, preserve_on_failure=False):
        del preserve_on_failure
        target = case or self.current_case
        opened.append(target.active_video if target else None)
        return True

    monkeypatch.setattr(MainWindow, "_open_active_video", open_video)

    def create(workspace):
        window = MainWindow(
            workspace,
            project_tabs_store=ProjectTabsStore(tmp_path / f"refresh-tabs-{len(windows)}.json"),
        )
        windows.append(window)
        qtbot.addWidget(window)
        return window

    create.opened = opened
    yield create
    for window in reversed(windows):
        window._detection_job = None
        window._commit_in_progress = False
        window._video_import_in_progress = False
        window._workspace_load_failed = True
        window._watch_refresh_timer.stop()
        window._project_tabs_timer.stop()
        window._statistics_timer.stop()
        window._statistics_media_timer.stop()
        window._statistics_media_cancel.set()
        window.close()
        window.folder_watcher.shutdown()
        window._storage_worker.shutdown()
        window._statistics_media_worker.shutdown()


def _refresh_action(window):
    matches = [
        action for action in window.findChildren(QAction)
        if action.text().replace("&", "").rstrip("….") == "목록 새로고침"
    ]
    assert len(matches) == 1
    return matches[0]


def _disk_payload(repository):
    with repository._connection() as connection:
        return repository._read_payload(connection)


def test_manual_accident_creation_has_no_menu_button_or_keyboard_shortcut(refresh_window, tmp_path):
    window = refresh_window(tmp_path / "project")
    actions = window.findChildren(QAction)
    assert not any("새 사고" in action.text() for action in actions)
    assert not any(action.shortcut() == QKeySequence("Ctrl+N") for action in actions)
    assert not any("새 사고" in button.text() for button in window.findChildren(QPushButton))
    assert not window.cases
    assert window.case_list.refresh_button.text() == "목록 새로고침"
    assert _refresh_action(window).shortcut() == QKeySequence("Ctrl+R")


def test_refresh_button_discovers_nested_videos_and_keeps_current_work(
    refresh_window, tmp_path, qtbot,
):
    workspace = tmp_path / "project"
    first = _video(workspace / "영상폴더" / "2021년" / "2021010100001.mp4")
    window = refresh_window(workspace)
    current = window.current_case
    assert current is not None
    current.set_value("remarks", "현재 조사 내용을 유지합니다.")
    current.set_trigger(20)
    window._mark_case_dirty(current)
    window._refresh_case_presentation(rebuild_list=True)
    window.case_list.search.setText("202101")
    state = window.case_list.capture_view_state()
    before = deepcopy(current.to_dict())
    before_opened = list(refresh_window.opened)
    second = _video(workspace / "영상폴더" / "2023년" / "하위" / "2023010100002.mp4")

    window.show()
    qtbot.mouseClick(window.case_list.refresh_button, Qt.MouseButton.LeftButton)

    assert len(window.cases) == 2
    assert window.current_uid == current.uid
    assert window.current_case.active_video == first
    assert window.current_case.values == before["values"]
    assert window.current_case.markers_by_video == before["markers_by_video"]
    assert refresh_window.opened == before_opened
    after_state = window.case_list.capture_view_state()
    for key in ("search", "current", "selected"):
        assert after_state[key] == state[key]
    imported = next(case for case in window.cases if case.uid != current.uid)
    assert imported.videos == [second]
    stored = CaseRepository(workspace).load()
    assert len(stored) == 2
    assert next(case for case in stored if case.uid == current.uid).values["remarks"] == before["values"]["remarks"]


def test_refresh_menu_action_scans_off_gui_thread_and_does_not_duplicate_existing_video(
    refresh_window, tmp_path, monkeypatch,
):
    workspace = tmp_path / "project"
    window = refresh_window(workspace)
    added = _video(workspace / "영상폴더" / "2022010100001_1.mp4")
    threads = []
    gui_thread = get_ident()
    original_scan = main_module.scan_video_folder

    def scan(*args, **kwargs):
        threads.append(get_ident())
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(main_module, "scan_video_folder", scan)
    _refresh_action(window).trigger()
    _refresh_action(window).trigger()
    assert len(window.cases) == 1
    assert window.cases[0].videos == [added]
    assert threads and gui_thread not in threads


def test_refresh_is_blocked_during_detection_without_scanning_or_writing(
    refresh_window, tmp_path, monkeypatch,
):
    workspace = tmp_path / "project"
    window = refresh_window(workspace)
    _video(workspace / "영상폴더" / "2022010100001.mp4")
    before = list(window.cases)

    def unexpected(*args, **kwargs):
        pytest.fail("a running detector must not trigger discovery or a DB write")

    monkeypatch.setattr(main_module, "scan_video_folder", unexpected)
    monkeypatch.setattr(window.repository, "save_changes", unexpected)
    window._detection_job = object()
    try:
        window.refresh_video_list()
    finally:
        window._detection_job = None
    assert window.cases == before


def test_accident_delete_question_contains_only_requested_file_move_notice(
    refresh_window, tmp_path, monkeypatch,
):
    workspace = tmp_path / "project"
    path = _video(workspace / "영상폴더" / "2022010100001.mp4")
    window = refresh_window(workspace)
    before = deepcopy(window.current_case.to_dict())
    prompts = []

    def question(parent, title, message, buttons, default):
        prompts.append((title, message, buttons, default))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    window.delete_case(window.current_uid)
    assert prompts == [(
        "사고 삭제",
        "영상 파일은 프로젝트의 ‘삭제사고’ 폴더로 이동합니다.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )]
    assert window.current_case.to_dict() == before
    assert Path(path).exists()
    assert not (workspace / "삭제사고").exists()


def test_workspace_folders_are_tabs_and_list_contains_accidents_and_videos(refresh_window, tmp_path):
    workspace = tmp_path / "project"
    video_2021 = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2021년" / "2021010100001_1.mp4")
    video_2023 = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2023년" / "2023010100001.mp4")
    window = refresh_window(workspace)
    panel = window.case_list
    assert len(window.cases) == 2
    labels = [window.project_tabs.tabText(i) for i in range(window.project_tabs.count())]
    assert labels == ["2021년", "2023년"]
    # 현재 폴더 탭의 사고만 목록 항목으로 만든다(다른 폴더 사고는 숨기지 않고 생략).
    for label, video in (("2021년", video_2021), ("2023년", video_2023)):
        assert window.switch_video_folder(labels.index(label))
        items = panel.tree.case_items()
        assert len(items) == 1
        item = items[0]
        assert not item.isHidden()
        assert item.parent() is None
        assert item.childCount() == 1
        assert item.child(0).parent() is item
        assert item.child(0).toolTip(0) == video
        assert panel.case_item(item.data(0, 256)) is item


def test_refresh_in_empty_project_does_not_make_manual_placeholder(refresh_window, tmp_path):
    window = refresh_window(tmp_path / "project")
    window.refresh_video_list()
    assert window.cases == []
    assert window.current_uid is None
    assert window.case_list.tree.case_items() == []


def test_open_and_refresh_preserve_completed_accident_without_recovery_changes(
    refresh_window, tmp_path,
):
    workspace = tmp_path / "project"
    first = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2022년/2022010100001_1.mp4")
    second = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2022년/2022010100001_2.mp4")
    worked = InvestigationCase(values=_completed_values("2022010100001"), videos=[first, second],
                               active_video_index=1)
    worked.set_trigger(14)
    worked.set_collision(56)
    worked.set_video_rotation(90)
    worked.complete()
    before = deepcopy(worked.to_dict())
    repository = CaseRepository(workspace)
    repository.save([worked], active_case_uid=worked.uid)
    window = refresh_window(workspace)

    assert len(window.cases) == 1
    restored = window.current_case
    assert restored.uid == worked.uid and window.current_uid == worked.uid
    assert restored.to_dict() == before
    assert all(Path(path).is_file() for path in (first, second))
    expected = restored.to_dict()
    assert CaseRepository(workspace).load()[0].to_dict() == expected
    assert window.refresh_video_list()
    assert window.current_case.to_dict() == expected
    assert len(window.cases) == 1


def test_new_video_save_failure_keeps_memory_db_and_files_then_retries(
    refresh_window, tmp_path, monkeypatch,
):
    workspace = tmp_path / "project"
    first = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2022년/2022010100001_1.mp4")
    worked = InvestigationCase(values=_completed_values("2022010100001"), videos=[first])
    CaseRepository(workspace).save([worked], active_case_uid=worked.uid)
    window = refresh_window(workspace)
    second = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2022년/2022010100001_2.mp4")
    original_cases = window.cases
    before = deepcopy([case.to_dict() for case in window.cases])
    persisted = _disk_payload(window.repository)

    def fail(*args, **kwargs):
        raise WorkspaceError("fixture save failure")

    with monkeypatch.context() as failures:
        failures.setattr(window.repository, "save_changes", fail)
        assert not window.refresh_video_list()
    assert window.cases is original_cases
    assert [case.to_dict() for case in window.cases] == before
    assert _disk_payload(window.repository) == persisted
    assert Path(first).is_file() and Path(second).is_file()
    assert window.refresh_video_list()
    assert len(window.cases) == 1 and window.current_uid == worked.uid
    assert set(window.current_case.videos) == {first, second}
    assert len(CaseRepository(workspace).load()) == 1


def test_existing_001_accident_is_not_renamed_or_removed_by_discovery(
    refresh_window, tmp_path,
):
    workspace = tmp_path / "project"
    source = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2026년/2026100200002_2.mp4")
    case = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"},
                             videos=[source])
    case.set_trigger(60)
    case.set_collision(108)
    before = deepcopy(case.to_dict())
    CaseRepository(workspace).save([case], active_case_uid=case.uid)

    window = refresh_window(workspace)

    assert [item.to_dict() for item in window.cases] == [before]
    assert window.current_uid == case.uid
    assert window.refresh_video_list()
    assert [item.to_dict() for item in window.cases] == [before]
    assert [item.to_dict() for item in CaseRepository(workspace).load()] == [before]
    assert Path(source).is_file()


def test_existing_001_does_not_transfer_video_markers_to_numbered_accident(
    refresh_window, tmp_path,
):
    workspace = tmp_path / "project"
    source = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2026년/2026100200002_2.mp4")
    shell = InvestigationCase(values={"institution": "samsung", "accident_classification": "001"},
                              videos=[source])
    shell.set_trigger(60)
    target = InvestigationCase(values={"institution": "samsung", "accident_classification": "2026100200002"},
                               videos=[source])
    before = deepcopy([shell.to_dict(), target.to_dict()])
    CaseRepository(workspace).save([shell, target], active_case_uid=shell.uid)

    window = refresh_window(workspace)

    assert [item.to_dict() for item in window.cases] == before
    assert window.refresh_video_list()
    assert [item.to_dict() for item in window.cases] == before
    assert [item.to_dict() for item in CaseRepository(workspace).load()] == before
    assert Path(source).is_file()


def test_video_names_no_longer_trigger_automatic_record_consolidation(
    refresh_window, tmp_path,
):
    workspace = tmp_path / "project"
    filename = "2022010100001_1.mp4"
    old = str(tmp_path / "missing" / filename)
    current = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2022년" / filename)
    case = InvestigationCase(
        values={"institution": "samsung", "accident_classification": "2022010100001"},
        videos=[old, current],
        markers_by_video={old: {"trigger_frame": 12, "collision_frames": [34]},
                          current: {"trigger_frame": 22, "collision_frames": [34]}},
    )
    CaseRepository(workspace).save([case], active_case_uid=case.uid)
    window = refresh_window(workspace)
    restored = window.current_case
    assert restored.uid == case.uid
    assert restored.videos == [old, current]
    assert restored.markers_by_video[old]["trigger_frame"] == 12
    assert restored.markers_by_video[current]["trigger_frame"] == 22
    expected = restored.to_dict()
    assert window.refresh_video_list()
    assert window.current_case.to_dict() == expected
    assert Path(current).is_file()


def test_same_named_distinct_paths_are_imported_without_filename_dedup(
    refresh_window, tmp_path,
):
    workspace = tmp_path / "project"
    paths = [_video(workspace / MANAGED_VIDEO_DIRECTORY / folder / "2022010100001_1.mp4")
             for folder in ("A", "B")]
    window = refresh_window(workspace)
    assert len(window.cases) == 1
    assert set(window.cases[0].videos) == set(paths)
    expected = window.cases[0].to_dict()
    assert window.refresh_video_list()
    assert window.cases[0].to_dict() == expected
    assert all(Path(path).is_file() for path in paths)
