from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

from investigator.widgets.form_panel import InvestigationPanel


@pytest.mark.parametrize("key", ["vehicle_manufacturer", "vehicle_fuel"])
@pytest.mark.parametrize("focused", [False, True])
def test_closed_dropdown_wheel_scrolls_form_without_changing_value(qtbot, key, focused):
    panel = InvestigationPanel()
    qtbot.addWidget(panel)
    panel.resize(520, 760)
    panel.tabs.setCurrentIndex(1)
    panel.show()
    combo = panel.editors[key].control
    combo.setCurrentIndex(1)
    scroll = panel.tabs.currentWidget()
    scroll.widget().setMinimumHeight(1000)
    scroll.ensureWidgetVisible(combo)
    if focused:
        combo.setFocus()
    else:
        panel.editors["accident_classification"].line_edit.setFocus()
    qtbot.wait(10)
    assert scroll.verticalScrollBar().maximum() > 0
    before = scroll.verticalScrollBar().value()
    changed = []
    panel.value_changed.connect(lambda *args: changed.append(args))
    # Route a real window wheel event through Qt's hit testing / propagation.
    # For editable combos, also cover the child line edit under the pointer.
    target = combo.lineEdit() if combo.isEditable() else combo
    if combo.isEditable():
        assert isinstance(target, QLineEdit)
    position = target.mapTo(panel, target.rect().center())
    QTest.wheelEvent(panel.windowHandle(), position, QPoint(0, -120))
    assert combo.currentIndex() == 1
    assert not changed
    assert scroll.verticalScrollBar().value() > before


def test_open_dropdown_wheel_scrolls_popup_not_parent(qtbot):
    panel = InvestigationPanel()
    qtbot.addWidget(panel)
    panel.resize(520, 760)
    panel.tabs.setCurrentIndex(1)
    panel.show()
    combo = panel.editors["vehicle_manufacturer"].control
    combo.setStyleSheet("QComboBox { combobox-popup: 0; }")
    combo.setMaxVisibleItems(4)
    combo.setCurrentIndex(1)
    qtbot.wait(10)
    scroll = panel.tabs.currentWidget()
    parent_before = scroll.verticalScrollBar().value()
    qtbot.mouseClick(
        combo, Qt.MouseButton.LeftButton, pos=QPoint(combo.width() - 10, combo.height() // 2)
    )
    qtbot.waitUntil(lambda: combo.view().isVisible())
    view = combo.view()
    qtbot.waitUntil(lambda: view.verticalScrollBar().maximum() > 0)
    popup_before = view.verticalScrollBar().value()
    popup = view.window()
    position = view.viewport().mapTo(popup, view.viewport().rect().center())
    QTest.wheelEvent(popup.windowHandle(), position, QPoint(0, -120))
    assert view.verticalScrollBar().value() > popup_before
    assert scroll.verticalScrollBar().value() == parent_before
    assert view.isVisible()
    combo.hidePopup()
