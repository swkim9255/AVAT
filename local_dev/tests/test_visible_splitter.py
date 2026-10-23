"""Visible main workspace boundaries retain Qt's native resize interaction."""

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import QApplication, QSplitter, QWidget

from investigator.theme import BACKGROUND, CONTROL_BORDER, STAT_BAR, apply_theme


def splitter(qtbot, qapp, name="mainSplitter"):
    apply_theme(qapp)
    widget = QSplitter(Qt.Orientation.Horizontal)
    widget.setObjectName(name)
    widget.setHandleWidth(8)
    widget.setChildrenCollapsible(False)
    for _ in range(3):
        pane = QWidget()
        pane.setMinimumWidth(50)
        widget.addWidget(pane)
    widget.resize(900, 400)
    widget.setSizes([200, 400, 300])
    qtbot.addWidget(widget)
    widget.show()
    QApplication.processEvents()
    return widget


def center_color(handle):
    image = handle.grab().toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


def test_both_sidebar_boundaries_have_visible_neutral_bars(qtbot, qapp):
    widget = splitter(qtbot, qapp)
    for index in (1, 2):
        handle = widget.handle(index)
        QApplication.sendEvent(handle, QEvent(QEvent.Type.Leave))
        assert handle.width() >= 8
        assert handle.cursor().shape() == Qt.CursorShape.SplitHCursor
        assert center_color(handle) == QColor(CONTROL_BORDER)
        assert center_color(handle) != QColor(BACKGROUND)


def test_bar_highlights_on_hover_without_changing_pane_sizes(qtbot, qapp):
    widget = splitter(qtbot, qapp)
    before = widget.sizes()
    for index in (1, 2):
        handle = widget.handle(index)
        qtbot.mouseMove(handle, pos=handle.rect().center())
        QApplication.processEvents()
        assert center_color(handle) == QColor(STAT_BAR)
        qtbot.mouseMove(widget.widget(0), pos=QPoint(10, 10))
        QApplication.processEvents()
        assert center_color(handle) == QColor(CONTROL_BORDER)
    assert widget.sizes() == before


def test_visual_bars_preserve_native_drag_resize_on_both_boundaries(qtbot, qapp):
    widget = splitter(qtbot, qapp)
    for index in (1, 2):
        handle = widget.handle(index)
        before = widget.sizes()
        origin = handle.rect().center()
        qtbot.mousePress(handle, Qt.MouseButton.LeftButton, pos=origin)
        destination = origin + QPoint(40, 0)
        event = QMouseEvent(
            QEvent.Type.MouseMove, destination, handle.mapToGlobal(destination),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(handle, event)
        qtbot.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=destination)
        after = widget.sizes()
        assert after[index - 1] > before[index - 1]
        assert after[index] < before[index]
        assert all(size >= 50 for size in after)


def test_main_workspace_style_does_not_change_other_splitters(qtbot, qapp):
    widget = splitter(qtbot, qapp, name="otherSplitter")
    assert center_color(widget.handle(1)) == QColor(BACKGROUND)
