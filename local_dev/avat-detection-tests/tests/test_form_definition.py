from __future__ import annotations

import csv
import json
import sqlite3

import pytest
from PySide6.QtWidgets import QDialog, QLineEdit, QMessageBox

from investigator.form_definition import FormDefinition, new_field
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.schema import FieldKind
from investigator.storage import CaseRepository, WorkspaceError
from investigator.widgets.form_designer import FormDesigner
from investigator.widgets.form_panel import InvestigationPanel
from tests.helpers import valid_values


def custom_form(kind=FieldKind.TEXT, default=None, required=False):
    definition = FormDefinition.default()
    record = new_field()
    record.update(
        label="추가 조사",
        csv_column="extra_question",
        kind=kind.value,
        default=default,
        required=required,
    )
    if kind in {FieldKind.SINGLE, FieldKind.MULTI}:
        record["choices"] = [
            {"label": "선택 A", "value": "a", "csv_column": ""},
            {"label": "선택 B", "value": "b", "csv_column": ""},
        ]
    definition.tabs.append(
        {"key": "extra", "title": "추가 조사", "short_title": "추가", "fields": [record]}
    )
    definition.validate()
    return definition, record


def test_custom_defaults_roundtrip_clear_and_csv_contract(tmp_path):
    definition, record = custom_form(FieldKind.MULTI, ["a"])
    repo = CaseRepository(tmp_path)
    old = InvestigationCase.from_dict(InvestigationCase(values=valid_values()).to_dict())
    repo.save([old], form_definition=definition)
    assert record["key"] not in old.values
    case = InvestigationCase(values={"institution": "samsung", "accident_classification": "002"})
    repo.save([old, case])
    assert case.values[record["key"]] == ["a"]
    with sqlite3.connect(repo.database_path) as connection:
        stored_version = connection.execute(
            "SELECT value FROM workspace_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert json.loads(stored_version) == 2

    loaded_repo = CaseRepository(tmp_path)
    loaded = loaded_repo.load()
    assert loaded_repo.form_definition == definition
    assert loaded[1].values[record["key"]] == ["a"]
    loaded[1].set_value(record["key"], [])
    loaded_repo.save(loaded)
    assert record["key"] not in loaded_repo.load()[1].values
    loaded[1].set_value(record["key"], ["a", "b"])
    path = loaded_repo.export_csv(tmp_path / "out.csv", loaded)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames[7 : 7 + len(definition.csv_headers())] == definition.csv_headers()
        rows = list(reader)
    assert rows[0]["extra_question"] == "null"
    assert rows[1]["extra_question"] == "a|b"


def test_renaming_builtin_labels_and_csv_keeps_storage_key_but_exports_current_column(tmp_path):
    definition = FormDefinition.default()
    record = next(f for f in definition.records() if f["key"] == "driver_sex")
    record.update(label="운전자 성별 수정", csv_column="renamed_sex")
    record["choices"][0]["label"] = "남성 표시 수정"
    definition.validate()
    repo = CaseRepository(tmp_path)
    case = InvestigationCase(values=valid_values())
    repo.save([case], form_definition=definition)
    restored = repo.load()[0]
    assert restored.values["driver_sex"] == "남성"
    row = definition.csv_row(restored.values)
    assert "driver_sex" not in row
    assert row["renamed_sex"] == "남성"


@pytest.mark.parametrize("column", ["case_id", "driver_sex", "=formula", "bad\ncolumn"])
def test_invalid_or_duplicate_custom_columns_are_rejected(column):
    definition, record = custom_form()
    record["csv_column"] = column
    with pytest.raises(ValueError):
        definition.validate()


def test_form_application_preserves_cases_and_reopens_newly_required(qtbot, tmp_path):
    repo = CaseRepository(tmp_path)
    original = InvestigationCase(values=valid_values())
    original.complete()
    repo.save([original])
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    definition, field = custom_form(required=True)
    assert window.apply_form_definition(definition)
    assert window.investigation.tabs.count() == 10
    assert window.investigation.tabs.tabText(9) == "추가"
    assert window.investigation.tabs.tabBar().usesScrollButtons()
    assert window.current_case.status == "draft"
    assert field["key"] in window.current_case.missing
    window._field_changed(field["key"], "조사 완료 값")
    window.complete_case()
    assert window.current_case.status == "completed"
    assert CaseRepository(tmp_path).load()[0].values[field["key"]] == "조사 완료 값"


def test_incompatible_type_change_and_failed_save_preserve_original(qtbot, monkeypatch, tmp_path):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    definition, field = custom_form()
    assert window.apply_form_definition(definition)
    window._field_changed(field["key"], "숫자가 아닌 기존 값")
    window.save_draft()
    original_bytes = window.repository.database_path.read_bytes()
    original_case = window.current_case
    original_panel = window.investigation
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[2]))
    changed = FormDefinition.from_dict(definition.to_dict())
    changed.records()[-1]["kind"] = FieldKind.DIGITS.value
    assert not window.apply_form_definition(changed)
    assert "기존 값" in messages[-1]
    assert window.current_case is original_case
    assert window.investigation is original_panel
    assert window.repository.database_path.read_bytes() == original_bytes

    def fail_save(*args, **kwargs):
        raise WorkspaceError("disk failure")

    monkeypatch.setattr(window.repository, "save", fail_save)
    changed.records()[-1]["kind"] = FieldKind.TEXT.value
    changed.records()[-1]["label"] = "변경 이름"
    assert not window.apply_form_definition(changed)
    assert window.repository.form_definition == definition
    assert window.current_case is original_case
    assert window.investigation is original_panel
    monkeypatch.undo()


def test_designer_adds_tab_field_and_choice_without_mutating_original(qtbot):
    original = FormDefinition.default()
    dialog = FormDesigner(original)
    qtbot.addWidget(dialog)
    dialog.add_tab_button.click()
    dialog.tab_title.setText("사용자 조사")
    dialog.tab_short_title.setText("사용자")
    dialog.add_field_button.click()
    dialog.field_label.setText("추가 선택")
    dialog.column_name.setText("additional_selection")
    dialog.kind.setCurrentIndex(dialog.kind.findData(FieldKind.SINGLE.value))
    dialog.add_choice_button.click()
    dialog.choices.item(0, 0).setText("선택 항목")
    dialog.choices.item(0, 1).setText("yes")
    dialog.default_value.setPlainText("yes")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert original == FormDefinition.default()
    record = dialog.definition.records()[-1]
    assert record["label"] == "추가 선택"
    assert record["default"] == "yes"
    assert record["kind"] == "single"
    assert dialog.definition.tabs[-1]["short_title"] == "사용자"
    assert dialog.definition.csv_headers()[-1] == "additional_selection"


def test_field_type_changes_render_new_control_and_workspace_settings_are_isolated(qtbot, tmp_path):
    window = MainWindow(tmp_path / "first")
    qtbot.addWidget(window)
    window.create_case()
    definition = FormDefinition.default()
    field = next(f for f in definition.records() if f["key"] == "driver_sex")
    field["kind"] = "text"
    field["label"] = "운전자 정보"
    assert window.apply_form_definition(definition)
    assert isinstance(window.investigation.editors["driver_sex"].line_edit, QLineEdit)
    other = MainWindow(tmp_path / "other")
    qtbot.addWidget(other)
    assert other.repository.form_definition == FormDefinition.default()
    restored = MainWindow(tmp_path / "first")
    qtbot.addWidget(restored)
    assert restored.investigation.editors["driver_sex"].spec.kind == FieldKind.TEXT


def test_legacy_json_without_form_still_loads_and_bad_form_never_overwrites(tmp_path):
    repo = CaseRepository(tmp_path / "valid")
    repo.ensure_workspace()
    data = {"schema_version": 1, "cases": [InvestigationCase(values=valid_values()).to_dict()]}
    repo.legacy_database_path.write_text(json.dumps(data), encoding="utf-8")
    assert repo.load()[0].values["driver_sex"] == "남성"
    assert repo.database_path.exists()
    repo = CaseRepository(tmp_path / "invalid")
    repo.ensure_workspace()
    data["form_definition"] = {"version": 99, "tabs": []}
    repo.legacy_database_path.write_text(json.dumps(data), encoding="utf-8")
    before = repo.legacy_database_path.read_bytes()
    with pytest.raises(WorkspaceError):
        repo.load()
    assert repo.legacy_database_path.read_bytes() == before
    assert not repo.database_path.exists()


def test_editable_dropdown_custom_value_roundtrips_from_blank_selection(qtbot, tmp_path):
    definition, record = custom_form(FieldKind.SINGLE)
    record["editable"] = True
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    assert window.apply_form_definition(definition)
    editor = window.investigation.editors[record["key"]]
    assert editor.control.currentIndex() == 0
    editor.control.setEditText("직접 입력한 새 값")
    assert editor.value() == "직접 입력한 새 값"
    assert window.current_case.values[record["key"]] == "직접 입력한 새 값"
    window.save_draft()
    assert CaseRepository(tmp_path).load()[0].values[record["key"]] == "직접 입력한 새 값"


def test_current_csv_column_preserves_special_boolean_encoding_without_old_header():
    definition = FormDefinition.default()
    record = next(f for f in definition.records() if f["key"] == "incident_damage_unknown")
    record["csv_column"] = "damage_unknown_alias"
    definition.validate()
    row = definition.csv_row({"incident_damage_unknown": "확인불가"})
    assert "incident_damage_Unknown" not in row
    assert row["damage_unknown_alias"] == "1"


def test_builtin_speed_changed_to_multi_can_be_saved_and_reopened(tmp_path):
    definition = FormDefinition.default()
    record = next(f for f in definition.records() if f["key"] == "trigger_vehicle_speed")
    record["kind"] = "multi"
    values = valid_values()
    values["trigger_vehicle_speed"] = ["정차(0kph)"]
    case = InvestigationCase(values=values)
    repo = CaseRepository(tmp_path)
    repo.save([case], form_definition=definition)
    assert repo.load()[0].values["trigger_vehicle_speed"] == ["정차(0kph)"]


def test_conditional_fields_cannot_be_made_unconditionally_required():
    definition = FormDefinition.default()
    record = next(
        f for f in definition.records() if f["key"] == "incident_minor_representative_injury_grade"
    )
    record["required"] = True
    with pytest.raises(ValueError, match="필수 입력 규칙"):
        definition.validate()


def test_form_transaction_failure_leaves_definition_cases_and_database_unchanged(qtbot, monkeypatch, tmp_path):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    before = [case.to_dict() for case in CaseRepository(tmp_path).load()]
    old_case = window.current_case
    definition, _ = custom_form()
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)

    with sqlite3.connect(window.repository.database_path) as connection:
        connection.execute(
            """CREATE TRIGGER reject_form_update BEFORE UPDATE ON workspace_metadata
               WHEN NEW.key = 'form_definition'
               BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END"""
        )
    assert not window.apply_form_definition(definition)
    reloaded = CaseRepository(tmp_path)
    assert [case.to_dict() for case in reloaded.load()] == before
    assert reloaded.form_definition == FormDefinition.default()
    assert window.repository.form_definition == FormDefinition.default()
    assert window.current_case is old_case
    assert window.investigation.tabs.count() == 9


def test_designer_keeps_previous_custom_column_for_compatibility_but_does_not_export_it(qtbot):
    definition, record = custom_form()
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(10).child(0))
    dialog.column_name.setText("new_column")
    dialog.accept()
    updated = dialog.definition.records()[-1]
    assert updated["key"] == record["key"]
    assert updated["aliases"] == ["extra_question"]
    row = dialog.definition.csv_row({record["key"]: "기존 조사 내용"})
    assert "extra_question" not in row
    assert row["new_column"] == "기존 조사 내용"


def test_designer_order_and_layout_are_saved_and_rendered(qtbot, tmp_path):
    definition = FormDefinition.default()
    original_keys = [f["key"] for f in definition.tabs[1]["fields"]]
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
    assert not dialog.move_up_button.isEnabled()
    assert not dialog.move_down_button.isEnabled()
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
    assert not dialog.move_up_button.isEnabled()
    dialog.tab_title.setText("운전자 수정")
    dialog.tab_columns.setCurrentIndex(dialog.tab_columns.findData(2))
    dialog.move_down_button.click()
    assert dialog.definition.tabs[2]["key"] == "driver"
    assert dialog.tab_title.text() == "운전자 수정"
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(2).child(0))
    dialog.field_label.setText("성별 수정")
    dialog.full_width.setChecked(True)
    dialog.move_down_button.click()
    assert dialog.field_label.text() == "성별 수정"
    assert dialog.full_width.isChecked()
    dialog.accept()

    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    old_values = dict(window.current_case.values)
    assert window.apply_form_definition(dialog.definition)
    assert window.current_case.values == old_values
    assert window.investigation.sections[window.investigation.tabs.currentIndex()].key == "driver"
    reopened = MainWindow(tmp_path)
    qtbot.addWidget(reopened)
    config = reopened.repository.form_definition
    assert config.tabs[2]["title"] == "운전자 수정"
    assert [f["key"] for f in config.tabs[2]["fields"]] == [
        original_keys[1],
        original_keys[0],
        original_keys[2],
    ]
    assert reopened.investigation.sections[1].key == "driver"
    grid = reopened.investigation.section_field_grids["driver"]
    assert grid.itemAtPosition(0, 0).widget().spec.key == "driver_exp"
    assert grid.getItemPosition(1) == (1, 0, 1, 2)
    assert grid.itemAtPosition(2, 0).widget().spec.key == "driver_age"


def test_collision_layout_and_v1_migration_export_current_form_order(qtbot):
    definition = FormDefinition.default()
    legacy = definition.to_dict()
    legacy["version"] = 1
    legacy.pop("csv_column_order")
    from investigator.schema import SECTIONS

    original_sections = {s.key: s for s in SECTIONS}
    for tab in legacy["tabs"]:
        tab.pop("columns", None)
        for f in tab["fields"]:
            f.pop("full_width", None)
        if tab["key"] in original_sections:
            order = [f.key for f in original_sections[tab["key"]].fields]
            tab["fields"].sort(key=lambda f: order.index(f["key"]))
    migrated = FormDefinition.from_dict(legacy)
    assert migrated == definition
    assert migrated.csv_headers() == definition.csv_headers()
    panel = InvestigationPanel(definition=migrated)
    qtbot.addWidget(panel)
    grid = panel.section_field_grids["collision_objects"]
    assert grid.itemAtPosition(0, 0).widget().spec.key == "collision_targets"
    assert grid.getItemPosition(0) == (0, 0, 1, 2)
    assert grid.itemAtPosition(1, 0).widget().spec.key == "incident_damage_unknown"
    assert grid.getItemPosition(1) == (1, 0, 1, 2)
    assert grid.itemAtPosition(2, 0).widget().spec.key == "incident_minor_injury_grades"
    assert grid.itemAtPosition(2, 1).widget().spec.key == "incident_severe_injury_grades"
    assert grid.itemAtPosition(5, 0).widget().spec.key == "incident_fatality"
    assert grid.itemAtPosition(5, 1).widget().spec.key == "incident_property_damage"
    config = FormDefinition.from_dict(migrated.to_dict())
    collision = next(t for t in config.tabs if t["key"] == "collision_objects")
    collision["columns"] = 1
    original_columns = [field["csv_column"] for field in collision["fields"]]
    collision["fields"].reverse()
    restored = FormDefinition.from_dict(config.to_dict())
    assert restored == config  # Subsequent loads must not reset the user's layout/order.
    assert restored.csv_headers() == config.csv_headers()
    start = restored.csv_headers().index(original_columns[-1])
    assert restored.csv_headers()[start : start + len(original_columns)] == original_columns[::-1]


def test_reordering_custom_fields_changes_csv_column_order(qtbot):
    definition, first = custom_form()
    second = new_field()
    second["csv_column"] = "second_extra"
    definition.tabs[-1]["fields"].append(second)
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    before = dialog.definition.csv_headers()
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(10).child(1))
    dialog.move_up_button.click()
    assert not dialog.move_up_button.isEnabled()
    dialog.accept()
    assert dialog.definition.tabs[-1]["fields"][0]["key"] == second["key"]
    assert before[-2:] == ["extra_question", "second_extra"]
    assert dialog.definition.csv_headers() == before[:-2] + ["second_extra", "extra_question"]
    assert dialog.definition.tabs[-1]["fields"][1]["key"] == first["key"]


def test_identity_two_columns_can_be_changed_and_restored(qtbot):
    definition = FormDefinition.default()
    panel = InvestigationPanel(definition=definition)
    qtbot.addWidget(panel)
    grid = panel.section_field_grids["identity"]
    assert grid.itemAtPosition(0, 0).widget().spec.key == "institution"
    assert grid.itemAtPosition(0, 1).widget().spec.key == "accident_classification"
    legacy = definition.to_dict()
    legacy["version"] = 2
    legacy["tabs"][0]["columns"] = 1
    assert FormDefinition.from_dict(legacy).tabs[0]["columns"] == 2
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    assert dialog.tab_columns.isEnabled()
    dialog.tab_columns.setCurrentIndex(dialog.tab_columns.findData(1))
    dialog.accept()
    loaded = FormDefinition.from_dict(dialog.definition.to_dict())
    assert loaded.tabs[0]["columns"] == 1
    vertical = InvestigationPanel(definition=loaded)
    qtbot.addWidget(vertical)
    assert (
        vertical.section_field_grids["identity"].itemAtPosition(1, 0).widget().spec.key
        == "accident_classification"
    )


def test_saved_initial_form_is_unchanged_by_edits_and_restore_keeps_user_data(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    initial = window.repository.initial_form_definition.to_dict()
    custom, record = custom_form()
    driver = next(t for t in custom.tabs if t["key"] == "driver")
    driver["short_title"] = "수정된 탭"
    driver["columns"] = 2
    sex = next(f for f in driver["fields"] if f["key"] == "driver_sex")
    sex["label"] = "수정된 이름"
    sex["csv_column"] = "renamed_sex"
    assert window.apply_form_definition(custom)
    window._field_changed(record["key"], "보존할 추가 조사 값")
    window._field_changed("driver_sex", "남성")
    window.save_draft()
    repo = CaseRepository(tmp_path)
    repo.load()
    assert repo.initial_form_definition.to_dict() == initial
    with sqlite3.connect(repo.database_path) as connection:
        stored_initial = connection.execute(
            "SELECT value FROM workspace_metadata WHERE key = 'initial_form_definition'"
        ).fetchone()[0]
    assert json.loads(stored_initial) == initial
    before = repo.database_path.read_bytes()
    dialog = FormDesigner(repo.form_definition, initial_definition=repo.initial_form_definition)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog.restore_button.click()
    restored_driver = next(t for t in dialog.definition.tabs if t["key"] == "driver")
    assert restored_driver["short_title"] == "운전자"
    assert restored_driver["columns"] == 1
    assert (
        next(f for f in restored_driver["fields"] if f["key"] == "driver_sex")["label"]
        == "운전자 성별"
    )
    assert repo.database_path.read_bytes() == before  # Staged until Apply.
    assert window.apply_form_definition(dialog.definition)
    assert window.current_case.values[record["key"]] == "보존할 추가 조사 값"
    assert window.current_case.values["driver_sex"] == "남성"
    assert record["key"] in window.investigation.editors
    exported = window.repository.form_definition.csv_row(window.current_case.values)
    assert "renamed_sex" not in exported
    assert exported["driver_sex"] == "남성"
    assert exported["extra_question"] == "보존할 추가 조사 값"
    assert window.repository.initial_form_definition.to_dict() == initial
