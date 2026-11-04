"""Speed analysis accompanies every investigation CSV without an opt-in."""

import csv

import pytest
from PySide6.QtWidgets import QCheckBox, QDialog, QLabel

from investigator.main_window import MainWindow
from investigator.speed_csv import speed_csv_path
from investigator.widgets.csv_export_dialog import CsvExportDialog


def test_csv_options_explain_automatic_speed_companion(qtbot, tmp_path):
    destination = tmp_path / "조사 결과.csv"
    dialog = CsvExportDialog(destination)
    qtbot.addWidget(dialog)

    detail = dialog.findChild(QLabel, "speedCsvExportDetail")
    assert detail is not None
    assert speed_csv_path(destination).name in detail.text()
    assert "자동 저장" in detail.text()
    assert "트리거·충돌·속도 지점" in detail.text()
    assert "기존 조사 CSV의 칼럼은 유지" in detail.text()
    assert dialog.findChild(QLabel, "speedCsvOverwriteWarning") is None
    assert len(dialog.findChildren(QCheckBox)) == 1
    assert dialog.include_specification.isChecked()


def test_existing_speed_csv_warning_is_not_tied_to_specification(qtbot, tmp_path):
    destination = tmp_path / "result.csv"
    speed_csv_path(destination).touch()
    dialog = CsvExportDialog(destination)
    qtbot.addWidget(dialog)
    dialog.show()

    warning = dialog.findChild(QLabel, "speedCsvOverwriteWarning")
    assert warning is not None
    assert "덮어씁니다" in warning.text()
    assert warning.isVisible()
    dialog.include_specification.setChecked(False)
    assert warning.isVisible()


def test_both_companion_files_keep_their_separate_warnings(qtbot, tmp_path):
    from investigator.storage import data_specification_path

    destination = tmp_path / "result.csv"
    speed_csv_path(destination).touch()
    data_specification_path(destination).touch()
    dialog = CsvExportDialog(destination)
    qtbot.addWidget(dialog)
    dialog.show()

    specification_warning = next(
        label for label in dialog.findChildren(QLabel)
        if "같은 이름의 명세서" in label.text()
    )
    speed_warning = dialog.findChild(QLabel, "speedCsvOverwriteWarning")
    assert specification_warning.isVisible()
    assert speed_warning is not None and speed_warning.isVisible()
    dialog.include_specification.setChecked(False)
    assert not specification_warning.isVisible()
    assert speed_warning.isVisible()


@pytest.mark.parametrize("selection", [True, False, None])
def test_main_window_exports_speed_with_either_specification_choice_but_not_cancel(
    qtbot, tmp_path, monkeypatch, selection,
):
    window = MainWindow(tmp_path / "project")
    qtbot.addWidget(window)
    window.create_case()
    window.cases[0].set_trigger(0)
    destination = tmp_path / "result.csv"
    companion = speed_csv_path(destination)
    companion.write_bytes(b"previous speed CSV")
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getSaveFileName",
        lambda *_: (str(destination), "CSV 파일 (*.csv)"),
    )

    def choose(dialog):
        if selection is None:
            return QDialog.DialogCode.Rejected
        dialog.include_specification.setChecked(selection)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CsvExportDialog, "exec", choose)
    window.export_csv()
    if selection is None:
        assert not destination.exists()
        assert companion.read_bytes() == b"previous speed CSV"
    else:
        with companion.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["event_type"] == "trigger"
        assert row["frame_index"] == "0"
        assert row["video_file"] == "null"
        assert row["time_seconds"] == "null"
        assert companion.name in window.statusBar().currentMessage()
    window.close()
