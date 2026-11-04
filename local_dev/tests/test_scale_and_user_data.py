"""대용량 대응(목록 범위·증분 반영·캐시)과 사용자데이터·백업 복원·중복 실행 방지."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

import investigator.video_trash as trash_module
from investigator.form_definition import FormDefinition
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.project_tabs_state import ProjectTabsStore
from investigator.storage import CaseRepository, WorkspaceError
from investigator.video_folders import UNLINKED_FOLDER_KEY, VideoFolder, VideoFolderIndex
from investigator.video_import import MANAGED_VIDEO_DIRECTORY, scan_video_files, scan_video_folder
from investigator.video_trash import quarantine_videos


def _video(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture; video decoding is deliberately stubbed")
    return str(path.resolve())


def _case(number: str, *videos: str) -> InvestigationCase:
    return InvestigationCase(
        values={"institution": "samsung", "accident_classification": number}, videos=list(videos),
    )


@pytest.fixture
def window_factory(qtbot, tmp_path, monkeypatch):
    windows = []
    for method in ("warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(MainWindow, "_watch_active_project", lambda self: None)
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda self, **kwargs: True)

    def create(workspace):
        window = MainWindow(
            workspace, project_tabs_store=ProjectTabsStore(tmp_path / f"tabs-{len(windows)}.json"),
        )
        windows.append(window)
        qtbot.addWidget(window)
        return window

    yield create
    for window in reversed(windows):
        window._detection_job = None
        window._commit_in_progress = False
        window._video_import_in_progress = False
        window._workspace_load_failed = True
        window.close()


# -- 양식 캐시 --------------------------------------------------------------------


def test_sealed_form_definition_returns_identical_results():
    plain = FormDefinition.default()
    sealed = FormDefinition.default().seal()
    values = {
        "institution": "samsung", "accident_classification": "2025010100001",
        "obstacle_exists": "유", "collision_targets": ["pedestrian.adult"], "driver_age": "35",
    }
    case = InvestigationCase(values=dict(values), form_definition=sealed)
    for method in ("missing", "errors", "csv_row", "canonical_values", "stored_values", "migrate_values"):
        assert getattr(plain, method)(case.values) == getattr(sealed, method)(case.values)
    assert plain.csv_headers() == sealed.csv_headers()
    assert plain.fields == sealed.fields and plain.records() == sealed.records()
    assert [plain.label(key) for key in ("driver_age", "unknown_key")] == [
        sealed.label(key) for key in ("driver_age", "unknown_key")
    ]
    # 캐시된 목록을 호출자가 바꿔도 다음 호출 결과는 오염되지 않는다.
    sealed.records().clear()
    assert sealed.records() == plain.records()


def test_in_place_internal_edit_disables_stale_cache():
    definition = FormDefinition.default().seal()
    assert definition.fields
    definition._upgrade_choices()
    assert definition._derived is None


# -- 폴더 분류 색인·스캔 ---------------------------------------------------------------


def test_folder_index_matches_path_semantics(tmp_path):
    root = tmp_path / MANAGED_VIDEO_DIRECTORY
    folders = [
        VideoFolder(MANAGED_VIDEO_DIRECTORY, MANAGED_VIDEO_DIRECTORY, root, direct_only=True),
        VideoFolder("a", "2022", root / "2022"),
        VideoFolder(UNLINKED_FOLDER_KEY, "미연결", tmp_path),
    ]
    index = VideoFolderIndex(folders)
    paths = [root / "direct.mp4", root / "2022" / "x" / "1.mp4", root / "20221" / "2.mp4",
             Path(str(root / "2022" / "3.mp4").upper()), tmp_path / "outside.mp4"]
    for path in paths:
        for folder in folders:
            expected = (
                not any(other.contains(path) for other in folders if other.key != UNLINKED_FOLDER_KEY)
                if folder.key == UNLINKED_FOLDER_KEY else folder.contains(path)
            )
            assert index.contains(folder, str(path)) == expected


def test_scan_video_files_groups_only_given_files_with_full_scan_rules(tmp_path):
    root = tmp_path / MANAGED_VIDEO_DIRECTORY
    good = _video(root / "2021" / "2021010100001_2.mp4")
    first = _video(root / "2021" / "2021010100001.mp4")
    ignored_name = _video(root / "2021" / "front.mp4")
    management = _video(root / "작업관리" / "2021010100009.mp4")
    outside = _video(tmp_path / "other" / "2021010100003.mp4")
    missing = str(root / "2021" / "2021010100004.mp4")
    scan = scan_video_files([good, first, ignored_name, management, outside, missing], root)
    assert scan.groups == (("2021010100001", (first, good)),)
    assert scan.ignored_videos == (ignored_name,)
    full = scan_video_folder(root)
    assert full.groups == scan.groups and full.ignored_videos == scan.ignored_videos


# -- 사고 목록 범위 ----------------------------------------------------------------


def test_list_builds_rows_only_for_active_folder_and_merges_incrementally(window_factory, tmp_path):
    workspace = tmp_path / "project"
    first = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2021" / "2021010100001.mp4")
    _video(workspace / MANAGED_VIDEO_DIRECTORY / "2022" / "2022010100001.mp4")
    window = window_factory(workspace)
    panel = window.case_list
    assert len(window.cases) == 2
    rows = panel.tree.case_items()
    assert len(rows) == 1 and rows[0].child(0).toolTip(0) == first
    assert panel.count_label.text() == "0/1 완료"
    kept = rows[0]
    # 감시로 새 영상 하나가 들어오면 기존 행은 그대로 두고 새 행만 끝에 붙인다.
    arrived = _video(workspace / MANAGED_VIDEO_DIRECTORY / "2021" / "2021010100002.mp4")
    window._session.pending_video_paths.add(arrived)
    window._refresh_watched_videos()
    rows = panel.tree.case_items()
    assert rows[0] is kept
    assert [row.child(0).toolTip(0) for row in rows] == [first, arrived]
    assert panel.count_label.text() == "0/2 완료"
    assert len(window.cases) == 3
    assert len(CaseRepository(workspace).load()) == 3


def test_search_typing_is_debounced_but_programmatic_text_applies_now(qtbot, tmp_path):
    from investigator.widgets.case_list import CaseListPanel

    panel = CaseListPanel()
    qtbot.addWidget(panel)
    cases = [_case(f"20210101{index:05d}", _video(tmp_path / f"{index}.mp4")) for index in range(3)]
    panel.set_cases(cases, cases[0].uid)
    panel.search.setText("00002")
    assert [item.isHidden() for item in panel.tree.case_items()] == [True, True, False]
    panel.show()
    panel.search.setFocus()
    qtbot.waitUntil(panel.search.hasFocus)
    panel.search.setText("00001")
    assert [item.isHidden() for item in panel.tree.case_items()] == [True, True, False]
    qtbot.waitUntil(lambda: [item.isHidden() for item in panel.tree.case_items()] == [True, False, True])


# -- 최신 백업·복원 -----------------------------------------------------------------


def test_latest_backup_is_single_file_and_skips_when_unchanged(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    assert repository.backup_latest() is None  # DB가 없으면 빈 DB로 덮어쓰지 않는다.
    assert not repository.database_path.exists()
    repository.save([_case("2021010100001")])
    first = repository.backup_latest()
    assert first == repository.latest_backup_path
    assert first.parent == tmp_path / "project" / "systems" / "사용자데이터" / "백업"
    before = first.stat().st_mtime_ns
    assert repository.backup_latest(only_if_changed=True) is None
    repository.save([_case("2021010100001"), _case("2021010100002")])
    assert repository.backup_latest(only_if_changed=True) == first
    assert first.stat().st_mtime_ns >= before
    assert list(first.parent.glob("*.db")) == [first]
    restored = CaseRepository(tmp_path / "copy")
    restored.restore(first)
    assert len(restored.load()) == 2


def test_restore_rejects_invalid_files_without_touching_current_data(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    repository.save([_case("2021010100001")])
    original = repository.database_path.read_bytes()
    bogus = tmp_path / "not-a-db.db"
    bogus.write_bytes(b"not sqlite")
    with pytest.raises(WorkspaceError):
        repository.restore(bogus)
    import sqlite3
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    with pytest.raises(WorkspaceError):
        repository.restore(other)
    assert repository.database_path.read_bytes() == original
    assert not list(repository.database_path.parent.glob(".avat-restore.*"))


def test_reinstall_restores_latest_backup_automatically(window_factory, tmp_path):
    original = tmp_path / "old-install"
    repository = CaseRepository(original)
    repository.save([_case("2021010100001"), _case("2021010100002")])
    backup = repository.backup_latest()
    reinstall = tmp_path / "new-install"
    target = reinstall / "systems" / "사용자데이터" / "백업" / "작업관리_백업.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(backup.read_bytes())
    window = window_factory(reinstall)
    assert not window._workspace_load_failed
    assert sorted(case.values["accident_classification"] for case in window.cases) == [
        "2021010100001", "2021010100002",
    ]
    assert "최신 백업 파일에서" in window.statusBar().currentMessage()
    assert CaseRepository(reinstall).database_path.is_file()


def test_restore_menu_backs_up_current_data_before_replacing(window_factory, tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    CaseRepository(workspace).save([_case("2021010100001")])
    source = CaseRepository(tmp_path / "source")
    source.save([_case("2022010100001"), _case("2022010100002")])
    exported = source.backup(tmp_path / "exported.db")
    window = window_factory(workspace)
    assert len(window.cases) == 1
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *args, **kwargs: (str(exported), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
    window.restore_workspace()
    assert sorted(case.values["accident_classification"] for case in window.cases) == [
        "2022010100001", "2022010100002",
    ]
    # 바꾸기 전 데이터는 최신 백업 1개로 남는다.
    previous = CaseRepository(tmp_path / "check")
    previous.restore(window.repository.latest_backup_path)
    assert [case.values["accident_classification"] for case in previous.load()] == ["2021010100001"]


def test_restore_selecting_latest_backup_itself_is_safe(window_factory, tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    repository = CaseRepository(workspace)
    repository.save([_case("2021010100001")])
    latest = repository.backup_latest()
    repository.save([_case("2021010100001"), _case("2021010100002")])
    window = window_factory(workspace)
    assert len(window.cases) == 2
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *args, **kwargs: (str(latest), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes))
    window.restore_workspace()
    assert [case.values["accident_classification"] for case in window.cases] == ["2021010100001"]


# -- 중복 실행 방지 -----------------------------------------------------------------


def test_second_window_on_same_workspace_is_refused(window_factory, tmp_path):
    workspace = tmp_path / "project"
    _video(workspace / MANAGED_VIDEO_DIRECTORY / "2021" / "2021010100001.mp4")
    first = window_factory(workspace)
    assert not first._workspace_load_failed
    second = window_factory(workspace)
    assert second._workspace_load_failed
    assert second.cases == []
    first.close()
    third = window_factory(workspace)
    assert not third._workspace_load_failed


# -- 삭제 격리 경로 -----------------------------------------------------------------


def test_quarantine_refuses_paths_over_windows_limit_before_moving(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    source = Path(_video(workspace / MANAGED_VIDEO_DIRECTORY / ("긴폴더" * 20) / "2021010100001.mp4"))
    monkeypatch.setattr(trash_module, "_long_paths_enabled", lambda: False)
    monkeypatch.setattr(trash_module, "WINDOWS_MAX_PATH", len(str(source)) + 10)
    with pytest.raises(OSError, match="최대 길이"):
        quarantine_videos([str(source)], workspace, case_label="SAMSUNG-2021010100001")
    assert source.is_file()
    assert not (workspace / "systems" / "사용자데이터" / "삭제사고").exists()


def test_quarantine_lives_in_user_data_folder(tmp_path):
    workspace = tmp_path / "project"
    source = Path(_video(workspace / MANAGED_VIDEO_DIRECTORY / "2021" / "2021010100001.mp4"))
    batch = quarantine_videos([str(source)], workspace, case_label="S")
    assert batch.operation_directory.parent == workspace / "systems" / "사용자데이터" / "삭제사고"
    assert len(batch.operation_directory.name) == len("20260101-000000-") + 8
    batch.rollback()
    assert source.is_file()


# -- 영상 FPS 확인 결과 캐시 ------------------------------------------------------------


def test_video_metadata_round_trips_with_relative_paths(tmp_path):
    workspace = tmp_path / "project"
    repository = CaseRepository(workspace)
    assert repository.load_video_metadata() == {}  # DB가 없을 때
    repository.save([_case("2021010100001")])
    assert repository.load_video_metadata() == {}  # 표가 없는 이전 DB
    inside = str((workspace / MANAGED_VIDEO_DIRECTORY / "a" / "1.mp4").resolve())
    outside = str((tmp_path / "external" / "2.mp4").resolve())
    repository.save_video_metadata({inside: (10, 20, 29.97), outside: (1, 2, 0.0)})
    repository.save_video_metadata({inside: (11, 21, 30.0)})
    assert repository.load_video_metadata() == {inside: (11, 21, 30.0), outside: (1, 2, 0.0)}
    with repository._connection() as connection:
        stored = {row[0] for row in connection.execute("SELECT path FROM video_metadata")}
    assert "01_사고동영상/a/1.mp4" in stored
    # 기존 조사 데이터 구조와 검증은 그대로다.
    assert len(repository.load()) == 1


def test_speed_csv_reuses_cached_fps_and_fills_new_entries(tmp_path, monkeypatch):
    import io

    from investigator import speed_csv, statistics_media

    videos = [_video(tmp_path / f"{index}.mp4") for index in range(2)]
    cases = []
    for index, path in enumerate(videos):
        case = _case(f"202101010000{index + 1}", path)
        case.markers_by_video = {path: {"trigger_frame": 1, "collision_frames": [5]}}
        cases.append(case)
    opened = []

    class FakeCapture:
        def __init__(self, path):
            opened.append(path)

        def isOpened(self):
            return True

        def get(self, prop):
            return 25.0

        def release(self):
            pass

    monkeypatch.setattr(statistics_media, "open_video_capture", FakeCapture)
    first_stat = Path(videos[0]).stat()
    cache = {str(Path(videos[0]).resolve()): (first_stat.st_size, first_stat.st_mtime_ns, 30.0)}
    speed_csv.write_speed_csv(io.StringIO(), cases, fps_cache=cache)
    # 크기·수정시각이 같은 영상은 다시 열지 않고, 처음 보는 영상만 열어 캐시에 채운다.
    assert opened == [str(Path(videos[1]).resolve())]
    assert cache[str(Path(videos[1]).resolve())][2] == 25.0
    assert cache[str(Path(videos[0]).resolve())][2] == 30.0


def test_statistics_probe_is_limited_to_videos_where_fps_matters(window_factory, tmp_path):
    workspace = tmp_path / "project"
    plain = _video(workspace / MANAGED_VIDEO_DIRECTORY / "a" / "2021010100001.mp4")
    marked = _video(workspace / MANAGED_VIDEO_DIRECTORY / "a" / "2021010100002.mp4")
    window = window_factory(workspace)
    case = next(case for case in window.cases if case.active_video == marked)
    case.markers_by_video[marked] = {"trigger_frame": 2, "collision_frames": [9]}
    assert window._fps_needed_paths() == [marked]
    assert plain not in window._fps_needed_paths()
