"""Same-name references protect videos until the last record is removed."""

from pathlib import Path

from investigator.models import InvestigationCase
from investigator.video_delete_plan import plan_video_deletion


def video(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test video")
    return str(path)


def test_stale_surviving_reference_protects_existing_same_name_file(tmp_path):
    actual = video(tmp_path, "01_사고동영상/2026/2026100200002_2.mp4")
    old = str(tmp_path / "old/2026100200002_2.mp4")
    plan = plan_video_deletion([InvestigationCase(videos=[old])], [actual], tmp_path)
    assert plan.movable == ()
    assert plan.shared == (actual,)
    assert not plan.excluded_paths
    assert Path(actual).is_file()


def test_last_missing_reference_finds_actual_file_without_changing_any_files(tmp_path):
    actual = video(tmp_path, "01_사고동영상/2026/2026100200002_2.mp4")
    old = str(tmp_path / "old/2026100200002_2.mp4")
    plan = plan_video_deletion([], [old], tmp_path)
    assert set(plan.movable) == {actual, old}
    assert not plan.shared
    assert len(plan.excluded_paths) == 2
    assert Path(actual).is_file()


def test_case_insensitive_name_in_another_case_also_protects_file(tmp_path):
    actual = video(tmp_path, "01_사고동영상/2026/FRONT.MP4")
    survivor = InvestigationCase(videos=[str(tmp_path / "old/front.mp4")])
    plan = plan_video_deletion([survivor], [actual], tmp_path)
    assert plan.movable == ()
    assert plan.shared == (actual,)


def test_final_deletion_includes_previously_retained_copies_but_not_other_names_or_trash(tmp_path):
    copies = [video(tmp_path, f"01_사고동영상/{folder}/same.mp4") for folder in ("a", "b")]
    ignored = [video(tmp_path, f"{folder}/same.mp4") for folder in ("systems/삭제사고", "01_사고동영상/작업관리")]
    other = video(tmp_path, "01_사고동영상/a/other.mp4")
    plan = plan_video_deletion([], [copies[1], copies[1]], tmp_path)
    assert set(plan.movable) == set(copies)
    assert len(plan.movable) == 2
    assert all(path not in plan.movable for path in [other, *ignored])
    assert all(Path(path).is_file() for path in [*copies, other, *ignored])


def test_mixed_batch_keeps_shared_name_and_only_moves_last_name(tmp_path):
    shared = video(tmp_path, "01_사고동영상/a/shared.mp4")
    last = video(tmp_path, "01_사고동영상/a/last.mp4")
    survivor = InvestigationCase(videos=[shared])
    plan = plan_video_deletion([survivor], [shared, last], tmp_path)
    assert plan.shared == (shared,)
    assert plan.movable == (last,)
    assert len(plan.excluded_paths) == 1
