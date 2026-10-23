"""Removing user-added form controls is staged and never targets schema controls."""

from copy import deepcopy

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from investigator.form_definition import FormDefinition, new_field
from investigator.schema import FIELD_BY_KEY
from investigator.widgets.form_designer import FormDesigner


def add_custom_tab(definition):
    first, second = new_field(), new_field()
    first["label"], second["label"] = "추가 항목 1", "추가 항목 2"
    tab = {
        "key": "tab_user_added",
        "title": "추가 조사",
        "short_title": "추가",
        "columns": 1,
        "fields": [first, second],
    }
    definition.tabs.append(tab)
    return tab, first, second


def select(dialog, tab_key, field_key=None):
    ti = next(i for i, tab in enumerate(dialog.definition.tabs) if tab["key"] == tab_key)
    item = dialog.tree.topLevelItem(ti)
    if field_key is not None:
        fi = next(
            i for i, field in enumerate(dialog.definition.tabs[ti]["fields"])
            if field["key"] == field_key
        )
        item = item.child(fi)
    dialog.tree.setCurrentItem(item)


def test_schema_tabs_and_fields_cannot_be_deleted_even_after_rename(qtbot, monkeypatch):
    definition = FormDefinition.default()
    definition.tabs[1]["title"] = "사용자 이름처럼 보이는 기본 탭"
    definition.tabs[1]["fields"][0]["label"] = "이름을 바꾼 기본 항목"
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    before = definition.to_dict()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_: pytest.fail("기본 조사 요소는 삭제 확인도 열지 않아야 합니다.")
    )

    for ti, tab in enumerate(dialog.definition.tabs):
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(ti))
        assert not dialog.remove_selected_button.isEnabled()
        selected_before = dialog.definition.to_dict()
        dialog.remove_selected()
        assert dialog.definition.to_dict() == selected_before
        for fi, field in enumerate(tab["fields"]):
            assert field["key"] in FIELD_BY_KEY
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(ti).child(fi))
            assert not dialog.remove_selected_button.isEnabled()
            selected_before = dialog.definition.to_dict()
            dialog.remove_selected()
            assert dialog.definition.to_dict() == selected_before

    assert definition.to_dict() == before


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Cancel])
def test_cancel_delete_keeps_pending_editor_and_staged_definition(qtbot, monkeypatch, answer):
    definition = FormDefinition.default()
    tab, first, _ = add_custom_tab(definition)
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    select(dialog, tab["key"], first["key"])
    dialog.field_label.setText("아직 적용하지 않은 편집")
    before = deepcopy(dialog.definition.tabs)
    selected = dialog._selected
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: prompts.append(args[2]) or answer)

    dialog.remove_selected_button.click()

    assert dialog.definition.tabs == before
    assert dialog._selected == selected
    assert dialog.field_label.text() == "아직 적용하지 않은 편집"
    assert len(prompts) == 1
    assert "CSV 출력에서 제외" in prompts[0]
    assert "저장된 조사 값은 보존" in prompts[0]
    assert "양식 적용 및 저장" in prompts[0]


@pytest.mark.parametrize("removed_index", [0, 1])
def test_delete_custom_field_selects_neighbor_without_overwriting_it(
    qtbot, monkeypatch, removed_index,
):
    original = FormDefinition.default()
    tab, first, second = add_custom_tab(original)
    fields = [first, second]
    removed, remaining = fields[removed_index], fields[1 - removed_index]
    dialog = FormDesigner(original)
    qtbot.addWidget(dialog)
    select(dialog, tab["key"], removed["key"])
    assert dialog.remove_selected_button.isEnabled()
    dialog.field_label.setText("삭제할 항목의 미저장 이름")
    dialog.column_name.setText("deleted_pending_column")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    dialog.remove_selected_button.click()

    assert dialog.definition.tabs[-1]["fields"] == [remaining]
    assert dialog.field_label.text() == remaining["label"]
    assert dialog.column_name.text() == remaining["csv_column"]
    assert dialog._selected == (len(dialog.definition.tabs) - 1, 0)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.definition.tabs[-1]["fields"] == [remaining]
    assert removed["csv_column"] not in dialog.definition.csv_headers()
    assert "deleted_pending_column" not in dialog.definition.csv_headers()
    assert original.tabs[-1]["fields"] == [first, second]


def test_delete_last_custom_field_selects_parent(qtbot, monkeypatch):
    definition = FormDefinition.default()
    # Keep a separate custom tab with one field so deleting its last field has
    # a parent-only selection; basic tabs must still protect their own fields.
    tab, first, _ = add_custom_tab(definition)
    tab["fields"] = [first]
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    select(dialog, tab["key"], first["key"])
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    dialog.remove_selected_button.click()

    assert dialog.definition.tabs[-1]["fields"] == []
    assert dialog._selected == (len(dialog.definition.tabs) - 1, None)
    assert dialog.pages.currentIndex() == 0
    assert dialog.remove_selected_button.isEnabled()
    dialog.definition.validate()


def test_custom_field_in_builtin_tab_is_deletable(qtbot, monkeypatch):
    definition = FormDefinition.default()
    custom = new_field()
    definition.tabs[1]["fields"].append(custom)
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    select(dialog, definition.tabs[1]["key"], custom["key"])
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    assert dialog.remove_selected_button.isEnabled()
    dialog.remove_selected_button.click()

    assert all(f["key"] != custom["key"] for f in dialog.definition.records())
    assert not dialog.remove_selected_button.isEnabled()
    dialog.definition.validate()


def test_delete_custom_tab_removes_its_controls_only_and_can_cancel_dialog(qtbot, monkeypatch):
    definition = FormDefinition.default()
    tab, first, second = add_custom_tab(definition)
    before = definition.to_dict()
    dialog = FormDesigner(definition)
    qtbot.addWidget(dialog)
    select(dialog, tab["key"])
    # A custom tab may be moved between basic tabs; deletion uses its key.
    dialog.move_selected(-1)
    assert dialog.remove_selected_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    dialog.remove_selected_button.click()

    assert all(t["key"] != tab["key"] for t in dialog.definition.tabs)
    assert all(f["key"] not in {first["key"], second["key"]} for f in dialog.definition.records())
    assert dialog._selected is not None
    assert not dialog.remove_selected_button.isEnabled()
    dialog.definition.validate()
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert definition.to_dict() == before
