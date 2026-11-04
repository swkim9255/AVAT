"""전체 프로젝트 보고서 번호를 원본 CSV 식별자 변경 없이 첫 칼럼으로 출력한다."""

from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path

import pytest

from investigator.case_numbering import REPORT_ACCIDENT_NUMBER_COLUMN
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.storage import CaseRepository


def make_case(number: str, *, institution: str = "samsung") -> InvestigationCase:
    return InvestigationCase(values={
        "institution": institution,
        "accident_classification": number,
        "remarks": "=text must remain escaped",
    })


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames or [], list(reader)


def test_csv_prepends_report_number_and_preserves_original_ids_order_and_payload(tmp_path: Path):
    cases = [
        make_case("2020011302572"),
        make_case("2019123100001"),
        make_case("2020011300002"),
        make_case("2020011300100"),
        make_case("2021011300001"),
    ]
    original = deepcopy([case.to_dict() for case in cases])
    repository = CaseRepository(tmp_path)
    headers, rows = read_csv(repository.export_csv(tmp_path / "cases.csv", cases))

    assert headers == [
        REPORT_ACCIDENT_NUMBER_COLUMN,
        "case_id", "workflow_status", "video_files", "active_video_file", "trigger_frame",
        "collision_frames", "video_markers", *repository.form_definition.csv_headers(),
    ]
    assert headers[8:] == repository.form_definition.csv_headers()
    assert len(headers) == len(set(headers))
    assert [row["accident_classification"] for row in rows] == [
        case.values["accident_classification"] for case in cases
    ]
    assert [row["case_id"] for row in rows] == [case.display_id for case in cases]
    assert [row[REPORT_ACCIDENT_NUMBER_COLUMN] for row in rows] == [
        "20200113_003", "20191231_001", "20200113_001", "20200113_002", "20210113_001",
    ]
    assert all(row["remarks"] == "'=text must remain escaped" for row in rows)
    assert all(row["trigger_frame"] == "null" for row in rows)
    assert [case.to_dict() for case in cases] == original


def test_csv_report_numbers_ignore_user_display_order_and_survive_reopen(tmp_path: Path):
    cases = [make_case("2020011302572"), make_case("2020011300002")]
    repository = CaseRepository(tmp_path)
    repository.save(cases)
    loaded = CaseRepository(tmp_path).load()
    _, first = read_csv(repository.export_csv(tmp_path / "first.csv", loaded))
    _, reordered = read_csv(repository.export_csv(tmp_path / "reordered.csv", loaded[::-1]))
    def number_by_id(rows):
        return {row["case_id"]: row[REPORT_ACCIDENT_NUMBER_COLUMN] for row in rows}

    assert number_by_id(first) == number_by_id(reordered) == {
        "SAMSUNG-2020011302572": "20200113_002",
        "SAMSUNG-2020011300002": "20200113_001",
    }


@pytest.mark.parametrize("number", ["", "manual", "202001130001", "2020023000001"])
def test_invalid_or_missing_source_number_exports_null(tmp_path: Path, number: str):
    headers, rows = read_csv(
        CaseRepository(tmp_path).export_csv(tmp_path / "invalid.csv", [make_case(number)])
    )
    assert REPORT_ACCIDENT_NUMBER_COLUMN in headers
    assert rows[0][REPORT_ACCIDENT_NUMBER_COLUMN] == "null"
    assert rows[0]["accident_classification"] == (number or "null")


def test_number_map_is_calculated_once_for_whole_export(tmp_path: Path, monkeypatch):
    cases = [make_case("2020011302572"), make_case("2020011300002")]
    calls = []

    def build_map(project_cases):
        calls.append(project_cases)
        return {case.uid: f"20200113_{index:03d}" for index, case in enumerate(cases, 1)}

    monkeypatch.setattr("investigator.storage.build_case_number_map", build_map)
    _, rows = read_csv(CaseRepository(tmp_path).export_csv(tmp_path / "once.csv", cases))
    assert len(calls) == 1
    assert calls[0] is cases
    assert [row[REPORT_ACCIDENT_NUMBER_COLUMN] for row in rows] == [
        "20200113_001", "20200113_002",
    ]


@pytest.mark.parametrize("collision", ["column", "field_alias", "choice_column", "choice_alias"])
def test_form_editor_rejects_report_number_column_collisions(collision: str):
    definition = FormDefinition.default()
    remarks = next(record for record in definition.records() if record["key"] == "remarks")
    if collision == "column":
        remarks["csv_column"] = REPORT_ACCIDENT_NUMBER_COLUMN
    elif collision == "field_alias":
        remarks["aliases"].append(REPORT_ACCIDENT_NUMBER_COLUMN)
    else:
        obstacle = next(
            record for record in definition.records() if record["key"] == "obstacle_pedestrians"
        )
        choice = obstacle["choices"][0]
        if collision == "choice_column":
            choice["csv_column"] = REPORT_ACCIDENT_NUMBER_COLUMN
        else:
            choice["csv_aliases"].append(REPORT_ACCIDENT_NUMBER_COLUMN)
    with pytest.raises(ValueError, match="이미 사용 중인 CSV 칼럼명"):
        definition.validate()


def test_custom_csv_rename_stays_after_fixed_leading_report_number(tmp_path: Path):
    repository = CaseRepository(tmp_path)
    remarks = next(
        record for record in repository.form_definition.records() if record["key"] == "remarks"
    )
    remarks["aliases"].append("remarks")
    remarks["csv_column"] = "custom_notes"
    repository.form_definition.validate()
    headers, rows = read_csv(
        repository.export_csv(tmp_path / "custom.csv", [make_case("2020011302572")])
    )
    assert headers[0] == REPORT_ACCIDENT_NUMBER_COLUMN
    assert "custom_notes" in headers
    assert "remarks" not in headers  # 이전 이름은 별도 출력 칼럼으로 추가하지 않는다.
    assert "remarks" in remarks["aliases"]  # 기존 보고서와 데이터의 연결은 내부에 유지한다.
    assert rows[0]["custom_notes"] == "'=text must remain escaped"
    assert "remarks" not in rows[0]
    assert rows[0][REPORT_ACCIDENT_NUMBER_COLUMN] == "20200113_001"
