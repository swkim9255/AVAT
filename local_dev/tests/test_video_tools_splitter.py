"""Native vertical splitter drag and independent speed/detection panel sizes."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QVBoxLayout, QWidget

from investigator.project_tabs_state import ProjectTabsStore
from investigator.theme import CONTROL_BORDER, STAT_BAR, apply_theme
from investigator.widgets.video_tools_splitter import VideoToolsSplitter


def process_layout():
    QApplication.processEvents()
    QApplication.processEvents()


def workspace(qtbot, qapp):
    apply_theme(qapp)
    splitter = VideoToolsSplitter()
    video = QWidget()
    video.setMinimumHeight(100)
    layout = QVBoxLayout(video)
    layout.setContentsMargins(0, 0, 0, 0)
    canvas = QLabel("VIDEO", video)
    playback = QLabel("PLAYBACK", video)
    playback.setFixedHeight(60)
    layout.addWidget(canvas, 1)
    layout.addWidget(playback)
    splitter.add_video_area(video)
    for key, height in (("speed", 300), ("detection", 340)):
        tool = QWidget()
        tool.setMinimumHeight(100)
        tool.hide()
        splitter.add_tool_panel(key, tool, default_height=height)
    splitter.resize(800, 800)
    qtbot.addWidget(splitter)
    splitter.show()
    process_layout()
    return splitter, canvas, playback


def show_tool(splitter, key):
    for other in ("speed", "detection"):
        if other != key:
            splitter.set_tool_visible(other, False)
    if key:
        splitter.set_tool_visible(key, True)
    process_layout()


def drag_handle(qtbot, splitter, index, delta):
    handle = splitter.handle(index)
    origin = handle.rect().center()
    qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=origin)
    destination = origin + QPoint(0, delta)
    event = QMouseEvent(QEvent.Type.MouseMove, destination, handle.mapToGlobal(destination),
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(handle, event)
    qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=destination)
    process_layout()


def center_color(handle):
    image = handle.grab().toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


def test_hidden_tools_leave_all_vertical_space_to_video_and_attached_playback(qtbot, qapp):
    splitter, canvas, playback = workspace(qtbot, qapp)
    assert splitter.orientation() == Qt.Orientation.Vertical
    assert not splitter.childrenCollapsible()
    assert splitter.sizes() == [splitter.height(), 0, 0]
    assert canvas.parentWidget() is playback.parentWidget() is splitter.widget(0)
    assert playback.geometry().top() > canvas.geometry().bottom()
    assert playback.geometry().bottom() == splitter.widget(0).height() - 1


def test_vertical_bar_visible_hoverable_and_native_drag_can_exceed_old_maximum(qtbot, qapp):
    splitter, _, _ = workspace(qtbot, qapp)
    show_tool(splitter, "speed")
    handle = splitter.handle(1)
    QApplication.sendEvent(handle, QEvent(QEvent.Type.Leave))
    assert handle.height() >= 8
    assert handle.cursor().shape() == Qt.CursorShape.SplitVCursor
    assert "드래그" in handle.toolTip()
    assert center_color(handle) == QColor(CONTROL_BORDER)
    qtbot.mouseMove(handle, pos=handle.rect().center())
    process_layout()
    assert center_color(handle) == QColor(STAT_BAR)
    before = splitter.sizes()
    drag_handle(qtbot, splitter, 1, -140)
    after = splitter.sizes()
    assert after[0] < before[0]
    assert after[1] > before[1]
    assert after[1] > 340  # Former speed-panel maximum is intentionally gone.
    assert after[0] >= 100 and after[1] >= 100


def test_switching_tools_restores_each_independent_user_height(qtbot, qapp):
    splitter, _, _ = workspace(qtbot, qapp)
    show_tool(splitter, "speed")
    drag_handle(qtbot, splitter, 1, -130)
    speed_height = splitter.sizes()[1]
    show_tool(splitter, "detection")
    assert abs(splitter.sizes()[2] - 340) <= 1
    drag_handle(qtbot, splitter, 2, 80)
    detection_height = splitter.sizes()[2]
    assert detection_height != speed_height
    for _ in range(3):
        show_tool(splitter, "speed")
        assert abs(splitter.sizes()[1] - speed_height) <= 1
        assert splitter.sizes()[2] == 0
        show_tool(splitter, "detection")
        assert abs(splitter.sizes()[2] - detection_height) <= 1
        assert splitter.sizes()[1] == 0
    show_tool(splitter, None)
    assert splitter.sizes() == [splitter.height(), 0, 0]


def test_window_resize_keeps_both_panes_visible_and_playback_below_video(qtbot, qapp):
    splitter, canvas, playback = workspace(qtbot, qapp)
    show_tool(splitter, "detection")
    splitter.resize(800, 500)
    process_layout()
    assert all(splitter.sizes()[index] >= 100 for index in (0, 2))
    assert sum(splitter.sizes()) + splitter.handle(2).height() == splitter.height()
    assert playback.geometry().top() > canvas.geometry().bottom()
    assert playback.geometry().bottom() == splitter.widget(0).height() - 1
    splitter.resize(800, 1100)
    process_layout()
    assert splitter.sizes()[2] > 360
    assert splitter.sizes()[0] >= 100


@pytest.fixture
def main_window(qtbot, tmp_path, monkeypatch):
    from investigator.main_window import MainWindow

    for method in ("question", "warning", "critical", "information"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.No))
    window = MainWindow(tmp_path / "workspace", project_tabs_store=ProjectTabsStore(tmp_path / "tabs.json"))
    qtbot.addWidget(window)
    window.show()
    process_layout()
    yield window
    window._workspace_load_failed = True
    window.close()


def test_real_main_window_toggle_cycle_preserves_layout_and_shortcut_ancestry(main_window, qtbot):
    window = main_window
    splitter = window.video_tool_splitter
    assert splitter.widget(0) is window.video_playback_area
    assert splitter.widget(1) is window.speed_scroll
    assert splitter.widget(2) is window.detection_scroll
    assert window.video_workspace.isAncestorOf(window.video_player)
    assert window.video_workspace.isAncestorOf(window.playback)
    assert window.speed_scroll.maximumHeight() > 340
    assert window.detection_scroll.maximumHeight() > 360
    window.playback.speed_panel_button.setChecked(True)
    process_layout()
    drag_handle(qtbot, splitter, 1, -55)
    speed_height = splitter.sizes()[1]
    for _ in range(3):
        window.playback.detection_panel_button.setChecked(True)
        process_layout()
        assert window.detection_scroll.isVisible()
        assert not window.speed_scroll.isVisible()
        assert not window.playback.speed_panel_button.isChecked()
        assert splitter.sizes()[2] > 0
        window.playback.speed_panel_button.setChecked(True)
        process_layout()
        assert window.speed_scroll.isVisible()
        assert not window.detection_scroll.isVisible()
        assert not window.playback.detection_panel_button.isChecked()
        assert abs(splitter.sizes()[1] - speed_height) <= 2
    window.playback.speed_panel_button.setChecked(False)
    process_layout()
    assert splitter.sizes() == [splitter.height(), 0, 0]
    assert window.playback.geometry().top() > window.video_player.geometry().bottom()
