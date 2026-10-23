"""Responsive object-list sizing and plainly named detection-range shortcuts."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QRadioButton, QScrollArea

from investigator.form_definition import FormDefinition
from investigator.object_detection import (
    ROLE_BOTH,
    ROLE_COLLISION,
    ROLE_TRIGGER,
    DetectedObject,
    ObjectBox,
    VideoObjects,
)
from investigator.theme import apply_theme
from investigator.widgets.detection_panel import DetectionPanel


def _objects(count: int) -> VideoObjects:
    return VideoObjects(
        [
            DetectedObject(
                uid=f"uid-{index}",
                object_id=f"U{index + 1:03}",
                role=ROLE_TRIGGER,
                label=None,
                boxes=[ObjectBox(index % 2, (10, 10, 20, 20))],
            )
            for index in range(count)
        ]
    )


@pytest.fixture()
def area(qtbot, qapp):
    old_style, old_palette, old_font = qapp.styleSheet(), qapp.palette(), qapp.font()
    apply_theme(qapp)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.resize(700, 360)
    panel = DetectionPanel()
    panel.set_video(200, 30.0)
    scroll.setWidget(panel)
    qtbot.addWidget(scroll)
    scroll.show()
    qtbot.wait(10)
    yield scroll
    scroll.close()
    qapp.setStyleSheet(old_style)
    qapp.setPalette(old_palette)
    qapp.setFont(old_font)


def test_table_height_follows_added_and_removed_objects(area, qtbot):
    panel = area.widget()
    empty_height = panel.table.height()
    panel.set_objects(_objects(2), FormDefinition.default())
    qtbot.wait(10)
    short_height = panel.table.height()
    panel.set_objects(_objects(12), FormDefinition.default())
    qtbot.wait(10)
    assert panel.table.height() > short_height > empty_height
    assert panel.table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel.table.viewport().height() >= panel.table.verticalHeader().length()
    panel.set_objects(_objects(2), FormDefinition.default())
    qtbot.wait(10)
    assert panel.table.height() == short_height
    panel.set_objects(VideoObjects(), FormDefinition.default())
    qtbot.wait(10)
    assert panel.table.height() == empty_height


def test_table_height_follows_current_frame_filter(area, qtbot):
    panel = area.widget()
    panel.set_objects(_objects(12), FormDefinition.default())
    qtbot.wait(10)
    full_height = panel.table.height()
    panel.current_only.setChecked(True)
    qtbot.wait(10)
    assert panel.table.rowCount() == 6
    assert panel.table.height() < full_height
    panel.set_current_frame(7)
    qtbot.wait(10)
    assert panel.table.rowCount() == 0
    assert panel.table.height() < full_height
    panel.current_only.setChecked(False)
    qtbot.wait(10)
    assert panel.table.rowCount() == 12
    assert panel.table.height() == full_height


def test_large_object_table_scrolls_parent_without_resizing_window(area, qtbot):
    panel = area.widget()
    outer_size = area.size()
    panel.set_objects(_objects(100), FormDefinition.default())
    qtbot.wait(10)
    assert panel.table.viewport().height() >= panel.table.verticalHeader().length()
    assert area.verticalScrollBar().maximum() > 0
    assert area.size() == outer_size
    panel.set_objects(VideoObjects(), FormDefinition.default())
    qtbot.wait(10)
    assert panel.table.height() < 100
    assert area.size() == outer_size


def test_range_buttons_explicitly_name_current_frame_and_keep_range_behavior(area):
    panel = area.widget()
    assert panel.start_here_button.text() == "현재 프레임을 시작으로 지정"
    assert panel.end_here_button.text() == "현재 프레임을 끝으로 지정"
    assert "시작 프레임" in panel.start_here_button.toolTip()
    assert "끝 프레임 포함" in panel.end_here_button.toolTip()
    panel.set_selected_range(10, 90)
    panel.set_current_frame(34)
    panel.start_here_button.click()
    assert panel.frame_range == (34, 90)
    assert panel.start_spin.value() == 35
    panel.set_current_frame(55)
    panel.end_here_button.click()
    assert panel.frame_range == (34, 55)
    assert panel.end_spin.value() == 56
    assert panel.start_here_button.isEnabled()
    assert panel.end_here_button.isEnabled()


@pytest.mark.parametrize("width", [450, 700, 1200])
def test_range_button_labels_fit_without_horizontal_panel_scroll(area, qtbot, width):
    area.resize(width, 360)
    qtbot.wait(20)
    panel = area.widget()
    assert area.horizontalScrollBar().maximum() == 0
    for button in (panel.start_here_button, panel.end_here_button):
        assert button.width() >= button.sizeHint().width()
        point = button.mapTo(panel, button.rect().topLeft())
        assert point.x() >= 0
        assert point.x() + button.width() <= panel.width()


def test_table_selection_and_double_click_seek_survive_resizing(area, qtbot):
    panel = area.widget()
    objects = _objects(4)
    panel.set_objects(objects, FormDefinition.default())
    panel.select_uids([objects.objects[1].uid])
    assert panel.selected_uids() == [objects.objects[1].uid]
    panel.set_objects(_objects(8), FormDefinition.default())
    assert panel.selected_uids() == [objects.objects[1].uid]
    row = panel.table.selectionModel().selectedRows()[0].row()
    with qtbot.waitSignal(panel.seek_requested) as emitted:
        panel.table.cellDoubleClicked.emit(row, 0)
    assert emitted.args == [1]


def test_simplified_panel_removes_role_and_range_mode_controls(area):
    panel = area.widget()
    assert not panel.findChildren(QRadioButton)
    for attribute in (
        "single_radio", "range_radio", "range_mode",
        "trigger_role_radio", "collision_role_radio", "role_group",
        "role_combo", "role_change_requested",
        "delete_box_button", "delete_box_requested", "_delete_box",
    ):
        assert not hasattr(panel, attribute)
    assert panel.current_only.text() == "현재 프레임만"
    assert panel.new_object_role == ROLE_BOTH
    for old_role in (ROLE_TRIGGER, ROLE_COLLISION):
        panel.set_new_object_role(old_role)
        assert panel.new_object_role == ROLE_BOTH


def test_selected_range_and_step_do_not_follow_playback_frame(area):
    panel = area.widget()
    panel.set_selected_range(10, 50)
    panel.step_spin.setValue(5)
    changes = QSignalSpy(panel.range_changed)
    for frame in (2, 10, 40, 99):
        panel.set_current_frame(frame)
    assert panel.frame_range == (10, 50)
    assert panel.frame_step == 5
    assert panel.processed_frame_count() == 9
    assert changes.count() == 0
    panel.current_only.setChecked(True)
    panel.set_current_frame(75)
    assert panel.frame_range == (10, 50)


def test_equal_start_and_end_is_a_single_frame_without_a_mode_switch(area):
    panel = area.widget()
    panel.step_spin.setValue(7)
    panel.set_selected_range(18, 18)
    assert panel.frame_range == (18, 18)
    assert panel.frame_step == 7
    assert panel.processed_frame_count() == 1
    assert panel.range_summary.text() == "19번 프레임"
    assert panel.start_spin.isEnabled() and panel.end_spin.isEnabled()
    assert panel.step_spin.isEnabled()
    panel.set_current_frame(21)
    assert panel.frame_range == (18, 18)


def test_range_controls_only_disable_without_video_or_while_running(area):
    panel = area.widget()
    controls = (
        panel.start_spin, panel.end_spin, panel.step_spin,
        panel.start_here_button, panel.end_here_button,
    )
    assert all(control.isEnabled() for control in controls)
    panel.set_selected_range(20, 80)
    panel.set_running(True)
    assert all(not control.isEnabled() for control in controls)
    panel.set_running(False)
    assert all(control.isEnabled() for control in controls)
    assert panel.frame_range == (20, 80)
    panel.set_video(0, 0.0)
    assert all(not control.isEnabled() for control in controls)
    panel.set_video(200, 30.0)
    assert all(control.isEnabled() for control in controls)
    assert panel.frame_range == (0, 0)


def test_trigger_collision_range_shortcut_and_timeline_range_remain_available(area):
    panel = area.widget()
    panel.set_markers(40, 10)
    assert panel.trigger_collision_button.isEnabled()
    changed = QSignalSpy(panel.range_changed)
    panel.trigger_collision_button.click()
    assert changed.count() == 1
    assert list(changed.at(0)) == [10, 40]
    assert panel.frame_range == (10, 40)
    panel.set_selected_range(14, 32)
    assert panel.frame_range == (14, 32)


def test_common_role_displays_and_enables_both_form_targets(area):
    panel = area.widget()
    objects = _objects(1)
    objects.objects[0].role = ROLE_BOTH
    panel.set_objects(objects, FormDefinition.default())
    assert panel.table.item(0, 1).text() == "공통"
    assert panel.apply_trigger_button.isEnabled()
    assert panel.apply_collision_button.isEnabled()
    panel.set_objects(VideoObjects(), FormDefinition.default())
    assert not panel.apply_trigger_button.isEnabled()
    assert not panel.apply_collision_button.isEnabled()


def test_whole_object_delete_and_new_id_split_keep_selected_uid(area, qtbot):
    panel = area.widget()
    objects = _objects(1)
    panel.set_objects(objects, FormDefinition.default())
    panel.select_uids([objects.objects[0].uid])
    assert panel.split_frame_button.isEnabled()
    with qtbot.waitSignal(panel.split_frame_requested) as split:
        panel.split_frame_button.click()
    assert split.args == [objects.objects[0].uid]
    with qtbot.waitSignal(panel.delete_requested) as deleted:
        panel.delete_button.click()
    assert deleted.args == [[objects.objects[0].uid]]
