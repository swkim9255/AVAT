"""Standalone CSV recovery consolidates rows without changing source files."""

import csv
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from investigator.models import InvestigationCase
from investigator.storage import CaseRepository, WorkspaceError
from recover_investigation_csv import _consolidate_duplicate_cases, restore_csv


def accident(number="2026100200002", institution="samsung", **values):
    return InvestigationCase(values={"institution": institution, "accident_classification": number, **values})


def test_more_required_inputs_win_over_older_blank_duplicate():
    blank = accident()
    worked = accident(driver_sex="남성", driver_exp="15", driver_age="45-49세", remarks="유지")
    before = deepcopy(worked.to_dict())
    result = _consolidate_duplicate_cases([blank, worked])
    assert result.removed_count == 1
    assert result.uid_mapping == {blank.uid: worked.uid}
    assert result.cases[0].to_dict() == before
    assert worked.to_dict() == before
    assert result.cases[0] is not worked


def test_more_optional_values_break_equal_required_missing_count_tie():
    blank = accident()
    worked = accident(remarks="추가 조사 내용")
    assert len(blank.missing) == len(worked.missing)
    result = _consolidate_duplicate_cases([blank, worked])
    assert result.cases[0].uid == worked.uid


def test_equal_work_keeps_original_first_case_and_list_position():
    first, other, later = accident(), accident("2026100200003"), accident()
    result = _consolidate_duplicate_cases([first, other, later])
    assert [item.uid for item in result.cases] == [first.uid, other.uid]
    assert result.uid_mapping == {later.uid: first.uid}
    assert result.cases[1] is other


def test_different_institutions_and_missing_identifiers_are_not_merged():
    cases = [accident(), accident(institution="ajou"), accident(number=""), accident(number=""),
             accident(institution=""), accident(institution="")]
    result = _consolidate_duplicate_cases(cases)
    assert result.cases == cases and not result.uid_mapping
    assert all(a is b for a, b in zip(result.cases, cases, strict=True))


def test_institution_case_and_surrounding_spaces_do_not_create_different_accidents():
    blank, worked = accident(institution=" SAMSUNG "), accident(remarks="입력")
    result = _consolidate_duplicate_cases([blank, worked])
    assert result.cases[0].uid == worked.uid


def test_unique_video_paths_and_metadata_survive_without_merging_winner_survey():
    loser, winner = accident(remarks="덮어쓰면 안 됨"), accident(driver_sex="남성")
    loser.videos = ["C:/videos/a/front.mp4", "C:/videos/shared.mp4"]
    winner.videos = ["C:/videos/b/front.mp4", "C:/videos/shared.mp4"]
    loser.markers_by_video = {loser.videos[0]: {"trigger_frame": 1}, loser.videos[1]: {"trigger_frame": 99}}
    winner.markers_by_video = {winner.videos[0]: {"trigger_frame": 2}, winner.videos[1]: {"trigger_frame": 3}}
    loser.rotation_by_video = {loser.videos[0]: 90}
    loser.objects_by_video = {loser.videos[0]: {"future": [1]}}
    before = deepcopy([loser.to_dict(), winner.to_dict()])
    result = _consolidate_duplicate_cases([loser, winner])
    retained, = result.cases
    assert retained.values == winner.values and "remarks" not in retained.values
    assert retained.videos == [*winner.videos, loser.videos[0]]
    assert retained.markers_by_video[loser.videos[0]] == {"trigger_frame": 1}
    assert retained.markers_by_video[winner.videos[1]] == {"trigger_frame": 3}
    assert retained.rotation_by_video[loser.videos[0]] == 90
    retained.objects_by_video[loser.videos[0]]["future"].append(2)
    assert [loser.to_dict(), winner.to_dict()] == before


def test_consolidation_is_idempotent_and_does_not_renumber_or_mark_edited():
    first, second = accident(), accident(remarks="유지")
    first_result = _consolidate_duplicate_cases([first, second])
    again = _consolidate_duplicate_cases(first_result.cases)
    assert not again.removed_count
    assert again.cases[0] is first_result.cases[0]
    assert again.cases[0].values["accident_classification"] == "2026100200002"
    assert again.cases[0].updated_at == second.updated_at


def test_normal_save_still_prohibits_creating_new_duplicate_accidents(tmp_path):
    repository = CaseRepository(tmp_path)
    first, second = accident(), accident(remarks="기존 DB 복구가 아닌 중복 저장")
    repository.save([first])
    with pytest.raises(WorkspaceError, match="중복 사고 식별자"):
        repository.save([first, second])
    assert [case.uid for case in repository.load()] == [first.uid]


def _csv_fixture(tmp_path):
    workspace = tmp_path / "workspace"
    video = workspace / "01_사고동영상/2026/2026100200002.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"source video; must remain unchanged")
    source = tmp_path / "investigation.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("Institution", "accident_classification", "driver_sex", "remarks"))
        writer.writeheader()
        writer.writerow({"Institution": "samsung", "accident_classification": "2026100200002"})
        writer.writerow({"Institution": "samsung", "accident_classification": "2026100200002",
                         "driver_sex": "남성", "remarks": "CSV 복구 시에만 중복 정리"})
    return source, workspace, video


def test_csv_recovery_writes_current_database_and_preserves_originals(tmp_path):
    source, workspace, video = _csv_fixture(tmp_path)
    original_csv, original_video = source.read_bytes(), video.read_bytes()
    output = workspace / "01_사고동영상/작업관리/작업관리.db"
    report = restore_csv(source, workspace, output)
    assert report["csv_rows"] == 2 and report["restored_cases"] == 1
    assert report["duplicate_cases_removed"] == 1
    with sqlite3.connect(output) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM cases").fetchone()[0])
        metadata = {key: json.loads(value) for key, value in connection.execute("SELECT * FROM workspace_metadata")}
    assert payload["values"]["driver_sex"] == "남성"
    assert payload["values"]["remarks"] == "CSV 복구 시에만 중복 정리"
    assert payload["videos"] == [video.relative_to(workspace).as_posix()]
    assert metadata["schema_version"] == 4
    assert metadata["path_storage"] == "workspace-relative-v1"
    assert source.read_bytes() == original_csv and video.read_bytes() == original_video
    restored, = CaseRepository(workspace).load()
    assert restored.values["remarks"] == payload["values"]["remarks"]


def test_csv_recovery_cli_remains_independent_of_gui_and_detector_libraries(tmp_path):
    source, workspace, video = _csv_fixture(tmp_path)
    output = workspace / "01_사고동영상/작업관리/작업관리.db"
    original_csv, original_video = source.read_bytes(), video.read_bytes()
    script = Path(__file__).resolve().parents[2] / "recover_investigation_csv.py"
    completed = subprocess.run([sys.executable, "-S", "-X", "utf8", str(script), "--source", str(source),
                                "--workspace", str(workspace), "--dry-run"], capture_output=True,
                               text=True, encoding="utf-8", timeout=15)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["restored_cases"] == 1 and report["duplicate_cases_removed"] == 1
    assert not output.exists()
    assert source.read_bytes() == original_csv and video.read_bytes() == original_video
