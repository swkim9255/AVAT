from __future__ import annotations

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from investigator.main_window import MainWindow
from investigator.storage import CaseRepository, WorkspaceError
from investigator.widgets.video_import_dialog import VideoImportDialog
from tests.test_ui_smoke import _Capture


def quick_watch(window):
    window.folder_watcher.settle_seconds = 0.03
    window.folder_watcher._rescan.setInterval(5)
    window.folder_watcher._poll.setInterval(10)


def test_automatic_arrival_preserves_current_video_fields_and_tree_items(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    quick_watch(window)
    video = tmp_path / "1234567890123.mp4"
    video.write_bytes(b"video")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=100)
    assert window.import_video_folder(tmp_path)
    uid = window.current_uid
    assert window.video_controller.seek(25)
    window._field_changed("remarks", "keep editing")
    window.investigation.tabs.setCurrentIndex(4)
    item = window.case_list.tree.topLevelItem(0)
    item.setExpanded(False)
    monkeypatch.setattr(window.case_list, "set_cases", lambda *a, **kw: pytest.fail("tree reset"))
    (tmp_path / "9999999999999.mp4").write_bytes(b"new")
    qtbot.waitUntil(lambda: len(window.cases) == 2, timeout=5000)
    assert window.current_uid == uid
    assert window.video_controller.current_frame == 25
    assert window.current_case.values["remarks"] == "keep editing"
    assert window.investigation.tabs.currentIndex() == 4
    assert window.case_list.tree.topLevelItem(0) is item
    assert not item.isExpanded()
    assert window.case_list.tree.topLevelItemCount() == 2
    assert CaseRepository(tmp_path).load()[0].values["remarks"] == "keep editing"


def test_inactive_project_arrival_is_applied_when_activated(qtbot, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    second.mkdir()
    window = MainWindow(first)
    qtbot.addWidget(window)
    quick_watch(window)
    assert window.open_project(second)
    (first / "1234567890123.mp4").write_bytes(b"new")
    qtbot.waitUntil(lambda: bool(window._projects[0].pending_video_paths), timeout=3000)
    assert window.repository.workspace == second
    assert window.cases == []
    assert "•" in window.project_tabs.tabText(0)
    assert window.switch_project(0)
    qtbot.waitUntil(lambda: len(window.cases) == 1, timeout=3000)
    assert "•" not in window.project_tabs.tabText(0)
    assert window.current_case.values["accident_classification"] == "1234567890123"
    assert window.switch_project(1)
    assert window.cases == []
    assert window.close_project(0)
    assert str(first) not in window.folder_watcher._native.directories()


def test_excluded_video_is_not_reintroduced_by_watcher(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    video = tmp_path / "1234567890123.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(window, "_open_active_video", lambda **kw: True)
    window.import_video_folder(tmp_path)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.delete_case(window.current_uid)
    window._watched_videos_ready(str(tmp_path), [str(video)])
    window._refresh_watched_videos()
    assert window.cases == []
    assert str(video).casefold() in window.repository.auto_import_exclusions


def test_automatic_save_failure_retries_without_dialog_or_data_loss(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    original_uid = window.current_uid
    window._field_changed("remarks", "unsaved")
    video = tmp_path / "1234567890123.mp4"
    video.write_bytes(b"video")
    original_save = window.repository.save_changes

    def fail(*args, **kwargs):
        raise WorkspaceError("disk full")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: pytest.fail("automatic modal"))
    window._watched_videos_ready(str(tmp_path), [str(video)])
    window._refresh_watched_videos()
    assert len(window.cases) == 1
    assert window._dirty
    assert window.current_case.values["remarks"] == "unsaved"
    assert window._projects[0].pending_video_paths == {str(video)}
    monkeypatch.setattr(window.repository, "save_changes", original_save)
    window._refresh_watched_videos()
    assert len(window.cases) == 2
    assert window.current_uid == original_uid
    assert not window._projects[0].pending_video_paths


def test_manual_copy_records_project_paths_and_rolls_back_save_failure(qtbot, tmp_path, monkeypatch):
    project, source = tmp_path / "project", tmp_path / "source"
    source.mkdir()
    video = source / "manual.mp4"
    video.write_bytes(b"original")
    window = MainWindow(project)
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_open_active_video", lambda **kw: True)
    window.attach_videos([str(video)], copy_to_workspace=True)
    assert window.current_case.videos == [str(project / "manual.mp4")]
    assert (project / "manual.mp4").read_bytes() == b"original"
    assert video.read_bytes() == b"original"
    second = source / "other.mp4"
    second.write_bytes(b"second")
    original_save = window.repository.save_changes

    def fail(*args, **kwargs):
        raise WorkspaceError("disk failure")

    monkeypatch.setattr(window.repository, "save_changes", fail)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: None)
    window.attach_videos([str(second)], copy_to_workspace=True)
    assert not (project / second.name).exists()
    assert second.exists()
    assert window.current_case.videos == [str(project / "manual.mp4")]
    monkeypatch.setattr(window.repository, "save_changes", original_save)


def test_folder_copy_only_copies_supported_conventional_files(qtbot, tmp_path, monkeypatch):
    project, source = tmp_path / "project", tmp_path / "source"
    source.mkdir()
    video = source / "1234567890123_1.mp4"
    video.write_bytes(b"original")
    (source / "manual.mp4").write_bytes(b"not conventional")
    window = MainWindow(project)
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_open_active_video", lambda **kw: True)
    assert window.import_video_folder(source, copy_to_workspace=True)
    assert window.current_case.videos == [str(project / video.name)]
    assert not (project / "manual.mp4").exists()
    assert not window.import_video_folder(source, copy_to_workspace=True)
    assert len(window.cases) == 1
    assert (project / video.name).exists()
    assert not list(project.glob(".avat-import-*"))


def test_cancel_import_options_does_not_import(qtbot, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    video = source / "1234567890123.mp4"
    video.write_bytes(b"source")
    window = MainWindow(tmp_path / "project")
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "_choose_video_import_options", lambda: None)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a: ([str(video)], ""))
    window.choose_videos()
    assert window.cases == []
    assert not (window.repository.workspace / video.name).exists()


def test_import_options_default_to_link_and_can_choose_copy(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    dialog = VideoImportDialog(tmp_path)
    qtbot.addWidget(dialog)
    assert not dialog.copy_to_workspace.isChecked()
    original = VideoImportDialog.exec

    def choose_copy(self):
        self.copy_to_workspace.setChecked(True)
        QTimer.singleShot(0, self.accept)
        return original(self)

    monkeypatch.setattr(VideoImportDialog, "exec", choose_copy)
    assert window._choose_video_import_options() is True
