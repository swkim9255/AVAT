from PySide6.QtCore import Qt

from investigator.form_definition import FormDefinition
from investigator.theme import apply_theme
from investigator.widgets.form_panel import InvestigationPanel


def test_two_columns_fit_narrow_panel_and_choices_reflow(qtbot, qapp):
    apply_theme(qapp)
    definition = FormDefinition.default()
    objects = next(t for t in definition.tabs if t["key"] == "trigger_objects")
    objects["columns"] = 2
    panel = InvestigationPanel(definition=definition)
    qtbot.addWidget(panel)
    panel.resize(448, 900)
    panel.load_values({"obstacle_exists": "유"})
    panel.tabs.setCurrentIndex(3)
    panel.show()
    qtbot.wait(30)
    scroll = panel.tabs.currentWidget()
    assert scroll.horizontalScrollBar().maximum() == 0
    for left, right in (
        ("obstacle_exists", "obstacle_vehicle_types"),
        ("obstacle_road_facilities", "obstacle_building_landscape"),
    ):
        assert abs(panel.editors[left].width() - panel.editors[right].width()) <= 1
        assert panel.editors[left].width() < scroll.viewport().width() / 2
        assert panel.editors[right].geometry().right() < scroll.widget().width()
    group = panel.editors["obstacle_road_facilities"].control
    assert group._columns == 1
    for checkbox, _ in panel.editors["obstacle_road_facilities"].checkboxes:
        assert checkbox.geometry().right() < group.width()
        assert checkbox.height() >= checkbox.heightForWidth(checkbox.width())
    checkbox = panel.editors["obstacle_road_facilities"].checkboxes[5][0]
    scroll.ensureWidgetVisible(checkbox)
    qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton)
    assert checkbox.isChecked()
    panel.resize(1000, 900)
    qtbot.wait(30)
    assert group._columns == 2
    assert checkbox.isChecked()
    panel.resize(448, 900)
    qtbot.wait(30)
    assert group._columns == 1
    assert checkbox.isChecked()
    assert scroll.horizontalScrollBar().maximum() == 0


def test_all_two_column_tabs_fit_long_labels_and_options(qtbot, qapp):
    apply_theme(qapp)
    definition = FormDefinition.default()
    for tab in definition.tabs:
        tab["columns"] = 2
    panel = InvestigationPanel(definition=definition)
    qtbot.addWidget(panel)
    panel.resize(448, 800)
    panel.show()
    for index in range(panel.tabs.count()):
        panel.tabs.setCurrentIndex(index)
        qtbot.wait(10)
        scroll = panel.tabs.currentWidget()
        assert scroll.horizontalScrollBar().maximum() == 0, panel.sections[index].key
    grid = panel.section_field_grids["identity"]
    assert (
        abs(grid.itemAtPosition(0, 0).widget().width() - grid.itemAtPosition(0, 1).widget().width())
        <= 1
    )
