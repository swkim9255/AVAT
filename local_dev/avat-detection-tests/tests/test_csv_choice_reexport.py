from __future__ import annotations

import csv
import json
from copy import deepcopy

from PySide6.QtWidgets import QDialog, QMessageBox

from investigator.form_definition import FormDefinition
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.storage import CaseRepository
from investigator.widgets.form_designer import FormDesigner
from tests.helpers import valid_values


def record_for(definition, key):
    return next(record for record in definition.records() if record["key"] == key)


def selected_adult_values(number):
    values = valid_values()
    values.update(
        accident_classification=str(number), obstacle_exists="유", obstacle_state="동적",
        obstacle_pedestrians=["어른"], collision_targets=["vehicle.passenger", "pedestrian.adult"],
    )
    return values


def select_field(dialog, key):
    for ti, tab in enumerate(dialog.definition.tabs):
        for fi, record in enumerate(tab["fields"]):
            if record["key"] == key:
                dialog.tree.setCurrentItem(dialog.tree.topLevelItem(ti).child(fi))
                return
    raise AssertionError(key)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def test_saved_automatic_expansion_is_restored_in_existing_workspace(tmp_path):
    legacy = FormDefinition.default().to_dict()
    legacy["version"] = 4
    for tab in legacy["tabs"]:
        for record in tab["fields"]:
            if record["key"] not in {"obstacle_pedestrians", "collision_targets"}:
                continue
            for choice in record["choices"]:
                if choice["value"] not in {"어른", "pedestrian.adult"}:
                    continue
                choice["value_aliases"] = [choice["value"]]
                choice["id"] = choice["value"] = "어른, 청소년"
                choice["label"] = choice["label"].replace("어른", "어른, 청소년")
                if record["key"] == "obstacle_pedestrians":
                    choice["csv_column"] = "obstacle_adult,youth"
    legacy["csv_column_order"] = [
        "obstacle_adult,youth" if c == "obstacle_adult" else c
        for c in legacy["csv_column_order"]
    ]
    case = InvestigationCase(values=selected_adult_values(1))
    case.complete()
    payload_case = case.to_dict()
    payload_case["values"]["obstacle_pedestrians"] = ["어른, 청소년"]
    payload_case["values"]["collision_targets"] = ["vehicle.passenger", "어른, 청소년"]
    repository = CaseRepository(tmp_path)
    repository.legacy_database_path.write_text(json.dumps({
        "schema_version": 1, "form_definition": legacy,
        "initial_form_definition": deepcopy(legacy), "cases": [payload_case],
    }, ensure_ascii=False), encoding="utf-8")
    original_bytes = repository.legacy_database_path.read_bytes()
    loaded = repository.load()[0]
    adult = record_for(repository.form_definition, "obstacle_pedestrians")["choices"][1]
    assert (adult["label"], adult["value"], adult["csv_column"]) == ("어른", "어른", "obstacle_adult")
    assert loaded.values["obstacle_pedestrians"] == ["어른"]
    assert loaded.values["collision_targets"] == ["vehicle.passenger", "pedestrian.adult"]
    assert loaded.status == "completed"
    assert not loaded.errors
    assert repository.legacy_database_path.read_bytes() == original_bytes
    assert repository.database_path.exists()
    headers, rows = read_csv(repository.export_csv(tmp_path / "restored.csv", [loaded]))
    assert len(headers) == 95
    assert "obstacle_adult,youth" not in headers
    assert rows[0]["obstacle_adult"] == "1"
    assert rows[0]["collision_target"] == "승용|어른"
    repository.save([loaded])
    assert CaseRepository(tmp_path).load()[0].values == loaded.values


def test_edit_values_and_headers_then_reexport_all_existing_accidents(qtbot, tmp_path, monkeypatch):
    repository = CaseRepository(tmp_path)
    cases = [InvestigationCase(values=selected_adult_values(number)) for number in range(1, 4)]
    for case in cases:
        case.complete()
    repository.save(cases)
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    dialog = FormDesigner(window.repository.form_definition)
    qtbot.addWidget(dialog)
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[2]))
    select_field(dialog, "obstacle_pedestrians")
    # A storage edit alone does not automatically rename the individual header.
    dialog.choices.item(1, 1).setText("어른,청소년")
    assert dialog.choices.item(1, 2).text() == "obstacle_adult"
    # The header is separately edited by the user.
    dialog.choices.item(1, 2).setText("obstacle_adult,youth")
    select_field(dialog, "collision_targets")
    row = next(row for row in range(dialog.choices.rowCount())
               if dialog.choices.item(row, 1).text() == "pedestrian.adult")
    original_label = dialog.choices.item(row, 0).text()
    dialog.choices.item(row, 1).setText("어른,청소년")
    assert dialog.choices.item(row, 0).text() == original_label
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert window.apply_form_definition(dialog.definition)
    assert not messages
    assert all(case.status == "completed" for case in window.cases)
    destination = tmp_path / "reexport.csv"
    window.repository.export_csv(destination, window.cases)
    headers, rows = read_csv(destination)
    assert len(rows) == 3
    assert len(headers) == 95
    assert "obstacle_adult,youth" in headers
    assert "obstacle_adult" not in headers
    assert all(row["obstacle_adult,youth"] == "1" for row in rows)
    assert all(row["collision_target"] == "승용|어른,청소년" for row in rows)
    # Applying/exporting and reopening must not revert deliberate edits again.
    reopened_repository = CaseRepository(tmp_path)
    reopened = reopened_repository.load()
    current = record_for(reopened_repository.form_definition, "obstacle_pedestrians")["choices"][1]
    assert current["value"] == "어른,청소년"
    assert current["csv_column"] == "obstacle_adult,youth"
    assert all(case.values["collision_targets"] == ["vehicle.passenger", "어른,청소년"] for case in reopened)
    headers2, rows2 = read_csv(reopened_repository.export_csv(tmp_path / "after-restart.csv", reopened))
    assert headers2 == headers
    assert rows2 == rows
    # Another edit must update the same columns/rows, including data still using
    # the first saved choice value, rather than accumulating historical headers.
    definition2 = FormDefinition.from_dict(reopened_repository.form_definition.to_dict())
    adult = record_for(definition2, "obstacle_pedestrians")["choices"][1]
    adult["csv_aliases"].append(adult["csv_column"])
    adult["csv_column"] = "adult_and_youth"
    target = next(c for c in record_for(definition2, "collision_targets")["choices"]
                  if c["id"] == "pedestrian.adult")
    target["value_aliases"].append(target["value"])
    target["value"] = "수정한 저장값"
    definition2.validate()
    reopened_repository.form_definition = definition2
    headers3, rows3 = read_csv(reopened_repository.export_csv(tmp_path / "edited-again.csv", reopened))
    assert len(headers3) == 95
    assert "obstacle_adult" not in headers3
    assert "obstacle_adult,youth" not in headers3
    assert all(row["adult_and_youth"] == "1" for row in rows3)
    assert all(row["collision_target"] == "승용|수정한 저장값" for row in rows3)


def test_export_uses_current_choice_values_for_old_case_data_without_manual_reentry(tmp_path):
    definition = FormDefinition.default()
    choice = next(c for c in record_for(definition, "collision_targets")["choices"]
                  if c["id"] == "pedestrian.adult")
    choice["value"] = "어른,청소년"
    repository = CaseRepository(tmp_path)
    repository.form_definition = definition
    cases = [InvestigationCase(values=selected_adult_values(number)) for number in range(1, 4)]
    headers, rows = read_csv(repository.export_csv(tmp_path / "latest-values.csv", cases))
    assert "collision_target" in headers
    assert all(row["collision_target"] == "승용|어른,청소년" for row in rows)
    assert all(case.values["collision_targets"][-1] == "pedestrian.adult" for case in cases)
