from __future__ import annotations

from pathlib import Path

import pytest

from investigator.folder_watcher import ProjectFolderWatcher
from investigator.video_copy import copy_videos
from investigator.video_import import MANAGED_VIDEO_DIRECTORY, scan_video_folder, scan_video_tree


def write_video(path: Path, content: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_recursive_scan_keeps_accident_names_but_does_not_guess_generic_camera_cases(tmp_path):
    root = tmp_path / "작업 폴더"
    first = write_video(root / MANAGED_VIDEO_DIRECTORY / "21년 영상" / "영상1" / "1234567890123_2.mp4")
    second = write_video(root / MANAGED_VIDEO_DIRECTORY / "22년 영상" / "영상2" / "1234567890123_1.MOV")
    front = write_video(first.parent / "전방영상.mp4")
    rear = write_video(second.parent / "전방영상.mp4")
    for excluded in ("작업관리", "작업 폴더 관리 DB", ".backups", "runtime", "models", "_internal"):
        write_video(root / excluded / "9999999999999.mp4")
    scan = scan_video_folder(root)
    assert scan.groups == (("1234567890123", (str(second), str(first))),)
    assert set(scan.ignored_videos) == {str(front), str(rear)}
    assert scan.video_count == 2


def test_recursive_scan_does_not_follow_directory_or_video_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    external = write_video(tmp_path / "external" / "1234567890123.mp4")
    try:
        (root / "outside").symlink_to(external.parent, target_is_directory=True)
        (root / "1234567890123.mp4").symlink_to(external)
        (root / "loop").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks requires Windows developer mode or elevation")
    videos, directories = scan_video_tree(root)
    assert videos == ()
    assert directories == (root,)


def test_copy_folder_preserves_year_case_camera_hierarchy_and_reuses_existing_files(tmp_path):
    origin = tmp_path / "incoming"
    source_paths = [
        write_video(origin / year / case / camera, f"{year}/{case}/{camera}".encode())
        for year in ("21년 영상", "22년 영상")
        for case in ("영상1", "영상2")
        for camera in ("전방영상.mp4", "후방영상.mp4", "페달영상.mp4")
    ]
    workspace = tmp_path / "작업 폴더"
    batch = copy_videos([str(path) for path in source_paths], workspace, source_root=origin)
    expected = [workspace / MANAGED_VIDEO_DIRECTORY / path.relative_to(origin) for path in source_paths]
    assert batch.paths == [str(path) for path in expected]
    assert batch.created == expected
    assert len(set(batch.paths)) == 12
    for source, target in zip(source_paths, expected, strict=True):
        assert target.read_bytes() == source.read_bytes()
    repeated = copy_videos([str(path) for path in source_paths], workspace, source_root=origin)
    assert repeated.paths == batch.paths
    assert repeated.created == []
    assert repeated.created_directories == []
    repeated.rollback()
    assert all(path.exists() for path in expected)


def test_manual_multi_folder_copy_does_not_flatten_duplicate_camera_names(tmp_path):
    first = write_video(tmp_path / "source" / "영상1" / "전방영상.mp4", b"first")
    second = write_video(tmp_path / "source" / "영상2" / "전방영상.mp4", b"second")
    workspace = tmp_path / "workspace"
    batch = copy_videos([str(first), str(second)], workspace)
    assert batch.paths == [
        str(workspace / MANAGED_VIDEO_DIRECTORY / "영상1" / first.name),
        str(workspace / MANAGED_VIDEO_DIRECTORY / "영상2" / second.name),
    ]
    assert [Path(path).read_bytes() for path in batch.paths] == [b"first", b"second"]


def test_copy_collision_keeps_accident_number_and_never_overwrites_existing_video(tmp_path):
    workspace = tmp_path / "workspace"
    existing = write_video(workspace / MANAGED_VIDEO_DIRECTORY / "1234567890123_1.mp4", b"original")
    source = write_video(tmp_path / "source" / existing.name, b"different")
    batch = copy_videos([str(source)], workspace)
    assert batch.paths == [str(existing.with_name("1234567890123_2.mp4"))]
    assert existing.read_bytes() == b"original"
    assert source.read_bytes() == b"different"
    assert copy_videos([str(source)], workspace).paths == batch.paths
    batch.rollback()
    assert existing.exists()
    assert not Path(batch.paths[0]).exists()


def test_already_managed_source_is_not_copied_again(tmp_path):
    source = write_video(tmp_path / MANAGED_VIDEO_DIRECTORY / "21년 영상" / "전방영상.mp4")
    batch = copy_videos([str(source)], tmp_path)
    assert batch.paths == [str(source)]
    assert batch.created == []
    assert batch.created_directories == []


def test_copy_failure_removes_only_new_files_and_empty_directories(tmp_path):
    workspace = tmp_path / "workspace"
    existing = write_video(workspace / MANAGED_VIDEO_DIRECTORY / "keep.mp4", b"keep")
    origin = tmp_path / "source"
    first = write_video(origin / "21年" / "영상1" / "전방영상.mp4")
    with pytest.raises(FileNotFoundError):
        copy_videos([str(first), str(origin / "missing.mp4")], workspace, source_root=origin)
    assert list(existing.parent.iterdir()) == [existing]
    assert existing.read_bytes() == b"keep"
    assert first.exists()


def test_copy_rejects_source_outside_selected_tree_before_copying(tmp_path):
    origin = tmp_path / "source"
    origin.mkdir()
    outside = write_video(tmp_path / "other" / "front.mp4")
    with pytest.raises(OSError, match="기준 폴더"):
        copy_videos([str(outside)], tmp_path / "workspace", source_root=origin)
    assert not (tmp_path / "workspace" / MANAGED_VIDEO_DIRECTORY).exists()


def test_recursive_native_watch_detects_arrival_inside_existing_nested_folder(qtbot, tmp_path):
    folder = tmp_path / MANAGED_VIDEO_DIRECTORY / "21년 영상" / "영상1"
    folder.mkdir(parents=True)
    watch = ProjectFolderWatcher(settle_seconds=0.03, poll_ms=10)
    ready = []
    watch.videos_ready.connect(lambda root, paths: ready.extend(paths))
    watch.add_directory(tmp_path)
    try:
        assert str(folder) in watch._native.directories()
        video = write_video(folder / "전방영상.mp4")
        qtbot.waitUntil(lambda: ready == [str(video)], timeout=3000)
        watch.rescan()
        qtbot.wait(60)
        assert ready == [str(video)]
    finally:
        watch.shutdown()


def test_recursive_watch_registers_new_year_and_case_folders_and_ignores_database_tree(qtbot, tmp_path):
    watch = ProjectFolderWatcher(settle_seconds=0.03, poll_ms=10)
    ready = []
    watch.videos_ready.connect(lambda root, paths: ready.extend(paths))
    watch.add_directory(tmp_path)
    try:
        video = write_video(tmp_path / MANAGED_VIDEO_DIRECTORY / "22년 영상" / "영상2" / "후방영상.mp4")
        management = tmp_path / MANAGED_VIDEO_DIRECTORY / "작업관리"
        write_video(management / "1234567890123.mp4")
        qtbot.waitUntil(lambda: ready == [str(video)], timeout=3000)
        assert str(video.parent) in watch._native.directories()
        assert str(management) not in watch._native.directories()
        watch.remove_directory(tmp_path)
        assert watch._native.directories() == []
    finally:
        watch.shutdown()


def test_removing_outer_watch_preserves_nested_project_watch(qtbot, tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    watch = ProjectFolderWatcher(settle_seconds=0.03, poll_ms=10)
    watch.add_directory(tmp_path)
    watch.add_directory(nested)
    try:
        watch.remove_directory(tmp_path)
        assert watch._native.directories() == [str(nested)]
    finally:
        watch.shutdown()


def test_workspace_startup_scans_nested_managed_videos_not_management_backups(qtbot, monkeypatch, tmp_path):
    from investigator.main_window import MainWindow
    from investigator.storage import DATABASE_DIRECTORY, CaseRepository

    workspace = tmp_path / "작업 폴더"
    repository = CaseRepository(workspace)
    repository.save([])
    assert repository.database_path.parent.name == "작업관리"
    assert repository.database_path == workspace / DATABASE_DIRECTORY / "작업관리.db"
    video = write_video(workspace / MANAGED_VIDEO_DIRECTORY / "21년 영상" / "영상1" / "1234567890123.mp4")
    write_video(workspace / DATABASE_DIRECTORY / "보관한 파일" / "9999999999999.mp4")
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda *args, **kwargs: True)
    window = MainWindow(workspace)
    qtbot.addWidget(window)
    assert len(window.cases) == 1
    assert window.cases[0].values["accident_classification"] == "1234567890123"
    assert window.cases[0].videos == [str(video)]
    assert CaseRepository(workspace).load()[0].videos == [str(video)]


def test_folder_copy_ui_passes_selected_root_for_stable_nested_tree(qtbot, monkeypatch, tmp_path):
    from investigator.main_window import MainWindow

    origin = tmp_path / "incoming"
    source = write_video(origin / "21년 영상" / "영상1" / "1234567890123_1.mp4")
    workspace = tmp_path / "작업 폴더"
    monkeypatch.setattr(MainWindow, "_open_active_video", lambda *args, **kwargs: True)
    window = MainWindow(workspace)
    qtbot.addWidget(window)
    assert window.import_video_folder(origin, copy_to_workspace=True)
    expected = workspace / MANAGED_VIDEO_DIRECTORY / source.relative_to(origin)
    assert window.cases[0].videos == [str(expected)]
    assert expected.read_bytes() == source.read_bytes()
    assert source.exists()
