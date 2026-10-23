from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from investigator.case_numbering import build_case_number_map
from investigator.main_window import MainWindow
from investigator.models import InvestigationCase
from investigator.report_export import ReportExportError
from investigator.widgets.case_list import CaseListPanel
from investigator.widgets.report_export_dialog import ReportExportDialog
from tests.helpers import valid_values


def case(number: str, *, completed: bool = False) -> InvestigationCase:
    result = InvestigationCase(videos=[f"{number}_1.mp4", f"{number}_2.mp4"])
    result.values["accident_classification"] = number
    result.status = "completed" if completed else "draft"
    return result


def test_report_selection_keeps_project_order_and_does_not_change_cases(qtbot) -> None:
    cases = [case(str(index), completed=index == 1) for index in range(3)]
    before = [entry.to_dict() for entry in cases]
    dialog = ReportExportDialog(cases, [cases[2].uid, cases[0].uid])
    qtbot.addWidget(dialog)

    assert dialog.selected_uids() == [cases[0].uid, cases[2].uid]
    assert dialog.count_label.text() == "3건 중 2건 선택"
    assert dialog.tree.topLevelItem(1).text(1) == "완료"
    assert dialog.tree.topLevelItem(0).text(2) == "2"
    assert dialog.export_button.isEnabled()
    assert [entry.to_dict() for entry in cases] == before


def test_report_selection_all_none_and_search_keep_hidden_checked_rows(qtbot) -> None:
    cases = [case("1234567890123"), case("9876543210123")]
    dialog = ReportExportDialog(cases, [cases[0].uid])
    qtbot.addWidget(dialog)
    dialog.search.setText("9876543210123_2.mp4")
    assert dialog.tree.topLevelItem(0).isHidden()
    assert not dialog.tree.topLevelItem(1).isHidden()
    assert dialog.selected_uids() == [cases[0].uid]

    dialog.select_all_button.click()
    assert dialog.selected_uids() == [entry.uid for entry in cases]
    dialog.clear_button.click()
    assert dialog.selected_uids() == []
    assert not dialog.export_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Rejected

    dialog.tree.topLevelItem(1).setCheckState(0, Qt.CheckState.Checked)
    assert dialog.export_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_sidebar_report_selection_maps_video_rows_to_distinct_parent_cases(qtbot) -> None:
    cases = [case("1"), case("2"), case("3")]
    panel = CaseListPanel()
    qtbot.addWidget(panel)
    panel.set_cases(cases)
    tree = panel.tree
    tree.clearSelection()
    tree.topLevelItem(2).setSelected(True)
    tree.topLevelItem(0).setSelected(True)
    tree.topLevelItem(0).child(0).setSelected(True)
    tree.topLevelItem(0).child(1).setSelected(True)
    assert panel.selected_case_uids() == [cases[0].uid, cases[2].uid]

    requested = []
    panel.report_export_requested.connect(lambda: requested.append(True))
    panel.report_export_button.click()
    assert requested == [True]


def prepare_window(qtbot, tmp_path: Path) -> MainWindow:
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    for _ in range(3):
        window.create_case()
    return window


def test_report_export_uses_selected_detached_current_data_without_saving(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window = prepare_window(qtbot, tmp_path)
    cases = window.cases
    cases[0].videos = [str(tmp_path / "clip.mp4")]
    window.case_list.set_cases(cases, window.current_uid)
    cases[0].values.update(valid_values())
    for item, number in zip(cases, ["2020011300003", "2020011300001", "2020011300002"], strict=True):
        item.values["accident_classification"] = number
    cases[0].values["driver_sex"] = "여성"
    cases[0].status = "completed"
    window._mark_case_dirty(cases[0])
    before = [entry.to_dict() for entry in cases]
    dirty = set(window._dirty_cases)
    tree = window.case_list.tree
    tree.clearSelection()
    tree.topLevelItem(0).child(0).setSelected(True)
    tree.topLevelItem(2).setSelected(True)
    destination = tmp_path / "reports"
    calls = []
    messages = []
    save_calls = []
    original_save = window._save

    def choose(dialog: ReportExportDialog) -> QDialog.DialogCode:
        assert dialog.selected_uids() == [cases[0].uid, cases[2].uid]
        return QDialog.DialogCode.Accepted

    def export(snapshots, folder, *, form_definition, report_number_by_uid):  # type: ignore[no-untyped-def]
        assert folder == destination
        assert form_definition is window.repository.form_definition
        assert [entry.uid for entry in snapshots] == [cases[0].uid, cases[2].uid]
        assert snapshots[0].values["driver_sex"] == "여성"
        assert snapshots[0] is not cases[0]
        assert report_number_by_uid == build_case_number_map(cases)
        assert report_number_by_uid[cases[0].uid] == "20200113_003"
        assert report_number_by_uid[cases[2].uid] == "20200113_002"
        calls.extend(snapshots)
        snapshots[0].status = "draft"
        snapshots[0].values["driver_sex"] = "남성"
        return [folder / f"{entry.uid}.docx" for entry in snapshots]

    def record_save(**kwargs):  # type: ignore[no-untyped-def]
        save_calls.append(kwargs)
        return original_save(**kwargs)

    monkeypatch.setattr(window, "_save", record_save)
    monkeypatch.setattr(ReportExportDialog, "exec", choose)
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getExistingDirectory",
        lambda *_args: str(destination),
    )
    monkeypatch.setattr("investigator.main_window.export_case_reports", export)
    monkeypatch.setattr(
        "investigator.main_window.QMessageBox.information",
        lambda *_args: messages.append(_args[-1]),
    )
    window.export_reports()

    assert len(calls) == 2
    assert save_calls == []
    assert [entry.to_dict() for entry in cases] == before
    assert window._dirty_cases == dirty
    assert messages == [f"사고 분석 보고서 2건을 저장했습니다: {destination}"]
    assert "2건" in window.statusBar().currentMessage()
    assert any(
        action.text() == "사고 분석 보고서 내보내기…"
        for action in window.workspace_menu.actions()[0].menu().actions()
    )


@pytest.mark.parametrize("cancel_at", ["selection", "folder"])
def test_report_export_cancel_does_not_export(
    qtbot, tmp_path: Path, monkeypatch, cancel_at: str
) -> None:
    window = prepare_window(qtbot, tmp_path)

    def choose(_dialog: ReportExportDialog) -> QDialog.DialogCode:
        return (QDialog.DialogCode.Rejected if cancel_at == "selection"
                else QDialog.DialogCode.Accepted)

    def folder(*_args) -> str:
        assert cancel_at == "folder"
        return ""

    def unexpected_export(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        pytest.fail("Cancelled report export must not create files")

    monkeypatch.setattr(ReportExportDialog, "exec", choose)
    monkeypatch.setattr("investigator.main_window.QFileDialog.getExistingDirectory", folder)
    monkeypatch.setattr("investigator.main_window.export_case_reports", unexpected_export)
    window.export_reports()


def test_report_export_busy_and_empty_project(qtbot, tmp_path: Path, monkeypatch) -> None:
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    messages = []
    monkeypatch.setattr(
        "investigator.main_window.QMessageBox.information",
        lambda *_args: messages.append(_args[-1]),
    )
    assert not window.case_list.report_export_button.isEnabled()
    assert not window.report_export_action.isEnabled()
    window._commit_in_progress = True
    window.export_reports()
    assert not messages
    window._commit_in_progress = False
    window.export_reports()
    assert messages == ["보고서로 만들 사고가 없습니다."]


@pytest.mark.parametrize("allow_edit", [False, True])
def test_report_commands_are_disabled_during_disk_operations(
    qtbot, tmp_path: Path, monkeypatch, allow_edit: bool
) -> None:
    window = prepare_window(qtbot, tmp_path)
    assert window.case_list.report_export_button.isEnabled()
    assert window.report_export_action.isEnabled()

    def run(operation, *args, **_kwargs):  # type: ignore[no-untyped-def]
        assert window._commit_in_progress
        assert not window.case_list.report_export_button.isEnabled()
        assert not window.report_export_action.isEnabled()
        return operation(*args)

    with monkeypatch.context() as patch:
        patch.setattr(window._storage_worker, "run", run)
        assert window._disk_operation(lambda: 42, allow_edit=allow_edit) == 42
    assert window.case_list.report_export_button.isEnabled()
    assert window.report_export_action.isEnabled()


def test_report_export_failure_is_reported(qtbot, tmp_path: Path, monkeypatch) -> None:
    window = prepare_window(qtbot, tmp_path)
    messages = []
    monkeypatch.setattr(ReportExportDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getExistingDirectory", lambda *_args: str(tmp_path)
    )

    def fail(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise ReportExportError("템플릿을 읽을 수 없습니다.")

    monkeypatch.setattr("investigator.main_window.export_case_reports", fail)
    monkeypatch.setattr(
        "investigator.main_window.QMessageBox.critical",
        lambda *_args: messages.append(_args[-1]),
    )
    window.export_reports()
    assert messages == ["템플릿을 읽을 수 없습니다."]
    assert "실패" in window.statusBar().currentMessage()


def test_report_export_creates_word_files_through_real_worker(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window = prepare_window(qtbot, tmp_path)
    destination = tmp_path / "reports"
    destination.mkdir()
    selected = [window.cases[0], window.cases[2]]
    before = [entry.to_dict() for entry in window.cases]

    def choose(dialog: ReportExportDialog) -> QDialog.DialogCode:
        dialog.clear_button.click()
        for index in (0, 2):
            dialog.tree.topLevelItem(index).setCheckState(0, Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReportExportDialog, "exec", choose)
    monkeypatch.setattr(
        "investigator.main_window.QFileDialog.getExistingDirectory",
        lambda *_args: str(destination),
    )
    monkeypatch.setattr("investigator.main_window.QMessageBox.information", lambda *_args: None)
    window.export_reports()

    reports = list(destination.glob("*.docx"))
    assert len(reports) == 2
    for exported_case in selected:
        path = next(report for report in reports if exported_case.display_id in report.name)
        with ZipFile(path) as document:
            content = document.read("word/document.xml").decode("utf-8")
        text = "".join(ElementTree.fromstring(content).itertext())
        assert str(exported_case.values["accident_classification"]) in text
        assert str(exported_case.values["institution"]) in text
        assert "{{" not in content
    assert [entry.to_dict() for entry in window.cases] == before
