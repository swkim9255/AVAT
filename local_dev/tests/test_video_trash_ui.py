"""Recoverable file deletion and DB updates stay in sync at the UI boundary."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

import investigator.main_window as main_module
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.object_detection import ROLE_TRIGGER, add_manual_object
from investigator.project_tabs_state import ProjectTabsStore
from investigator.storage import CaseRepository, WorkspaceError
from investigator.video_controller import PlaybackState, VideoMetadata
from investigator.video_trash import QuarantinedVideos


@pytest.fixture()
def trash_window(qtbot, tmp_path, monkeypatch):
    messages = []
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(
            lambda *args, **kwargs: messages.append(args),
        ))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    ))
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda *args, **kwargs: True)
    window = MainWindow(
        tmp_path / "project", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"),
    )

    def before_close(widget):
        widget._commit_in_progress = False
        widget._workspace_load_failed = True

    qtbot.addWidget(window, before_close_func=before_close)
    window.folder_watcher.shutdown()
    monkeypatch.setattr(window, "_show_current_case", lambda **kwargs: None)
    monkeypatch.setattr(window, "_watch_active_project", lambda: None)
    folder = window.repository.workspace / "영상폴더" / "2020"
    folder.mkdir(parents=True)
    cases = []
    for number, names in (
        ("2020010100001", ("2020010100001_1.mp4", "2020010100001_2.mp4")),
        ("2021010100002", ("2021010100002.mp4",)),
    ):
        paths = []
        for name in names:
            path = folder / name
            path.write_bytes(name.encode("ascii"))
            paths.append(str(path))
        case = InvestigationCase(
            values={
                "institution": "samsung", "accident_classification": number,
                "remarks": f"keep survey {number}",
            },
            videos=paths,
        )
        for index, path in enumerate(paths):
            case.active_video_index = index
            case.markers_by_video[path] = {
                "trigger_frame": 4, "collision_frames": [10],
                "speed_points": [{"frame": 2, "distance_m": None}, {"frame": 6, "distance_m": 8}],
            }
            case.rotation_by_video[path] = 90
            objects, _ = add_manual_object(
                case.video_objects(), 4, (1.0, 2.0, 30.0, 40.0), ROLE_TRIGGER, None,
            )
            case.set_video_objects(objects)
        case.active_video_index = 0
        cases.append(case)
    window.repository.save(cases, active_case_uid=cases[0].uid)
    window.cases = cases
    window.current_uid = cases[0].uid
    window._mark_saved(cases[0].uid)
    window.case_list.set_cases(cases, cases[0].uid)
    yield window, messages
    window._workspace_load_failed = True
    window.close()


def _saved_cases(window):
    return CaseRepository(window.repository.workspace).load()


def _archived_videos(window):
    return sorted((window.repository.workspace / "systems" / "사용자데이터" / "삭제사고").rglob("*.mp4"))


def _raw_db(window):
    with window.repository._connection() as connection:
        return window.repository._read_payload(connection)


def test_case_delete_moves_all_its_files_and_removes_only_target_case(trash_window):
    window, _ = trash_window
    removed, survivor = window.cases
    originals = [Path(path) for path in removed.videos]
    survivor_before = deepcopy(survivor.to_dict())

    window.delete_case(removed.uid)

    assert [case.uid for case in window.cases] == [survivor.uid]
    assert [case.uid for case in _saved_cases(window)] == [survivor.uid]
    assert window.current_uid == survivor.uid
    assert window.cases[0].to_dict() == survivor_before
    assert all(not path.exists() for path in originals)
    assert {path.name for path in _archived_videos(window)} == {path.name for path in originals}
    assert [path.read_bytes() for path in _archived_videos(window)] == [
        path.name.encode("ascii") for path in _archived_videos(window)
    ]
    assert all(Path(path).is_file() for path in survivor.videos)


def test_video_delete_removes_selected_annotations_and_preserves_other_video(trash_window):
    window, _ = trash_window
    case, other = window.cases
    selected, retained = case.videos
    retained_markers = deepcopy(case.markers_by_video[retained])
    retained_objects = deepcopy(case.objects_by_video[retained])
    values = deepcopy(case.values)

    window.delete_video(case.uid, 0)

    saved = {item.uid: item for item in _saved_cases(window)}
    edited = saved[case.uid]
    assert edited.videos == [retained]
    assert edited.values == values
    assert set(edited.markers_by_video) == {retained}
    assert set(edited.rotation_by_video) == {retained}
    assert set(edited.objects_by_video) == {retained}
    assert edited.markers_by_video[retained] == retained_markers
    assert edited.objects_by_video[retained] == retained_objects
    assert not Path(selected).exists()
    assert Path(retained).is_file()
    assert saved[other.uid].to_dict() == other.to_dict()
    assert [path.name for path in _archived_videos(window)] == [Path(selected).name]


def test_reopening_does_not_rediscover_quarantined_case(trash_window):
    window, _ = trash_window
    removed, survivor = window.cases
    window.delete_case(removed.uid)

    assert window._load_workspace()

    assert [case.uid for case in window.cases] == [survivor.uid]
    assert [case.uid for case in _saved_cases(window)] == [survivor.uid]
    assert len(_archived_videos(window)) == 2


def test_missing_source_still_allows_removal_of_stale_video_reference(trash_window):
    window, _ = trash_window
    case = window.current_case
    missing = Path(case.videos[0])
    retained = case.videos[1]
    missing.unlink()

    window.delete_video(case.uid, 0)

    saved = {item.uid: item for item in _saved_cases(window)}[case.uid]
    assert saved.videos == [retained]
    assert all(missing.name != path.name for path in _archived_videos(window))
    assert Path(retained).is_file()


@pytest.mark.parametrize("operation", ["backup", "save_changes"])
def test_db_failure_rolls_back_files_and_preserves_live_models(trash_window, monkeypatch, operation):
    window, messages = trash_window
    case = window.current_case
    original_cases = window.cases
    before = deepcopy([item.to_dict() for item in original_cases])
    persisted = _raw_db(window)

    def fail(*args, **kwargs):
        raise WorkspaceError("fixture disk failure")

    monkeypatch.setattr(window.repository, operation, fail)
    window.delete_video(case.uid, 0)

    assert window.cases is original_cases
    assert [item.to_dict() for item in window.cases] == before
    assert _raw_db(window) == persisted
    assert all(Path(path).is_file() for item in window.cases for path in item.videos)
    assert not _archived_videos(window)
    assert messages


def test_busy_source_move_failure_leaves_db_and_models_untouched(trash_window, monkeypatch):
    window, messages = trash_window
    case = window.current_case
    original_cases = window.cases
    before = deepcopy([item.to_dict() for item in original_cases])
    persisted = _raw_db(window)

    def fail(*args, **kwargs):
        raise PermissionError("fixture: video held open by another process")

    monkeypatch.setattr(main_module, "quarantine_videos", fail)
    window.delete_case(case.uid)

    assert window.cases is original_cases
    assert [item.to_dict() for item in window.cases] == before
    assert _raw_db(window) == persisted
    assert all(Path(path).is_file() for item in window.cases for path in item.videos)
    assert not _archived_videos(window)
    assert messages


def test_shared_file_is_kept_for_surviving_case_while_selected_case_is_deleted(trash_window):
    window, _ = trash_window
    removed, survivor = window.cases
    shared = removed.videos[0]
    survivor.attach_videos([shared])
    window.repository.save(window.cases, active_case_uid=removed.uid)
    window._mark_saved(removed.uid)

    window.delete_case(removed.uid)

    assert [item.uid for item in _saved_cases(window)] == [survivor.uid]
    assert Path(shared).is_file()
    assert shared in window.cases[0].videos
    assert Path(shared).name not in {path.name for path in _archived_videos(window)}
    assert [path.name for path in _archived_videos(window)] == [Path(removed.videos[1]).name]
    assert window._load_workspace()
    assert [item.uid for item in window.cases] == [survivor.uid]
    assert shared in window.cases[0].videos


def test_duplicate_video_delete_keeps_file_until_last_stale_record_is_deleted(trash_window):
    window, _ = trash_window
    case = window.current_case
    actual = case.videos[0]
    stale = str(window.repository.workspace / "old missing folder" / Path(actual).name)
    case.videos.append(stale)
    case.markers_by_video[stale] = {"trigger_frame": 1, "collision_frames": [3]}
    window.repository.save(window.cases, active_case_uid=case.uid)
    window._mark_saved(case.uid)

    window.delete_video(case.uid, 0)

    remaining = next(item for item in window.cases if item.uid == case.uid)
    assert stale in remaining.videos and actual not in remaining.videos
    assert remaining.markers_by_video[stale]["trigger_frame"] == 1
    assert Path(actual).is_file()
    assert not _archived_videos(window)
    assert actual.casefold() not in window.repository.auto_import_exclusions

    window.delete_video(case.uid, remaining.videos.index(stale))

    assert not Path(actual).exists()
    assert [path.name for path in _archived_videos(window)] == [Path(actual).name]
    assert case.uid in {item.uid for item in _saved_cases(window)}
    assert window._load_workspace()
    restored = next(item for item in window.cases if item.uid == case.uid)
    assert all(Path(path).name != Path(actual).name for path in restored.videos)


def test_identical_path_duplicate_deletion_preserves_surviving_annotation_set(trash_window):
    window, _ = trash_window
    case = window.current_case
    actual = case.videos[0]
    case.videos.append(actual)
    markers = deepcopy(case.markers_by_video[actual])
    objects = deepcopy(case.objects_by_video[actual])
    window.repository.save(window.cases, active_case_uid=case.uid)
    window._mark_saved(case.uid)
    window.delete_video(case.uid, 0)
    restored = next(item for item in _saved_cases(window) if item.uid == case.uid)
    assert restored.videos.count(actual) == 1
    assert restored.markers_by_video[actual] == markers
    assert restored.objects_by_video[actual] == objects
    assert restored.rotation_by_video[actual] == 90
    assert Path(actual).is_file()
    assert not _archived_videos(window)


def test_retained_file_can_relink_to_surviving_stale_record_after_refresh(trash_window):
    window, _ = trash_window
    case = window.current_case
    actual = case.videos[0]
    stale = str(window.repository.workspace / "old" / Path(actual).name)
    case.videos.append(stale)
    case.markers_by_video[stale] = {"trigger_frame": 1, "collision_frames": [3]}
    window.repository.save(window.cases, active_case_uid=case.uid)
    window._mark_saved(case.uid)
    window.delete_video(case.uid, 0)
    assert window._load_workspace()
    restored = next(item for item in window.cases if item.uid == case.uid)
    assert actual in restored.videos and stale not in restored.videos
    assert restored.markers_by_video[actual]["trigger_frame"] == 1
    assert Path(actual).is_file()


def test_different_same_name_path_in_other_accident_protects_file_on_case_delete(trash_window):
    window, _ = trash_window
    removed, survivor = window.cases
    actual = removed.videos[0]
    stale = str(window.repository.workspace / "old" / Path(actual).name)
    survivor.videos.append(stale)
    survivor.markers_by_video[stale] = {"trigger_frame": 1, "collision_frames": [3]}
    window.repository.save(window.cases, active_case_uid=removed.uid)
    window._mark_saved(removed.uid)
    window.delete_case(removed.uid)
    assert Path(actual).is_file()
    assert [case.uid for case in _saved_cases(window)] == [survivor.uid]
    assert Path(actual).name not in {path.name for path in _archived_videos(window)}
    window.delete_case(survivor.uid)
    assert not Path(actual).exists()
    assert len([path for path in _archived_videos(window) if path.name == Path(actual).name]) == 1
    assert _saved_cases(window) == []


def test_last_stale_record_delete_save_failure_restores_discovered_file(trash_window, monkeypatch):
    window, _ = trash_window
    case = window.current_case
    actual = case.videos[0]
    stale = str(window.repository.workspace / "old" / Path(actual).name)
    case.videos[0] = stale
    window.repository.save(window.cases, active_case_uid=case.uid)
    window._mark_saved(case.uid)
    before = _raw_db(window)

    def fail(*args, **kwargs):
        raise WorkspaceError("fixture disk full")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    window.delete_video(case.uid, 0)
    assert Path(actual).is_file()
    assert stale in window.current_case.videos
    assert _raw_db(window) == before
    assert not _archived_videos(window)


def test_deleting_only_video_keeps_its_accident_and_does_not_reattach_after_reopen(trash_window):
    window, _ = trash_window
    case = window.cases[1]
    video = case.videos[0]
    values = deepcopy(case.values)

    window.delete_video(case.uid, 0)

    saved = {item.uid: item for item in _saved_cases(window)}[case.uid]
    assert not saved.videos
    assert not saved.markers_by_video
    assert not saved.rotation_by_video
    assert not saved.objects_by_video
    assert saved.values == values
    assert not Path(video).exists()
    assert window._load_workspace()
    reopened = {item.uid: item for item in window.cases}[case.uid]
    assert not reopened.videos
    assert reopened.values == values


@pytest.mark.parametrize("kind", ["case", "video"])
def test_cancel_does_not_change_files_db_or_models(trash_window, monkeypatch, kind):
    window, _ = trash_window
    original_cases = window.cases
    before = deepcopy([item.to_dict() for item in original_cases])
    persisted = _raw_db(window)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    ))

    if kind == "case":
        window.delete_case(window.current_uid)
    else:
        window.delete_video(window.current_uid, 0)

    assert window.cases is original_cases
    assert [item.to_dict() for item in window.cases] == before
    assert _raw_db(window) == persisted
    assert all(Path(path).is_file() for item in window.cases for path in item.videos)
    assert not _archived_videos(window)


def _playing_controller(window, path):
    controller = window.video_controller
    controller._metadata = VideoMetadata(Path(path), 30.0, 100, 32, 32, 30.0)
    controller._current_frame = 17
    controller._state = PlaybackState.PLAYING
    return controller


def test_decoder_releases_current_video_before_file_is_moved(trash_window, monkeypatch):
    window, _ = trash_window
    selected = window.current_case.videos[0]
    controller = _playing_controller(window, selected)
    events = []
    close = controller.close_video
    quarantine = main_module.quarantine_videos

    def close_video():
        events.append("close")
        close()

    def move(*args, **kwargs):
        events.append("move")
        assert controller.metadata is None
        return quarantine(*args, **kwargs)

    monkeypatch.setattr(controller, "close_video", close_video)
    monkeypatch.setattr(main_module, "quarantine_videos", move)

    window.delete_video(window.current_uid, 0)

    assert events[:2] == ["close", "move"]
    assert not Path(selected).exists()
    assert Path(selected).name in {path.name for path in _archived_videos(window)}


def test_failed_db_save_restores_file_then_previous_playback_position(trash_window, monkeypatch):
    window, _ = trash_window
    selected = window.current_case.videos[0]
    controller = _playing_controller(window, selected)
    events = []
    close = controller.close_video

    def close_video():
        events.append("close")
        close()

    def reopen(*args, **kwargs):
        assert Path(selected).is_file(), "the archived file must be restored before reopening"
        assert window.current_case.active_video == selected
        events.append("open")
        return True

    def fail(*args, **kwargs):
        raise WorkspaceError("fixture save failure")

    monkeypatch.setattr(controller, "close_video", close_video)
    monkeypatch.setattr(controller, "seek", lambda frame: events.append(("seek", frame)) or True)
    monkeypatch.setattr(controller, "play", lambda: events.append("play") or True)
    monkeypatch.setattr(window, "_open_active_video", reopen)
    monkeypatch.setattr(window.repository, "save_changes", fail)

    window.delete_video(window.current_uid, 0)

    assert events == ["close", "open", ("seek", 17), "play"]
    assert Path(selected).is_file()
    assert not _archived_videos(window)


def test_backup_failure_does_not_interrupt_current_playback(trash_window, monkeypatch):
    window, _ = trash_window
    selected = window.current_case.videos[0]
    controller = _playing_controller(window, selected)
    events = []

    def fail(*args, **kwargs):
        raise WorkspaceError("fixture backup failure")

    monkeypatch.setattr(controller, "close_video", lambda: events.append("close"))
    monkeypatch.setattr(window, "_open_active_video", lambda **kwargs: events.append("open") or True)
    monkeypatch.setattr(window.repository, "backup", fail)

    window.delete_video(window.current_uid, 0)

    assert events == []
    assert controller.metadata.path == Path(selected)
    assert controller.current_frame == 17
    assert controller.is_playing
    assert Path(selected).is_file()


def test_deleting_inactive_accident_does_not_close_current_video(trash_window, monkeypatch):
    window, _ = trash_window
    current, inactive = window.cases
    selected = current.active_video
    controller = _playing_controller(window, selected)
    events = []
    monkeypatch.setattr(controller, "close_video", lambda: events.append("close"))
    monkeypatch.setattr(window, "_open_active_video", lambda **kwargs: events.append("open") or True)

    window.delete_case(inactive.uid)

    assert events == []
    assert window.current_uid == current.uid
    assert controller.metadata.path == Path(selected)
    assert controller.current_frame == 17
    assert controller.is_playing


def test_single_workspace_delete_keeps_another_workspaces_database_and_files_untouched(trash_window):
    window, _ = trash_window
    removed, survivor = window.cases
    shared = removed.videos[0]
    other_repo = CaseRepository(window.repository.workspace.parent / "other-project")
    other_repo.workspace.mkdir()
    other_video = other_repo.workspace / "other.mp4"
    other_video.write_bytes(b"other workspace video")
    other_case = InvestigationCase(
        values={"institution": "samsung", "accident_classification": "2022010100003"},
        videos=[str(other_video)],
    )
    other_repo.save([other_case], active_case_uid=other_case.uid)
    before = deepcopy(other_case.to_dict())

    window.delete_case(removed.uid)

    assert not Path(shared).is_file()
    assert other_video.read_bytes() == b"other workspace video"
    assert [item.uid for item in _saved_cases(window)] == [survivor.uid]
    assert other_repo.load()[0].to_dict() == before
    assert Path(shared).name in {path.name for path in _archived_videos(window)}
    assert window._load_workspace()
    assert [item.uid for item in window.cases] == [survivor.uid]


def test_journal_commit_failure_after_db_success_does_not_undo_file_deletion(trash_window, monkeypatch):
    window, messages = trash_window
    case = window.current_case
    selected, retained = case.videos
    original_write = QuarantinedVideos._write_journal

    def fail_commit(self, status):
        if status == "committed":
            raise OSError("fixture: journal finalization failure")
        return original_write(self, status)

    monkeypatch.setattr(QuarantinedVideos, "_write_journal", fail_commit)

    window.delete_video(case.uid, 0)

    assert {item.uid: item for item in _saved_cases(window)}[case.uid].videos == [retained]
    assert window.current_case.videos == [retained]
    assert not Path(selected).exists()
    assert [path.name for path in _archived_videos(window)] == [Path(selected).name]
    assert any("이동기록" in str(args) for args in messages)
