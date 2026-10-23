from __future__ import annotations

import pytest

from investigator.video_copy import copy_videos
from investigator.video_import import parse_accident_video_name


def test_copy_retains_source_and_can_rollback_only_created_files(tmp_path):
    source, workspace = tmp_path / "source", tmp_path / "project"
    source.mkdir()
    file = source / "video.mp4"
    file.write_bytes(b"source")
    batch = copy_videos([str(file)], workspace)
    assert batch.paths == [str(workspace / file.name)]
    assert (workspace / file.name).read_bytes() == file.read_bytes()
    batch.rollback()
    assert not (workspace / file.name).exists()
    assert file.read_bytes() == b"source"
    assert list(workspace.iterdir()) == []


def test_collision_keeps_accident_number_and_repeat_import_reuses_copy(tmp_path):
    source, workspace = tmp_path / "source", tmp_path / "project"
    source.mkdir()
    workspace.mkdir()
    video = source / "1234567890123_1.mp4"
    video.write_bytes(b"new source")
    existing = workspace / video.name
    existing.write_bytes(b"existing")
    first = copy_videos([str(video)], workspace)
    assert first.paths == [str(workspace / "1234567890123_2.mp4")]
    assert parse_accident_video_name(first.paths[0]) == ("1234567890123", 2)
    repeated = copy_videos([str(video)], workspace)
    assert repeated.paths == first.paths
    assert repeated.created == []
    repeated.rollback()
    assert existing.read_bytes() == b"existing"
    assert (workspace / "1234567890123_2.mp4").exists()


def test_self_copy_reuses_file(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"same")
    batch = copy_videos([str(video)], tmp_path)
    assert batch.paths == [str(video)]
    assert batch.created == []


def test_mid_batch_copy_failure_cleans_partial_files(tmp_path):
    source, workspace = tmp_path / "source", tmp_path / "project"
    source.mkdir()
    first = source / "first.mp4"
    first.write_bytes(b"good")
    with pytest.raises(FileNotFoundError):
        copy_videos([str(first), str(source / "missing.mp4")], workspace)
    assert list(workspace.iterdir()) == []
    assert first.exists()
