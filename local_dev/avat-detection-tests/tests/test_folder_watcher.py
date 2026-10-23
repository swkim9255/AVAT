from __future__ import annotations

import time

from investigator.folder_watcher import ProjectFolderWatcher


def watcher(qtbot, tmp_path):
    instance = ProjectFolderWatcher(settle_seconds=0.03, poll_ms=10)
    instance.add_directory(tmp_path)
    return instance


def test_native_watch_detects_new_video_once(qtbot, tmp_path):
    watch = watcher(qtbot, tmp_path)
    ready = []
    watch.videos_ready.connect(lambda folder, paths: ready.extend(paths))
    video = tmp_path / "1234567890123.mp4"
    video.write_bytes(b"video")
    qtbot.waitUntil(lambda: ready == [str(video)], timeout=3000)
    (tmp_path / "investigations.db").write_bytes(b"database")
    watch.rescan()
    qtbot.wait(60)
    assert ready == [str(video)]
    watch.shutdown()


def test_pending_video_waits_for_changes_and_ignores_empty_and_nested(qtbot, tmp_path):
    watch = watcher(qtbot, tmp_path)
    ready = []
    watch.videos_ready.connect(lambda folder, paths: ready.extend(paths))
    video = tmp_path / "1234567890123.mp4"
    empty = tmp_path / "1234567890124.mp4"
    video.write_bytes(b"a")
    empty.touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "1234567890125.mp4").write_bytes(b"nested")
    watch.rescan()
    # Signature change resets the quiet period, even if its age was long.
    state = watch._directories[str(tmp_path)]
    signature, _ = state.pending[str(video)]
    state.pending[str(video)] = (signature, time.monotonic() - 1)
    video.write_bytes(b"more data")
    watch._check_pending()
    assert ready == []
    qtbot.waitUntil(lambda: str(video) in ready, timeout=1000)
    assert ready == [str(video)]
    watch.shutdown()


def test_removed_project_is_not_watched(qtbot, tmp_path):
    watch = watcher(qtbot, tmp_path)
    ready = []
    watch.videos_ready.connect(lambda folder, paths: ready.extend(paths))
    watch.remove_directory(tmp_path)
    (tmp_path / "1234567890123.mp4").write_bytes(b"a")
    watch.rescan()
    watch._check_pending()
    assert ready == []
    assert watch._native.directories() == []
    watch.shutdown()
