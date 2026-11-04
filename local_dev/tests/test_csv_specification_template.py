"""Live CSV dictionary content and native workbook preservation contracts."""

from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from investigator.csv_specification import (
    DataSpecificationError,
    build_specification,
    csv_export_headers,
    specification_rows,
)
from investigator.form_definition import FormDefinition, field_record
from investigator.schema import Choice, FieldKind, FieldSpec

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SHEET = "xl/worksheets/sheet1.xml"


@pytest.fixture
def template() -> bytes:
    return files("investigator").joinpath("assets", "AVAT 데이터 명세서.xlsx").read_bytes()


def workbook_parts(content: bytes) -> dict[str, bytes]:
    with ZipFile(BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def replace_part(content: bytes, name: str, value: bytes | None) -> bytes:
    result = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(result, "w") as output:
        for info in original.infolist():
            if info.filename != name:
                output.writestr(info, original.read(info.filename))
            elif value is not None:
                output.writestr(info, value)
        output.comment = original.comment
    return result.getvalue()


def read_sheet(content: bytes) -> tuple[ET.Element, list[list[str]]]:
    parts = workbook_parts(content)
    strings = (
        ["".join(item.itertext()) for item in ET.fromstring(parts["xl/sharedStrings.xml"])]
        if "xl/sharedStrings.xml" in parts else []
    )
    sheet = ET.fromstring(parts[SHEET])
    rows = []
    for row in sheet.findall("m:sheetData/m:row", NS):
        values = []
        for cell in row.findall("m:c", NS):
            if cell.get("t") == "inlineStr":
                inline = cell.find("m:is", NS)
                values.append("".join(inline.itertext()) if inline is not None else "")
            else:
                value = cell.find("m:v", NS)
                text = value.text if value is not None and value.text is not None else ""
                values.append(strings[int(text)] if cell.get("t") == "s" else text)
        rows.append(values)
    return sheet, rows


def tab_for(definition: FormDefinition, key: str) -> dict:
    return next(tab for tab in definition.tabs if tab["key"] == key)


def add_text(definition: FormDefinition, key: str, label: str, column: str | None = None) -> dict:
    record = field_record(FieldSpec(key, label, FieldKind.TEXT, column or key))
    tab_for(definition, "driver")["fields"].append(record)
    return record


def test_default_dictionary_exactly_matches_csv_order_and_current_labels(template):
    definition = FormDefinition.default()
    _sheet, rows = read_sheet(build_specification(template, definition))
    assert rows[0] == ["기록항목(영문)", "소분류"]
    assert rows[1] == [
        "report_accident_number", "보고서 사고번호(날짜_순번)(비식별화 목적)",
    ]
    assert [row[0] for row in rows[1:]] == csv_export_headers(definition)
    assert len(rows) == 96
    labels = dict(rows[1:])
    assert labels["vehicle_manufacturer"] == "제작사"
    assert labels["remarks"] == "사고 추가정보"
    assert labels["data_list_Front_cam"] == "접근 가능 데이터 (자차전방블랙박스)"
    assert labels["report_accident_number"] == "보고서 사고번호(날짜_순번)(비식별화 목적)"


def test_preserves_other_native_zip_members_and_archive_comment(template):
    buffer = BytesIO()
    with ZipFile(BytesIO(template)) as original, ZipFile(buffer, "w") as source:
        for info in original.infolist():
            source.writestr(info, original.read(info.filename))
        source.comment = b"AVAT dictionary template"
    source_bytes = buffer.getvalue()
    generated = build_specification(source_bytes, FormDefinition.default())
    before, after = workbook_parts(source_bytes), workbook_parts(generated)
    assert before.keys() == after.keys()
    assert before[SHEET] != after[SHEET]
    assert {name: value for name, value in before.items() if name != SHEET} == {
        name: value for name, value in after.items() if name != SHEET
    }
    with ZipFile(BytesIO(generated)) as archive:
        assert archive.comment == b"AVAT dictionary template"


def test_preserves_header_column_widths_styles_and_native_namespace_bindings(template):
    old_sheet, old_rows = read_sheet(template)
    content = build_specification(template, FormDefinition.default())
    sheet, rows = read_sheet(content)
    assert ET.tostring(old_sheet.find("m:cols", NS)) == ET.tostring(sheet.find("m:cols", NS))
    old_header = old_sheet.find("m:sheetData/m:row", NS)
    new_header = sheet.find("m:sheetData/m:row", NS)
    assert ET.tostring(old_header) == ET.tostring(new_header)
    assert sheet.find("m:dimension", NS).get("ref") == f"A1:B{len(rows)}"
    assert sheet.find("m:sheetViews/m:sheetView", NS).get("topLeftCell") == "A1"
    assert sheet.find("m:sheetViews/m:sheetView/m:selection", NS).get("activeCell") == "A1"
    assert sheet.find("m:sheetViews/m:sheetView/m:selection", NS).get("sqref") == "A1"
    for prefix in ("mc", "x14ac", "xr", "xr2", "xr3"):
        assert f"xmlns:{prefix}=".encode() in workbook_parts(content)[SHEET]
    for row in sheet.findall("m:sheetData/m:row", NS):
        assert len(row.findall("m:c", NS)) == 2
    # 행이 이동해도 같은 칼럼의 셀 서식은 그대로 유지되어야 한다.
    original_rows = dict(zip(
        (values[0] for values in old_rows[1:]), old_sheet.find("m:sheetData", NS)[1:], strict=True,
    ))
    generated_rows = dict(zip(
        (values[0] for values in rows[1:]), sheet.find("m:sheetData", NS)[1:], strict=True,
    ))
    for column, original_row in original_rows.items():
        assert [cell.get("s") for cell in generated_rows[column].findall("m:c", NS)] == [
            cell.get("s") for cell in original_row.findall("m:c", NS)
        ]


def test_reordered_tabs_fields_and_choices_move_dictionary_rows(template):
    definition = FormDefinition.default()
    evidence = tab_for(definition, "evidence")
    evidence["fields"][0]["choices"].reverse()
    definition.tabs.remove(evidence)
    definition.tabs.insert(1, evidence)
    tab_for(definition, "driver")["fields"].reverse()
    environment = tab_for(definition, "environment")["fields"]
    date = next(record for record in environment if record["key"] == "incident_date")
    day = next(record for record in environment if record["key"] == "incident_day")
    environment.remove(day)
    environment.insert(environment.index(date), day)
    definition.validate()
    _sheet, rows = read_sheet(build_specification(template, definition))
    columns = [row[0] for row in rows[1:]]
    assert columns == csv_export_headers(definition)
    assert columns[0] == "report_accident_number"
    assert columns[10:13] == ["data_list_Report", "data_list_CCTV", "data_list_Pedal_cam"]
    assert columns.index("driver_age") < columns.index("driver_sex")
    assert columns.index("incident_day") < columns.index("incident_date")


def test_current_names_and_labels_only_no_historical_aliases_or_deleted_fields(template):
    definition = FormDefinition.default()
    note = next(record for record in definition.records() if record["key"] == "remarks")
    note.update(label="새 비고 이름", csv_column="current_notes", aliases=["remarks", "old_notes"])
    pedestrian = next(record for record in definition.records() if record["key"] == "obstacle_pedestrians")
    adult = pedestrian["choices"][1]
    adult.update(label="새 어른 명칭", csv_column="current_adult", csv_aliases=["obstacle_adult", "old_adult"])
    deleted = add_text(definition, "custom_deleted", "삭제한 항목")
    tab_for(definition, "driver")["fields"].remove(deleted)
    definition.csv_column_order = ["custom_deleted", "old_notes", "old_adult"]
    definition.validate()
    _sheet, rows = read_sheet(build_specification(template, definition))
    actual = dict(rows[1:])
    assert actual["current_notes"] == "새 비고 이름"
    assert actual["current_adult"] == "장애물(보행자) (새 어른 명칭)"
    assert not {"remarks", "old_notes", "obstacle_adult", "old_adult", "custom_deleted"} & actual.keys()


def test_custom_aggregate_and_individual_choice_columns_all_documented_in_order(template):
    definition = FormDefinition.default()
    custom = field_record(FieldSpec(
        "custom_types", "추가 유형", FieldKind.MULTI, "custom_types",
        (Choice("알파", "a", "custom_alpha"), Choice("베타", "b", "custom_beta")),
    ))
    definition.tabs.insert(1, {
        "key": "custom_tab", "title": "새 탭", "short_title": "추가", "columns": 1,
        "fields": [custom],
    })
    definition.validate()
    _sheet, rows = read_sheet(build_specification(template, definition))
    assert rows[11:14] == [
        ["custom_types", "추가 유형"],
        ["custom_alpha", "추가 유형 (알파)"],
        ["custom_beta", "추가 유형 (베타)"],
    ]
    assert [row[0] for row in rows[1:]] == csv_export_headers(definition)


def test_comma_column_name_is_one_column_and_entities_and_formula_like_labels_are_literal(template):
    definition = FormDefinition.default()
    label = '=IF(A1="유", "<성인>&청소년", "")'
    column = "custom_adult, youth & <type>"
    add_text(definition, "custom_entities", label, column)
    definition.validate()
    sheet, rows = read_sheet(build_specification(template, definition))
    assert [column, label] in rows
    assert [row[0] for row in rows[1:]].count(column) == 1
    assert sheet.findall(".//m:f", NS) == []
    actual_row = next(row for row in sheet.find("m:sheetData", NS) if any(
        cell.find("m:is/m:t", NS) is not None and cell.find("m:is/m:t", NS).text == column
        for cell in row
    ))
    assert all(cell.get("t") == "inlineStr" for cell in actual_row)


def test_growth_then_shrink_rebuilds_used_range_without_stale_rows(template):
    definition = FormDefinition.default()
    for index in range(30):
        add_text(definition, f"custom_{index}", f"새 항목 {index}")
    definition.validate()
    expanded = build_specification(template, definition)
    sheet, rows = read_sheet(expanded)
    assert len(rows) == 126
    assert sheet.find("m:dimension", NS).get("ref") == "A1:B126"
    assert rows[1] == [
        "report_accident_number", "보고서 사고번호(날짜_순번)(비식별화 목적)",
    ]
    shrunk = build_specification(expanded, FormDefinition.default())
    sheet, rows = read_sheet(shrunk)
    assert len(rows) == 96
    assert sheet.find("m:dimension", NS).get("ref") == "A1:B96"
    assert not any(row[0].startswith("custom_") for row in rows)
    assert [row.get("r") for row in sheet.find("m:sheetData", NS)] == [str(i) for i in range(1, 97)]


def test_multiline_and_long_labels_expand_row_height_without_mutating_definition(template):
    definition = FormDefinition.default()
    add_text(definition, "custom_long", "첫 번째 줄\n두 번째 줄\n" + "긴 항목 이름 " * 10)
    definition.validate()
    before = deepcopy(definition.to_dict())
    sheet, rows = read_sheet(build_specification(template, definition))
    index = next(i for i, row in enumerate(rows) if row[0] == "custom_long")
    actual_row = sheet.find("m:sheetData", NS)[index]
    assert float(actual_row.get("ht")) >= 51
    assert actual_row.get("customHeight") == "1"
    assert definition.to_dict() == before


def test_literal_tab_crlf_and_supplementary_unicode_survive_inline_strings(template):
    definition = FormDefinition.default()
    label = "줄\t구분\r\n다음 줄 🚗"
    add_text(definition, "custom_whitespace", label)
    _sheet, rows = read_sheet(build_specification(template, definition))
    assert ["custom_whitespace", label] in rows


def test_overlong_excel_cell_label_raises_data_specification_error(template):
    definition = FormDefinition.default()
    add_text(definition, "custom_too_long", "x" * 32_768)
    with pytest.raises(DataSpecificationError):
        build_specification(template, definition)


@pytest.mark.parametrize("content", [b"not a workbook", b"PK\x03\x04broken archive", b""])
def test_invalid_zip_is_an_oserror_class_data_specification_error(content):
    with pytest.raises(DataSpecificationError) as caught:
        build_specification(content, FormDefinition.default())
    assert isinstance(caught.value, OSError)


@pytest.mark.parametrize("member", [SHEET, "xl/sharedStrings.xml"])
def test_invalid_xml_is_wrapped_in_data_specification_error(template, member):
    broken = replace_part(template, member, b"<broken XML")
    with pytest.raises(DataSpecificationError):
        build_specification(broken, FormDefinition.default())


def test_missing_dictionary_sheet_is_wrapped_in_data_specification_error(template):
    broken = replace_part(template, SHEET, None)
    with pytest.raises(DataSpecificationError):
        build_specification(broken, FormDefinition.default())


@pytest.mark.parametrize("control", ["\x00", "\x08", "\x0b", "\x1f", "\ud800", "\uffff"])
def test_unsupported_xml_text_controls_fail_cleanly(template, control):
    definition = FormDefinition.default()
    add_text(definition, "custom_invalid_label", f"추가{control}항목")
    with pytest.raises(DataSpecificationError):
        build_specification(template, definition)


def test_csv_header_order_mismatch_fails_instead_of_exporting_wrong_dictionary(template):
    definition = FormDefinition.default()
    headers = csv_export_headers(definition)
    headers[10], headers[11] = headers[11], headers[10]
    with pytest.raises(DataSpecificationError, match="순서"):
        build_specification(template, definition, headers=headers)
    with pytest.raises(DataSpecificationError, match="순서"):
        specification_rows(definition, headers=headers[:-1])


def test_supplied_correct_headers_are_accepted_without_mutation(template):
    definition = FormDefinition.default()
    headers = csv_export_headers(definition)
    before = headers.copy()
    _sheet, rows = read_sheet(build_specification(template, definition, headers=headers))
    assert [row[0] for row in rows[1:]] == headers == before
