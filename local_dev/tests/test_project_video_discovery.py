"""Project opening discovers new videos and safely repairs unambiguous links."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import get_ident

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

import investigator.main_window as main_module
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_BOTH, DetectedObject, ObjectBox, VideoObjects
from investigator.project_tabs_state import ProjectTabsStore
from investigator.schema import ALL_FIELDS, FieldKind
from investigator.storage import CaseRepository, WorkspaceError


def video(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real video; decoding is stubbed")
    return str(path.resolve())


def valid_values(number: str) -> dict:
    values = {}
    for field in ALL_FIELDS:
        if not field.required:
            continue
        if field.kind in {FieldKind.SINGLE, FieldKind.MULTI}:
            value = field.choices[0].value
            values[field.key] = [value] if field.kind is FieldKind.MULTI else value
        elif field.key == "vehicle_model_year":
            values[field.key] = "2023"
        elif field.key == "incident_time":
            values[field.key] = "0930"
        elif field.key == "incident_date":
            values[field.key] = "20210828"
        elif field.kind is FieldKind.DECIMAL:
            values[field.key] = "10"
        elif field.kind is FieldKind.DIGITS:
            values[field.key] = "1"
        else:
            values[field.key] = "테스트"
    values.update(institution="samsung", accident_classification=number,
                  vehicle_manufacturer="kia", vehicle_model_name="테스트 차량",
                  obstacle_exists="무", incident_damage_unknown="확인불가",
                  remarks="조사값과 완료 상태 유지")
    day = datetime.strptime(str(values["incident_date"]), "%Y%m%d").weekday()
    values["incident_day"] = ("월", "화", "수", "목", "금", "토", "일")[day]
    return values


def completed_case(old: str, number="2021082800001") -> InvestigationCase:
    case = InvestigationCase(values=valid_values(number), videos=[old])
    case.markers_by_video = {old: {
        "trigger_frame": 4, "collision_frames": [10],
        "speed_points": [{"frame": 2, "distance_m": None}, {"frame": 6, "distance_m": 8}],
    }}
    case.rotation_by_video = {old: 270}
    case.set_video_objects(VideoObjects([DetectedObject(
        "stable-object-uid", "V001", ROLE_BOTH, None,
        [ObjectBox(4, (1, 2, 30, 40), .9, "car")],
        class_key="vehicle", subtype_key="passenger",
    )]))
    assert case.can_complete
    case.complete()
    return case


def persisted_payload(repository):
    with repository._connection() as connection:
        return repository._read_payload(connection)


@pytest.fixture
def discovery_window(qtbot, tmp_path, monkeypatch):
    windows = []
    opened_paths = []
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(MainWindow, "_watch_active_project", lambda self: None)

    def open_video(self, *, case=None, preserve_on_failure=False):
        del preserve_on_failure
        case = case or self.current_case
        opened_paths.append(case.active_video if case else None)
        return True

    monkeypatch.setattr(MainWindow, "_open_active_video", open_video)

    def create(workspace):
        window = MainWindow(workspace, project_tabs_store=ProjectTabsStore(tmp_path / f"tabs-{len(windows)}.json"))
        windows.append(window)

        def before_close(widget):
            widget._commit_in_progress = False
            widget._video_import_in_progress = False
            widget._workspace_load_failed = True

        qtbot.addWidget(window, before_close_func=before_close)
        return window

    create.opened_paths = opened_paths
    yield create
    for window in reversed(windows):
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


@pytest.mark.parametrize("filename", ["2021082800001_1.mp4", "전방영상.mp4"])
def test_startup_relinks_unique_external_copy_before_opening_and_keeps_completed_annotations(
    discovery_window, tmp_path, filename,
):
    workspace = tmp_path / "project"
    old = str(tmp_path / "missing old external folder" / filename)
    case = completed_case(old)
    before = deepcopy(case.to_dict())
    repository = CaseRepository(workspace)
    repository.save([case], active_case_uid=case.uid)
    new = video(workspace / "영상폴더" / "21년 영상" / "영상1" / filename)
    window = discovery_window(workspace)
    assert len(window.cases) == 1
    restored = window.cases[0]
    assert restored.uid == before["uid"]
    assert restored.videos == [new]
    after = restored.to_dict()
    assert after["values"] == before["values"]
    assert after["workflow"] == before["workflow"]
    for key in ("markers_by_video", "rotation_by_video", "objects_by_video"):
        assert after[key][new] == before[key][old]
    assert old not in discovery_window.opened_paths
    assert new in discovery_window.opened_paths
    assert CaseRepository(workspace).load()[0].to_dict() == after
    assert list((workspace / "systems" / "backups").glob("영상경로변경전-*.db"))


def test_startup_recursively_discovers_numbered_clips_without_creating_generic_accidents(discovery_window, tmp_path):
    workspace = tmp_path / "project"
    first = video(workspace / "영상폴더" / "21년" / "영상1" / "2021082800001_1.mp4")
    second = video(workspace / "영상폴더" / "21년" / "영상1" / "2021082800001_2.mp4")
    video(workspace / "영상폴더" / "22년" / "영상2" / "전방영상.mp4")
    window = discovery_window(workspace)
    assert len(window.cases) == 1
    assert window.cases[0].values["accident_classification"] == "2021082800001"
    assert window.cases[0].videos == [first, second]
    assert CaseRepository(workspace).load()[0].videos == [first, second]


def test_startup_directory_scan_runs_outside_gui_thread(discovery_window, tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    video(workspace / "영상폴더" / "2021082800001.mp4")
    main_thread = get_ident()
    threads = []
    original = main_module.scan_video_folder

    def scan(*args, **kwargs):
        threads.append(get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "scan_video_folder", scan)
    window = discovery_window(workspace)
    assert window.cases
    assert threads and main_thread not in threads


def test_reopening_current_project_discovers_newly_added_video(discovery_window, tmp_path):
    workspace = tmp_path / "project"
    window = discovery_window(workspace)
    assert not window.cases
    new = video(workspace / "영상폴더" / "2022010100001.mp4")
    assert window.open_project(workspace)
    assert len(window.cases) == 1
    assert window.cases[0].videos == [new]
    assert window._session.repository is window.repository


def test_switching_back_to_initialized_project_discovers_new_video(discovery_window, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    second.mkdir()
    window = discovery_window(first)
    assert window.open_project(second)
    assert window.repository.workspace == second
    new = video(first / "영상폴더" / "2023010100001.mp4")
    assert window.open_project(first)
    assert window.repository.workspace == first
    assert len(window.cases) == 1
    assert window.cases[0].videos == [new]


@pytest.mark.parametrize("filename", ["2021082800001_1.mp4", "전방영상.mp4"])
def test_ambiguous_old_link_remains_reserved_but_unrelated_new_accident_is_imported(
    discovery_window, tmp_path, filename,
):
    workspace = tmp_path / "project"
    old = str(tmp_path / "missing folder" / filename)
    case = completed_case(old)
    before = deepcopy(case.to_dict())
    repository = CaseRepository(workspace)
    repository.save([case], active_case_uid=case.uid)
    candidates = [video(workspace / "영상폴더" / year / filename) for year in ("21년", "22년")]
    unrelated = video(workspace / "영상폴더" / "2023010100001.mp4")
    window = discovery_window(workspace)
    assert len(window.cases) == 2
    existing = next(item for item in window.cases if item.uid == case.uid)
    assert existing.to_dict() == before
    assert all(path not in item.videos for item in window.cases for path in candidates)
    imported = next(item for item in window.cases if item.uid != case.uid)
    assert imported.videos == [unrelated]
    assert len(CaseRepository(workspace).load()) == 2


@pytest.mark.parametrize("operation", ["backup", "save_changes"])
def test_automatic_repair_failure_preserves_memory_and_db_without_importing_duplicates(
    discovery_window, tmp_path, monkeypatch, operation,
):
    workspace = tmp_path / "project"
    old = str(tmp_path / "missing folder" / "2021082800001_1.mp4")
    case = completed_case(old)
    repository = CaseRepository(workspace)
    repository.save([case], active_case_uid=case.uid)
    window = discovery_window(workspace)
    original_cases = window.cases
    before = deepcopy([item.to_dict() for item in original_cases])
    stored = persisted_payload(window.repository)
    video(workspace / "영상폴더" / "2021082800001_1.mp4")
    video(workspace / "영상폴더" / "2023010100001.mp4")

    def fail(*args, **kwargs):
        raise WorkspaceError("simulated disk unavailable")

    monkeypatch.setattr(window.repository, operation, fail)
    assert not window._synchronize_project_videos(open_video=False)
    assert window.cases is original_cases
    assert [item.to_dict() for item in window.cases] == before
    assert persisted_payload(window.repository) == stored


def test_no_manual_video_folder_import_action_is_exposed(discovery_window, tmp_path):
    window = discovery_window(tmp_path / "project")
    action_titles = [action.text().replace("&", "") for action in window.findChildren(QAction)]
    assert not any("영상 폴더 불러오기" in text or "영상 폴더 가져오기" in text for text in action_titles)
    assert not any("영상 폴더 연결" in text or "영상 폴더 다시 연결" in text for text in action_titles)
    assert any("현재 사고에 영상 추가" in text for text in action_titles)


def test_startup_relinks_current_canonical_db_without_schema_conversion(
    discovery_window, tmp_path,
):
    workspace = tmp_path / "current project"
    repository = CaseRepository(workspace)
    repository.ensure_workspace()
    old = str(tmp_path / "missing external folder" / "2021082800001_1.mp4")
    case = completed_case(old)
    original = deepcopy(case.to_dict())
    repository.save([case], active_case_uid=case.uid)
    assert repository.database_path.exists()
    new = video(workspace / "영상폴더" / "21년 영상" / "영상1" / "2021082800001_1.mp4")

    window = discovery_window(workspace)

    assert len(window.cases) == 1
    restored = window.cases[0]
    after = restored.to_dict()
    assert restored.uid == case.uid
    assert restored.status == "completed"
    assert restored.videos == [new]
    assert window.current_uid == case.uid
    assert after["values"] == original["values"]
    assert after["workflow"] == original["workflow"]
    for key in ("markers_by_video", "rotation_by_video", "objects_by_video"):
        assert after[key][new] == original[key][old]
    backup, = repository.backup_directory.glob("영상경로변경전-*.db")
    with repository._connection(backup, read_only=True) as connection:
        backed_up = repository._read_payload(connection)
    assert backed_up["schema_version"] == 4
    assert backed_up["path_storage"] == "workspace-relative-v1"
    assert backed_up["cases"][0]["videos"] == ["../missing external folder/2021082800001_1.mp4"]
    assert backed_up["cases"][0]["values"] == original["values"]
    assert backed_up["cases"][0]["workflow"] == original["workflow"]
    assert window.repository.database_path == workspace / "01_사고동영상" / "작업관리" / "작업관리.db"
    payload = persisted_payload(window.repository)
    assert payload["schema_version"] == 4
    assert payload["path_storage"] == "workspace-relative-v1"
    assert payload["cases"][0]["videos"] == ["영상폴더/21년 영상/영상1/2021082800001_1.mp4"]
    assert old not in discovery_window.opened_paths
    assert new in discovery_window.opened_paths
    assert CaseRepository(workspace).load()[0].to_dict() == after
    assert not list(repository.backup_directory.glob("investigations.pre-*.db"))
