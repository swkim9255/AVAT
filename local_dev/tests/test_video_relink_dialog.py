from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from investigator.models import InvestigationCase
from investigator.widgets.video_relink_dialog import VideoRelinkDialog


def make_video(root: Path, relative: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return str(path.resolve())


def dialog(qtbot, tmp_path, cases, root):
    result = VideoRelinkDialog(cases, tmp_path)
    qtbot.addWidget(result)
    result.new_root_edit.setText(str(root))
    result.refresh()
    return result


def test_dialog_ambiguous_repeated_camera_names_start_unselected(qtbot, tmp_path):
    root = tmp_path / "videos"
    first = make_video(root, "21년 영상/영상1/전방영상.mp4")
    second = make_video(root, "22년 영상/영상1/전방영상.mp4")
    case = InvestigationCase(videos=[str(tmp_path / "old/영상1/전방영상.mp4")])
    widget = dialog(qtbot, tmp_path, [case], root)
    assert widget.table.rowCount() == 1
    assert not widget.apply_button.isEnabled()
    assert widget.selected_mapping() == {}
    combo = widget._combos[0]
    assert {combo.itemData(index) for index in range(1, combo.count())} == {first, second}
    assert "직접 선택" in widget.table.item(0, 3).text()
    widget.reject()


def test_dialog_filename_only_never_auto_selected(qtbot, tmp_path):
    root = tmp_path / "videos"
    make_video(root, "unrelated/front.mp4")
    case = InvestigationCase(videos=[str(tmp_path / "old/original/front.mp4")])
    widget = dialog(qtbot, tmp_path, [case], root)
    assert widget._combos[0].currentIndex() == 0
    assert widget.selected_mapping() == {}
    widget.reject()


def test_explicit_old_root_matches_direct_child(qtbot, tmp_path):
    root = tmp_path / "videos"
    new = make_video(root, "전방영상.mp4")
    old_root = tmp_path / "previous"
    old = str(old_root / "전방영상.mp4")
    case = InvestigationCase(videos=[old])
    widget = dialog(qtbot, tmp_path, [case], root)
    widget.old_root_edit.setText(str(old_root))
    widget.refresh()
    assert widget.selected_mapping() == {old: new}
    assert widget.apply_button.isEnabled()
    widget.accept()
    assert widget.result() == QDialog.DialogCode.Accepted
    assert case.videos == [old]


def test_manual_file_can_be_outside_scanned_root(qtbot, tmp_path, monkeypatch):
    root = tmp_path / "videos"
    root.mkdir()
    outside = make_video(tmp_path, "other folder/camera.mp4")
    old = str(tmp_path / "previous/front.mp4")
    case = InvestigationCase(videos=[old])
    widget = dialog(qtbot, tmp_path, [case], root)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (outside, ""))
    widget._choose_file(0)
    assert widget.selected_mapping() == {old: outside}
    assert widget.apply_button.isEnabled()
    assert "직접 지정" in widget.table.item(0, 3).text()
    widget.reject()
    assert case.videos == [old]


def test_manual_file_must_exist_and_have_supported_extension(qtbot, tmp_path, monkeypatch):
    root = tmp_path / "videos"
    root.mkdir()
    old = str(tmp_path / "previous/front.mp4")
    widget = dialog(qtbot, tmp_path, [InvestigationCase(videos=[old])], root)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(tmp_path / "gone.mp4"), ""))
    widget._choose_file(0)
    assert warnings
    assert widget.selected_mapping() == {}
    widget.reject()


def test_partial_selection_and_cancel_never_mutates_case(qtbot, tmp_path):
    root = tmp_path / "videos"
    new = make_video(root, "case1/front.mp4")
    old1 = str(tmp_path / "previous/case1/front.mp4")
    old2 = str(tmp_path / "previous/case2/rear.mp4")
    case = InvestigationCase(videos=[old1, old2])
    case.status = "completed"
    case.markers_by_video = {old1: {"trigger_frame": 5}}
    before = copy.deepcopy(case)
    widget = dialog(qtbot, tmp_path, [case], root)
    assert widget.selected_mapping() == {old1: new}
    assert "미선택 1개" in widget.count_label.text()
    assert widget.apply_button.isEnabled()
    widget.reject()
    assert widget.result() == QDialog.DialogCode.Rejected
    assert case == before


def test_partial_selection_accepts_without_mutating_unselected_or_live_data(qtbot, tmp_path):
    root = tmp_path / "videos"
    new = make_video(root, "case1/front.mp4")
    old1 = str(tmp_path / "previous/case1/front.mp4")
    old2 = str(tmp_path / "previous/case2/rear.mp4")
    case = InvestigationCase(videos=[old1, old2])
    widget = dialog(qtbot, tmp_path, [case], root)
    widget.accept()
    assert widget.result() == QDialog.DialogCode.Accepted
    assert widget.selected_mapping() == {old1: new}
    assert case.videos == [old1, old2]


def test_duplicate_explicit_targets_block_accept_without_mutation(qtbot, tmp_path, monkeypatch):
    root = tmp_path / "videos"
    new = make_video(root, "unrelated/front.mp4")
    old1 = str(tmp_path / "previous/case1/front.mp4")
    old2 = str(tmp_path / "previous/case2/front.mp4")
    cases = [InvestigationCase(videos=[old1]), InvestigationCase(videos=[old2])]
    widget = dialog(qtbot, tmp_path, cases, root)
    for combo in widget._combos:
        combo.setCurrentIndex(combo.findData(new))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    widget.accept()
    assert warnings and "같은 새 영상" in warnings[0]
    assert widget.result() != QDialog.DialogCode.Accepted
    assert cases[0].videos == [old1]
    assert cases[1].videos == [old2]
    widget.reject()


def test_changing_folder_requires_rescan_even_after_combo_selection(qtbot, tmp_path):
    root = tmp_path / "videos"
    new = make_video(root, "case1/front.mp4")
    old = str(tmp_path / "previous/case1/front.mp4")
    widget = dialog(qtbot, tmp_path, [InvestigationCase(videos=[old])], root)
    assert widget.apply_button.isEnabled()
    widget.new_root_edit.setText(str(tmp_path / "different"))
    combo = widget._combos[0]
    combo.setCurrentIndex(0)
    combo.setCurrentIndex(combo.findData(new))
    assert not widget.apply_button.isEnabled()
    widget.reject()


def test_narrow_dialog_new_path_combo_does_not_force_huge_minimum(qtbot, tmp_path):
    root = tmp_path / "videos"
    make_video(root, "long directory name/another directory/case1/front.mp4")
    old = str(tmp_path / "previous/case1/front.mp4")
    widget = dialog(qtbot, tmp_path, [InvestigationCase(videos=[old])], root)
    widget.resize(660, 420)
    widget.show()
    qtbot.waitExposed(widget)
    assert widget._combos[0].minimumWidth() == 0
    assert widget.minimumSizeHint().width() < 1000
    assert widget.width() <= 700
    assert widget.table.item(0, 1).toolTip() == old
    widget.reject()
