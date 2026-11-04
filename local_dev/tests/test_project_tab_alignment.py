"""Close-button hitboxes must not depend on a follow-up paint event."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QTabBar

from investigator.theme import apply_theme
from investigator.widgets.workspace_menu import ProjectTabBar


@pytest.fixture
def tabs(qtbot, qapp):
    apply_theme(qapp)
    widget = ProjectTabBar()
    widget.setTabsClosable(True)
    widget.setMovable(True)
    widget.setExpanding(False)
    for name in ("project_A", "project_B", "project_C"):
        widget.addTab(name)
    widget.resize(700, 36)
    qtbot.addWidget(widget)
    widget.show()
    qtbot.wait(20)
    return widget


def assert_aligned(tabs):
    for index in range(tabs.count()):
        close = tabs.tabButton(index, QTabBar.ButtonPosition.RightSide)
        assert close.geometry() == tabs._item_rects(index)[2]


def test_tab_layout_realigns_close_hitboxes_without_repainting(tabs, qtbot):
    # The native bar can relayout children while paints are deferred by another
    # widget's event loop. Alignment must be a layout responsibility as well.
    tabs.setUpdatesEnabled(False)
    tabs.setTabText(0, "project_with_a_longer_folder_name")
    tabs.moveTab(0, 2)
    tabs.resize(560, 40)
    qtbot.wait(20)
    assert_aligned(tabs)


def test_release_queued_alignment_repairs_native_cleanup_without_paint(tabs, qtbot):
    tabs.setUpdatesEnabled(False)
    position = tabs.tabRect(0).center()
    qtbot.mousePress(tabs, Qt.MouseButton.LeftButton, pos=position)
    native_cleanup = []

    def finish_native_layout():
        for index in range(tabs.count()):
            close = tabs.tabButton(index, QTabBar.ButtonPosition.RightSide)
            close.setGeometry(tabs._item_rects(index)[2].translated(17, 0))
        native_cleanup.append(True)

    # Model native drag/layout cleanup posted before the release handler has
    # returned. No paint is permitted to accidentally repair the hitboxes.
    QTimer.singleShot(0, finish_native_layout)
    qtbot.mouseRelease(tabs, Qt.MouseButton.LeftButton, pos=position)
    qtbot.wait(20)
    assert native_cleanup
    assert_aligned(tabs)
    assert tabs._reorder_press is None
    assert not tabs._painting_drag


def test_delayed_native_slide_move_cannot_override_close_hitboxes(tabs, qtbot):
    tabs.setUpdatesEnabled(False)
    position = tabs.tabRect(0).center()
    qtbot.mousePress(tabs, Qt.MouseButton.LeftButton, pos=position)
    qtbot.mouseRelease(tabs, Qt.MouseButton.LeftButton, pos=position)
    native_cleanup = []

    def delayed_slide_step():
        for index in range(tabs.count()):
            close = tabs.tabButton(index, QTabBar.ButtonPosition.RightSide)
            close.setGeometry(tabs._item_rects(index)[2].translated(17, 0))
        native_cleanup.append(True)

    # A slide timer may fire after the zero-delay release/layout repair. The
    # child Move event must enforce the geometry without waiting for painting.
    QTimer.singleShot(10, delayed_slide_step)
    qtbot.wait(40)
    assert native_cleanup
    assert_aligned(tabs)
