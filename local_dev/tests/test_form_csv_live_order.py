"""CSV fields follow the live form without resurrecting deleted custom fields."""

from __future__ import annotations

import csv
from copy import deepcopy
from pathlib import Path

from investigator.form_definition import FormDefinition, field_record
from investigator.models import InvestigationCase
from investigator.schema import Choice, FieldKind, FieldSpec, csv_headers
from investigator.storage import CaseRepository


def tab_for(definition: FormDefinition, key: str) -> dict:
    return next(tab for tab in definition.tabs if tab["key"] == key)


def text_field(key: str, column: str | None = None) -> dict:
    return field_record(FieldSpec(key, key, FieldKind.TEXT, column or key))


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames or [], list(reader)


def test_default_csv_schema_remains_compatible_and_has_95_export_columns(tmp_path):
    definition = FormDefinition.default()
    assert set(definition.csv_headers()) == set(csv_headers())
    assert definition.csv_headers().index("incident_date") < definition.csv_headers().index(
        "incident_day"
    )
    repository = CaseRepository(tmp_path)
    headers, _rows = read_csv(repository.export_csv(tmp_path / "default.csv", []))
    assert len(headers) == 95
    assert len(headers) == len(set(headers))
    assert headers[7:-1] == definition.csv_headers()
    assert headers[-1] == "report_accident_number"


def test_date_and_day_columns_follow_user_field_order_without_legacy_exception():
    definition = FormDefinition.default()
    environment = tab_for(definition, "environment")
    records = environment["fields"]
    date_index = next(i for i, record in enumerate(records) if record["key"] == "incident_date")
    day_index = next(i for i, record in enumerate(records) if record["key"] == "incident_day")
    assert date_index < day_index
    headers = definition.csv_headers()
    assert headers.index("incident_date") < headers.index("incident_day")

    records[date_index], records[day_index] = records[day_index], records[date_index]
    definition.validate()
    headers = definition.csv_headers()
    assert headers.index("incident_day") < headers.index("incident_date")
    restored = FormDefinition.from_dict(definition.to_dict())
    assert restored.csv_headers() == headers


def test_csv_emits_only_current_field_and_choice_names_not_historical_aliases():
    definition = FormDefinition.default()
    note = next(record for record in definition.records() if record["key"] == "remarks")
    note["aliases"] = ["remarks", "old_remarks"]
    note["csv_column"] = "current_remarks"
    pedestrians = next(
        record for record in definition.records() if record["key"] == "obstacle_pedestrians"
    )
    adult = next(choice for choice in pedestrians["choices"] if choice["value"] == "어른")
    adult["csv_aliases"] = ["obstacle_adult", "old_adult"]
    adult["csv_column"] = "current_adult"
    # Persisted legacy names are migration clues, not extra output columns.
    definition.csv_column_order = ["remarks", "old_remarks", "obstacle_adult", "old_adult"]
    definition.validate()
    values = {"remarks": "이전 키에 저장된 비고", "obstacle_exists": "유", "obstacle_pedestrians": ["어른"]}
    before = deepcopy(values)

    headers = definition.csv_headers()
    assert {"current_remarks", "current_adult"} <= set(headers)
    assert not {"remarks", "old_remarks", "obstacle_adult", "old_adult"}.intersection(headers)
    row = definition.csv_row(values)
    assert row["current_remarks"] == "이전 키에 저장된 비고"
    assert row["current_adult"] == "1"
    assert list(row) == headers
    assert values == before


def test_new_field_in_middle_of_tab_is_exported_next_to_neighbor_fields():
    definition = FormDefinition.default()
    driver = tab_for(definition, "driver")
    driver["fields"].insert(1, text_field("custom_driver_note"))
    definition.validate()

    headers = definition.csv_headers()
    start = headers.index("driver_sex")
    assert headers[start : start + 4] == [
        "driver_sex", "custom_driver_note", "driver_exp", "driver_age",
    ]
    assert definition.csv_row({"custom_driver_note": "조사 메모"})[
        "custom_driver_note"
    ] == "조사 메모"


def test_reordering_existing_fields_changes_csv_order_without_changing_values():
    definition = FormDefinition.default()
    driver = tab_for(definition, "driver")
    driver["fields"] = [driver["fields"][2], *driver["fields"][:2]]
    definition.validate()
    values = {"driver_sex": "여성", "driver_exp": "10", "driver_age": "35-39세"}
    before = deepcopy(values)

    headers = definition.csv_headers()
    start = headers.index("driver_age")
    assert headers[start : start + 3] == ["driver_age", "driver_sex", "driver_exp"]
    row = definition.csv_row(values)
    assert list(row) == headers
    assert [row[key] for key in headers[start : start + 3]] == ["35-39세", "여성", "10"]
    assert values == before


def test_tab_reordering_moves_all_individual_choice_columns_as_a_group():
    definition = FormDefinition.default()
    evidence = tab_for(definition, "evidence")
    evidence_columns = [choice["csv_column"] for choice in evidence["fields"][0]["choices"]]
    definition.tabs.remove(evidence)
    definition.tabs.insert(1, evidence)
    definition.validate()

    headers = definition.csv_headers()
    assert headers[2 : 2 + len(evidence_columns)] == evidence_columns
    assert headers[2 + len(evidence_columns)] == "driver_sex"
    assert len(headers) == len(set(headers)) == 87


def test_custom_field_moves_across_tabs_at_its_current_position():
    definition = FormDefinition.default()
    custom = text_field("custom_shared_note")
    driver = tab_for(definition, "driver")
    vehicle = tab_for(definition, "vehicle")
    driver["fields"].append(custom)
    # Save the old output order to simulate an already saved workspace.
    definition = FormDefinition.from_dict(definition.to_dict())
    driver = tab_for(definition, "driver")
    vehicle = tab_for(definition, "vehicle")
    custom = next(record for record in driver["fields"] if record["key"] == custom["key"])
    driver["fields"].remove(custom)
    vehicle["fields"].insert(1, custom)
    definition.validate()

    headers = definition.csv_headers()
    start = headers.index("vehicle_manufacturer")
    assert headers[start : start + 3] == [
        "vehicle_manufacturer", "custom_shared_note", "vehicle_model_fuel",
    ]


def test_deleted_custom_field_removes_current_alias_and_choice_columns():
    definition = FormDefinition.default()
    custom = field_record(FieldSpec(
        "custom_observation", "추가 관찰", FieldKind.MULTI, "custom_observations",
        (Choice("유형 A", "a", "custom_type_a"), Choice("유형 B", "b", "custom_type_b")),
    ))
    custom["aliases"] = ["custom_observations_old"]
    custom["choices"][0]["csv_aliases"] = ["custom_type_a_old"]
    tab_for(definition, "driver")["fields"].append(custom)
    definition = FormDefinition.from_dict(definition.to_dict())
    values = {"custom_observation": ["a"], "driver_sex": "남성"}
    original_values = deepcopy(values)
    before_columns = set(definition.csv_headers())
    assert {"custom_observations", "custom_type_a", "custom_type_b"} <= before_columns

    driver = tab_for(definition, "driver")
    driver["fields"] = [r for r in driver["fields"] if r["key"] != custom["key"]]
    # A stale persisted column order must never restore a deleted field.
    definition.csv_column_order.extend(["custom_type_a_old", "orphaned_column"])
    definition.validate()
    headers = definition.csv_headers()
    assert not any(column.startswith("custom_") for column in headers)
    assert "orphaned_column" not in headers
    assert list(definition.csv_row(values)) == headers
    assert values == original_values
    migrated = definition.migrate_values(values)
    assert migrated["custom_observation"] == ["a"]


def test_removing_custom_tab_drops_its_columns_despite_saved_order():
    definition = FormDefinition.default()
    definition.tabs.append({
        "key": "custom_tab", "title": "추가 탭", "short_title": "추가", "columns": 1,
        "fields": [text_field("custom_one"), text_field("custom_two")],
    })
    definition = FormDefinition.from_dict(definition.to_dict())
    assert "custom_one" in definition.csv_headers()
    definition.tabs.pop()
    definition.validate()
    assert "custom_one" not in definition.csv_headers()
    assert "custom_two" not in definition.csv_headers()


def test_saved_field_and_tab_order_survives_export_and_workspace_reload(tmp_path):
    repository = CaseRepository(tmp_path)
    definition = repository.form_definition
    driver = tab_for(definition, "driver")
    driver["fields"].reverse()
    driver["fields"].insert(1, text_field("custom_inserted"))
    notes = tab_for(definition, "notes")
    definition.tabs.remove(notes)
    definition.tabs.insert(1, notes)
    definition.validate()
    repository.form_definition = definition
    case = InvestigationCase(values={
        "institution": "samsung", "accident_classification": "2020011300001",
        "custom_inserted": "내용", "remarks": "비고 내용", "driver_exp": "12",
    })
    original_values = deepcopy(case.values)
    expected = definition.csv_headers()
    assert expected[2:7] == [
        "remarks", "driver_age", "custom_inserted", "driver_exp", "driver_sex",
    ]
    repository.save([case], form_definition=definition)

    reopened = CaseRepository(tmp_path)
    cases = reopened.load()
    assert reopened.form_definition.csv_headers() == expected
    headers, rows = read_csv(reopened.export_csv(tmp_path / "ordered.csv", cases))
    assert headers[7:-1] == expected
    assert rows[0]["custom_inserted"] == "내용"
    assert rows[0]["driver_exp"] == "12"
    assert rows[0]["remarks"] == "비고 내용"
    assert cases[0].values == original_values


def test_deleted_custom_values_survive_save_but_not_csv_export(tmp_path):
    repository = CaseRepository(tmp_path)
    custom = text_field("custom_removed")
    tab_for(repository.form_definition, "driver")["fields"].append(custom)
    case = InvestigationCase(values={
        "institution": "samsung", "accident_classification": "2020011300001",
        "custom_removed": "이전 조사값 보존",
    })
    repository.save([case], form_definition=repository.form_definition)
    before = repository.form_definition
    after = FormDefinition.from_dict(before.to_dict())
    driver = tab_for(after, "driver")
    driver["fields"] = [r for r in driver["fields"] if r["key"] != custom["key"]]
    after.validate()
    case.values = after.migrate_values(case.values, before)
    repository.form_definition = after
    repository.save([case], form_definition=after)

    reopened = CaseRepository(tmp_path)
    cases = reopened.load()
    assert cases[0].values["custom_removed"] == "이전 조사값 보존"
    headers, rows = read_csv(reopened.export_csv(tmp_path / "removed.csv", cases))
    assert "custom_removed" not in headers
    assert "custom_removed" not in rows[0]
    assert cases[0].values["custom_removed"] == "이전 조사값 보존"
