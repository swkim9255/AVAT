from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QMimeData, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QDropEvent, QPalette, QValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QToolButton,
)

from investigator.main_window import MainWindow
from investigator.schema import INVALID_FORMAT_MESSAGE, MISSING_VALUE_MESSAGE
from investigator.theme import BORDER, DANGER, PRIMARY, apply_theme
from tests.helpers import valid_values


class _Capture:
    def __init__(self, *, opened: bool, total_frames: int = 1) -> None:
        self.opened = opened
        self.total_frames = total_frames
        self.position = 0
        self.frame = np.zeros((4, 6, 3), dtype=np.uint8)

    def isOpened(self) -> bool:  # noqa: N802
        return self.opened

    def read(self):  # type: ignore[no-untyped-def]
        if not self.opened or self.position >= self.total_frames:
            return False, None
        self.position += 1
        return True, self.frame.copy()

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FPS:
            return 20.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.total_frames)
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        if prop != cv2.CAP_PROP_POS_FRAMES:
            return False
        self.position = int(value)
        return True

    def release(self) -> None:
        pass


def test_time_shortcuts_seek_one_second(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QKeyEvent

    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"fake")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=120)
    assert window.video_controller.open_video(path)
    assert window.video_controller.seek(40)
    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_J, Qt.KeyboardModifier.NoModifier))
    assert window.video_controller.current_frame == 20
    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_L, Qt.KeyboardModifier.NoModifier))
    assert window.video_controller.current_frame == 40


def test_pending_seek_markers_use_displayed_image_frame(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    path = tmp_path / "manual.mp4"
    path.write_bytes(b"fake")
    window.video_controller._capture_factory = lambda _: _Capture(opened=True, total_frames=120)
    window.attach_videos([str(path)])
    assert window.video_controller.seek(25)
    window.video_controller._seek_target = 75
    window.video_controller.seek_requested_position.emit(75)
    window.set_trigger_marker()
    window.set_collision_marker()
    window.toggle_speed_point()
    assert window.current_case.trigger_frame == 25
    assert window.current_case.collision_frame == 25
    assert [point.frame for point in window.current_case.speed_points] == [25]
    assert window.playback.timeline.value() == 75
    assert window.playback.frame_input.value() == 76


def test_main_window_has_requested_structure(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    application = QApplication.instance()
    assert isinstance(application, QApplication)
    application.setStyle("Fusion")
    apply_theme(application)
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.resize(1180, 760)
    window.show()
    qtbot.wait(10)
    assert window.cases == []
    assert window.current_case is None
    assert window.case_list.tree.topLevelItemCount() == 0
    assert window.case_list.count_label.text() == "0/0 완료"
    assert not window.investigation.isEnabled()

    window.create_case()

    assert window.case_list.tree.dragDropMode() is QAbstractItemView.DragDropMode.InternalMove

    assert window.splitter.count() == 3
    assert window.case_list.tree.columnWidth(1) <= 45
    assert window.splitter.widget(1) is window.video_workspace
    assert window.playback.parentWidget() is window.video_workspace
    assert window.video_workspace.layout().indexOf(window.playback) >= 0
    assert window.investigation.tabs.count() == 9
    assert window.investigation.tabs.tabText(8) == "비고"
    tab_bar = window.investigation.tabs.tabBar()
    assert not tab_bar.usesScrollButtons()
    assert tab_bar.tabRect(tab_bar.count() - 1).right() <= tab_bar.width()
    assert window.investigation.editors["institution"].isVisible()
    assert window.investigation.editors["accident_classification"].isVisible()
    assert window.investigation.editors["accident_classification"].line_edit.maxLength() == 13
    assert window.current_case is not None
    assert window.current_case.display_id == "SAMSUNG-001"
    institution = window.investigation.editors["institution"]
    assert institution.value() == "samsung"
    assert institution.control.currentText() == "samsung"
    assert institution.required_mark.isHidden()
    assert window.investigation.editors["accident_classification"].required_mark.isHidden()
    remarks = window.investigation.editors["remarks"]
    assert isinstance(remarks.control, QTextEdit)
    assert not remarks.control.acceptRichText()
    long_note = "사고 추가정보\n" + ("가나다" * 20_000)
    remarks.control.setPlainText(long_note)
    assert remarks.value() == long_note
    assert window.current_case.values["remarks"] == long_note
    driver_sex = window.investigation.editors["driver_sex"]
    assert driver_sex.required_mark.isVisible()
    driver_age = window.investigation.editors["driver_age"]
    assert isinstance(driver_age.control, QComboBox)
    assert driver_age.control.count() == 17
    assert driver_age.control.itemText(1) == "20-24세"
    assert driver_age.control.itemText(16) == "95-99세"
    assert not [
        label
        for label in window.investigation.findChildren(QLabel)
        if label.text() == "식별 구분 · 항상 표시" or label.objectName() == "fieldHelp"
    ]
    assert window.playback.play_button.text() == ""
    assert window.playback.collision_button.text() == "충돌 지정"
    assert window.playback.collision_button.toolTip() == "현재 프레임의 충돌 마커 지정/해제"
    assert window.playback.play_button.size() == QSize(42, 42)
    assert window.playback.play_button.iconSize() == QSize(26, 26)
    for button in (
        window.playback.back_1s,
        window.playback.back_10f,
        window.playback.back_1f,
        window.playback.next_1f,
        window.playback.next_10f,
        window.playback.next_1s,
    ):
        assert button.text() == ""
        assert button.size() == QSize(34, 34)
        assert button.iconSize() == QSize(20, 20)
        assert button.toolTip()
        assert button.accessibleName() == button.toolTip()
    assert window.investigation.save_button.isVisible()
    assert window.investigation.complete_button.isVisible()
    assert window.investigation.next_button.isVisible()
    assert not hasattr(window.playback, "save_button")
    assert window.investigation.next_button.width() > window.investigation.save_button.width()
    assert not any(
        button.objectName() == "importVideoFolderButton"
        for button in window.case_list.findChildren(type(window.case_list.new_button))
    )
    assert not any(
        action.text() == "영상 폴더 불러오기…"
        for action in window.menuBar().actions()[0].menu().actions()
    )
    date_editor = window.investigation.editors["incident_date"]
    day_editor = window.investigation.editors["incident_day"]
    assert date_editor.calendar_button.objectName() == "incidentDateCalendarButton"
    assert date_editor.calendar_button.toolTip() == "사고일자 선택"
    assert date_editor.calendar_widget.firstDayOfWeek() is Qt.DayOfWeek.Sunday
    assert (
        date_editor.calendar_widget.verticalHeaderFormat()
        is date_editor.calendar_widget.VerticalHeaderFormat.NoVerticalHeader
    )
    assert (
        date_editor.calendar_widget.palette().color(QPalette.ColorRole.Highlight).name()
        == BORDER.casefold()
    )
    assert date_editor.calendar_widget.headerTextFormat().background().color().name() == "#f8fafc"
    assert date_editor.parentWidget() is day_editor.parentWidget()
    environment_layout = date_editor.parentWidget().layout()
    assert environment_layout.indexOf(date_editor) + 1 == environment_layout.indexOf(day_editor)
    assert isinstance(day_editor.control, QLineEdit)
    assert day_editor.line_edit.isReadOnly()
    assert day_editor.line_edit.placeholderText() == "사고일자 선택 시 자동 계산"
    assert "자동으로 계산" in day_editor.line_edit.toolTip()
    assert "background: #F1F5F9" in day_editor.line_edit.styleSheet()

    mileage_editor = window.investigation.editors["vehicle_mileage"]
    mileage_validator = mileage_editor.line_edit.validator()
    assert mileage_validator is not None
    long_mileage = "123456789012345678901234567890"
    state, _, _ = mileage_validator.validate(long_mileage, len(long_mileage))
    assert state is QValidator.State.Acceptable
    state, _, _ = mileage_validator.validate("123km", 5)
    assert state is QValidator.State.Invalid

    trigger_grid = window.investigation.section_field_grids["trigger"]
    assert trigger_grid.count() == 11
    assert trigger_grid.getItemPosition(0) == (0, 0, 1, 1)
    assert trigger_grid.getItemPosition(1) == (0, 1, 1, 1)
    assert trigger_grid.getItemPosition(10) == (5, 0, 1, 1)

    speed_editor = window.investigation.editors["trigger_vehicle_speed"]
    assert [speed_editor.control.itemText(index) for index in range(1, 6)] == [
        "정차(0kph)",
        "크립(0-10kph)",
        "저속(10-30kph)",
        "중속(30-60kph)",
        "고속(60kph이상)",
    ]

    obstacle_exists = window.investigation.editors["obstacle_exists"]
    obstacle_exists.set_value("무")
    window.investigation._update_conditional_states(
        {name: editor.value() for name, editor in window.investigation.editors.items()}
    )
    for key in ("obstacle_dist_source", "obstacle_dist_value", "obstacle_dist_range"):
        assert window.investigation.editors[key].isEnabled()

    damage_unknown = window.investigation.editors["incident_damage_unknown"].control
    assert isinstance(damage_unknown, QComboBox)
    damage_unknown.setCurrentIndex(damage_unknown.findData("확인 가능"))
    minor_grades = window.investigation.editors["incident_minor_injury_grades"]
    minor_representative = window.investigation.editors[
        "incident_minor_representative_injury_grade"
    ]
    severe_grades = window.investigation.editors["incident_severe_injury_grades"]
    severe_representative = window.investigation.editors[
        "incident_severe_representative_injury_grade"
    ]
    for representative in (minor_representative, severe_representative):
        assert representative.line_edit.isReadOnly()
        assert representative.line_edit.placeholderText() == ""
        assert representative.line_edit.toolTip() == ""

    assert isinstance(minor_grades.line_edit, QLineEdit)
    minor_grades.line_edit.setText("14, 13, 12")
    minor_grades.line_edit.editingFinished.emit()
    assert minor_grades.value() == "12, 13, 14"
    assert minor_representative.value() == "12"
    assert window.investigation.editors["incident_minor_injury"].value() == "3"
    assert window.investigation.editors["incident_minor_injury"].line_edit.isReadOnly()

    severe_grades.line_edit.setText("11, 7, 1")
    severe_grades.line_edit.editingFinished.emit()
    assert severe_grades.value() == "1, 7, 11"
    assert severe_representative.value() == "1"
    assert window.investigation.editors["incident_severe_injury"].value() == "3"
    assert window.investigation.editors["incident_severe_injury"].line_edit.isReadOnly()
    assert window.current_case.values["incident_minor_injury_grades"] == "12, 13, 14"
    assert window.current_case.values["incident_severe_injury_grades"] == "1, 7, 11"

    left_edge = window.playback.back_1s.mapTo(window.playback, QPoint(0, 0)).x()
    right_edge = window.playback.next_1s.mapTo(
        window.playback, QPoint(window.playback.next_1s.width(), 0)
    ).x()
    assert abs(left_edge - (window.playback.width() - right_edge)) <= 2

    search_buttons = window.case_list.search.findChildren(QToolButton)
    assert len(search_buttons) == 1
    assert (
        abs(search_buttons[0].geometry().center().y() - window.case_list.search.rect().center().y())
        <= 1
    )
    first_case_item = window.case_list.tree.topLevelItem(0)
    assert first_case_item.flags() & Qt.ItemFlag.ItemIsDragEnabled
    assert first_case_item.foreground(1).color().name() == DANGER.casefold()

    institution.control.setEditText("NFS")
    assert institution.control.currentText() == "nfs"
    assert institution.value() == "nfs"
    assert window.current_case.values["institution"] == "nfs"

    driver_sex.control.setCurrentIndex(1)
    assert driver_sex.required_mark.isHidden()
    driver_sex.control.setCurrentIndex(0)
    assert driver_sex.required_mark.isVisible()

    manufacturer = window.investigation.editors["vehicle_manufacturer"]
    assert isinstance(manufacturer.control, QComboBox)
    assert manufacturer.control.count() == 28
    completer = manufacturer.control.completer()
    assert completer.completionMode() is QCompleter.CompletionMode.PopupCompletion
    assert completer.caseSensitivity() is Qt.CaseSensitivity.CaseInsensitive
    assert completer.filterMode() == Qt.MatchFlag.MatchContains
    assert manufacturer.control.insertPolicy() is QComboBox.InsertPolicy.NoInsert
    completer.setCompletionPrefix("land")
    assert completer.currentCompletion() == "jaguar land rover"

    window.activateWindow()
    qtbot.wait(10)
    window.investigation.focus_field("driver_age")
    qtbot.wait(10)
    assert QApplication.focusWidget() is window.investigation.editors["driver_age"].control
    window.investigation.focus_field("collision_directions")
    qtbot.wait(10)
    assert (
        QApplication.focusWidget()
        is window.investigation.editors["collision_directions"].checkboxes[0][0]
    )


def test_case_drag_drop_order_persists_without_changing_identifiers(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.show()
    for _ in range(3):
        window.create_case()
    qtbot.wait(10)
    original_ids = [case.display_id for case in window.cases]
    original_cases = list(window.cases)
    moved_case = original_cases[2]
    moved_uid = moved_case.uid
    video = tmp_path / "evidence.mp4"
    video.write_bytes(b"video")
    moved_case.attach_videos([video])
    window._mark_case_dirty(moved_case)
    window.case_list.set_cases(window.cases, moved_uid)

    tree = window.case_list.tree
    source_item = tree.topLevelItem(2)
    source_child = source_item.child(0)
    original_items = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
    source_item.setExpanded(True)
    tree.setCurrentItem(source_item)
    target_rect = tree.visualItemRect(tree.topLevelItem(0))
    event = QDropEvent(
        QPointF(target_rect.center().x(), target_rect.top() + 1),
        Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tree.dropEvent(event)

    assert event.isAccepted()
    expected_uids = [moved_uid, original_cases[0].uid, original_cases[1].uid]
    expected_ids = [
        original_ids[2],
        original_ids[0],
        original_ids[1],
    ]
    expected_items = [original_items[2], original_items[0], original_items[1]]
    original_uids = [case.uid for case in original_cases]

    assert tree.topLevelItemCount() == 3
    assert [tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) for index in range(3)] == (
        original_uids
    )
    assert [tree.topLevelItem(index) for index in range(3)] == original_items
    assert [case.uid for case in window.cases] == original_uids
    assert all(
        actual is expected for actual, expected in zip(window.cases, original_cases, strict=True)
    )
    assert tree.topLevelItem(2) is source_item
    assert source_item.childCount() == 1
    assert source_item.child(0) is source_child
    assert source_item.isExpanded()
    assert tree.currentItem() is source_item
    assert window.current_uid == moved_uid

    qtbot.waitUntil(
        lambda: (
            tree.topLevelItemCount() == 3
            and [case.uid for case in window.cases] == expected_uids
            and [tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) for index in range(3)]
            == expected_uids
            and tree.currentItem() is source_item
        )
    )
    assert [tree.topLevelItem(index) for index in range(3)] == expected_items
    assert all(
        actual is expected
        for actual, expected in zip(window.cases, [moved_case, *original_cases[:2]], strict=True)
    )
    assert tree.topLevelItem(0) is source_item
    assert [case.display_id for case in window.cases] == expected_ids
    assert source_item.childCount() == 1
    assert source_item.child(0) is source_child
    assert source_item.isExpanded()
    window.save_draft()

    reloaded = MainWindow(tmp_path)
    qtbot.addWidget(reloaded)
    assert [case.display_id for case in reloaded.cases] == expected_ids
    assert reloaded.current_uid == moved_uid
    assert reloaded.case_list.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == moved_uid
    assert reloaded.case_list.tree.topLevelItem(0).childCount() == 1


def test_video_rotation_cycles_clockwise_and_is_restored_per_video(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    window.video_controller._capture_factory = lambda path: _Capture(opened=True)
    window.attach_videos([str(first), str(second)])
    case = window.current_case
    assert case is not None
    assert window.video_player.rotate_button.isEnabled()
    assert not window.video_player.rotate_button.isCheckable()
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(6, 4)

    window.video_player.rotate_button.click()
    assert window.video_player.canvas.rotation_degrees == 90
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(4, 6)
    assert case.video_rotation == 90

    window.video_player.rotate_button.click()
    assert window.video_player.canvas.rotation_degrees == 180
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(6, 4)
    assert case.video_rotation == 180

    window.select_video(case.uid, 1)
    assert window.video_player.canvas.rotation_degrees == 0
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(6, 4)
    window.select_video(case.uid, 0)
    assert window.video_player.canvas.rotation_degrees == 180
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(6, 4)

    window.video_player.rotate_button.click()
    assert window.video_player.canvas.rotation_degrees == 270
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(4, 6)
    assert case.video_rotation == 270

    window.video_player.rotate_button.click()
    assert window.video_player.canvas.rotation_degrees == 0
    assert window.video_player.canvas._image is not None
    assert window.video_player.canvas._image.size() == QSize(6, 4)
    assert case.video_rotation == 0
    assert first.read_bytes() == b"first"


def test_corrupt_workspace_is_never_overwritten(qtbot, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "investigations.json"
    original = b"{broken evidence"
    database.write_bytes(original)
    monkeypatch.setattr(
        QMessageBox, "critical", lambda *args, **kwargs: QMessageBox.StandardButton.Ok
    )

    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    assert window._workspace_load_failed
    assert window.cases == []
    window.close()
    assert database.read_bytes() == original


def test_failed_video_switch_rolls_back_selection_and_keeps_matching_decoder(
    qtbot, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    good = tmp_path / "good.mp4"
    bad = tmp_path / "bad.mp4"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    window.video_controller._capture_factory = lambda path: _Capture(
        opened=Path(path).name == "good.mp4"
    )
    window.attach_videos([str(good), str(bad)])
    case = window.current_case
    assert case is not None
    assert case.active_video_index == 0
    assert window.video_controller.metadata is not None
    assert window.video_controller.metadata.path == good

    active_child = window.case_list.tree.topLevelItem(0).child(0)
    failed_child = window.case_list.tree.topLevelItem(0).child(1)
    assert active_child.foreground(0).color().name() == PRIMARY.casefold()
    assert failed_child.foreground(0).color().name() != PRIMARY.casefold()

    window.select_video(case.uid, 1)
    assert case.active_video_index == 0
    assert window.video_controller.metadata is not None
    assert window.video_controller.metadata.path == good

    active_child = window.case_list.tree.topLevelItem(0).child(0)
    failed_child = window.case_list.tree.topLevelItem(0).child(1)
    assert active_child.foreground(0).color().name() == PRIMARY.casefold()
    assert failed_child.foreground(0).color().name() != PRIMARY.casefold()


def test_selecting_no_obstacle_keeps_distance_inputs_enabled_and_preserves_values(
    qtbot, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    case.values.update(
        {
            "obstacle_exists": "유",
            "obstacle_dist_source": "사고영상",
            "obstacle_dist_value": "8",
            "obstacle_dist_range": "1.5m~10m",
        }
    )
    window.investigation.load_values(case.values)

    obstacle_exists = window.investigation.editors["obstacle_exists"].control
    assert isinstance(obstacle_exists, QComboBox)
    obstacle_exists.setCurrentIndex(obstacle_exists.findData("무"))

    for key, expected in (
        ("obstacle_dist_source", "사고영상"),
        ("obstacle_dist_value", "8"),
        ("obstacle_dist_range", "1.5m~10m"),
    ):
        assert window.investigation.editors[key].isEnabled()
        assert window.investigation.editors[key].value() == expected
        assert case.values[key] == expected


def test_only_playing_video_is_blue_when_each_case_has_an_active_index(
    qtbot, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    first = window.create_case()
    assert first is None
    first_case = window.current_case
    assert first_case is not None
    first_case.attach_videos([str(tmp_path / "first.mp4")])

    window.create_case()
    second_case = window.current_case
    assert second_case is not None
    second_case.attach_videos([str(tmp_path / "second.mp4")])
    assert first_case.active_video_index == second_case.active_video_index == 0

    window.current_uid = second_case.uid
    window.case_list.set_cases(window.cases, second_case.uid)

    first_video = window.case_list.tree.topLevelItem(0).child(0)
    second_video = window.case_list.tree.topLevelItem(1).child(0)
    assert first_video.foreground(0).color().name() != PRIMARY.casefold()
    assert second_video.foreground(0).color().name() == PRIMARY.casefold()


def test_collision_marker_replaces_previous_frame_and_reclick_clears(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    window = MainWindow(tmp_path / "workspace")
    qtbot.addWidget(window)
    window.video_controller._capture_factory = lambda path: _Capture(opened=True, total_frames=100)
    window.attach_videos([str(video)])
    case = window.current_case
    assert case is not None

    window.set_collision_marker()
    assert case.collision_frame == 0

    window._seek_video(24)
    window.set_collision_marker()
    assert case.collision_frame == 24
    assert case.collision_frames == [24]
    assert window.playback.timeline.collision_frame == 24

    window.set_collision_marker()
    assert case.collision_frame is None
    assert case.collision_frames == []
    assert window.playback.timeline.collision_frame is None


def test_failed_completion_save_rolls_back_workflow(qtbot, monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    case.values = valid_values()
    original_updated_at = case.updated_at
    window._dirty = True
    original_save = window._save
    monkeypatch.setattr(window, "_save", lambda *, show_message: False)

    window.complete_case()

    assert case.status == "draft"
    assert case.completed_at is None
    assert case.updated_at == original_updated_at
    assert window._dirty
    monkeypatch.setattr(window, "_save", original_save)


def test_complete_with_missing_values_uses_unified_warning(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    warnings: list[tuple[str, str]] = []
    save_called = False

    def capture_warning(parent, title: str, message: str):  # type: ignore[no-untyped-def]
        del parent
        warnings.append((title, message))
        return QMessageBox.StandardButton.Ok

    def unexpected_save(*, show_message: bool) -> bool:
        nonlocal save_called
        del show_message
        save_called = True
        return True

    monkeypatch.setattr(QMessageBox, "warning", capture_warning)
    monkeypatch.setattr(window, "_save", unexpected_save)
    window.complete_case()

    assert not save_called
    assert len(warnings) == 1
    assert warnings[0][0] == MISSING_VALUE_MESSAGE
    assert MISSING_VALUE_MESSAGE in warnings[0][1]
    assert window.investigation.editors["driver_sex"].control.toolTip() == MISSING_VALUE_MESSAGE


def test_complete_with_invalid_date_uses_unified_format_warning(
    qtbot, monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    window = MainWindow(tmp_path)
    qtbot.addWidget(window)
    window.create_case()
    case = window.current_case
    assert case is not None
    case.values = valid_values()
    case.values["incident_date"] = "20260230"
    window.investigation.load_values(case.values)
    window._refresh_case_presentation(rebuild_list=False)
    warnings: list[tuple[str, str]] = []

    def capture_warning(parent, title: str, message: str):  # type: ignore[no-untyped-def]
        del parent
        warnings.append((title, message))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", capture_warning)
    window.complete_case()

    assert warnings == [
        (
            INVALID_FORMAT_MESSAGE,
            f"{INVALID_FORMAT_MESSAGE}.\n\n• 사고일자: {INVALID_FORMAT_MESSAGE}",
        )
    ]
    assert (
        window.investigation.editors["incident_date"].line_edit.toolTip() == INVALID_FORMAT_MESSAGE
    )
