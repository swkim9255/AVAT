from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.report_export import ReportExportError, export_case_reports
from investigator.schema import FieldKind
from tests.helpers import valid_values

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "investigator" / "assets" / "사고분석보고서.docx"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
NS = {"w": W, "w14": W14}
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def case(number: str = "2026102000001", **updates) -> InvestigationCase:
    values = valid_values()
    values.update(
        institution="samsung",
        accident_classification=number,
        incident_date="20261020",
        incident_time="0930",
        vehicle_fuel="전기",
        vehicle_mileage="9876543",
        incident_damage_unknown="확인 가능",
        site_manual_input="서울 시험도로",
        remarks="추가 사고 정보",
    )
    values.update(updates)
    return InvestigationCase(values=values)


def document_root(path: Path) -> ET.Element:
    with ZipFile(path) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def visible_text(root: ET.Element) -> str:
    return "".join(element.text or "" for element in root.findall(".//w:t", NS))


def paragraphs(root: ET.Element) -> list[str]:
    return [visible_text(paragraph) for paragraph in root.findall(".//w:p", NS)]


def checkbox_states(root: ET.Element) -> dict[str, tuple[bool, str]]:
    states = {}
    for control in root.findall(".//w:sdt", NS):
        checkbox = control.find("w:sdtPr/w14:checkbox", NS)
        if checkbox is None:
            continue
        tag = control.find("w:sdtPr/w:tag", NS)
        checked = checkbox.find("w14:checked", NS)
        assert tag is not None and checked is not None
        states[tag.get(f"{{{W}}}val")] = (
            checked.get(f"{{{W14}}}val") in {"1", "true", "on"},
            visible_text(control.find("w:sdtContent", NS)),
        )
    return states


def template_with_parts(tmp_path: Path, **parts: str) -> Path:
    """Use the genuine package and replace only specified test XML parts."""
    destination = tmp_path / "custom-template.docx"
    with ZipFile(TEMPLATE) as original, ZipFile(destination, "w", ZIP_DEFLATED) as output:
        for item in original.infolist():
            output.writestr(item, parts.get(item.filename, original.read(item.filename)))
    return destination


def document_xml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:w14="{W14}">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )


def field_record(definition: FormDefinition, key: str) -> dict:
    return next(record for record in definition.records() if record["key"] == key)


def test_default_report_resolves_every_genuine_template_placeholder_and_preserves_package(tmp_path):
    original_hash = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    source = case(incident_minor_injury_grades="14, 12, 12", incident_fatality="0")
    snapshot = deepcopy(source.to_dict())
    paths = export_case_reports([source], tmp_path / "reports")

    assert len(paths) == 1 and isinstance(paths[0], Path)
    assert paths[0].is_file() and paths[0].suffix == ".docx"
    assert "20261020_001" in paths[0].stem
    assert paths[0].parent == tmp_path / "reports"
    assert source.to_dict() == snapshot
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == original_hash
    root = document_root(paths[0])
    text = visible_text(root)
    assert not PLACEHOLDER.findall(text)
    for value in (
        "samsung", "20261020_001", "2026-10-20", "화", "09:30",
        "전기", "9876543", "서울 시험도로", "추가 사고 정보", "12, 12, 14",
    ):
        assert value in text
    assert len(checkbox_states(root)) == 85
    with ZipFile(TEMPLATE) as original, ZipFile(paths[0]) as generated:
        assert generated.namelist() == original.namelist()
        for name in original.namelist():
            if name != "word/document.xml":
                assert generated.read(name) == original.read(name), name


def test_all_checkbox_variables_can_be_selected_and_cached_glyphs_follow_state(tmp_path):
    definition = FormDefinition.default()
    values = valid_values()
    values.update(institution="samsung", accident_classification="2026102000002", obstacle_exists="유")
    for record in definition.records():
        if record["kind"] == FieldKind.MULTI.value:
            values[record["key"]] = [choice["value"] for choice in record["choices"]]
    source = InvestigationCase(values=values)
    output = export_case_reports([source], tmp_path)[0]
    states = checkbox_states(document_root(output))
    assert len(states) == 85
    assert all(checked and glyph == "☑" for checked, glyph in states.values()), states


def test_absent_checkbox_values_are_unchecked_and_do_not_inherit_template_state(tmp_path):
    # Use a previously filled report as another template to exercise clearing.
    definition = FormDefinition.default()
    source = case()
    for record in definition.records():
        if record["kind"] == FieldKind.MULTI.value:
            source.values[record["key"]] = [choice["value"] for choice in record["choices"]]
    filled = export_case_reports([source], tmp_path / "filled")[0]
    blank = InvestigationCase(values={"institution": "samsung", "accident_classification": "2"})
    output = export_case_reports([blank], tmp_path / "blank", template_path=filled)[0]
    states = checkbox_states(document_root(output))
    assert len(states) == 85
    assert all(not checked and glyph == "☐" for checked, glyph in states.values())


def test_checkbox_mapping_uses_individual_columns_and_option_membership(tmp_path):
    source = case(
        obstacle_exists="유",
        site_types=["교차로", "횡단보도"],
        site_special_areas=["주유소"],
        available_data=["EDR", "타차후방블랙박스"],
        obstacle_pedestrians=["어른"],
        obstacle_vehicle_types=["승용", "화물"],
        collision_targets=["vehicle.passenger", "pedestrian.adult", "facility.guardrail"],
        collision_directions=["전면", "후면"],
    )
    output = export_case_reports([source], tmp_path)[0]
    states = checkbox_states(document_root(output))
    selected = {tag for tag, (checked, _glyph) in states.items() if checked}
    expected = {
        "site_intersection", "site_crosswalk", "site_special_area|주유소",
        "data_list_EDR", "data_list_Other_Rear_cam", "obstacle_adult",
        "obstacle_vehicle|승용", "obstacle_vehicle|화물",
        "collision_target|승용", "collision_target|어른", "collision_target|가드레일",
        "incident_collision_direction_front", "incident_collision_direction_rear",
    }
    assert expected <= selected
    for tag in ("site_tunnel", "data_list_CCTV", "obstacle_child", "collision_target|화물"):
        assert states[tag] == (False, "☐")
    assert all(glyph == ("☑" if checked else "☐") for checked, glyph in states.values())


def test_split_run_replacements_keep_surrounding_text_and_run_formatting(tmp_path):
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>앞 {{vehi</w:t></w:r>'
        '<w:r><w:rPr><w:i/></w:rPr><w:t>cle_model_name}} 뒤</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>{{vehicle_model_name}} / {{vehicle_model_name}}</w:t></w:r></w:p>'
    )})
    output = export_case_reports([case(vehicle_model_name="안전 시험차")], tmp_path / "out", template_path=template)[0]
    root = document_root(output)
    assert paragraphs(root) == ["앞 안전 시험차 뒤", "안전 시험차 / 안전 시험차"]
    runs = root.findall("w:body/w:p/w:r", NS)
    assert runs[0].find("w:rPr/w:b", NS) is not None
    assert runs[1].find("w:rPr/w:i", NS) is not None
    assert visible_text(runs[1]).endswith(" 뒤")


def test_xml_characters_linebreaks_and_placeholder_like_input_are_literal(tmp_path):
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{remarks}}</w:t></w:r></w:p>'
    )})
    note = '첫 줄 & &lt; <차량> "표기"\n둘째 줄\n{{driver_sex}}는 입력한 원문'
    output = export_case_reports([case(remarks=note)], tmp_path / "out", template_path=template)[0]
    root = document_root(output)
    assert visible_text(root) == note.replace("\n", "")
    assert len(root.findall(".//w:br", NS)) == 2
    assert "{{driver_sex}}" in visible_text(root)


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_missing_values_show_dash_but_numeric_zero_is_preserved(tmp_path, missing):
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{vehicle_model_name}}|{{incident_fatality}}|{{collision_dist_value}}</w:t></w:r></w:p>'
    )})
    source = case(vehicle_model_name=missing, incident_fatality="0", collision_dist_value=0)
    output = export_case_reports([source], tmp_path / "out", template_path=template)[0]
    assert visible_text(document_root(output)) == "-|0|0"


def test_literal_null_text_is_not_confused_with_missing_value(tmp_path):
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{remarks}}</w:t></w:r></w:p>'
    )})
    output = export_case_reports([case(remarks="null")], tmp_path / "out", template_path=template)[0]
    assert visible_text(document_root(output)) == "null"


def test_multiline_template_with_alternate_word_namespace_prefix_stays_valid(tmp_path):
    xml = (
        f'<x:document xmlns:x="{W}"><x:body>'
        '<x:p><x:r><x:t>{{remarks}}</x:t></x:r></x:p>'
        '<x:sectPr/></x:body></x:document>'
    )
    template = template_with_parts(tmp_path, **{"word/document.xml": xml})
    output = export_case_reports([case(remarks="a\nb\tc")], tmp_path / "out", template_path=template)[0]
    root = document_root(output)
    assert visible_text(root) == "abc"
    assert len(root.findall(".//w:br", NS)) == 1
    assert len(root.findall(".//w:tab", NS)) == 1


@pytest.mark.parametrize("checked_prefix", ["q14:", ""])
def test_checkbox_with_nonstandard_namespace_prefix_keeps_valid_checked_attribute(tmp_path, checked_prefix):
    xml = (
        f'<x:document xmlns:x="{W}" xmlns:q14="{W14}"><x:body><x:p>'
        '<x:sdt><x:sdtPr><x:tag x:val="data_list_EDR"/>'
        f'<q14:checkbox><{checked_prefix}checked xmlns="{W14}" q14:val="0"/>'
        '</q14:checkbox></x:sdtPr><x:sdtContent><x:r><x:t>☐</x:t></x:r>'
        '</x:sdtContent></x:sdt></x:p><x:sectPr/></x:body></x:document>'
    )
    template = template_with_parts(tmp_path, **{"word/document.xml": xml})
    output = export_case_reports([case(available_data=["EDR"])], tmp_path / "out", template_path=template)[0]
    assert checkbox_states(document_root(output)) == {"data_list_EDR": (True, "☑")}


def test_long_remarks_keep_the_existing_heading_with_its_table(tmp_path):
    note = "\n".join(f"긴 비고 {number}" for number in range(50))
    output = export_case_reports([case(remarks=note)], tmp_path)[0]
    body = document_root(output).find("w:body", NS)
    children = list(body)
    remarks_table = next(child for child in children if child.tag == f"{{{W}}}tbl" and "긴 비고 49" in visible_text(child))
    heading = children[children.index(remarks_table) - 1]
    assert heading.tag == f"{{{W}}}p"
    assert visible_text(heading).strip()
    assert heading.find("w:pPr/w:keepNext", NS) is not None


def test_aliases_and_derived_weekday_use_current_case_values(tmp_path):
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{institution}}|{{Institution}}|{{incident_date}}|{{incident_day}}|{{incident_hour}}|{{vehicle_fuel}}|{{vehicle_mileage}}|{{vehicle_model_fuel}}|{{vehicle_milege}}</w:t></w:r></w:p>'
    )})
    source = case(institution="SAMSUNG", incident_date="20260201", incident_time="0005")
    # A stale stored weekday must never leak into a report.
    source.values["incident_day"] = "월"
    snapshot = deepcopy(source.to_dict())
    output = export_case_reports([source], tmp_path / "out", template_path=template)[0]
    assert visible_text(document_root(output)) == "samsung|samsung|2026-02-01|일|00:05|전기|9876543|전기|9876543"
    assert source.to_dict() == snapshot


def test_header_and_footer_template_variables_are_filled_without_dropping_other_parts(tmp_path):
    footer = f'<w:ftr xmlns:w="{W}"><w:p><w:r><w:t>사고 {{' + '{accident_classification}}' + '</w:t></w:r></w:p></w:ftr>'
    template = template_with_parts(tmp_path, **{"word/footer1.xml": footer})
    output = export_case_reports([case()], tmp_path / "out", template_path=template)[0]
    with ZipFile(output) as archive:
        assert visible_text(ET.fromstring(archive.read("word/footer1.xml"))) == "사고 20261020_001"


def test_batch_creates_only_selected_cases_and_never_overwrites_existing_documents(tmp_path):
    first, second, omitted = case("2026102000011"), case("2026102000012"), case("2026102000013")
    first_generation = export_case_reports([second, first], tmp_path)
    originals = {path: path.read_bytes() for path in first_generation}
    second_generation = export_case_reports([second, first], tmp_path)
    assert len(set(first_generation + second_generation)) == 4
    assert "20261020_002" in first_generation[0].name
    assert "20261020_001" in first_generation[1].name
    assert all(omitted.values["accident_classification"] not in p.name for p in tmp_path.glob("*.docx"))
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert all("20261020_002" in visible_text(document_root(p)) for p in (first_generation[0], second_generation[0]))


def test_duplicate_identifiers_get_distinct_safe_filenames(tmp_path):
    sources = [case("2026102000001"), case("2026102000001")]
    outputs = export_case_reports(sources, tmp_path / "out")
    assert len(outputs) == len(set(outputs)) == 2
    assert all(output.parent == tmp_path / "out" for output in outputs)
    assert all(output.is_file() for output in outputs)


def test_repeated_selection_of_same_accident_is_exported_once(tmp_path):
    source = case()
    output = export_case_reports([source, source], tmp_path)
    assert len(output) == 1
    assert len(list(tmp_path.glob("*.docx"))) == 1


def test_case_identifier_cannot_escape_destination_directory(tmp_path):
    source = case("../../wrong:target?*", institution="../samsung/..")
    destination = tmp_path / "reports"
    output = export_case_reports([source], destination)[0]
    assert output.parent == destination
    assert output.is_file()
    assert not any(character in output.name for character in '<>:"/\\|?*')


def test_customized_fields_choices_and_csv_names_remain_linked_by_stable_identity(tmp_path):
    definition = FormDefinition.default()
    manufacturer = field_record(definition, "vehicle_manufacturer")
    manufacturer.update(label="변경된 제작사", csv_column="custom_maker", aliases=["previous_maker"])
    adult = field_record(definition, "obstacle_pedestrians")["choices"][1]
    adult.update(label="성인 보행자", value="성인", csv_column="custom_adult", value_aliases=["어른"], csv_aliases=["previous_adult"])
    collision = field_record(definition, "collision_targets")
    collision.update(csv_column="custom_targets", aliases=["previous_targets"])
    collision_adult = next(choice for choice in collision["choices"] if choice["id"] == "pedestrian.adult")
    collision_adult.update(label="보행자 · 성인 보행자", value="성인대상", value_aliases=["pedestrian.adult"])
    definition.validate()
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{vehicle_manufacturer}}|{{custom_maker}}|{{previous_maker}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>{{custom_adult}}|{{previous_adult}}</w:t></w:r></w:p>'
        # Preserve the genuine checkbox controls so the old report bindings are tested too.
        + "".join(ET.tostring(control, encoding="unicode") for control in document_root(TEMPLATE).findall(".//w:sdt", NS))
    )})
    source = case(obstacle_exists="유", obstacle_pedestrians=["성인"], collision_targets=["성인대상"])
    snapshot = deepcopy(source.to_dict())
    output = export_case_reports([source], tmp_path / "out", form_definition=definition, template_path=template)[0]
    root = document_root(output)
    assert paragraphs(root)[0] == "kia|kia|kia"
    assert paragraphs(root)[1] == "1|1"
    states = checkbox_states(root)
    assert states["obstacle_adult"] == (True, "☑")
    assert states["collision_target|어른"] == (True, "☑")
    assert states["obstacle_child"] == (False, "☐")
    assert source.to_dict() == snapshot


def test_custom_template_can_use_added_field_key_or_csv_name_and_case_form_definition(tmp_path):
    from investigator.form_definition import new_field

    definition = FormDefinition.default()
    additional = new_field()
    additional.update(key="custom_report_detail", label="보고서 추가 정보", csv_column="report_detail")
    definition.tabs[-1]["fields"].append(additional)
    definition.validate()
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{custom_report_detail}}|{{report_detail}}</w:t></w:r></w:p>'
    )})
    source = case(custom_report_detail="자유 입력 & 한글")
    source.form_definition = definition
    output = export_case_reports([source], tmp_path / "out", template_path=template)[0]
    assert visible_text(document_root(output)) == "자유 입력 & 한글|자유 입력 & 한글"


def test_custom_csv_alias_cannot_replace_existing_model_identity_bindings(tmp_path):
    from investigator.form_definition import new_field

    definition = FormDefinition.default()
    for key, column in (
        ("custom_fuel_note", "vehicle_fuel"),
        ("custom_institution_note", "institution"),
    ):
        extra = new_field()
        extra.update(key=key, label=f"사용자 항목 {key}", csv_column=column)
        definition.tabs[-1]["fields"].append(extra)
    # These columns are legal: the corresponding built-in CSV names are
    # vehicle_model_fuel and Institution, unlike the stable model identities.
    definition.validate()
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{vehicle_fuel}}|{{vehicle_model_fuel}}|{{institution}}|{{Institution}}|{{custom_fuel_note}}|{{custom_institution_note}}</w:t></w:r></w:p>'
    )})
    source = case(custom_fuel_note="사용자 연료 메모", custom_institution_note="사용자 기관 메모")
    output = export_case_reports([source], tmp_path / "out", form_definition=definition, template_path=template)[0]
    assert visible_text(document_root(output)) == "전기|전기|samsung|samsung|사용자 연료 메모|사용자 기관 메모"


def test_swapped_collision_choice_labels_do_not_swap_retained_template_checkboxes(tmp_path):
    definition = FormDefinition.default()
    targets = field_record(definition, "collision_targets")["choices"]
    passenger = next(choice for choice in targets if choice["id"] == "vehicle.passenger")
    van = next(choice for choice in targets if choice["id"] == "vehicle.van")
    passenger["label"], van["label"] = van["label"], passenger["label"]
    definition.validate()
    source = case(collision_targets=["vehicle.passenger"])
    output = export_case_reports([source], tmp_path, form_definition=definition)[0]
    states = checkbox_states(document_root(output))
    assert states["collision_target|승용"] == (True, "☑")
    assert states["collision_target|승합"] == (False, "☐")


def test_report_recomputes_injury_counts_and_representative_grades_on_a_copy(tmp_path):
    template = template_with_parts(tmp_path, **{"word/document.xml": document_xml(
        '<w:p><w:r><w:t>{{incident_minor_injury_grades}}|{{incident_minor_injury}}|{{incident_minor_representative_injury_grade}}|{{incident_severe_injury_grades}}|{{incident_severe_injury}}|{{incident_severe_representative_injury_grade}}</w:t></w:r></w:p>'
    )})
    source = case()
    source.values.update(
        incident_minor_injury_grades="14, 12, 12",
        incident_minor_injury="99",
        incident_minor_representative_injury_grade="14",
        incident_severe_injury_grades="11, 3, 5",
        incident_severe_injury="99",
        incident_severe_representative_injury_grade="11",
    )
    snapshot = deepcopy(source.to_dict())
    output = export_case_reports([source], tmp_path / "out", template_path=template)[0]
    assert visible_text(document_root(output)) == "12, 12, 14|3|12|3, 5, 11|3|3"
    assert source.to_dict() == snapshot


@pytest.mark.parametrize("bad_xml", [
    document_xml('<w:p><w:r><w:t>{{unknown_report_column}}</w:t></w:r></w:p>'),
    document_xml('<w:p><w:sdt><w:sdtPr><w:tag w:val="unknown_checkbox"/><w14:checkbox><w14:checked w14:val="0"/></w14:checkbox></w:sdtPr><w:sdtContent><w:r><w:t>☐</w:t></w:r></w:sdtContent></w:sdt></w:p>'),
    "<not-closed",
])
def test_invalid_template_bindings_or_xml_raise_clear_export_error_without_partial_output(tmp_path, bad_xml):
    template = template_with_parts(tmp_path, **{"word/document.xml": bad_xml})
    destination = tmp_path / "out"
    with pytest.raises(ReportExportError):
        export_case_reports([case()], destination, template_path=template)
    assert not list(destination.glob("*.docx"))


def test_missing_template_raises_export_error(tmp_path):
    with pytest.raises(ReportExportError):
        export_case_reports([case()], tmp_path / "out", template_path=tmp_path / "missing.docx")


def test_corrupt_zip_template_raises_export_error(tmp_path):
    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"This is not a ZIP document")
    with pytest.raises(ReportExportError):
        export_case_reports([case()], tmp_path / "out", template_path=corrupt)


def test_failed_batch_removes_its_own_exports_without_touching_previous_reports(tmp_path):
    previous = export_case_reports([case("2026102000999")], tmp_path / "reports")[0]
    previous_bytes = previous.read_bytes()
    invalid = case("2026102000002", remarks="Invalid XML control \x00 character")
    with pytest.raises(ReportExportError):
        export_case_reports([case("2026102000001"), invalid], tmp_path / "reports")
    assert list((tmp_path / "reports").glob("*.docx")) == [previous]
    assert previous.read_bytes() == previous_bytes


def test_empty_selection_does_not_create_output_files(tmp_path):
    destination = tmp_path / "out"
    with pytest.raises(ReportExportError):
        export_case_reports([], destination)
    assert not destination.exists() or not list(destination.iterdir())
