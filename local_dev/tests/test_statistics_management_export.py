"""Statistics export defaults use management storage, not the video root."""

from __future__ import annotations

from pathlib import Path

import pytest

from investigator.storage import CSV_EXPORT_DIRECTORY, SYSTEMS_DIRECTORY
from investigator.widgets.statistics_panel import StatisticsPanel


@pytest.mark.parametrize("kind", ["csv", "pdf", "png"])
def test_statistics_export_dialog_defaults_to_management_directory(qtbot, tmp_path, monkeypatch, kind):
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    workspace = tmp_path / "사고 프로젝트"
    panel.workspace = workspace
    observed = []

    def save_dialog(parent, caption, initial, filter_text):
        observed.append((Path(initial), filter_text))
        assert Path(initial).parent.is_dir()
        return initial, filter_text

    exports = []
    monkeypatch.setattr("investigator.widgets.statistics_panel.QFileDialog.getSaveFileName", save_dialog)
    monkeypatch.setattr(panel, "export_to", lambda path, selected: exports.append((path, selected)))
    panel.export_dialog(kind)
    expected = workspace / (CSV_EXPORT_DIRECTORY if kind == "csv" else SYSTEMS_DIRECTORY) / f"statistics.{kind}"
    assert observed == [(expected, f"{kind.upper()} (*.{kind})")]
    assert exports == [(expected, kind)]
    assert panel.workspace == workspace


def test_statistics_export_still_allows_another_destination_and_adds_suffix(qtbot, tmp_path, monkeypatch):
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.workspace = tmp_path / "project"
    selected = tmp_path / "chosen elsewhere" / "analysis"
    monkeypatch.setattr("investigator.widgets.statistics_panel.QFileDialog.getSaveFileName", lambda *args: (str(selected), ""))
    exports = []
    monkeypatch.setattr(panel, "export_to", lambda path, kind: exports.append((path, kind)))
    panel.export_dialog("csv")
    assert exports == [(selected.with_suffix(".csv"), "csv")]


def test_statistics_export_directory_failure_warns_and_does_not_open_dialog(qtbot, tmp_path, monkeypatch):
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.workspace = tmp_path / "project"
    panel.workspace.mkdir()
    (panel.workspace / SYSTEMS_DIRECTORY).write_bytes(b"not a directory")
    warnings = []
    monkeypatch.setattr("investigator.widgets.statistics_panel.QMessageBox.warning", lambda parent, title, message: warnings.append((title, message)))
    monkeypatch.setattr("investigator.widgets.statistics_panel.QFileDialog.getSaveFileName", lambda *args: pytest.fail("Cannot open save dialog without a valid management directory"))
    monkeypatch.setattr(panel, "export_to", lambda *args: pytest.fail("Cannot export after directory failure"))
    panel.export_dialog("pdf")
    assert len(warnings) == 1
    assert warnings[0][0] == "통계 내보내기 오류"
    assert str(panel.workspace / SYSTEMS_DIRECTORY) in warnings[0][1]


def test_statistics_export_cancel_does_not_export(qtbot, tmp_path, monkeypatch):
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.workspace = tmp_path
    monkeypatch.setattr("investigator.widgets.statistics_panel.QFileDialog.getSaveFileName", lambda *args: ("", ""))
    monkeypatch.setattr(panel, "export_to", lambda *args: pytest.fail("Cancelled export"))
    panel.export_dialog("csv")


def test_statistics_export_title_retains_project_name(qtbot, tmp_path, monkeypatch):
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.workspace = tmp_path / "Actual project"
    captured = {}

    def export(path, headers, rows, **kwargs):
        captured.update(kwargs)
        return path

    monkeypatch.setattr("investigator.statistics_export.export_statistics_csv", export)
    panel.export_to(panel.workspace / SYSTEMS_DIRECTORY / "statistics.csv", "csv")
    assert "Actual project" in captured["title"]
    assert SYSTEMS_DIRECTORY not in captured["title"]
