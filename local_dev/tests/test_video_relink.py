from __future__ import annotations

import copy
from pathlib import Path

import pytest

from investigator.models import InvestigationCase
from investigator.video_relink import (
    apply_video_path_mapping,
    apply_workspace_video_path_mapping,
    build_auto_relink_plan,
    plan_relink,
    remap_video_path_list,
    scan_relink_videos,
)


def video(root: Path, relative: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video fixture")
    return str(path.resolve())


def old_video(root: Path, relative: str) -> str:
    return str(root / "missing" / relative)


def test_recursive_scan_includes_arbitrary_unicode_names_not_only_case_numbers(tmp_path):
    root = tmp_path / "영상폴더"
    front = video(root, "21년 영상/영상1/전방영상.MP4")
    rear = video(root, "22년 영상/영상2/후방영상.mkv")
    video(root, "22년 영상/영상2/readme.txt")
    assert set(scan_relink_videos(root)) == {front, rear}


def test_renamed_root_maps_repeated_filenames_by_longest_suffix(tmp_path):
    root = tmp_path / "영상폴더"
    new_paths = [
        video(root, f"{year}년 영상/영상{number}/{camera}영상.mp4")
        for year in (21, 22)
        for number in (1, 2)
        for camera in ("전방", "후방", "페달")
    ]
    old_paths = [
        old_video(tmp_path, str(Path(path).relative_to(root))) for path in new_paths
    ]
    case = InvestigationCase(videos=old_paths)
    matches = plan_relink([case], root)
    assert [match.proposed_path for match in matches] == new_paths
    assert all(match.reason == "suffix" for match in matches)


def test_duplicate_parent_and_filename_stays_ambiguous(tmp_path):
    root = tmp_path / "영상폴더"
    first = video(root, "21년 영상/영상1/전방영상.mp4")
    second = video(root, "22년 영상/영상1/전방영상.mp4")
    old = old_video(tmp_path, "영상1/전방영상.mp4")
    match, = plan_relink([InvestigationCase(videos=[old])], root)
    assert match.proposed_path is None
    assert match.reason == "ambiguous"
    assert set(match.candidates) == {first, second}


def test_unique_filename_alone_is_not_sufficient_identity(tmp_path):
    root = tmp_path / "영상폴더"
    new = video(root, "unrelated/전방영상.mp4")
    old = old_video(tmp_path, "different/전방영상.mp4")
    match, = plan_relink([InvestigationCase(videos=[old])], root)
    assert match.candidates == (new,)
    assert match.proposed_path is None
    assert match.reason == "filename_only"


def test_explicit_root_allows_direct_child_and_case_insensitive_aliases(tmp_path):
    root = tmp_path / "영상폴더"
    new = video(root, "전방영상.MP4")
    old_root = tmp_path / "OLD FOLDER"
    old = str(old_root / "전방영상.mp4")
    match, = plan_relink(
        [InvestigationCase(videos=[old])], root,
        old_root=str(old_root).swapcase().replace("\\", "/"),
    )
    assert match.proposed_path == new
    assert match.reason == "relative_root"


def test_valid_videos_not_touched_and_exclusions_are_respected(tmp_path):
    root = tmp_path / "영상폴더"
    valid = video(root, "21년 영상/영상1/전방영상.mp4")
    excluded = old_video(tmp_path, "21년 영상/영상1/후방영상.mp4")
    target = video(root, "21년 영상/영상1/후방영상.mp4")
    case = InvestigationCase(videos=[valid, excluded])
    assert plan_relink([case], root, excluded_paths=[excluded]) == ()
    match, = plan_relink([case], root, excluded_paths=[target])
    assert match.reason == "not_found"
    assert match.old_path == excluded


def test_explicit_root_missing_relative_path_does_not_map_other_year(tmp_path):
    root = tmp_path / "영상폴더"
    new = video(root, "22년 영상/영상1/전방영상.mp4")
    old_root = tmp_path / "previous videos"
    old = str(old_root / "21년 영상/영상1/전방영상.mp4")
    match, = plan_relink([InvestigationCase(videos=[old])], root, old_root=old_root)
    assert match.reason == "relative_missing"
    assert match.proposed_path is None
    assert match.candidates == (new,)


def test_two_old_roots_cannot_silently_merge_to_one_file(tmp_path):
    root = tmp_path / "영상폴더"
    video(root, "21년 영상/영상1/전방영상.mp4")
    old_a = str(tmp_path / "old_a/21년 영상/영상1/전방영상.mp4")
    old_b = str(tmp_path / "old_b/21년 영상/영상1/전방영상.mp4")
    matches = plan_relink(
        [InvestigationCase(videos=[old_a]), InvestigationCase(videos=[old_b])], root
    )
    assert all(match.proposed_path is None for match in matches)
    assert all(match.reason == "target_conflict" for match in matches)


def test_target_already_attached_to_other_case_is_conflict(tmp_path):
    root = tmp_path / "영상폴더"
    new = video(root, "21년 영상/영상1/전방영상.mp4")
    old = old_video(tmp_path, "21년 영상/영상1/전방영상.mp4")
    match, = plan_relink(
        [InvestigationCase(videos=[old]), InvestigationCase(videos=[new])], root
    )
    assert match.reason == "target_conflict"
    assert match.proposed_path is None


def test_case_copy_preserves_every_annotation_and_workflow_field(tmp_path):
    new = video(tmp_path, "영상폴더/21년 영상/영상1/전방영상.mp4")
    unchanged = video(tmp_path, "external/rear.mp4")
    old = old_video(tmp_path, "21년 영상/영상1/전방영상.mp4")
    case = InvestigationCase(videos=[unchanged, old], active_video_index=1)
    case.uid = "keep-identity"
    case.values = {"remarks": "unchanged", "unknown_future": {"key": [1]}}
    case.status = "completed"
    case.created_at = "created"
    case.updated_at = "updated"
    case.completed_at = "completed"
    case.markers_by_video = {
        old: {"trigger_frame": 15, "collision_frames": [43],
              "speed_points": [{"frame": 10, "distance_m": None},
                               {"frame": 20, "distance_m": 8.2}]},
        "__case__": {"trigger_frame": 3},
    }
    case.rotation_by_video = {old: 270}
    case.objects_by_video = {old: {"schema_version": 2, "objects": [
        {"id": "P001", "frame_annotations": {"15": {"subtype_key": "adult"}},
         "boxes": [{"frame": 15, "bbox": [0.1, 0.2, 0.3, 0.4]}]},
    ]}}
    before = copy.deepcopy(case)
    result = apply_video_path_mapping(case, {old.replace("\\", "/").swapcase(): new})
    assert case == before
    assert result.videos == [unchanged, new]
    assert result.active_video_index == 1
    assert result.active_video == new
    for field in ("uid", "values", "status", "created_at", "updated_at", "completed_at"):
        assert getattr(result, field) == getattr(case, field)
    assert result.markers_by_video[new] == case.markers_by_video[old]
    assert result.markers_by_video["__case__"] == case.markers_by_video["__case__"]
    assert result.rotation_by_video == {new: 270}
    assert result.objects_by_video[new] == case.objects_by_video[old]
    result.objects_by_video[new]["objects"][0]["boxes"].clear()
    assert case.objects_by_video[old]["objects"][0]["boxes"]


@pytest.mark.parametrize("payload_name", ["markers_by_video", "rotation_by_video", "objects_by_video"])
def test_existing_destination_annotations_never_overwritten(tmp_path, payload_name):
    new = video(tmp_path, "영상폴더/전방영상.mp4")
    old = old_video(tmp_path, "전방영상.mp4")
    case = InvestigationCase(videos=[old])
    setattr(case, payload_name, {old: {"old": 1}, new: {"existing": 2}})
    snapshot = copy.deepcopy(getattr(case, payload_name))
    with pytest.raises(ValueError, match="주석"):
        apply_video_path_mapping(case, {old: new})
    assert getattr(case, payload_name) == snapshot


def test_explicit_choice_can_resolve_ambiguous_candidates(tmp_path):
    root = tmp_path / "영상폴더"
    new = video(root, "21년 영상/영상1/전방영상.mp4")
    video(root, "22년 영상/영상1/전방영상.mp4")
    old = old_video(tmp_path, "영상1/전방영상.mp4")
    case = InvestigationCase(videos=[old])
    assert plan_relink([case], root)[0].proposed_path is None
    assert apply_workspace_video_path_mapping([case], {old: new})[0].videos == [new]


def test_workspace_explicit_mapping_rejects_cross_case_target_conflict(tmp_path):
    new = video(tmp_path, "영상폴더/전방영상.mp4")
    old_a = old_video(tmp_path, "a/전방영상.mp4")
    old_b = old_video(tmp_path, "b/전방영상.mp4")
    cases = [InvestigationCase(videos=[old_a]), InvestigationCase(videos=[old_b])]
    with pytest.raises(ValueError, match="같은 새 영상"):
        apply_workspace_video_path_mapping(cases, {old_a: new, old_b: new})
    assert [case.videos for case in cases] == [[old_a], [old_b]]


def test_workspace_mapping_allows_existing_shared_reference_not_distinct_sources(tmp_path):
    new = video(tmp_path, "영상폴더/전방영상.mp4")
    old = old_video(tmp_path, "전방영상.mp4")
    cases = [InvestigationCase(videos=[old]), InvestigationCase(videos=[old])]
    assert [case.videos for case in apply_workspace_video_path_mapping(cases, {old: new})] == [
        [new], [new],
    ]


def test_workspace_mapping_copies_only_affected_case_identities(tmp_path):
    new = video(tmp_path, "영상폴더/case1/front.mp4")
    old = old_video(tmp_path, "case1/front.mp4")
    unaffected_path = video(tmp_path, "영상폴더/case2/rear.mp4")
    changed = InvestigationCase(videos=[old])
    untouched = InvestigationCase(videos=[unaffected_path])
    untouched.objects_by_video = {unaffected_path: {"large_future_payload": [1, 2, 3]}}
    empty = InvestigationCase()
    result = apply_workspace_video_path_mapping([changed, untouched, empty], {old: new})
    assert result[0] is not changed
    assert result[0].videos == [new]
    assert changed.videos == [old]
    assert result[1] is untouched
    assert result[1].objects_by_video is untouched.objects_by_video
    assert result[2] is empty


def test_empty_workspace_mapping_is_identity_preserving(tmp_path):
    path = video(tmp_path, "영상폴더/front.mp4")
    case = InvestigationCase(videos=[path])
    result = apply_workspace_video_path_mapping([case], {})
    assert result == [case]
    assert result[0] is case


def test_workspace_mapping_does_not_re_resolve_entire_mapping_for_every_case(tmp_path, monkeypatch):
    from investigator import video_relink

    new_paths = [video(tmp_path, f"영상폴더/case{number}/front.mp4") for number in range(3)]
    old_paths = [old_video(tmp_path, f"case{number}/front.mp4") for number in range(3)]
    cases = [InvestigationCase(videos=[old]) for old in old_paths]
    sizes = []
    original = video_relink._normalized_mapping

    def record(mapping, workspace_root):
        sizes.append(len(mapping))
        return original(mapping, workspace_root)

    monkeypatch.setattr(video_relink, "_normalized_mapping", record)
    result = apply_workspace_video_path_mapping(cases, dict(zip(old_paths, new_paths, strict=True)))
    assert sizes == [3, 1, 1, 1]
    assert [case.videos for case in result] == [[path] for path in new_paths]


def test_missing_target_and_valid_source_are_rejected(tmp_path):
    valid = video(tmp_path, "valid.mp4")
    other = video(tmp_path, "other.mp4")
    with pytest.raises(ValueError, match="이미 정상 연결"):
        apply_video_path_mapping(InvestigationCase(videos=[valid]), {valid: other})
    old = old_video(tmp_path, "front.mp4")
    with pytest.raises(ValueError, match="영상 파일이 없습니다"):
        apply_video_path_mapping(InvestigationCase(videos=[old]), {old: tmp_path / "absent.mp4"})


def test_workspace_relative_source_and_path_exclusions_can_be_remapped(tmp_path):
    new = video(tmp_path, "영상폴더/영상1/전방영상.mp4")
    old = "old/영상1/전방영상.mp4"
    case = InvestigationCase(videos=[old])
    match, = plan_relink([case], tmp_path / "영상폴더", workspace_root=tmp_path)
    assert match.proposed_path == new
    result = apply_video_path_mapping(case, {old: new}, workspace_root=tmp_path)
    assert result.videos == [new]
    assert remap_video_path_list([old, "unchanged.mp4"], {old: new}, workspace_root=tmp_path) == [
        new, "unchanged.mp4",
    ]


def test_no_invented_hash_or_size_evidence_for_missing_old_file(tmp_path):
    root = tmp_path / "영상폴더"
    video(root, "unrelated/2020011302572_1.mp4")
    old = old_video(tmp_path, "original/2020011302572_1.mp4")
    match, = plan_relink([InvestigationCase(videos=[old])], root)
    assert match.reason == "filename_only"
    assert match.proposed_path is None


def test_unknown_source_rejected_by_workspace_validation(tmp_path):
    new = video(tmp_path, "영상폴더/front.mp4")
    with pytest.raises(ValueError, match="사고 목록에 없는"):
        apply_workspace_video_path_mapping([], {"unknown.mp4": new})


def test_auto_unique_generic_filename_recovers_after_all_parent_names_changed(tmp_path):
    workspace = tmp_path / "프로젝트"
    new = video(workspace, "영상폴더/새 폴더/전방영상.mp4")
    old = str(tmp_path / "옛 프로젝트/옛 이름/전방영상.mp4")
    case = InvestigationCase(videos=[old])
    plan = build_auto_relink_plan([case], workspace)
    assert plan.mapping == {old: new}
    assert plan.unresolved == ()
    assert plan.reserved_paths == frozenset()
    assert case.videos == [old]
    # The explicit review tool deliberately keeps its stricter behavior.
    assert plan_relink([case], workspace)[0].proposed_path is None


def test_auto_repeated_cameras_recovers_unique_year_case_suffixes(tmp_path):
    workspace = tmp_path / "프로젝트"
    new_paths = [
        video(workspace, f"영상폴더/{year}년 영상/영상{number}/{camera}영상.mp4")
        for year in (21, 22)
        for number in (1, 2)
        for camera in ("전방", "후방", "페달")
    ]
    old_paths = [
        str(tmp_path / "old root" / Path(path).relative_to(workspace / "영상폴더"))
        for path in new_paths
    ]
    plan = build_auto_relink_plan([InvestigationCase(videos=old_paths)], workspace)
    assert plan.mapping == dict(zip(old_paths, new_paths, strict=True))
    assert not plan.unresolved
    assert not plan.reserved_paths


def test_auto_ambiguous_duplicate_filenames_reserved_instead_of_imported(tmp_path):
    workspace = tmp_path / "프로젝트"
    first = video(workspace, "영상폴더/21년 영상/영상1/전방영상.mp4")
    second = video(workspace, "영상폴더/22년 영상/영상1/전방영상.mp4")
    old = str(tmp_path / "previous/영상1/전방영상.mp4")
    plan = build_auto_relink_plan([InvestigationCase(videos=[old])], workspace)
    assert plan.mapping == {}
    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].reason == "ambiguous"
    assert plan.reserved_paths == frozenset({first, second})


def test_auto_two_distinct_sources_never_share_one_target(tmp_path):
    workspace = tmp_path / "프로젝트"
    new = video(workspace, "영상폴더/case1/front.mp4")
    old_a = str(tmp_path / "old A/case1/front.mp4")
    old_b = str(tmp_path / "old B/case1/front.mp4")
    plan = build_auto_relink_plan(
        [InvestigationCase(videos=[old_a]), InvestigationCase(videos=[old_b])], workspace,
    )
    assert plan.mapping == {}
    assert len(plan.unresolved) == 2
    assert all(match.reason == "target_conflict" for match in plan.unresolved)
    assert plan.reserved_paths == frozenset({new})


def test_auto_shared_old_source_across_cases_counts_as_one(tmp_path):
    workspace = tmp_path / "프로젝트"
    new = video(workspace, "영상폴더/new names/front.mp4")
    old = str(tmp_path / "old root/old names/front.mp4")
    cases = [InvestigationCase(videos=[old]), InvestigationCase(videos=[old])]
    plan = build_auto_relink_plan(cases, workspace)
    assert plan.mapping == {old: new}
    recovered = apply_workspace_video_path_mapping(cases, plan.mapping)
    assert all(case.videos == [new] for case in recovered)
    assert all(case.videos == [old] for case in cases)


def test_auto_old_aliases_case_and_slash_count_once(tmp_path):
    workspace = tmp_path / "프로젝트"
    new = video(workspace, "영상폴더/unrelated/FRONT.mp4")
    old = str(tmp_path / "old root/old names/front.mp4")
    cases = [InvestigationCase(videos=[old]), InvestigationCase(videos=[old.swapcase().replace("\\", "/")])]
    plan = build_auto_relink_plan(cases, workspace)
    assert plan.mapping == {old: new}
    assert not plan.unresolved


def test_auto_valid_owned_paths_are_not_stolen(tmp_path):
    workspace = tmp_path / "프로젝트"
    occupied = video(workspace, "영상폴더/new/front.mp4")
    old = str(tmp_path / "old root/old names/front.mp4")
    cases = [InvestigationCase(videos=[old]), InvestigationCase(videos=[occupied])]
    plan = build_auto_relink_plan(cases, workspace)
    assert plan.mapping == {}
    assert plan.unresolved[0].reason == "not_found"
    assert not plan.reserved_paths


def test_auto_excluded_targets_not_considered_or_reserved(tmp_path):
    workspace = tmp_path / "프로젝트"
    excluded = video(workspace, "영상폴더/new/front.mp4")
    old = str(tmp_path / "old root/old names/front.mp4")
    plan = build_auto_relink_plan(
        [InvestigationCase(videos=[old])], workspace,
        excluded_paths=[excluded.swapcase().replace("\\", "/")],
    )
    assert plan.mapping == {}
    assert plan.unresolved[0].reason == "not_found"
    assert not plan.reserved_paths


def test_auto_ignores_backup_runtime_management_and_hidden_trees(tmp_path):
    workspace = tmp_path / "프로젝트"
    visible = video(workspace, "영상폴더/new/front.mp4")
    for directory in (".backups", "runtime", "작업관리", "작업 폴더 관리 DB", "models", ".hidden"):
        video(workspace, f"{directory}/camera/front.mp4")
    old = str(tmp_path / "old names/front.mp4")
    case = InvestigationCase(videos=[old])
    assert build_auto_relink_plan([case], workspace).mapping == {old: visible}


def test_auto_reuses_supplied_scan_without_second_walk(tmp_path, monkeypatch):
    workspace = tmp_path / "프로젝트"
    new = video(workspace, "영상폴더/new/front.mp4")
    old = str(tmp_path / "previous/old/front.mp4")

    def fail_scan(_folder):
        raise AssertionError("duplicate directory walk")

    monkeypatch.setattr("investigator.video_relink.scan_video_tree", fail_scan)
    plan = build_auto_relink_plan([InvestigationCase(videos=[old])], workspace, discovered_paths=[new])
    assert plan.mapping == {old: new}


def test_auto_does_not_infer_leftover_filename_from_initially_ambiguous_group(tmp_path):
    workspace = tmp_path / "프로젝트"
    exact = video(workspace, "영상폴더/case1/front.mp4")
    leftover = video(workspace, "영상폴더/something unrelated/front.mp4")
    old_exact = str(tmp_path / "previous/case1/front.mp4")
    old_unknown = str(tmp_path / "previous/case2/front.mp4")
    plan = build_auto_relink_plan([InvestigationCase(videos=[old_exact, old_unknown])], workspace)
    assert plan.mapping == {old_exact: exact}
    assert len(plan.unresolved) == 1
    assert plan.unresolved[0].old_path == old_unknown
    assert plan.reserved_paths == frozenset({leftover})


def test_auto_not_found_file_has_no_reservation(tmp_path):
    workspace = tmp_path / "프로젝트"
    video(workspace, "영상폴더/new/unrelated.mp4")
    old = str(tmp_path / "previous/old/front.mp4")
    plan = build_auto_relink_plan([InvestigationCase(videos=[old])], workspace)
    assert plan.mapping == {}
    assert plan.unresolved[0].reason == "not_found"
    assert plan.reserved_paths == frozenset()


def test_auto_ignores_external_candidates_even_if_passed_by_caller(tmp_path):
    workspace = tmp_path / "프로젝트"
    workspace.mkdir()
    outside = video(tmp_path, "external/front.mp4")
    old = str(tmp_path / "previous/old/front.mp4")
    plan = build_auto_relink_plan([InvestigationCase(videos=[old])], workspace, discovered_paths=[outside])
    assert not plan.mapping
    assert not plan.reserved_paths


def test_auto_no_missing_case_is_identity_noop_without_scan(tmp_path, monkeypatch):
    new = video(tmp_path, "영상폴더/front.mp4")

    def fail_scan(_folder):
        raise AssertionError("unnecessary scan")

    monkeypatch.setattr("investigator.video_relink.scan_video_tree", fail_scan)
    plan = build_auto_relink_plan([InvestigationCase(videos=[new])], tmp_path)
    assert not plan.mapping
    assert not plan.unresolved
    assert not plan.reserved_paths


def test_auto_duplicate_camera_groups_share_candidates_and_use_linear_path_parsing(tmp_path, monkeypatch):
    from investigator import video_relink

    workspace = tmp_path / "프로젝트"
    paths = [video(workspace, f"영상폴더/new folder {index}/front.mp4") for index in range(20)]
    old_paths = [str(tmp_path / f"old root/previous folder {index}/front.mp4") for index in range(20)]
    calls = 0
    original = video_relink._components

    def record(path, workspace_root=None):
        nonlocal calls
        calls += 1
        return original(path, workspace_root)

    def forbid_pairwise_comparison(*_args):
        raise AssertionError("automatic repair should use its suffix index")

    monkeypatch.setattr(video_relink, "_components", record)
    monkeypatch.setattr(video_relink, "_suffix_length", forbid_pairwise_comparison)
    plan = build_auto_relink_plan(
        [InvestigationCase(videos=old_paths)], workspace, discovered_paths=reversed(paths),
    )
    assert not plan.mapping
    assert len(plan.unresolved) == 20
    assert calls == 40  # Each missing source and available target is parsed once.
    assert len({id(match.candidates) for match in plan.unresolved}) == 1
    assert plan.unresolved[0].candidates == tuple(sorted(paths, key=str.casefold))
    assert plan.reserved_paths == frozenset(paths)


def test_auto_suffix_index_keeps_longest_unique_match_over_shared_shorter_suffix(tmp_path):
    workspace = tmp_path / "프로젝트"
    first = video(workspace, "영상폴더/21년/case1/front.mp4")
    second = video(workspace, "영상폴더/22년/case1/front.mp4")
    old_a = str(tmp_path / "previous/21년/case1/front.mp4")
    old_b = str(tmp_path / "previous/22년/case1/front.mp4")
    plan = build_auto_relink_plan([InvestigationCase(videos=[old_a, old_b])], workspace)
    assert plan.mapping == {old_a: first, old_b: second}
    assert not plan.unresolved
    assert not plan.reserved_paths
