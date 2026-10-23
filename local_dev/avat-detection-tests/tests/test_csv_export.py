from __future__ import annotations

import csv
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from PySide6.QtWidgets import QDialog

from investigator.main_window import MainWindow
from investigator.models import new_case
from investigator.storage import (
    DATA_SPECIFICATION_FILENAME,
    CaseRepository,
    WorkspaceError,
    data_specification_path,
)
from investigator.widgets.csv_export_dialog import CsvExportDialog


def test_packaged_specification_is_copied_verbatim_and_names_all_95_default_columns(tmp_path: Path) -> None:
    repository = CaseRepository(tmp_path)
    destination = repository.export_csv(
        tmp_path / "result.csv", [new_case([])], include_specification=True
    )
    content = files("investigator").joinpath("assets", DATA_SPECIFICATION_FILENAME).read_bytes()
    generated = data_specification_path(destination).read_bytes()
    assert generated == content  # 조사 양식과 무관하게 번들 원본을 그대로 복사한다.
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        headers = next(csv.reader(handle))
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(generated)) as workbook:
        strings = [
            "".join(element.itertext())
            for element in ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
        ] if "xl/sharedStrings.xml" in workbook.namelist() else []
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        columns = [
            strings[int(cell.find("m:v", namespace).text)] if cell.get("t") == "s"
            else (cell.find("m:v", namespace).text if cell.get("t") == "str"
                  else "".join(cell.find("m:is", namespace).itertext()))
            for row in sheet.findall(".//m:row", namespace)[1:]
            for cell in row.findall("m:c", namespace)
            if cell.get("r", "").startswith("A") and cell.get("t") in {"s", "str", "inlineStr"}
        ]
    assert len(columns) == 95
    assert len(headers) == 95
    # 원본은 기본 CSV의 모든 칼럼을 설명한다. 행 순서는 원본 파일의 순서를 따른다.
    assert set(columns) == set(headers)


def test_missing_specification_leaves_existing_csv_unchanged(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    destination = tmp_path / "result.csv"
    destination.write_text("previous CSV", encoding="utf-8")

    def missing_resource(_package: str):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("missing packaged specification")

    monkeypatch.setattr("investigator.storage.files", missing_resource)
    with pytest.raises(WorkspaceError, match="CSV와 데이터 명세서를 내보내지 못했습니다"):
        CaseRepository(tmp_path).export_csv(destination, [], include_specification=True)
    assert destination.read_text(encoding="utf-8") == "previous CSV"
    assert not list(tmp_path.glob("*.tmp"))


def test_locked_specification_reports_csv_was_saved(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import investigator.storage as storage

    destination = tmp_path / "result.csv"
    companion = data_specification_path(destination)
    companion.write_bytes(b"previous specification")
    original_replace = storage.os.replace

    def replace(source, target):  # type: ignore[no-untyped-def]
        if target == companion:
            raise PermissionError("file is open in Excel")
        return original_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", replace)
    with pytest.raises(WorkspaceError, match="CSV는 저장했습니다"):
        CaseRepository(tmp_path).export_csv(destination, [], include_specification=True)
    assert destination.read_bytes().startswith(b"\xef\xbb\xbfcase_id")
    assert companion.read_bytes() == b"previous specification"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("selection", [True, False, None])
def test_export_options_default_selection_opt_out_and_cancel(
    qtbot, tmp_path: Path, monkeypatch, selection: bool | None
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    destination = tmp_path / "result.csv"
    companion = data_specification_path(destination)
    # Deselecting/cancelling must preserve any previously exported specification.
    companion.write_bytes(b"previous specification")
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getSaveFileName",
        lambda *args: (str(destination), "CSV 파일 (*.csv)"),
    )

    def choose(dialog: CsvExportDialog) -> QDialog.DialogCode:
        assert dialog.include_specification.isChecked()
        if selection is None:
            return QDialog.DialogCode.Rejected
        dialog.include_specification.setChecked(selection)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CsvExportDialog, "exec", choose)
    window.export_csv()
    assert destination.exists() == (selection is not None)
    if selection:
        assert companion.read_bytes().startswith(b"PK")
        assert "데이터 명세서" in window.statusBar().currentMessage()
    else:
        assert companion.read_bytes() == b"previous specification"


def test_cancel_file_picker_does_not_open_options(qtbot, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getSaveFileName", lambda *args: ("", "")
    )

    def unexpected_options(_dialog: CsvExportDialog) -> None:
        pytest.fail("Options should not appear after cancelling the file picker")

    monkeypatch.setattr(CsvExportDialog, "exec", unexpected_options)
    window.export_csv()
