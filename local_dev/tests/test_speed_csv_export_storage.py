"""Speed CSV staging is automatic and preserves previous exports on failure."""

from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path

import pytest

import investigator.storage as storage
from investigator.case_numbering import build_case_number_map
from investigator.models import InvestigationCase
from investigator.speed_csv import speed_csv_path
from investigator.storage import CaseRepository, WorkspaceError, data_specification_path


def sample_cases() -> list[InvestigationCase]:
    return [
        InvestigationCase(values={
            "institution": "samsung", "accident_classification": number,
        })
        for number in ["2020011309999", "2020011300001"]
    ]


def assert_no_staging_files(directory: Path) -> None:
    assert not list(directory.glob("*.tmp"))


@pytest.mark.parametrize("include_specification", [False, True])
def test_speed_companion_always_staged_with_project_report_numbers(
    tmp_path, monkeypatch, include_specification,
):
    cases = sample_cases()
    before = deepcopy([case.to_dict() for case in cases])
    expected_numbers = build_case_number_map(cases)
    observed = []

    def write_speed(handle, project_cases, *, report_numbers=None):
        observed.append((project_cases, report_numbers))
        writer = csv.writer(handle)
        writer.writerow(["case_id", "report_accident_number"])
        for case in project_cases:
            writer.writerow([case.display_id, report_numbers[case.uid]])

    monkeypatch.setattr(storage, "write_speed_csv", write_speed)
    destination = tmp_path / "nested" / "조사 결과.csv"
    result = CaseRepository(tmp_path).export_csv(
        destination, cases, include_specification=include_specification,
    )

    assert result == destination.resolve()
    assert observed == [(cases, expected_numbers)]
    assert observed[0][0] is cases
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    companion = speed_csv_path(destination)
    with companion.open(encoding="utf-8-sig", newline="") as handle:
        speed_rows = list(csv.DictReader(handle))
    assert companion.read_bytes().startswith(b"\xef\xbb\xbf")
    assert [row["report_accident_number"] for row in speed_rows] == [
        row["report_accident_number"] for row in rows
    ] == ["20200113_002", "20200113_001"]
    assert data_specification_path(destination).exists() == include_specification
    assert [case.to_dict() for case in cases] == before
    assert_no_staging_files(destination.parent)


@pytest.mark.parametrize("include_specification", [False, True])
@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_speed_generation_failure_preserves_every_previous_output(
    tmp_path, monkeypatch, include_specification, failure_type,
):
    destination = tmp_path / "result.csv"
    outputs = {
        destination: b"previous investigation",
        speed_csv_path(destination): b"previous speed analysis",
        data_specification_path(destination): b"previous specification",
    }
    for path, content in outputs.items():
        path.write_bytes(content)

    def fail_after_partial_write(handle, *_args, **_kwargs):
        handle.write("partial speed analysis")
        raise failure_type("cannot complete speed analysis")

    monkeypatch.setattr(storage, "write_speed_csv", fail_after_partial_write)
    error_type = WorkspaceError if failure_type is OSError else ValueError
    with pytest.raises(error_type):
        CaseRepository(tmp_path).export_csv(
            destination, sample_cases(), include_specification=include_specification,
        )
    assert {path: path.read_bytes() for path in outputs} == outputs
    assert_no_staging_files(tmp_path)


def test_locked_primary_preserves_existing_companions(tmp_path, monkeypatch):
    destination = tmp_path / "result.csv"
    outputs = {
        destination: b"previous investigation",
        speed_csv_path(destination): b"previous speed analysis",
        data_specification_path(destination): b"previous specification",
    }
    for path, content in outputs.items():
        path.write_bytes(content)
    original_replace = storage.os.replace

    def replace(source, target):
        if target == destination:
            raise PermissionError("investigation CSV is open")
        return original_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", replace)
    with pytest.raises(WorkspaceError, match="CSV와 데이터 명세서를 내보내지 못했습니다"):
        CaseRepository(tmp_path).export_csv(destination, [], include_specification=True)
    assert {path: path.read_bytes() for path in outputs} == outputs
    assert_no_staging_files(tmp_path)


@pytest.mark.parametrize("include_specification", [False, True])
def test_locked_speed_csv_reports_partial_save_without_replacing_other_companions(
    tmp_path, monkeypatch, include_specification,
):
    destination = tmp_path / "result.csv"
    companion = speed_csv_path(destination)
    specification = data_specification_path(destination)
    companion.write_bytes(b"previous speed analysis")
    specification.write_bytes(b"previous specification")
    original_replace = storage.os.replace

    def replace(source, target):
        if target == companion:
            raise PermissionError("speed CSV is open")
        return original_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", replace)
    with pytest.raises(WorkspaceError, match="CSV는 저장했습니다") as caught:
        CaseRepository(tmp_path).export_csv(
            destination, [], include_specification=include_specification,
        )
    assert "속도 분석 CSV는 저장하지 못했습니다" in str(caught.value)
    assert ("데이터 명세서도 저장하지 못했습니다" in str(caught.value)) == include_specification
    assert destination.read_bytes().startswith(b"\xef\xbb\xbfcase_id")
    assert companion.read_bytes() == b"previous speed analysis"
    assert specification.read_bytes() == b"previous specification"
    assert_no_staging_files(tmp_path)


def test_locked_specification_reports_both_csvs_were_saved(tmp_path, monkeypatch):
    destination = tmp_path / "result.csv"
    companion = speed_csv_path(destination)
    specification = data_specification_path(destination)
    specification.write_bytes(b"previous specification")
    original_replace = storage.os.replace

    def replace(source, target):
        if target == specification:
            raise PermissionError("specification is open")
        return original_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", replace)
    with pytest.raises(WorkspaceError, match="CSV는 저장했습니다") as caught:
        CaseRepository(tmp_path).export_csv(destination, [], include_specification=True)
    assert "속도 분석 CSV도 저장했습니다" in str(caught.value)
    assert destination.read_bytes().startswith(b"\xef\xbb\xbfcase_id")
    assert companion.read_bytes().startswith(b"\xef\xbb\xbf")
    assert specification.read_bytes() == b"previous specification"
    assert_no_staging_files(tmp_path)


def test_empty_project_still_exports_header_only_speed_csv(tmp_path):
    destination = CaseRepository(tmp_path).export_csv(tmp_path / "empty.csv", [])
    companion = speed_csv_path(destination)
    with companion.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames
        assert list(reader) == []
    assert_no_staging_files(tmp_path)
