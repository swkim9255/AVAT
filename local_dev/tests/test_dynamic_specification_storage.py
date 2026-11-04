"""The companion workbook describes exactly the live investigation CSV schema."""

from __future__ import annotations

import csv
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

import investigator.storage as storage
from investigator.form_definition import FormDefinition, field_record
from investigator.models import InvestigationCase
from investigator.schema import Choice, FieldKind, FieldSpec
from investigator.speed_csv import speed_csv_path
from investigator.storage import CaseRepository, WorkspaceError, data_specification_path


def tab_for(definition: FormDefinition, key: str) -> dict:
    return next(tab for tab in definition.tabs if tab["key"] == key)


def text_field(key: str, label: str, column: str | None = None) -> dict:
    return field_record(FieldSpec(key, label, FieldKind.TEXT, column or key))


def csv_headers(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def specification_rows(path: Path) -> list[tuple[str, str]]:
    """Read the two-column table independently of the production builder."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(path.read_bytes())) as workbook:
        strings = (
            ["".join(element.itertext()) for element in ElementTree.fromstring(
                workbook.read("xl/sharedStrings.xml")
            )]
            if "xl/sharedStrings.xml" in workbook.namelist() else []
        )
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in sheet.findall(".//m:sheetData/m:row", ns)[1:]:
            values = {}
            for cell in row.findall("m:c", ns):
                ref = cell.get("r", "")
                column = "".join(char for char in ref if char.isalpha())
                if column not in {"A", "B"}:
                    continue
                value = cell.find("m:v", ns)
                if cell.get("t") == "s":
                    text = strings[int(value.text)] if value is not None else ""
                elif cell.get("t") == "inlineStr":
                    inline = cell.find("m:is", ns)
                    text = "".join(inline.itertext()) if inline is not None else ""
                else:
                    text = value.text or "" if value is not None else ""
                values[column] = text
            if values.get("A"):
                rows.append((values["A"], values.get("B", "")))
        return rows


def exported_schema(repository: CaseRepository, destination: Path) -> tuple[list[str], dict[str, str]]:
    repository.export_csv(destination, [], include_specification=True)
    headers = csv_headers(destination)
    rows = specification_rows(data_specification_path(destination))
    assert [column for column, _description in rows] == headers
    assert rows[0] == (
        "report_accident_number", "보고서 사고번호(날짜_순번)(비식별화 목적)",
    )
    assert headers[1:8] == [
        "case_id", "workflow_status", "video_files", "active_video_file", "trigger_frame",
        "collision_frames", "video_markers",
    ]
    assert headers[8:] == repository.form_definition.csv_headers()
    assert len(rows) == len(set(headers))
    assert all(description for _column, description in rows)
    return headers, dict(rows)


def test_default_specification_uses_csv_order_including_date_before_weekday(tmp_path):
    headers, _descriptions = exported_schema(CaseRepository(tmp_path), tmp_path / "default.csv")
    assert len(headers) == 95
    assert headers.index("incident_date") < headers.index("incident_day")
    assert headers[0] == "report_accident_number"


def test_edited_tabs_fields_and_individual_choices_follow_current_csv_order(tmp_path):
    repository = CaseRepository(tmp_path)
    definition = repository.form_definition
    driver = tab_for(definition, "driver")
    driver["fields"].reverse()
    driver["fields"].insert(1, text_field("custom_driver_note", "추가 운전자 메모"))
    evidence = tab_for(definition, "evidence")
    evidence["fields"][0]["choices"].reverse()
    definition.tabs.remove(evidence)
    definition.tabs.insert(1, evidence)
    # There is no date/weekday exception in either output.
    environment = tab_for(definition, "environment")
    date = next(record for record in environment["fields"] if record["key"] == "incident_date")
    environment["fields"].remove(date)
    environment["fields"].append(date)
    definition.validate()

    headers, descriptions = exported_schema(repository, tmp_path / "ordered.csv")
    assert headers[8:] == definition.csv_headers()
    start = headers.index("driver_age")
    assert headers[start:start + 4] == [
        "driver_age", "custom_driver_note", "driver_exp", "driver_sex",
    ]
    assert headers.index("incident_day") < headers.index("incident_date")
    assert "추가 운전자 메모" in descriptions["custom_driver_note"]


def test_new_custom_tab_and_multi_choice_columns_are_described(tmp_path):
    repository = CaseRepository(tmp_path)
    custom = field_record(FieldSpec(
        "custom_observation", "추가 관찰 유형", FieldKind.MULTI, "custom_observations",
        (Choice("보행 위험", "risk", "custom_risk"),
         Choice("시야 제한", "visibility", "custom_visibility")),
    ))
    repository.form_definition.tabs.insert(2, {
        "key": "custom_notes", "title": "추가 조사", "short_title": "추가", "columns": 1,
        "fields": [custom, text_field("custom_note", "추가 설명")],
    })
    repository.form_definition.validate()

    headers, descriptions = exported_schema(repository, tmp_path / "custom.csv")
    start = headers.index("custom_observations")
    assert headers[start:start + 4] == [
        "custom_observations", "custom_risk", "custom_visibility", "custom_note",
    ]
    assert "추가 관찰 유형" in descriptions["custom_observations"]
    assert "보행 위험" in descriptions["custom_risk"]
    assert "시야 제한" in descriptions["custom_visibility"]
    assert "추가 설명" in descriptions["custom_note"]


def test_renamed_columns_and_labels_replace_previous_specification_entries(tmp_path):
    repository = CaseRepository(tmp_path)
    definition = repository.form_definition
    remarks = next(record for record in definition.records() if record["key"] == "remarks")
    remarks.update(label="현장 추가 의견", csv_column="current_notes", aliases=["remarks"])
    pedestrians = next(record for record in definition.records() if record["key"] == "obstacle_pedestrians")
    adult = next(choice for choice in pedestrians["choices"] if choice["value"] == "어른")
    adult.update(label="성인 보행자", csv_column="current_adult", csv_aliases=["obstacle_adult"])
    definition.validate()

    headers, descriptions = exported_schema(repository, tmp_path / "renamed.csv")
    assert "remarks" not in headers
    assert "obstacle_adult" not in headers
    assert "현장 추가 의견" in descriptions["current_notes"]
    assert "성인 보행자" in descriptions["current_adult"]


def test_deleted_custom_fields_and_choices_disappear_from_reexported_workbook(tmp_path):
    repository = CaseRepository(tmp_path)
    custom = field_record(FieldSpec(
        "custom_modes", "추가 유형", FieldKind.MULTI, "custom_modes",
        (Choice("유형 A", "a", "custom_mode_a"), Choice("유형 B", "b", "custom_mode_b")),
    ))
    driver = tab_for(repository.form_definition, "driver")
    driver["fields"].extend([custom, text_field("custom_removed", "삭제할 항목")])
    destination = tmp_path / "result.csv"
    exported_schema(repository, destination)
    definition = FormDefinition.from_dict(repository.form_definition.to_dict())
    driver = tab_for(definition, "driver")
    driver["fields"] = [record for record in driver["fields"] if record["key"] != "custom_removed"]
    modes = next(record for record in driver["fields"] if record["key"] == "custom_modes")
    modes["choices"].pop()
    # Old persisted order remains migration history, never live worksheet data.
    definition.csv_column_order.extend(["orphaned_column", "custom_removed", "custom_mode_b"])
    definition.validate()
    repository.form_definition = definition

    headers, _descriptions = exported_schema(repository, destination)
    assert {"orphaned_column", "custom_removed", "custom_mode_b"}.isdisjoint(headers)
    assert {"custom_modes", "custom_mode_a"}.issubset(headers)
    assert not list(tmp_path.glob("*.tmp"))


def test_specification_follows_form_persisted_in_project_after_restart(tmp_path):
    repository = CaseRepository(tmp_path)
    definition = repository.form_definition
    driver = tab_for(definition, "driver")
    driver["fields"].reverse()
    driver["fields"].insert(1, text_field("custom_saved", "저장된 추가 항목"))
    notes = tab_for(definition, "notes")
    definition.tabs.remove(notes)
    definition.tabs.insert(1, notes)
    definition.validate()
    expected = definition.csv_headers()
    assert expected[2:7] == [
        "remarks", "driver_age", "custom_saved", "driver_exp", "driver_sex",
    ]
    case = InvestigationCase(values={
        "institution": "samsung", "accident_classification": "2020011300001",
        "custom_saved": "조사 내용", "remarks": "참고 의견", "driver_exp": "12",
    })
    before = deepcopy(case.values)
    repository.save([case], form_definition=definition)
    reopened = CaseRepository(tmp_path)
    cases = reopened.load()
    destination = reopened.export_csv(tmp_path / "reopened.csv", cases, include_specification=True)
    headers = csv_headers(destination)
    rows = specification_rows(data_specification_path(destination))
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))

    assert [column for column, _description in rows] == headers
    assert rows[0] == (
        "report_accident_number", "보고서 사고번호(날짜_순번)(비식별화 목적)",
    )
    assert reopened.form_definition.csv_headers() == expected
    assert headers[8:] == expected
    assert "저장된 추가 항목" in dict(rows)["custom_saved"]
    assert records[0]["report_accident_number"] == "20200113_001"
    assert records[0]["accident_classification"] == "2020011300001"
    assert records[0]["custom_saved"] == "조사 내용"
    assert records[0]["remarks"] == "참고 의견"
    assert records[0]["driver_exp"] == "12"
    assert cases[0].values == before


def test_designer_reordering_survives_apply_restart_and_matches_csv_and_specification(
    qtbot, tmp_path,
):
    from PySide6.QtWidgets import QDialog

    from investigator.main_window import MainWindow
    from investigator.widgets.form_designer import FormDesigner

    project = tmp_path / "project"
    repository = CaseRepository(project)
    case = InvestigationCase(values={
        "institution": "samsung", "accident_classification": "2020011300001",
        "driver_sex": "여성", "driver_exp": "12", "driver_age": "35-39세",
        "remarks": "순서를 바꿔도 보존할 조사 내용",
    })
    before = deepcopy(case.values)
    repository.save([case])
    window = MainWindow(project)
    qtbot.addWidget(window)
    original_headers = window.repository.form_definition.csv_headers()
    dialog = FormDesigner(window.repository.form_definition, window)
    qtbot.addWidget(dialog)

    def select(tab_key: str, field_key: str | None = None) -> int:
        index = next(i for i, tab in enumerate(dialog.definition.tabs) if tab["key"] == tab_key)
        item = dialog.tree.topLevelItem(index)
        if field_key is not None:
            field_index = next(
                i for i, field in enumerate(dialog.definition.tabs[index]["fields"])
                if field["key"] == field_key
            )
            item = item.child(field_index)
        dialog.tree.setCurrentItem(item)
        return index

    # 실제 편집기의 이동 버튼으로 탭 묶음과 개별 항목의 순서를 모두 바꾼다.
    evidence_index = select("evidence")
    evidence_columns = [
        choice["csv_column"]
        for choice in tab_for(dialog.definition, "evidence")["fields"][0]["choices"]
    ]
    for _ in range(evidence_index - 1):
        assert dialog.move_up_button.isEnabled()
        dialog.move_up_button.click()
    select("driver", "driver_age")
    dialog.move_up_button.click()
    dialog.move_up_button.click()
    select("environment", "incident_date")
    dialog.move_down_button.click()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert window.repository.form_definition.csv_headers() == original_headers
    assert window.apply_form_definition(dialog.definition)
    window.close()

    # 프로젝트를 다시 연 뒤에도 동일한 순서와 원본 값으로 두 파일을 내보낸다.
    reopened = CaseRepository(project)
    cases = reopened.load()
    headers, descriptions = exported_schema(reopened, tmp_path / "designer.csv")
    destination = reopened.export_csv(tmp_path / "designer.csv", cases, include_specification=True)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))
    assert csv_headers(destination) == headers
    assert [column for column, _description in specification_rows(
        data_specification_path(destination)
    )] == headers
    assert headers[8:10] == ["Institution", "accident_classification"]
    assert headers[10:10 + len(evidence_columns)] == evidence_columns
    assert headers[10 + len(evidence_columns):13 + len(evidence_columns)] == [
        "driver_age", "driver_sex", "driver_exp",
    ]
    assert headers.index("incident_day") < headers.index("incident_date")
    assert headers[8:] != original_headers
    assert descriptions["report_accident_number"] == "보고서 사고번호(날짜_순번)(비식별화 목적)"
    assert list(records[0]) == headers
    assert records[0]["report_accident_number"] == "20200113_001"
    assert records[0]["accident_classification"] == before["accident_classification"]
    assert records[0]["driver_exp"] == before["driver_exp"]
    assert records[0]["remarks"] == before["remarks"]
    assert cases[0].values == before


def test_omitting_specification_does_not_read_template_or_run_builder(tmp_path, monkeypatch):
    def not_used(*_args, **_kwargs):
        raise AssertionError("The disabled specification must not be generated")

    monkeypatch.setattr(storage, "files", not_used)
    monkeypatch.setattr(storage, "build_specification", not_used)
    destination = tmp_path / "without.csv"
    previous_specification = data_specification_path(destination)
    previous_specification.write_bytes(b"previous workbook")

    CaseRepository(tmp_path).export_csv(destination, [], include_specification=False)

    assert previous_specification.read_bytes() == b"previous workbook"
    assert destination.exists()
    assert speed_csv_path(destination).exists()


@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_builder_failure_preserves_all_previous_exports_and_removes_temps(
    tmp_path, monkeypatch, failure_type,
):
    destination = tmp_path / "result.csv"
    outputs = {
        destination: b"previous investigation",
        speed_csv_path(destination): b"previous speed",
        data_specification_path(destination): b"previous workbook",
    }
    for path, content in outputs.items():
        path.write_bytes(content)

    def build_failure(*_args, **_kwargs):
        raise failure_type("Cannot build live specification")

    monkeypatch.setattr(storage, "build_specification", build_failure)
    expected_error = WorkspaceError if failure_type is OSError else ValueError
    with pytest.raises(expected_error):
        CaseRepository(tmp_path).export_csv(destination, [], include_specification=True)

    assert {path: path.read_bytes() for path in outputs} == outputs
    assert not list(tmp_path.glob("*.tmp"))
