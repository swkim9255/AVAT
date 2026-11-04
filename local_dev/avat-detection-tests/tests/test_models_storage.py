from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from investigator.models import InvestigationCase, new_case
from investigator.schema import csv_headers
from investigator.storage import CaseRepository, WorkspaceError
from tests.helpers import valid_values


def test_case_round_trip_preserves_leading_zero_unicode_and_markers(tmp_path: Path) -> None:
    values = valid_values()
    values["remarks"] = "첫 줄\n추가 사고 정보"
    case = InvestigationCase(values=values)
    case.set_trigger(12)
    case.set_collision(30)
    repository = CaseRepository(tmp_path)
    repository.save([case], active_case_uid=case.uid)

    loaded = repository.load()
    assert len(loaded) == 1
    assert loaded[0].display_id == "SAMSUNG-001"
    assert loaded[0].values["vehicle_model_name"] == "테스트 차량"
    assert loaded[0].values["remarks"] == "첫 줄\n추가 사고 정보"
    assert loaded[0].trigger_frame == 12
    assert loaded[0].collision_frame == 30
    assert loaded[0].collision_frames == [30]
    assert repository.active_case_uid() == case.uid


@pytest.mark.parametrize("legacy_day", [None, "월"])
def test_json_load_normalizes_missing_or_mismatched_incident_day(legacy_day: str | None) -> None:
    values: dict[str, object] = {"incident_date": "20260904"}
    if legacy_day is not None:
        values["incident_day"] = legacy_day

    case = InvestigationCase.from_dict({"values": values})

    assert case.values["incident_date"] == "20260904"
    assert case.values["incident_day"] == "금"
    assert case.to_dict()["values"]["incident_day"] == "금"


def test_json_load_clears_stale_day_and_keeps_invalid_incident_date_as_format_error() -> None:
    case = InvestigationCase.from_dict(
        {"values": {"incident_date": "20260230", "incident_day": "월"}}
    )

    assert case.values["incident_date"] == "20260230"
    assert "incident_day" not in case.values
    assert case.errors["incident_date"] == "형식이 잘못되었습니다"


def test_markers_are_isolated_per_video_and_round_trip(tmp_path: Path) -> None:
    case = InvestigationCase(values=valid_values(), videos=["first.mp4", "second.mp4"])
    case.set_trigger(10)
    case.set_collision(20)
    case.active_video_index = 1
    assert case.trigger_frame is None
    assert case.collision_frame is None
    assert case.collision_frames == []
    case.set_trigger(30)
    case.set_collision(40)

    repository = CaseRepository(tmp_path)
    repository.save([case])
    loaded = repository.load()[0]
    assert loaded.trigger_frame == 30
    assert loaded.collision_frame == 40
    assert loaded.collision_frames == [40]
    loaded.active_video_index = 0
    assert loaded.trigger_frame == 10
    assert loaded.collision_frame == 20
    assert loaded.collision_frames == [20]


def test_video_rotation_is_isolated_per_video_and_round_trips(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    case = InvestigationCase(values=valid_values(), videos=[str(first), str(second)])
    case.complete()

    case.set_video_rotation(90)
    assert case.video_rotation == 90
    assert case.rotation_by_video[str(first)] == 90
    case.set_video_rotation(180)
    assert case.video_rotation == 180
    assert case.rotation_by_video[str(first)] == 180
    case.set_video_rotation(270)
    assert case.video_rotation == 270
    assert case.rotation_by_video[str(first)] == 270
    assert case.status == "completed"
    case.active_video_index = 1
    assert case.video_rotation == 0
    case.set_video_rotation(180)
    assert case.video_rotation == 180
    assert case.rotation_by_video[str(second)] == 180

    repository = CaseRepository(tmp_path / "workspace")
    repository.save([case])
    loaded = repository.load()[0]
    assert loaded.video_rotation == 180
    loaded.active_video_index = 0
    assert loaded.video_rotation == 270
    assert loaded.status == "completed"

    loaded.set_video_rotation(0)
    assert loaded.video_rotation == 0
    assert str(first) not in loaded.rotation_by_video
    loaded.active_video_index = 1
    assert loaded.video_rotation == 180
    loaded.remove_video(1)
    assert loaded.rotation_by_video == {}


def test_collision_marker_is_replaced_and_can_be_cleared() -> None:
    case = InvestigationCase(values=valid_values())

    case.set_collision(20)
    case.set_collision(40)
    assert case.collision_frame == 40
    assert case.collision_frames == [40]

    case.set_collision(None)
    assert case.collision_frame is None
    assert case.collision_frames == []


def test_legacy_multiple_collisions_are_migrated_to_first_collision() -> None:
    case = InvestigationCase.from_dict(
        {
            "uid": "legacy-markers",
            "values": valid_values(),
            "videos": ["legacy.mp4"],
            "markers": {
                "trigger_frame": 10,
                "collision_frames": [70, 30, 50, 30],
            },
            "workflow": {"status": "draft"},
        }
    )

    assert case.collision_frame == 30
    assert case.collision_frames == [30]
    assert case.to_dict()["markers_by_video"]["legacy.mp4"]["collision_frames"] == [30]


def test_new_case_numbers_are_unique_and_zero_padded() -> None:
    first = new_case([])
    second = new_case([first])
    assert first.values["institution"] == "samsung"
    assert second.values["institution"] == "samsung"
    assert first.display_id == "SAMSUNG-001"
    assert second.display_id == "SAMSUNG-002"


def test_complete_workflow_and_edit_returns_to_draft() -> None:
    case = InvestigationCase(values=valid_values())
    case.complete()
    assert case.status == "completed"
    case.set_value("driver_age", "45-49세")
    assert case.status == "draft"
    assert case.completed_at is None


def test_reattaching_the_same_video_is_a_completed_case_no_op(tmp_path: Path) -> None:
    video = tmp_path / "0123456789012.mp4"
    video.write_bytes(b"video")
    case = InvestigationCase(values=valid_values())
    assert case.attach_videos([video]) == 1
    case.complete()
    completed_at = case.completed_at
    updated_at = case.updated_at

    assert case.attach_videos([video]) == 0

    assert case.status == "completed"
    assert case.completed_at == completed_at
    assert case.updated_at == updated_at
    assert case.videos == [str(video.resolve())]


def test_legacy_numeric_driver_age_is_migrated_to_five_year_range() -> None:
    case = InvestigationCase(values={"driver_age": "47"})
    assert case.values["driver_age"] == "45-49세"


def test_legacy_speed_and_camera_values_are_migrated() -> None:
    case = InvestigationCase(
        values={
            "trigger_vehicle_speed": "저속",
            "available_data": ["EDR", "전방블랙박스", "후방블랙박스"],
        }
    )

    assert case.values["trigger_vehicle_speed"] == "저속(10-30kph)"
    assert case.values["available_data"] == [
        "EDR",
        "자차전방블랙박스",
        "자차후방블랙박스",
    ]


def test_case_migrates_combined_injury_grades_to_separate_minor_and_severe_fields() -> None:
    case = InvestigationCase(
        values={
            "incident_injury_grades": "14, 7, 12, 1, 12",
            "incident_minor_injury": "99",
            "incident_severe_injury": "99",
            "incident_representative_injury_grade": "14",
        }
    )

    assert "incident_injury_grades" not in case.values
    assert "incident_representative_injury_grade" not in case.values
    assert case.values["incident_minor_injury_grades"] == "12, 12, 14"
    assert case.values["incident_minor_injury"] == "3"
    assert case.values["incident_minor_representative_injury_grade"] == "12"
    assert case.values["incident_severe_injury_grades"] == "1, 7"
    assert case.values["incident_severe_injury"] == "2"
    assert case.values["incident_severe_representative_injury_grade"] == "1"

    case.set_value("incident_severe_injury_grades", "11, 1, 8")
    assert case.values["incident_severe_injury_grades"] == "1, 8, 11"
    assert case.values["incident_severe_injury"] == "3"


def test_separate_injury_grade_fields_round_trip_and_export(tmp_path: Path) -> None:
    case = InvestigationCase(
        values={
            "incident_minor_injury_grades": "14, 12, 12",
            "incident_severe_injury_grades": "11, 3, 1",
        }
    )
    repository = CaseRepository(tmp_path)
    repository.save([case])

    loaded = repository.load()[0]
    assert loaded.values["incident_minor_injury_grades"] == "12, 12, 14"
    assert loaded.values["incident_minor_injury"] == "3"
    assert loaded.values["incident_minor_representative_injury_grade"] == "12"
    assert loaded.values["incident_severe_injury_grades"] == "1, 3, 11"
    assert loaded.values["incident_severe_injury"] == "3"
    assert loaded.values["incident_severe_representative_injury_grade"] == "1"

    destination = repository.export_csv(tmp_path / "injury-grades.csv", [loaded])
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["incident_minor_injury_grade"] == "12, 12, 14"
    assert row["incident_minor_representative_injury_grade"] == "12"
    assert row["incident_severe_injury_grade"] == "1, 3, 11"
    assert row["incident_severe_representative_injury_grade"] == "1"
    assert case.values["incident_severe_representative_injury_grade"] == "1"

    case.set_value("incident_minor_injury_grades", "")
    for key in (
        "incident_minor_injury_grades",
        "incident_minor_injury",
        "incident_minor_representative_injury_grade",
    ):
        assert key not in case.values
    assert case.values["incident_severe_injury"] == "3"


def test_duplicate_identity_is_rejected_before_save(tmp_path: Path) -> None:
    repository = CaseRepository(tmp_path)
    with pytest.raises(WorkspaceError, match="중복"):
        repository.save(
            [InvestigationCase(values=valid_values()), InvestigationCase(values=valid_values())]
        )


def test_malformed_case_entry_is_rejected_without_rewriting_source(tmp_path: Path) -> None:
    database = tmp_path / "investigations.json"
    source = json.dumps({"schema_version": 1, "cases": [7]}, ensure_ascii=False)
    database.write_text(source, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"cases\[0\]"):
        CaseRepository(tmp_path).load()
    assert database.read_text(encoding="utf-8") == source


def test_string_video_list_is_rejected_without_rewriting_source(tmp_path: Path) -> None:
    database = tmp_path / "investigations.json"
    payload = {
        "schema_version": 1,
        "cases": [
            {
                "uid": "broken-videos",
                "values": {},
                "videos": "clip.mp4",
                "markers_by_video": {},
                "workflow": {"status": "draft"},
            }
        ],
    }
    source = json.dumps(payload, ensure_ascii=False)
    database.write_text(source, encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"cases\[0\]"):
        CaseRepository(tmp_path).load()
    assert database.read_text(encoding="utf-8") == source


def test_file_cannot_be_used_as_workspace(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "workspace-file"
    not_a_directory.write_text("data", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="작업 폴더"):
        CaseRepository(not_a_directory).load()


def test_csv_export_is_utf8_deterministic_and_formula_safe(tmp_path: Path) -> None:
    values = valid_values()
    values["site_manual_input"] = "=1+1"
    case = InvestigationCase(values=values)
    case.set_collision(25)
    case.complete()
    destination = CaseRepository(tmp_path).export_csv(tmp_path / "result.csv", [case])
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["case_id"] == "SAMSUNG-001"
    assert row["site_manual_input"] == "'=1+1"
    assert row["workflow_status"] == "completed"
    assert row["video_files"] == "null"
    assert row["active_video_file"] == "null"
    assert row["trigger_frame"] == "null"
    assert row["incident_minor_injury"] == "null"
    assert row["collision_frames"] == "25"
    assert json.loads(row["video_markers"])["__case__"]["collision_frames"] == [25]


def test_csv_export_writes_null_for_unselected_cells_but_preserves_zero(
    tmp_path: Path,
) -> None:
    case = InvestigationCase(
        values={
            "institution": "samsung",
            "accident_classification": "001",
            "available_data": ["EDR"],
        }
    )
    case.set_trigger(0)

    destination = CaseRepository(tmp_path).export_csv(tmp_path / "draft.csv", [case])
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["driver_age"] == "null"
    assert row["video_files"] == "null"
    assert row["active_video_file"] == "null"
    assert row["trigger_frame"] == "0"
    assert row["collision_frames"] == "null"
    assert row["data_list_EDR"] == "1"
    assert row["data_list_CCTV"] == "null"
    assert row["incident_collision_direction_front"] == "null"


def test_csv_export_fills_every_missing_schema_and_metadata_cell_with_null(
    tmp_path: Path,
) -> None:
    case = InvestigationCase(
        values={
            "institution": "samsung",
            "accident_classification": "0000000000123",
            "available_data": [],
            "site_manual_input": "   ",
        }
    )

    destination = CaseRepository(tmp_path).export_csv(tmp_path / "missing.csv", [case])
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert row["case_id"] == "SAMSUNG-0000000000123"
    assert row["workflow_status"] == "draft"
    assert all(value != "" for value in row.values())
    for header in csv_headers():
        expected = {
            "Institution": "samsung",
            "accident_classification": "0000000000123",
        }.get(header, "null")
        assert row[header] == expected, header
    assert row["video_files"] == "null"
    assert row["active_video_file"] == "null"
    assert row["trigger_frame"] == "null"
    assert row["collision_frames"] == "null"
    assert row["video_markers"] == "null"
    assert row["driver_sex"] == "null"
    assert row["site_manual_input"] == "null"
    assert row["data_list_EDR"] == "null"
    assert row["data_list_CCTV"] == "null"


def test_csv_export_preserves_explicit_binary_and_numeric_zero_but_nulls_unselected(
    tmp_path: Path,
) -> None:
    case = InvestigationCase(
        values={
            "institution": "samsung",
            "accident_classification": "0000000000123",
            "available_data": ["EDR"],
            "collision_directions": ["전면"],
            "obstacle_exists": "무",
            "incident_damage_unknown": "확인 가능",
            "incident_minor_injury": "0",
        }
    )
    case.set_trigger(0)

    destination = CaseRepository(tmp_path).export_csv(tmp_path / "answered.csv", [case])
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert all(value != "" for value in row.values())
    assert row["trigger_frame"] == "0"
    assert row["incident_minor_injury"] == "0"
    assert row["incident_damage_Unknown"] == "0"
    assert row["data_list_EDR"] == "1"
    assert row["data_list_CCTV"] == "null"
    assert row["incident_collision_direction_front"] == "1"
    assert row["incident_collision_direction_right"] == "null"
    assert row["obstacle_child"] == "null"
    assert row["obstacle_guardrail"] == "null"
