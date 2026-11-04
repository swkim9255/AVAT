"""Safe relink commits and workspace-local export destinations."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import get_ident

import pytest
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

import investigator.main_window as main_module
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_BOTH, DetectedObject, ObjectBox, VideoObjects
from investigator.project_tabs_state import ProjectTabsStore
from investigator.storage import (
    CSV_EXPORT_DIRECTORY,
    REPORT_EXPORT_DIRECTORY,
    CaseRepository,
    WorkspaceError,
)


@pytest.fixture()
def relink_window(qtbot, tmp_path, monkeypatch):
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *args, **kwargs: None))
    window = MainWindow(
        tmp_path / "project", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"),
    )

    def before_close(widget):
        widget._commit_in_progress = False
        widget._workspace_load_failed = True

    qtbot.addWidget(window, before_close_func=before_close)
    monkeypatch.setattr(window, "_show_current_case", lambda **kwargs: None)
    old = str(tmp_path / "old-folder" / "21년 영상" / "영상1" / "전방영상.mp4")
    case = InvestigationCase(
        values={"institution": "samsung", "accident_classification": "2021010100001", "remarks": "keep survey"},
        videos=[old],
    )
    case.markers_by_video = {
        old: {"trigger_frame": 4, "collision_frames": [10], "speed_points": [
            {"frame": 2, "distance_m": None}, {"frame": 6, "distance_m": 8},
        ]},
    }
    case.rotation_by_video = {old: 270}
    case.set_video_objects(VideoObjects([DetectedObject(
        "obj-uid", "V001", ROLE_BOTH, None, [ObjectBox(4, (1, 2, 30, 40), .9, "car")],
        class_key="vehicle", subtype_key="passenger",
    )]))
    other = InvestigationCase(values={"institution": "samsung", "accident_classification": "2022010100001"})
    window.repository.save([other, case], active_case_uid=case.uid)
    window.cases = [other, case]
    window.current_uid = case.uid
    window._mark_saved(case.uid)
    new = window.repository.workspace / "영상폴더" / "21년 영상" / "영상1" / "전방영상.mp4"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"test fixture, not decoded")
    yield window, old, str(new)
    window._workspace_load_failed = True
    window.close()


def _disk_payload(window):
    with window.repository._connection() as connection:
        return window.repository._read_payload(connection)


def test_relink_saves_relative_paths_preserves_annotations_order_and_original_backup(relink_window):
    window, old, new = relink_window
    before = deepcopy(window.current_case.to_dict())
    original_uids = [case.uid for case in window.cases]
    assert window.relink_video_paths({old: new})

    assert [case.uid for case in window.cases] == original_uids
    assert window.current_uid == before["uid"]
    restored_repository = CaseRepository(window.repository.workspace)
    restored = restored_repository.load()[-1]
    after = restored.to_dict()
    for key in ("uid", "values", "workflow", "active_video_index"):
        if key in before:
            assert after[key] == before[key]
    assert restored.videos == [new]
    for key in ("markers_by_video", "rotation_by_video", "objects_by_video"):
        assert after[key][new] == before[key][old]
    raw = _disk_payload(window)["cases"][-1]
    assert raw["videos"] == ["영상폴더/21년 영상/영상1/전방영상.mp4"]
    assert set(raw["objects_by_video"]) == set(raw["videos"])
    backup = window.repository.latest_backup_path  # 최신 백업 1개만 유지한다.
    assert backup.is_file()
    with window.repository._connection(backup, read_only=True) as connection:
        backup_payload = window.repository._read_payload(connection)
    assert backup_payload["cases"][-1]["videos"] != raw["videos"]
    assert not window._dirty


@pytest.mark.parametrize("operation", ["backup", "save_changes"])
def test_relink_failure_preserves_live_objects_and_saved_data(relink_window, monkeypatch, operation):
    window, old, new = relink_window
    original_cases = window.cases
    before = deepcopy([case.to_dict() for case in original_cases])
    persisted = _disk_payload(window)

    def fail(*args, **kwargs):
        raise WorkspaceError("disk unavailable")

    monkeypatch.setattr(window.repository, operation, fail)
    assert not window.relink_video_paths({old: new})
    assert window.cases is original_cases
    assert [case.to_dict() for case in window.cases] == before
    assert _disk_payload(window) == persisted


def test_watcher_recovers_moved_video_before_discovering_new_cases(relink_window):
    window, _, new = relink_window
    uid = window.current_uid
    project = window._session
    project.pending_video_paths.add(new)
    window._refresh_watched_videos()
    assert window.current_uid == uid
    assert window.current_case.videos == [new]
    assert window.current_case.values["remarks"] == "keep survey"
    assert len(window.cases) == 2
    assert new not in project.pending_video_paths


def test_alias_mapping_clears_old_undo_context(relink_window, monkeypatch):
    window, old, new = relink_window
    expected = window._object_history_key(window.current_case, old)
    cleared = []
    monkeypatch.setattr(window._object_edit_history, "clear", lambda context: cleared.append(context))
    assert window.relink_video_paths({old.replace("\\", "/").swapcase(): new})
    assert cleared == [expected]


def test_relink_prepares_off_gui_thread_and_only_upserts_affected_case(relink_window, monkeypatch):
    window, old, new = relink_window
    main_thread = get_ident()
    copied_threads = []
    upserts = []
    original_copy = window._copy_case
    original_save = window.repository.save_changes
    unaffected = window.cases[0]
    affected_uid = window.current_uid

    def copy_case(case):
        copied_threads.append(get_ident())
        return original_copy(case)

    def save(cases, **kwargs):
        upserts.extend(case.uid for case in cases)
        return original_save(cases, **kwargs)

    monkeypatch.setattr(window, "_copy_case", copy_case)
    monkeypatch.setattr(window.repository, "save_changes", save)
    assert window.relink_video_paths({old: new})
    assert copied_threads and main_thread not in copied_threads
    assert upserts == [affected_uid]
    assert window.cases[0] is unaffected


def test_automatic_repair_of_other_case_does_not_reopen_playing_video(relink_window, monkeypatch):
    window, old, new = relink_window
    current = window.cases[0]
    playing = window.repository.workspace / "playing.mp4"
    playing.write_bytes(b"fake")
    current.attach_videos([str(playing)])
    window.current_uid = current.uid
    window.repository.save(window.cases, active_case_uid=current.uid)
    window._mark_saved(current.uid)

    def unexpected(**kwargs):
        pytest.fail("an inactive repair must not reload the playing video or current form")

    monkeypatch.setattr(window, "_show_current_case", unexpected)
    assert window.relink_video_paths({old: new}, automatic=True)
    assert window.current_uid == current.uid
    assert window.current_case.active_video == str(playing)


def test_workspace_load_recovers_missing_video_without_duplicate_accident(relink_window):
    window, _, new = relink_window
    uid = window.current_uid
    assert window._load_workspace()
    assert "자동으로 다시 연결" in window.statusBar().currentMessage()
    assert window.current_uid == uid
    assert window.current_case.videos == [new]
    assert len(window.cases) == 2


def test_investigation_csv_default_is_management_folder(relink_window, monkeypatch):
    window, _, _ = relink_window
    requested = []
    monkeypatch.setattr(window, "_save", lambda **kwargs: True)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(
        lambda parent, title, suggested, filters: (requested.append(Path(suggested)) or "", ""),
    ))
    window.export_csv()
    assert requested[0].parent == window.repository.workspace / CSV_EXPORT_DIRECTORY
    assert requested[0].suffix == ".csv"


def test_word_default_is_management_folder_and_custom_choice_still_used(relink_window, monkeypatch, tmp_path):
    window, _, _ = relink_window
    uid = window.current_uid

    class AcceptedReportOptions:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_uids(self):
            return [uid]

    requested = []
    custom = tmp_path / "custom reports"
    exported = []
    monkeypatch.setattr(main_module, "ReportExportDialog", AcceptedReportOptions)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(
        lambda parent, title, suggested, options: requested.append(Path(suggested)) or str(custom),
    ))

    def export(cases, folder, **kwargs):
        exported.append((cases, folder))
        return [folder / "test.docx"]

    monkeypatch.setattr(main_module, "export_case_reports", export)
    window.export_reports()
    assert requested == [window.repository.workspace / REPORT_EXPORT_DIRECTORY]
    assert exported[0][1] == custom
    assert [case.uid for case in exported[0][0]] == [uid]
