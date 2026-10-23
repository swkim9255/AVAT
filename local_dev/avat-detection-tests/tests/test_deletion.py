from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.storage import WorkspaceError
from tests.helpers import valid_values


def _confirm(*args, **kwargs):  # type: ignore[no-untyped-def]
    return QMessageBox.StandardButton.Yes


def test_remove_video_discards_only_its_markers_and_keeps_active_video(tmp_path: Path) -> None:
    videos = [tmp_path / f"video-{index}.mp4" for index in range(3)]
    for video in videos:
        video.write_bytes(b"source")
    case = InvestigationCase(values=valid_values())
    case.attach_videos(videos)
    for index, frame in enumerate((10, 20, 30)):
        case.active_video_index = index
        case.set_trigger(frame)

    case.active_video_index = 1
    removed = case.remove_video(0)

    assert removed == str(videos[0].resolve())
    assert case.active_video_index == 0
    assert case.active_video == str(videos[1].resolve())
    assert case.trigger_frame == 20
    assert str(videos[0].resolve()) not in case.markers_by_video
    assert all(video.exists() for video in videos)

    case.remove_video(0)
    assert case.active_video_index == 0
    assert case.active_video == str(videos[2].resolve())
    assert case.trigger_frame == 30


def test_video_selection_survives_tree_rebuild_and_emits_video_delete_target(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    case.attach_videos([tmp_path / "first.mp4", tmp_path / "second.mp4"])
    window.case_list.set_cases(window.cases, case.uid)
    monkeypatch.setattr(window, "_open_active_video", lambda **kwargs: True)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )

    child = window.case_list.tree.topLevelItem(0).child(1)
    window.case_list.tree.setCurrentItem(child)

    selected = window.case_list.tree.currentItem()
    assert selected is not None and selected.parent() is not None
    assert selected.text(0) == "second.mp4"
    assert window.case_list.delete_button.text() == "영상 삭제"

    requested: list[tuple[str, int]] = []
    window.case_list.delete_video_requested.connect(
        lambda uid, index: requested.append((uid, index))
    )
    qtbot.mouseClick(window.case_list.delete_button, Qt.MouseButton.LeftButton)
    assert requested == [(case.uid, 1)]


def test_video_and_last_case_deletion_are_persisted_without_deleting_source(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video")
    window = MainWindow(workspace)
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    case.attach_videos([source])
    case.set_trigger(12)
    window.repository.save(window.cases, active_case_uid=case.uid)
    monkeypatch.setattr(window, "_open_active_video", lambda **kwargs: True)
    monkeypatch.setattr(QMessageBox, "question", _confirm)

    window.delete_video(case.uid, 0)

    assert source.exists()
    assert window.current_case is not None
    assert window.current_case.videos == []
    assert window.current_case.markers_by_video == {}
    assert window.repository.load()[0].videos == []

    removed_uid = window.current_case.uid
    window.delete_case(removed_uid)

    assert source.exists()
    assert window.cases == []
    assert window.current_case is None
    assert window.case_list.tree.topLevelItemCount() == 0
    persisted = window.repository.load()
    assert persisted == []

    window.close()
    reopened = MainWindow(workspace)
    qtbot.addWidget(reopened)
    assert reopened.cases == []
    assert reopened.current_case is None
    assert reopened.case_list.tree.topLevelItemCount() == 0


def test_failed_case_delete_keeps_models_and_tree_unchanged(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video")
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    case.attach_videos([source])
    window.case_list.set_cases(window.cases, case.uid)
    original_cases = window.cases
    original_values = case.to_dict()
    original_uid = window.current_uid
    monkeypatch.setattr(QMessageBox, "question", _confirm)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    def fail_save(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise WorkspaceError("쓰기 실패")

    monkeypatch.setattr(window.repository, "save_changes", fail_save)
    window.delete_case(case.uid)

    assert window.cases is original_cases
    assert window.current_uid == original_uid
    assert window.current_case is case
    assert case.to_dict() == original_values
    assert window.case_list.tree.topLevelItemCount() == 1
    assert window.case_list.tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == case.uid
    assert source.exists()


def test_failed_new_case_save_keeps_empty_workspace_without_ghost_001(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    original_cases = window.cases
    def fail_save(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise WorkspaceError("쓰기 실패")

    monkeypatch.setattr(window.repository, "save_changes", fail_save)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    window.create_case()

    assert window.cases is original_cases
    assert window.cases == []
    assert window.current_case is None
    assert window.case_list.tree.topLevelItemCount() == 0


def test_deleted_auto_discovered_video_stays_deleted_until_explicit_reimport(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "3333333333333.mp4"
    source.write_bytes(b"source video")
    monkeypatch.setattr(
        "investigator.video_controller.VideoController.open_video",
        lambda self, path: True,
    )
    monkeypatch.setattr(QMessageBox, "question", _confirm)

    window = MainWindow(workspace)
    qtbot.addWidget(window)
    assert window.current_case is not None
    imported_uid = window.current_case.uid
    window.delete_video(imported_uid, 0)
    assert source.exists()
    assert window.current_case is not None and window.current_case.videos == []
    assert str(source.resolve()).casefold() in window.repository.auto_import_exclusions
    window.close()

    reopened = MainWindow(workspace)
    qtbot.addWidget(reopened)
    assert reopened.current_case is not None
    assert reopened.current_case.uid == imported_uid
    assert reopened.current_case.videos == []

    reopened.import_video_folder(workspace)
    assert reopened.current_case is not None
    assert reopened.current_case.videos == [str(source.resolve())]
    assert str(source.resolve()).casefold() not in reopened.repository.auto_import_exclusions

    recovered_uid = reopened.current_case.uid
    reopened.delete_case(recovered_uid)
    assert source.exists()
    assert str(source.resolve()).casefold() in reopened.repository.auto_import_exclusions
    reopened.close()

    after_case_delete = MainWindow(workspace)
    qtbot.addWidget(after_case_delete)
    assert after_case_delete.cases == []
    assert after_case_delete.current_case is None
    assert after_case_delete.case_list.tree.topLevelItemCount() == 0
