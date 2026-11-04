from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from investigator import video_trash
from investigator.video_trash import (
    QUARANTINE_VIDEO_DIRECTORY,
    QuarantineRollbackError,
    quarantine_videos,
)


def video(path: Path, content: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_quarantine_preserves_nested_tree_and_writes_recovery_journal(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "영상폴더" / "2021" / "사고1" / "전방영상.mp4", b"front")
    batch = quarantine_videos([str(source)], workspace, case_label="SAMSUNG-001")
    target = Path(batch.mapping[str(source)])
    assert target.is_relative_to(workspace / QUARANTINE_VIDEO_DIRECTORY)
    assert target.relative_to(batch.operation_directory) == Path("SAMSUNG-001/영상폴더/2021/사고1/전방영상.mp4")
    assert target.read_bytes() == b"front"
    assert not source.exists()
    manifest = json.loads(batch.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "awaiting_database_save"
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["source"] == str(source)
    assert manifest["files"][0]["destination"] == str(target)
    assert manifest["files"][0]["moved"] is True
    assert manifest["files"][0]["destination_fingerprint"] is not None
    assert batch.commit()
    assert json.loads(batch.manifest_path.read_text(encoding="utf-8"))["status"] == "committed"
    with pytest.raises(ValueError, match="DB"):
        batch.rollback()


def test_rollback_restores_only_its_files_and_leaves_unrelated_files_unchanged(tmp_path):
    workspace = tmp_path / "project"
    first = video(workspace / "영상폴더/사고1/전방영상.mp4", b"one")
    second = video(workspace / "영상폴더/사고2/전방영상.mp4", b"two")
    keep = video(workspace / QUARANTINE_VIDEO_DIRECTORY / "previous/keep.mp4", b"keep")
    batch = quarantine_videos([str(first), str(second)], workspace)
    destinations = list(batch.mapping.values())
    batch.rollback()
    batch.rollback()  # Safe to retry a completed rollback.
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"
    assert keep.read_bytes() == b"keep"
    assert all(not Path(path).exists() for path in destinations)
    assert batch.mapping == {}
    assert json.loads(batch.manifest_path.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_missing_links_do_not_prevent_db_deletion_and_create_no_quarantine(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    missing = workspace / "영상폴더/missing.mp4"
    batch = quarantine_videos([str(missing)], workspace)
    assert batch.missing == (str(missing),)
    assert batch.mapping == {}
    assert batch.moved == []
    assert batch.operation_directory is None
    assert not (workspace / QUARANTINE_VIDEO_DIRECTORY).exists()
    batch.rollback()
    assert batch.commit()


def test_mixed_existing_and_missing_paths_record_both(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "existing.mp4")
    missing = workspace / "missing.mp4"
    batch = quarantine_videos([str(source), str(missing)], workspace)
    assert len(batch.moved) == 1
    assert batch.missing == (str(missing),)
    assert json.loads(batch.manifest_path.read_text(encoding="utf-8"))["missing"] == [str(missing)]
    batch.rollback()


def test_duplicate_paths_and_windows_case_aliases_move_once(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "영상폴더/front.mp4")
    batch = quarantine_videos([str(source), str(source), "영상폴더/front.mp4", str(source).upper()], workspace)
    assert len(batch.records) == 1
    assert len(batch.mapping) == 1
    assert batch.missing == ()
    batch.rollback()


def test_all_sources_validate_before_any_move(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "source.mp4")
    directory = workspace / "not-a-video"
    directory.mkdir()
    with pytest.raises(OSError, match="영상 파일"):
        quarantine_videos([str(source), str(directory)], workspace)
    assert source.exists()
    assert not (workspace / QUARANTINE_VIDEO_DIRECTORY).exists()


@pytest.mark.parametrize("filename", ["작업관리.db", "export.csv", "보고서.docx", "values.json"])
def test_nonvideo_file_in_corrupt_db_is_rejected_before_any_move(tmp_path, filename):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"video")
    protected = video(workspace / "작업관리" / filename, b"important data")
    with pytest.raises(ValueError, match="확장자"):
        quarantine_videos([str(source), str(protected)], workspace)
    assert source.read_bytes() == b"video"
    assert protected.read_bytes() == b"important data"
    assert not (workspace / QUARANTINE_VIDEO_DIRECTORY).exists()


def test_missing_unsupported_link_can_be_removed_without_touching_any_file(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    missing = workspace / "missing.db"
    batch = quarantine_videos([str(missing)], workspace)
    assert batch.missing == (str(missing),)
    assert batch.mapping == {}
    assert batch.operation_directory is None
    assert not missing.exists()


def test_uppercase_supported_video_suffix_is_accepted(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "front.MP4")
    batch = quarantine_videos([str(source)], workspace)
    assert len(batch.moved) == 1
    batch.rollback()


def test_empty_path_rejected_instead_of_moving_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with pytest.raises(ValueError, match="비어"):
        quarantine_videos([" "], workspace)
    assert workspace.is_dir()


def test_external_files_are_recoverable_and_do_not_collide(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    first = video(tmp_path / "2021/case1/front.mp4", b"one")
    second = video(tmp_path / "2022/case1/front.mp4", b"two")
    batch = quarantine_videos([str(first), str(second)], workspace)
    assert len(set(batch.mapping.values())) == 2
    for destination in batch.mapping.values():
        assert Path(destination).relative_to(batch.operation_directory).parts[0] == "외부영상"
    batch.rollback()
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_existing_quarantine_is_never_moved_again(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / QUARANTINE_VIDEO_DIRECTORY / "existing/front.mp4")
    with pytest.raises(OSError, match="이미"):
        quarantine_videos([str(source)], workspace)
    assert source.exists()


def test_case_label_cannot_escape_quarantine(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4")
    batch = quarantine_videos([str(source)], workspace, case_label="../../CON\\bad:label")
    assert batch.case_label == "_.._CON_bad_label"
    assert Path(batch.mapping[str(source)]).is_relative_to(batch.operation_directory)
    batch.rollback()


def test_new_quarantine_operations_never_overwrite_previous_results(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"first")
    first = quarantine_videos([str(source)], workspace)
    first.commit()
    video(source, b"second")
    second = quarantine_videos([str(source)], workspace)
    assert first.operation_directory != second.operation_directory
    assert Path(first.mapping[str(source)]).read_bytes() == b"first"
    assert Path(second.mapping[str(source)]).read_bytes() == b"second"
    second.rollback()


def test_partial_move_failure_restores_previous_moves(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    first = video(workspace / "first.mp4", b"first")
    second = video(workspace / "second.mp4", b"second")
    move = video_trash._move_no_replace

    def fail_second(source, destination):
        if source == second:
            raise PermissionError("locked video")
        move(source, destination)

    monkeypatch.setattr(video_trash, "_move_no_replace", fail_second)
    with pytest.raises(PermissionError, match="locked"):
        quarantine_videos([str(first), str(second)], workspace)
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert list((workspace / QUARANTINE_VIDEO_DIRECTORY).rglob("*.mp4")) == []


def test_journal_write_failure_after_move_rolls_back_source(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"original")
    write = video_trash.QuarantinedVideos._write_journal

    def fail_after_move(self, status):
        if status == "moving":
            raise OSError("journal full")
        write(self, status)

    monkeypatch.setattr(video_trash.QuarantinedVideos, "_write_journal", fail_after_move)
    with pytest.raises(OSError, match="journal full"):
        quarantine_videos([str(source)], workspace)
    assert source.read_bytes() == b"original"


def test_rollback_never_overwrites_new_file_at_original_location(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"original")
    batch = quarantine_videos([str(source)], workspace)
    target = Path(batch.mapping[str(source)])
    video(source, b"new file")
    with pytest.raises(QuarantineRollbackError) as caught:
        batch.rollback()
    assert caught.value.failures == {str(source): str(target)}
    assert source.read_bytes() == b"new file"
    assert target.read_bytes() == b"original"
    assert json.loads(batch.manifest_path.read_text(encoding="utf-8"))["status"] == "rollback_failed"


def test_rollback_continues_for_other_files_after_one_failure(tmp_path):
    workspace = tmp_path / "project"
    first = video(workspace / "first.mp4", b"first")
    second = video(workspace / "second.mp4", b"second")
    batch = quarantine_videos([str(first), str(second)], workspace)
    video(second, b"new second")
    with pytest.raises(QuarantineRollbackError):
        batch.rollback()
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"new second"
    assert list(batch.mapping) == [str(second)]


def test_modified_quarantined_file_is_not_restored_as_original(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"original")
    batch = quarantine_videos([str(source)], workspace)
    target = Path(batch.mapping[str(source)])
    target.write_bytes(b"different replacement video")
    with pytest.raises(QuarantineRollbackError):
        batch.rollback()
    assert not source.exists()
    assert target.read_bytes() == b"different replacement video"


def test_changed_source_during_staging_restores_earlier_moves(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    first = video(workspace / "first.mp4", b"one")
    second = video(workspace / "second.mp4", b"two")
    write = video_trash.QuarantinedVideos._write_journal

    def change_source(self, status):
        write(self, status)
        if status == "prepared":
            second.write_bytes(b"changed original")

    monkeypatch.setattr(video_trash.QuarantinedVideos, "_write_journal", change_source)
    with pytest.raises(OSError, match="변경"):
        quarantine_videos([str(first), str(second)], workspace)
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"changed original"


def test_sanitizes_reserved_windows_case_labels(tmp_path):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4")
    batch = quarantine_videos([str(source)], workspace, case_label="CON")
    assert batch.case_label == "사고_CON"
    batch.rollback()


def test_commit_journal_failure_does_not_enable_rollback_of_saved_db(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4")
    batch = quarantine_videos([str(source)], workspace)

    def fail_journal(_status):
        raise OSError("disk full")

    monkeypatch.setattr(batch, "_write_journal", fail_journal)
    assert batch.commit() is False
    assert batch.committed
    assert batch.journal_error == "disk full"
    assert not source.exists()
    assert all(Path(path).is_file() for path in batch.mapping.values())
    with pytest.raises(ValueError):
        batch.rollback()


def test_cross_volume_fallback_copies_then_removes_source(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"original")
    publish = video_trash._publish_no_replace

    def cross_volume(original, target):
        if original == source:
            raise OSError(errno.EXDEV, "Different volume")
        publish(original, target)

    monkeypatch.setattr(video_trash, "_publish_no_replace", cross_volume)
    batch = quarantine_videos([str(source)], workspace)
    assert not source.exists()
    assert Path(batch.mapping[str(source)]).read_bytes() == b"original"
    assert list(workspace.rglob("*.part")) == []
    batch.rollback()
    assert source.read_bytes() == b"original"


def test_cross_volume_copy_failure_keeps_original(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4", b"original")
    publish = video_trash._publish_no_replace

    def cross_volume(original, target):
        if original == source:
            raise OSError(errno.EXDEV, "Different volume")
        publish(original, target)

    def fail_copy(_reader, _writer, **_kwargs):
        raise OSError("copy interrupted")

    monkeypatch.setattr(video_trash, "_publish_no_replace", cross_volume)
    monkeypatch.setattr(video_trash.shutil, "copyfileobj", fail_copy)
    with pytest.raises(OSError, match="interrupted"):
        quarantine_videos([str(source)], workspace)
    assert source.read_bytes() == b"original"
    assert list(workspace.rglob("*.part")) == []


@pytest.mark.parametrize("link_source", [True, False])
def test_symlink_sources_and_destinations_are_rejected_without_moving(tmp_path, link_source):
    workspace = tmp_path / "project"
    source = video(workspace / "front.mp4")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        if link_source:
            selected = workspace / "linked.mp4"
            selected.symlink_to(source)
        else:
            selected = source
            (workspace / QUARANTINE_VIDEO_DIRECTORY).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Windows symlinks require Developer Mode or elevation")
    with pytest.raises(OSError, match="연결된"):
        quarantine_videos([str(selected)], workspace)
    assert source.exists()
    assert list(outside.iterdir()) == []
