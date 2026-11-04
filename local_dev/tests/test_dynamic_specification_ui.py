"""Export options describe the current form rather than a fixed reference schema."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog, QLabel

from investigator.csv_specification import csv_export_headers
from investigator.form_definition import FormDefinition, new_field
from investigator.main_window import MainWindow
from investigator.widgets.csv_export_dialog import CsvExportDialog


@pytest.mark.parametrize("explicit_definition", [False, True])
def test_default_specification_option_counts_current_95_columns(
    qtbot, tmp_path, explicit_definition,
):
    options = {"definition": FormDefinition.default()} if explicit_definition else {}
    dialog = CsvExportDialog(tmp_path / "result.csv", **options)
    qtbot.addWidget(dialog)

    assert dialog.include_specification.text() == "데이터 명세서(95개 항목)도 함께 내보내기"
    assert dialog.include_specification.isChecked()
    detail = dialog.findChild(QLabel, "dataSpecificationExportDetail")
    assert detail is not None
    assert "result_데이터명세서.xlsx" in detail.text()
    assert "현재 CSV의 칼럼 순서와 이름" in detail.text()
    assert "새로 추가한 항목" in detail.text()
    assert "기본 칼럼의 참고 명세서" not in detail.text()


def test_specification_count_follows_added_removed_and_renamed_live_fields(qtbot, tmp_path):
    definition = FormDefinition.default()
    driver = next(tab for tab in definition.tabs if tab["key"] == "driver")
    custom = new_field()
    custom["label"] = "추가 조사 메모"
    custom["csv_column"] = "custom_driver_note"
    driver["fields"].insert(1, custom)
    # Rename a current column, then move the entire tab ahead of the driver tab.
    driver["fields"][0]["csv_column"] = "renamed_driver_sex"
    definition.tabs.remove(driver)
    definition.tabs.insert(1, driver)
    definition.validate()

    dialog = CsvExportDialog(tmp_path / "custom.csv", definition=definition)
    qtbot.addWidget(dialog)
    assert len(csv_export_headers(definition)) == 96
    assert dialog.include_specification.text() == "데이터 명세서(96개 항목)도 함께 내보내기"

    driver["fields"].remove(custom)
    restored = CsvExportDialog(tmp_path / "removed.csv", definition=definition)
    qtbot.addWidget(restored)
    assert restored.include_specification.text() == "데이터 명세서(95개 항목)도 함께 내보내기"


def test_main_window_passes_exact_project_form_to_export_options(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path / "project")
    qtbot.addWidget(window)
    definition = window.repository.form_definition
    definition.tabs[-1]["fields"].append(new_field())
    definition.validate()
    destination = tmp_path / "result.csv"
    captured = []
    monkeypatch.setattr(window, "_save", lambda **_kwargs: True)
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getSaveFileName",
        lambda *_args: (str(destination), "CSV 파일 (*.csv)"),
    )

    class InspectExportDialog:
        def __init__(self, path, parent=None, *, definition=None):
            captured.append((path, parent, definition))

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("investigator.main_window.CsvExportDialog", InspectExportDialog)
    window.export_csv()

    assert captured == [(Path(destination), window, definition)]
    assert captured[0][2] is definition
    assert not destination.exists()
    window.close()
