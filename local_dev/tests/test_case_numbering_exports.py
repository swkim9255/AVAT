"""CSV, report text and filenames share project-wide rather than subset IDs."""

import csv
from zipfile import ZipFile

from investigator.case_numbering import build_case_number_map
from investigator.models import InvestigationCase
from investigator.report_export import export_case_reports
from investigator.storage import CaseRepository


def test_partial_report_and_csv_use_identical_number_and_preserve_original(tmp_path):
    numbers = ["2020011309999", "2019011300001", "2020011302572"]
    cases = [InvestigationCase(values={"institution": "samsung", "accident_classification": value})
             for value in numbers]
    repository = CaseRepository(tmp_path / "project")
    repository.save(cases)
    mapping = build_case_number_map(cases)
    report = export_case_reports([cases[0]], tmp_path / "reports", report_number_by_uid=mapping)[0]
    assert report.name == "SAMSUNG-20200113_002_사고분석보고서.docx"
    with ZipFile(report) as archive:
        xml = archive.read("word/document.xml").decode()
    assert "20200113_002" in xml
    assert numbers[0] not in xml
    destination = repository.export_csv(tmp_path / "result.csv", cases, include_specification=True)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["report_accident_number"] == "20200113_002"
    assert rows[2]["report_accident_number"] == "20200113_001"
    assert [row["accident_classification"] for row in rows] == numbers
    assert build_case_number_map(CaseRepository(repository.workspace).load()) == mapping
    assert [case.values["accident_classification"] for case in cases] == numbers
