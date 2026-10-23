"""Report IDs share project-wide numbering without changing stored case IDs."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from investigator.case_numbering import build_case_number_map
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.report_export import export_case_reports

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def case(number: str, *, institution: str = "samsung") -> InvestigationCase:
    return InvestigationCase(values={
        "institution": institution,
        "accident_classification": number,
        "remarks": f"원본 번호 메모: {number}",
    })


def template(tmp_path: Path, placeholders: str) -> Path:
    path = tmp_path / "template.docx"
    xml = (
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r>'
        f"<w:t>{placeholders}</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("unchanged-member.bin", b"source template bytes")
    return path


def visible_text(path: Path) -> str:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        assert archive.read("unchanged-member.bin") == b"source template bytes"
    return "".join(node.text or "" for node in root.iter(f"{{{W}}}t"))


def test_report_selection_keeps_project_number_and_stored_source_unchanged(tmp_path):
    first, second, third = [case(number) for number in (
        "2020011302572", "2020011303000", "2020011400001",
    )]
    before = deepcopy(second.to_dict())
    source = template(
        tmp_path,
        "{{accident_classification}}|{{case_id}}|{{report_accident_number}}|{{remarks}}",
    )

    output = export_case_reports(
        [second],
        tmp_path / "reports",
        template_path=source,
        report_number_by_uid=build_case_number_map([third, second, first]),
    )[0]

    assert output.name == "SAMSUNG-20200113_002_사고분석보고서.docx"
    assert visible_text(output) == (
        "20200113_002|SAMSUNG-20200113_002|20200113_002|원본 번호 메모: 2020011303000"
    )
    assert second.to_dict() == before


def test_report_default_numbering_uses_numeric_order_not_selection_order(tmp_path):
    first, second, tomorrow = [case(number) for number in (
        "2020011300001", "2020011399999", "2020011400001",
    )]
    source = template(tmp_path, "{{accident_classification}}")
    outputs = export_case_reports(
        [second, tomorrow, first], tmp_path / "reports", template_path=source,
    )

    assert [visible_text(output) for output in outputs] == [
        "20200113_002", "20200114_001", "20200113_001",
    ]
    assert outputs[0].name == "SAMSUNG-20200113_002_사고분석보고서.docx"


def test_report_number_reaches_current_and_legacy_classification_aliases(tmp_path):
    definition = FormDefinition.default()
    field = next(record for record in definition.records() if record["key"] == "accident_classification")
    field.update(csv_column="custom_accident_number", aliases=["old_accident_number"])
    definition.validate()
    source = template(
        tmp_path,
        "{{accident_classification}}|{{custom_accident_number}}|{{old_accident_number}}|{{Institution}}",
    )

    output = export_case_reports(
        [case("2020011302572")], tmp_path / "reports",
        form_definition=definition, template_path=source,
    )[0]

    assert visible_text(output) == "20200113_001|20200113_001|20200113_001|samsung"


def test_numbered_report_collision_preserves_previous_document(tmp_path):
    item = case("2020011302572")
    source = template(tmp_path, "{{accident_classification}}")
    first = export_case_reports([item], tmp_path / "reports", template_path=source)[0]
    original = first.read_bytes()
    second = export_case_reports([item], tmp_path / "reports", template_path=source)[0]

    assert first.name == "SAMSUNG-20200113_001_사고분석보고서.docx"
    assert second.name == "SAMSUNG-20200113_001_사고분석보고서_2.docx"
    assert first.read_bytes() == original
    assert visible_text(first) == visible_text(second) == "20200113_001"


def test_invalid_original_number_keeps_original_report_identity(tmp_path):
    source = template(tmp_path, "{{accident_classification}}|{{case_id}}|{{report_accident_number}}")
    output = export_case_reports(
        [case("custom-case")], tmp_path / "reports", template_path=source,
    )[0]

    assert output.name == "SAMSUNG-custom-case_사고분석보고서.docx"
    assert visible_text(output) == "custom-case|SAMSUNG-custom-case|-"


def test_same_original_id_two_cases_still_get_distinct_numbered_reports(tmp_path):
    sources = [case("2020011302572"), case("2020011302572")]
    source = template(tmp_path, "{{accident_classification}}")
    outputs = export_case_reports(sources, tmp_path / "reports", template_path=source)

    assert len(outputs) == len(set(outputs)) == 2
    assert {visible_text(output) for output in outputs} == {"20200113_001", "20200113_002"}
