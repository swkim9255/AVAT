"""One database, folder-only navigation and independent export destinations."""

from __future__ import annotations

import os
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtGui import QAction, QFontDatabase
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.project_tabs_state import ProjectTabsState, ProjectTabsStore
from investigator.storage import (
    CSV_EXPORT_DIRECTORY,
    DATABASE_DIRECTORY,
    REPORT_EXPORT_DIRECTORY,
    SYSTEMS_DIRECTORY,
    CaseRepository,
    WorkspaceError,
)
from investigator.theme import apply_theme
from investigator.video_folders import discover_video_folders, video_root
from investigator.video_import import MANAGED_VIDEO_DIRECTORY, scan_video_tree
from investigator.widgets.csv_export_dialog import CsvExportDialog
from investigator.widgets.report_export_dialog import ReportExportDialog


def video(root, folder, name):
    path = root / MANAGED_VIDEO_DIRECTORY / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic video; decoder is stubbed")
    return str(path.resolve())


@pytest.fixture
def windows(qtbot, tmp_path, monkeypatch):
    opened, created = [], []
    monkeypatch.setattr(MainWindow, "_watch_active_project", lambda self: None)
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, lambda *args, **kwargs: None)

    def open_video(self, *, case=None, **kwargs):
        target = case or self.current_case
        opened.append(target.active_video if target else None)
        return True

    monkeypatch.setattr(MainWindow, "_open_active_video", open_video)

    def create(workspace=None, store=None):
        result = MainWindow(workspace, project_tabs_store=store or ProjectTabsStore(tmp_path / "prefs.json"))
        qtbot.addWidget(result)
        created.append(result)
        return result

    create.opened = opened
    yield create
    for window in reversed(created):
        window._workspace_load_failed = True
        window._detection_job = None
        window._project_tabs_timer.stop()
        window.close()


def visible_cases(window):
    return [item.data(0, 256) for item in window.case_list.tree.case_items() if not item.isHidden()]


def tab(window, label):
    return next(index for index in range(window.video_folder_tabs.count())
                if window.video_folder_tabs.tabText(index) == label)


def test_layout_names_and_canonical_db_only(tmp_path):
    assert DATABASE_DIRECTORY == "01_사고동영상/작업관리"
    assert SYSTEMS_DIRECTORY == "systems"
    assert MANAGED_VIDEO_DIRECTORY == "01_사고동영상"
    assert CSV_EXPORT_DIRECTORY == "02_csv내보내기"
    assert REPORT_EXPORT_DIRECTORY == "03_Report내보내기"
    repository = CaseRepository(tmp_path)
    assert repository.database_path == tmp_path / "01_사고동영상" / "작업관리" / "작업관리.db"
    # Other names and old directories are not accidentally opened or modified.
    for name in ("systems/작업관리.db", "systems/기존.db", "작업관리/작업관리.db", "작업관리DB/작업관리.db",
                 "01_사고동영상/작업관리/기존.db"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"do not read this file")
    assert repository.load() == []
    assert (tmp_path / "systems/기존.db").read_bytes() == b"do not read this file"


def test_database_placed_under_video_root_restores_work_and_relative_video_paths(windows, tmp_path):
    first = video(tmp_path, "2019년", "2019010100001.mp4")
    second = video(tmp_path, "2020년", "2020010100001.mp4")
    case = InvestigationCase(values={"institution": "samsung", "accident_classification": "2019010100001",
                                     "remarks": "기존 조사 내용"}, videos=[first],
                             markers_by_video={first: {"trigger_frame": 10}},
                             rotation_by_video={first: 90})
    repository = CaseRepository(tmp_path)
    repository.save([case], active_case_uid=case.uid)
    # A closed-app copy from the old slot must work without editing the DB.
    original = tmp_path / SYSTEMS_DIRECTORY / "작업관리.db"
    original.parent.mkdir()
    repository.database_path.rename(original)
    shutil.copy2(original, repository.database_path)
    before = original.read_bytes()
    window = windows(tmp_path)
    restored = next(item for item in window.cases if item.uid == case.uid)
    assert restored.values["remarks"] == "기존 조사 내용"
    assert restored.markers_by_video[first]["trigger_frame"] == 10
    assert restored.rotation_by_video[first] == 90
    assert len(window.cases) == 2 and any(second in item.videos for item in window.cases)
    assert [window.project_tabs.tabText(i) for i in range(window.project_tabs.count())] == ["2019년", "2020년"]
    window._field_changed("remarks", "새 경로에 저장한 조사 내용")
    assert window._save(show_message=False)
    assert original.read_bytes() == before
    reopened = CaseRepository(tmp_path)
    restored = next(item for item in reopened.load() if item.uid == case.uid)
    assert restored.values["remarks"] == "새 경로에 저장한 조사 내용"
    with reopened._connection() as connection:
        payload = reopened._read_payload(connection)
    raw = next(item for item in payload["cases"] if item["uid"] == case.uid)
    assert raw["videos"] == ["01_사고동영상/2019년/2019010100001.mp4"]
    assert set(raw["markers_by_video"]) == set(raw["videos"])


def test_folders_are_tabs_with_only_their_accidents_and_original_video_indices(windows, tmp_path):
    first = video(tmp_path, "2019년", "2019010100001.mp4")
    second = video(tmp_path, "2020년", "2020010100001.mp4")
    window = windows(tmp_path)
    assert [window.project_tabs.tabText(i) for i in range(window.project_tabs.count())] == ["2019년", "2020년"]
    assert not window.project_tabs.tabsClosable()
    assert window.project_tabs.accessibleName() == "영상 폴더 탭"
    repository = window.repository
    by_video = {case.active_video: case for case in window.cases}
    assert visible_cases(window) == [by_video[first].uid]
    assert window.case_list.case_item(by_video[first].uid).parent() is None
    assert window.switch_video_folder(tab(window, "2020년"))
    assert window.repository is repository
    assert visible_cases(window) == [by_video[second].uid]
    assert window.current_case.active_video == second
    assert window.case_list.count_label.text() == "0/1 완료"
    assert len(CaseRepository(tmp_path).load()) == 2
    assert not any("프로젝트 폴더" in action.text() for action in window.findChildren(QAction))


def test_same_case_in_two_folders_shares_identity_values_and_annotations(windows, tmp_path, monkeypatch):
    first = video(tmp_path, "2019년", "2019010100001_1.mp4")
    second = video(tmp_path, "2020년", "2019010100001_2.mp4")
    case = InvestigationCase(values={"institution": "samsung", "accident_classification": "2019010100001"},
                             videos=[first, second], markers_by_video={first: {"trigger_frame": 10}})
    CaseRepository(tmp_path).save([case], active_case_uid=case.uid)
    window = windows(tmp_path)
    original = window.current_case
    window._field_changed("remarks", "모든 폴더에서 같은 조사 내용")
    monkeypatch.setattr(window.repository, "load", lambda **kwargs: pytest.fail("folder changes must not reload DB"))
    assert window.switch_video_folder(tab(window, "2020년"))
    assert len(window.cases) == 1 and window.current_case is original
    assert original.values["remarks"] == "모든 폴더에서 같은 조사 내용"
    assert original.markers_by_video[first]["trigger_frame"] == 10
    assert original.active_video_index == 1
    item = window.case_list.case_item(case.uid)
    assert item.child(0).isHidden() and not item.child(1).isHidden()
    window.case_list.tree.setCurrentItem(item.child(1))
    assert window.current_case.active_video == second
    assert window.switch_video_folder(tab(window, "2019년"))
    assert original.active_video_index == 0
    assert original.values["remarks"] == "모든 폴더에서 같은 조사 내용"


def test_save_failure_keeps_original_folder_and_work(windows, tmp_path, monkeypatch):
    video(tmp_path, "2019년", "2019010100001.mp4")
    video(tmp_path, "2020년", "2020010100001.mp4")
    window = windows(tmp_path)
    window._field_changed("remarks", "저장 실패해도 유지")
    original = window.current_case
    old_index = window.project_tabs.currentIndex()

    def fail(*args, **kwargs):
        raise WorkspaceError("disk full")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    window.project_tabs.setCurrentIndex(tab(window, "2020년"))
    assert window.project_tabs.currentIndex() == old_index
    assert window.current_case is original
    assert original.values["remarks"] == "저장 실패해도 유지"
    assert window._dirty


def test_refresh_adds_new_folder_without_changing_current_scope_or_playback(windows, tmp_path):
    first = video(tmp_path, "2019년", "2019010100001.mp4")
    window = windows(tmp_path)
    before_opened = list(windows.opened)
    video(tmp_path, "2023년", "2023010100001.mp4")
    assert window.refresh_video_list()
    assert window.project_tabs.count() == 2
    assert window.current_case.active_video == first
    assert windows.opened == before_opened
    assert len(visible_cases(window)) == 1
    assert len(window.cases) == 2


def test_tab_move_and_restart_restore_only_one_workspace(windows, tmp_path):
    workspace = tmp_path / "work"
    video(workspace, "2019년", "2019010100001.mp4")
    video(workspace, "2023년", "2023010100001.mp4")
    store = ProjectTabsStore(tmp_path / "settings.json")
    # Older versions can contain several projects; only the last selected one opens.
    unused = tmp_path / "unused"
    store.save(ProjectTabsState((unused, workspace), workspace))
    window = windows(store=store)
    assert window.repository.workspace == workspace
    assert not unused.exists()
    assert window.switch_video_folder(tab(window, "2023년"))
    before = list(windows.opened)
    window.project_tabs.moveTab(1, 0)
    assert window.current_case.active_video.endswith("2023010100001.mp4")
    assert windows.opened == before
    window._save_project_tabs()
    state = store.load()
    assert state.folders == (workspace,)
    assert state.video_folder_order[0].endswith("2023년")
    window.close()
    restored = windows(store=store)
    assert restored.project_tabs.tabText(0) == "2023년"
    assert restored.project_tabs.currentIndex() == 0
    assert restored.current_case.active_video.endswith("2023010100001.mp4")


def test_opening_another_workspace_replaces_instead_of_stacking(windows, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    video(first, "2019년", "2019010100001.mp4")
    video(second, "2020년", "2020010100001.mp4")
    window = windows(first)
    window._field_changed("remarks", "이전 작업 폴더에 보존")
    assert window.open_project(second, new_tab=True)  # Old callers cannot create project tabs.
    assert window.repository.workspace == second
    assert len(window.cases) == 1
    assert window.project_tabs.count() == 1
    assert window.project_tabs.tabText(0) == "2020년"
    assert CaseRepository(first).load()[0].values["remarks"] == "이전 작업 폴더에 보존"


def test_failed_workspace_open_restores_original_work(windows, tmp_path):
    original_root, broken = tmp_path / "original", tmp_path / "broken"
    video(original_root, "2019년", "2019010100001.mp4")
    database = CaseRepository(broken).database_path
    database.parent.mkdir(parents=True)
    database.write_bytes(b"corrupt sqlite")
    window = windows(original_root)
    case = window.current_case
    window._field_changed("remarks", "다시 돌아와야 하는 사고")
    assert not window.open_project(broken)
    assert window.repository.workspace == original_root
    assert window.current_case.uid == case.uid
    assert window.current_case.values["remarks"] == "다시 돌아와야 하는 사고"
    assert visible_cases(window) == [case.uid]


def test_exports_default_to_csv_and_report_directories(windows, tmp_path, monkeypatch):
    video(tmp_path, "2019년", "2019010100001.mp4")
    window = windows(tmp_path)
    requested = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda parent, title, initial, kind:
                        (requested.append(Path(initial)) or str(initial), kind))
    monkeypatch.setattr(CsvExportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    exported = []
    monkeypatch.setattr(window.repository, "export_csv", lambda path, cases, **kwargs: exported.append(Path(path)) or Path(path))
    window.export_csv()
    assert requested[0].parent == tmp_path / CSV_EXPORT_DIRECTORY
    assert exported == requested
    monkeypatch.setattr(ReportExportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda parent, title, initial, *args:
                        (requested.append(Path(initial)) or ""))
    before = deepcopy([case.to_dict() for case in window.cases])
    window.export_reports()
    assert requested[-1] == tmp_path / REPORT_EXPORT_DIRECTORY
    assert requested[-1].is_dir()
    assert [case.to_dict() for case in window.cases] == before


def test_video_scopes_ignore_system_and_export_folders(tmp_path):
    video(tmp_path, "2019년", "2019010100001.mp4")
    for name in (DATABASE_DIRECTORY, SYSTEMS_DIRECTORY, CSV_EXPORT_DIRECTORY, REPORT_EXPORT_DIRECTORY):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / name / "9999999999999.mp4").write_bytes(b"must not import")
    assert [folder.label for folder in discover_video_folders(tmp_path)] == ["2019년"]
    files, directories = scan_video_tree(tmp_path)
    assert [path.name for path in files] == ["2019010100001.mp4"]
    assert tmp_path / DATABASE_DIRECTORY not in directories


def test_management_only_video_root_does_not_hide_legacy_video_layout(tmp_path):
    repository = CaseRepository(tmp_path)
    repository.save([])
    legacy = tmp_path / "영상폴더"
    legacy.mkdir()
    (legacy / "2019010100001.mp4").touch()
    assert video_root(tmp_path) == tmp_path
    assert [folder.label for folder in discover_video_folders(tmp_path)] == ["영상폴더"]
    (tmp_path / MANAGED_VIDEO_DIRECTORY / "2019년").mkdir()
    assert video_root(tmp_path) == tmp_path / MANAGED_VIDEO_DIRECTORY
    assert [folder.label for folder in discover_video_folders(tmp_path)] == ["2019년"]


def test_empty_workspace_db_does_not_create_management_tab(tmp_path):
    repository = CaseRepository(tmp_path)
    repository.save([])
    folders = discover_video_folders(tmp_path)
    assert [folder.label for folder in folders] == [MANAGED_VIDEO_DIRECTORY]
    assert repository.database_path.is_file()
    assert not (tmp_path / SYSTEMS_DIRECTORY / "작업관리.db").exists()


def test_folder_tabs_fit_menu_bar_at_minimum_width(windows, tmp_path, qtbot, qapp):
    # The offscreen Qt plugin does not enumerate Windows' system fonts.
    font_path = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "Fonts/malgun.ttf"
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
    apply_theme(qapp)
    for year in range(2019, 2024):
        video(tmp_path, f"{year}년", f"{year}010100001.mp4")
    window = windows(tmp_path)
    window.resize(1180, 760)
    window.show()
    qtbot.wait(30)
    assert window.project_tabs.count() == 5
    assert not window.project_tabs.geometry().intersects(window.view_mode.geometry())
    assert not window.project_add_button.geometry().intersects(window.view_mode.geometry())
    assert window.video_workspace.width() > 300
    assert window.investigation.width() > 300
    assert window.grab().save(str(tmp_path / "folder-tabs.png"))
