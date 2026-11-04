from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from investigator.form_definition import FormDefinition
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.storage import CaseRepository
from investigator.widgets.form_designer import FormDesigner
from tests.helpers import valid_values


def adult_record(definition):
    return next(record for record in definition.records() if record["key"] == "obstacle_pedestrians")


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Cancel])
def test_cancel_restore_keeps_editor_and_definition_unchanged(qtbot, monkeypatch, answer):
    dialog = FormDesigner(FormDefinition.default())
    qtbot.addWidget(dialog)
    dialog.tab_title.setText("아직 적용하지 않은 편집")
    before = dialog.definition.to_dict()
    questions = []

    def confirm(*args):
        questions.append(args)
        assert args[-1] == QMessageBox.StandardButton.No
        assert args[-2] == (QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return answer

    monkeypatch.setattr(QMessageBox, "question", confirm)
    dialog.restore_button.click()
    assert len(questions) == 1
    assert dialog.definition.to_dict() == before
    assert dialog.tab_title.text() == "아직 적용하지 않은 편집"


@pytest.mark.parametrize("snapshot_version", [4, 5])
def test_confirm_restore_returns_adult_and_preserves_case_until_apply(
    qtbot, tmp_path, monkeypatch, snapshot_version
):
    initial = FormDefinition.default()
    # Reproduce the previously saved initial snapshot that also contained youth.
    adult = adult_record(initial)["choices"][1]
    adult.update(id="어른, 청소년", label="어른, 청소년", value="어른, 청소년",
                 csv_column="obstacle_adult,youth")
    raw_initial = initial.to_dict()
    raw_initial["version"] = snapshot_version
    initial = FormDefinition.from_dict(raw_initial)

    definition = FormDefinition.default()
    adult = adult_record(definition)["choices"][1]
    adult.update(label="사용자가 수정한 이름", value="사용자가 수정한 값", csv_column="edited_adult")
    values = valid_values()
    values.update(obstacle_exists="유", obstacle_state="동적",
                  obstacle_pedestrians=["사용자가 수정한 값"])
    repository = CaseRepository(tmp_path)
    repository.initial_form_definition = initial
    repository.save([InvestigationCase(values=values)], form_definition=definition)
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    dialog = FormDesigner(
        window.repository.form_definition, initial_definition=window.repository.initial_form_definition
    )
    qtbot.addWidget(dialog)
    original_bytes = window.repository.database_path.read_bytes()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    dialog.restore_button.click()
    restored_adult = adult_record(dialog.definition)["choices"][1]
    assert (restored_adult["label"], restored_adult["value"], restored_adult["csv_column"]) == (
        "어른", "어른", "obstacle_adult"
    )
    assert window.current_case.values["obstacle_pedestrians"] == ["사용자가 수정한 값"]
    assert window.repository.database_path.read_bytes() == original_bytes
    assert window.apply_form_definition(dialog.definition)
    assert window.current_case.values["obstacle_pedestrians"] == ["어른"]
    assert window.current_case.form_definition.csv_row(window.current_case.values)["obstacle_adult"] == "1"
