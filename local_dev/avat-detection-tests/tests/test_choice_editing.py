from __future__ import annotations

import csv
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from investigator.form_definition import FormDefinition
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.storage import CaseRepository, WorkspaceError
from investigator.widgets.form_designer import FormDesigner
from tests.helpers import valid_values


def record_for(definition, key):
    return next(record for record in definition.records() if record["key"] == key)


def adult_values():
    values = valid_values()
    values.update(obstacle_exists="유", obstacle_pedestrians=["어른"],
                  obstacle_state="동적", collision_targets=["pedestrian.adult"])
    return values


@pytest.mark.parametrize("with_form", [True, False])
def test_old_workspace_keeps_original_adult_values_and_column(tmp_path, with_form):
    definition = FormDefinition.default().to_dict()
    definition["version"] = 3
    for tab in definition["tabs"]:
        for record in tab["fields"]:
            for choice in record["choices"]:
                choice.pop("id", None)
                choice.pop("value_aliases", None)
                choice.pop("csv_aliases", None)
    values = adult_values()
    case = InvestigationCase(values=values)
    repository = CaseRepository(tmp_path)
    payload = {"schema_version": 1, "cases": [case.to_dict()]}
    if with_form:
        payload.update(form_definition=definition, initial_form_definition=definition)
    repository.legacy_database_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = repository.load()[0]
    assert loaded.values == case.values
    assert loaded.values["obstacle_pedestrians"] == ["어른"]
    assert loaded.values["collision_targets"] == ["pedestrian.adult"]
    assert not loaded.errors
    destination = repository.export_csv(tmp_path / "result.csv", [loaded])
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert len(reader.fieldnames) == 95
        assert "obstacle_adult,youth" not in reader.fieldnames
        row = next(reader)
        assert row["obstacle_adult"] == "1"
        assert row["collision_target"] == "어른"
    repository.save([loaded])
    assert CaseRepository(tmp_path).load()[0].values == loaded.values


def test_designer_edits_storage_and_choice_column_and_migrates_completed_cases(qtbot, tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path)
    case = InvestigationCase(values=adult_values())
    case.complete()
    repository.save([case])
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    dialog = FormDesigner(window.repository.form_definition)
    qtbot.addWidget(dialog)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[2]))
    for ti, tab in enumerate(dialog.definition.tabs):
        for fi, record in enumerate(tab["fields"]):
            if record["key"] == "obstacle_pedestrians":
                dialog.tree.setCurrentItem(dialog.tree.topLevelItem(ti).child(fi))
    assert dialog.choices.item(1, 1).flags() & Qt.ItemFlag.ItemIsEditable
    assert dialog.choices.item(1, 2).flags() & Qt.ItemFlag.ItemIsEditable
    dialog.choices.item(1, 1).setText("성인청소년")
    dialog.choices.item(1, 2).setText("custom_adult,youth")
    dialog.default_value.setPlainText("어른")
    dialog.accept()
    assert not messages
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert record_for(dialog.definition, "obstacle_pedestrians")["default"] == ["성인청소년"]
    assert window.apply_form_definition(dialog.definition)
    assert not messages
    assert window.current_case.values["obstacle_pedestrians"] == ["성인청소년"]
    assert window.current_case.status == "completed"
    loaded_repository = CaseRepository(tmp_path)
    loaded = loaded_repository.load()[0]
    row = loaded_repository.form_definition.csv_row(loaded.values)
    assert row["custom_adult,youth"] == "1"
    assert "obstacle_adult" not in row
    restored = loaded_repository.form_definition.restored(loaded_repository.initial_form_definition)
    assert window.apply_form_definition(restored)
    assert window.current_case.values["obstacle_pedestrians"] == ["어른"]
    assert len(record_for(restored, "obstacle_pedestrians")["choices"]) == 2
    assert restored.csv_row(window.current_case.values)["obstacle_adult"] == "1"
    assert "custom_adult,youth" not in restored.csv_headers()


def test_renamed_rule_values_preserve_conditions_auto_calculations_and_reload(qtbot, tmp_path):
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    definition = FormDefinition.default()
    for key, canonical, stored in (
        ("obstacle_exists", "무", "없음"),
        ("obstacle_exists", "유", "있음"),
        ("incident_damage_unknown", "확인불가", "모름"),
        ("incident_day", "월", "Monday"),
        ("collision_dist_range", "10m 이내", "close"),
        ("driver_age", "20-24세", "early-twenties"),
    ):
        choice = next(c for c in record_for(definition, key)["choices"] if c["id"] == canonical)
        choice["value_aliases"].append(choice["value"])
        choice["value"] = stored
    assert window.apply_form_definition(definition)
    window._field_changed("obstacle_pedestrians", ["어른"])
    window._field_changed("obstacle_exists", "없음")
    assert "obstacle_pedestrians" not in window.current_case.values
    assert not window.investigation.editors["obstacle_pedestrians"].isEnabled()
    window.investigation.editors["incident_date"].set_value("20261019")
    window.investigation.editors["incident_date"].value_changed.emit("incident_date", "20261019")
    assert window.current_case.values["incident_day"] == "Monday"
    window.investigation.editors["collision_dist_value"].set_value("2")
    window.investigation.editors["collision_dist_value"].value_changed.emit("collision_dist_value", "2")
    assert window.current_case.values["collision_dist_range"] == "close"
    window._field_changed("driver_age", "early-twenties")
    window._field_changed("incident_damage_unknown", "모름")
    window.save_draft()
    loaded = CaseRepository(tmp_path).load()[0]
    assert loaded.values["incident_day"] == "Monday"
    assert loaded.values["driver_age"] == "early-twenties"
    assert loaded.values["collision_dist_range"] == "close"
    assert not loaded.errors
    assert definition.csv_row(loaded.values)["incident_damage_Unknown"] == "1"


def test_failed_storage_value_change_preserves_cases_and_disk(qtbot, tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path)
    repository.save([InvestigationCase(values=adult_values())])
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    original_values = dict(window.current_case.values)
    original_bytes = window.repository.database_path.read_bytes()
    definition = FormDefinition.from_dict(window.repository.form_definition.to_dict())
    choice = record_for(definition, "obstacle_pedestrians")["choices"][1]
    choice["value"] = "새 저장값"
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)

    def fail_save(*args, **kwargs):
        raise WorkspaceError("disk full")

    monkeypatch.setattr(window.repository, "save", fail_save)
    assert not window.apply_form_definition(definition)
    assert window.current_case.values == original_values
    assert window.repository.database_path.read_bytes() == original_bytes
    monkeypatch.undo()
