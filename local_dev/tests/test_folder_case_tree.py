"""Workspace folder grouping retains accident navigation and display order."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QMimeData, QPointF, Qt
from PySide6.QtGui import QDropEvent

from investigator.models import InvestigationCase
from investigator.theme import PRIMARY
from investigator.widgets.case_list import KIND_ROLE, CaseListPanel


def _video(root: Path, relative: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return str(path)


def _case(number: int, *videos: str) -> InvestigationCase:
    return InvestigationCase(
        values={"Institution": "samsung", "accident_classification": f"{number:013d}"},
        videos=list(videos),
    )


@pytest.fixture
def panel(qtbot, tmp_path):
    widget = CaseListPanel()
    widget.set_workspace(tmp_path)
    widget.tree.setAnimated(False)
    widget.resize(380, 750)
    qtbot.addWidget(widget)
    return widget


def test_workspace_groups_folder_case_video_and_refresh_replaces_new_case(panel, tmp_path, qtbot):
    case = _case(1, _video(tmp_path, "영상폴더/2021년/전방.mp4"))
    panel.set_cases([case])
    folder = panel.tree.topLevelItem(0)
    row = panel.case_item(case.uid)
    assert folder.text(0) == "영상폴더/2021년"
    assert folder.data(0, KIND_ROLE) == "folder"
    assert row.parent() is folder
    assert row.data(0, KIND_ROLE) == "case"
    assert row.child(0).data(0, KIND_ROLE) == "video"
    assert row.child(0).text(0) == "전방.mp4"
    assert panel.refresh_button.text() == "목록 새로고침"
    assert not hasattr(panel, "new_button")
    with qtbot.waitSignal(panel.refresh_requested):
        qtbot.mouseClick(panel.refresh_button, Qt.MouseButton.LeftButton)


def test_common_folder_has_one_accident_despite_multiple_video_subfolders(panel, tmp_path):
    case = _case(
        1,
        _video(tmp_path, "영상폴더/2021년/전방/영상.mp4"),
        _video(tmp_path, "영상폴더/2021년/후방/영상.mp4"),
    )
    panel.set_cases([case])
    assert panel.tree.topLevelItemCount() == 1
    assert panel.tree.topLevelItem(0).text(0) == "영상폴더/2021년"
    assert panel.tree.case_items() == [panel.case_item(case.uid)]
    assert panel.case_item(case.uid).childCount() == 2


def test_missing_legacy_path_does_not_override_actual_folder(panel, tmp_path):
    case = _case(1, str(tmp_path / "이전폴더/전방.mp4"), _video(tmp_path, "영상폴더/2023년/전방.mp4"))
    panel.set_cases([case])
    assert panel.tree.topLevelItem(0).text(0) == "영상폴더/2023년"


def test_empty_and_external_video_cases_have_clear_folders(panel, tmp_path):
    external = tmp_path.parent / f"{tmp_path.name}-external"
    case, empty = _case(1, _video(external, "외부.mp4")), _case(2)
    panel.set_cases([case, empty])
    assert panel.case_item(case.uid).parent().text(0) == external.name
    assert panel.case_item(case.uid).parent().toolTip(0) == str(external)
    assert panel.case_item(empty.uid).parent().text(0) == "영상 없음"


def test_folder_is_not_accident_selection_delete_or_drag_target(panel, tmp_path):
    case = _case(1, _video(tmp_path, "영상폴더/전방.mp4"))
    panel.set_cases([case])
    folder = panel.tree.topLevelItem(0)
    assert not folder.flags() & Qt.ItemFlag.ItemIsSelectable
    assert not folder.flags() & Qt.ItemFlag.ItemIsDragEnabled
    selected, deleted = [], []
    panel.case_selected.connect(selected.append)
    panel.delete_case_requested.connect(deleted.append)
    panel.tree.setCurrentItem(folder)
    panel._request_selected_delete()
    assert panel.selected_case_uids() == []
    assert panel.tree.selected_cases() == []
    assert not panel.delete_button.isEnabled()
    assert not selected and not deleted


def test_nested_case_and_video_emit_distinct_navigation_and_delete(panel, tmp_path):
    case = _case(1, _video(tmp_path, "영상폴더/전방.mp4"))
    panel.set_cases([case])
    row = panel.case_item(case.uid)
    cases, videos, deleted = [], [], []
    panel.case_selected.connect(cases.append)
    panel.video_selected.connect(lambda *args: videos.append(args))
    panel.delete_video_requested.connect(lambda *args: deleted.append(args))
    panel.tree.setCurrentItem(row)
    assert cases == [case.uid]
    assert panel.delete_button.text() == "사고 삭제"
    panel.tree.setCurrentItem(row.child(0))
    assert videos == [(case.uid, 0)]
    assert panel.delete_button.text() == "영상 삭제"
    panel._request_selected_delete()
    assert deleted == [(case.uid, 0)]


def test_selection_maps_video_and_cases_without_folder_uids(panel, tmp_path):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in (1, 2)]
    panel.set_cases(cases, cases[0].uid, 0)
    panel.case_item(cases[1].uid).setSelected(True)
    assert panel.selected_case_uids() == [case.uid for case in cases]
    assert panel.tree.selected_cases() == [panel.case_item(cases[1].uid)]


def test_search_hides_empty_folders_and_accepts_folder_text(panel, tmp_path):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{year}년/{i}.mp4")) for i, year in [(1, 2021), (2, 2023)]]
    panel.set_cases(cases)
    first, second = (panel.case_item(case.uid) for case in cases)
    panel.search.setText("2023년")
    assert first.isHidden() and first.parent().isHidden()
    assert not second.isHidden() and not second.parent().isHidden()
    assert not second.child(0).isHidden()
    panel.search.setText("1.mp4")
    assert not first.isHidden() and not first.parent().isHidden()
    assert second.isHidden() and second.parent().isHidden()


def test_folder_and_case_expansion_remain_independent_across_rebuild(panel, tmp_path):
    first, second = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in (1, 2)]
    panel.set_cases([first, second])
    panel.case_item(first.uid).setExpanded(False)
    panel.case_item(second.uid).parent().setExpanded(False)
    panel.set_cases([first, second])
    assert not panel.case_item(first.uid).isExpanded()
    assert panel.case_item(first.uid).parent().isExpanded()
    assert panel.case_item(second.uid).isExpanded()
    assert not panel.case_item(second.uid).parent().isExpanded()


def test_capture_restore_after_sort_preserves_nested_targets_and_folder_state(panel, tmp_path):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}年/{i}.mp4")) for i in (3, 1, 2)]
    panel.set_cases(cases, cases[0].uid, 0)
    panel.case_item(cases[2].uid).setSelected(True)
    panel.case_item(cases[1].uid).parent().setExpanded(False)
    state = panel.capture_view_state()
    panel.set_cases(sorted(cases, key=lambda case: case.display_id), cases[0].uid)
    panel.restore_view_state(state)
    assert panel.capture_view_state() == state
    assert panel.tree.currentItem() is panel.case_item(cases[0].uid).child(0)


def test_incremental_merge_preserves_rows_and_reparents_only_changed_video_folder(panel, tmp_path):
    first = _case(1, _video(tmp_path, "영상폴더/2021年/1.mp4"))
    second = _case(2, _video(tmp_path, "영상폴더/2022年/2.mp4"))
    panel.set_cases([first, second], first.uid, 0)
    first_row, second_row = panel.case_item(first.uid), panel.case_item(second.uid)
    old_folder = first_row.parent()
    second_row.setSelected(True)
    first.videos = [_video(tmp_path, "영상폴더/2023年/1.mp4")]
    third = _case(3, _video(tmp_path, "영상폴더/2023年/3.mp4"))
    panel.merge_cases([first, second, third], first.uid)
    assert panel.case_item(first.uid) is first_row
    assert panel.case_item(second.uid) is second_row
    assert first_row.parent().text(0) == "영상폴더/2023年"
    assert first_row.parent() is panel.case_item(third.uid).parent()
    assert panel.tree.indexOfTopLevelItem(old_folder) == -1
    assert second_row.isSelected()
    panel.merge_cases([second], second.uid)
    assert panel.tree.topLevelItemCount() == 1
    assert panel.tree.case_items() == [second_row]


def test_reorder_retains_item_identity_and_filesystem_group(panel, tmp_path):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i % 2}년/{i}.mp4")) for i in range(4)]
    panel.set_cases(cases, cases[1].uid)
    rows = {case.uid: panel.case_item(case.uid) for case in cases}
    parents = {uid: row.parent() for uid, row in rows.items()}
    rows[cases[3].uid].setSelected(True)
    panel.move_cases([cases[1].uid, cases[3].uid], 0)
    assert [case.uid for case in panel._cases] == [cases[i].uid for i in (1, 3, 0, 2)]
    assert panel.tree.selected_cases() == [rows[cases[i].uid] for i in (1, 3)]
    assert panel.tree.currentItem() is rows[cases[1].uid]
    for uid, row in rows.items():
        assert panel.case_item(uid) is row
        assert row.parent() is parents[uid]


def test_nested_drop_uses_global_flat_accident_destination(panel, tmp_path, qtbot):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i % 2}년/{i}.mp4")) for i in range(4)]
    panel.set_cases(cases, cases[1].uid)
    panel.show()
    qtbot.wait(10)
    target = panel.case_item(cases[2].uid)
    point = panel.tree.visualItemRect(target).topRight()
    event = QDropEvent(QPointF(point), Qt.DropAction.MoveAction, QMimeData(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    with qtbot.waitSignal(panel.case_reordered) as signal:
        panel.tree.dropEvent(event)
    assert event.isAccepted()
    assert signal.args == [cases[1].uid, 1]  # Before case 2, after removing source case 1.


def test_only_one_playing_video_is_blue_across_all_groups(panel, tmp_path):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in (1, 2)]
    panel.set_cases(cases, cases[0].uid)
    first, second = (panel.case_item(case.uid).child(0) for case in cases)
    panel.set_active_video(cases[1].uid)
    assert first.foreground(0).color().name() != PRIMARY.casefold()
    assert second.foreground(0).color().name() == PRIMARY.casefold()
    assert panel.tree.currentItem() is panel.case_item(cases[0].uid)


def test_hidden_selection_survives_group_reorder(panel, tmp_path):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in (1, 2)]
    panel.set_cases(cases)
    first, second = [panel.case_item(case.uid) for case in cases]
    panel.tree.setCurrentItem(first, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
    second.setSelected(True)
    panel.search.setText("1.mp4")
    panel.move_cases([cases[1].uid], 0)
    assert second.isSelected()
    panel.search.clear()
    assert panel.selected_case_uids() == [cases[1].uid]


def test_ctrl_shift_case_selection_spans_folders_without_opening_every_case(panel, tmp_path, qtbot):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in range(4)]
    panel.set_cases(cases)
    panel.show()
    qtbot.wait(10)
    opened = []
    panel.case_selected.connect(opened.append)

    def click(index, modifier=Qt.KeyboardModifier.NoModifier):
        row = panel.case_item(cases[index].uid)
        qtbot.mouseClick(
            panel.tree.viewport(), Qt.MouseButton.LeftButton, modifier,
            panel.tree.visualItemRect(row).center(),
        )

    click(0)
    click(2, Qt.KeyboardModifier.ControlModifier)
    assert panel.tree.selected_cases() == [panel.case_item(cases[i].uid) for i in (0, 2)]
    click(1)
    click(3, Qt.KeyboardModifier.ShiftModifier)
    assert panel.tree.selected_cases() == [panel.case_item(cases[i].uid) for i in (1, 2, 3)]
    assert opened == [cases[0].uid, cases[1].uid]


def test_rebuild_cancels_deferred_nested_drop(panel, tmp_path, qtbot):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in range(3)]
    panel.set_cases(cases, cases[0].uid)
    panel.show()
    qtbot.wait(10)
    target = panel.case_item(cases[2].uid)
    event = QDropEvent(
        QPointF(panel.tree.visualItemRect(target).bottomRight()), Qt.DropAction.MoveAction,
        QMimeData(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    drops = []
    panel.case_reordered.connect(lambda *args: drops.append(args))
    panel.tree.dropEvent(event)
    assert event.isAccepted()
    panel.set_cases(cases)
    qtbot.wait(10)
    assert not drops


def test_field_edit_does_not_recheck_disk_or_rebuild_other_case_rows(panel, tmp_path, monkeypatch):
    cases = [_case(i, _video(tmp_path, f"영상폴더/{i}년/{i}.mp4")) for i in range(20)]
    panel.set_cases(cases)
    calls = []
    original_filter = panel._filter_item

    def filter_one(item, needle):
        calls.append(item)
        original_filter(item, needle)

    def unexpected(*args):
        raise AssertionError("An ordinary field edit must not inspect video folders.")

    monkeypatch.setattr(panel, "_folder_details", unexpected)
    monkeypatch.setattr(panel, "_filter_item", filter_one)
    row = panel.case_item(cases[7].uid)
    cases[7].values["driver_sex"] = "남성"
    assert panel.update_case(cases[7])
    assert calls == [row]
