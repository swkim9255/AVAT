from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QStylePainter, QVBoxLayout, QWidget

from investigator.schema import FIELD_BY_KEY
from investigator.theme import INVESTIGATION_FONT_SIZE, apply_theme
from investigator.widgets.form_panel import FieldEditor, InvestigationPanel
from investigator.widgets.responsive_choices import WrappingCheckBox


def test_investigation_fields_use_previous_font_size_without_changing_other_ui(qtbot, qapp):
    apply_theme(qapp)
    panel = InvestigationPanel()
    qtbot.addWidget(panel)
    panel.show()
    qapp.processEvents()
    for editor in panel.editors.values():
        assert editor.font().pixelSize() == INVESTIGATION_FONT_SIZE == 13
        for child in editor.findChildren(QLineEdit):
            assert child.font().pixelSize() == 13, editor.spec.key
        for checkbox, _ in editor.checkboxes:
            assert checkbox.font().pixelSize() == 13, editor.spec.key
        if isinstance(editor.control, QComboBox):
            assert editor.control.font().pixelSize() == 13, editor.spec.key
            popup = editor.control.view()
            popup.ensurePolished()
            assert popup.font().pixelSize() == 13, editor.spec.key
    completer = panel.editors["vehicle_manufacturer"].control.completer()
    completer.popup().ensurePolished()
    assert completer.popup().font().pixelSize() == 13
    other = QLabel("Other UI")
    qtbot.addWidget(other)
    other.ensurePolished()
    assert other.font().pixelSize() == 13


def test_repeated_choices_and_validation_do_not_change_text_metrics(qtbot, qapp):
    apply_theme(qapp)
    panel = InvestigationPanel()
    qtbot.addWidget(panel)
    panel.resize(448, 900)
    panel.show()
    qapp.processEvents()
    combo = panel.editors["driver_sex"].control
    editor = panel.editors["site_types"]
    checkbox = editor.checkboxes[0][0]
    baseline = (checkbox.font().toString(), checkbox.fontMetrics().height())
    for index in range(30):
        combo.setCurrentIndex(1 + index % 3)
        checkbox.toggle()
        editor.set_invalid("Invalid" if index % 3 == 0 else None)
        qapp.processEvents()
        assert combo.font().pixelSize() == 13
        assert (checkbox.font().toString(), checkbox.fontMetrics().height()) == baseline
    editor.set_invalid(None)
    panel.tabs.setCurrentIndex(5)
    qapp.processEvents()
    for item, _ in editor.checkboxes:
        assert item.height() >= item.heightForWidth(item.width())


def test_unchanged_validation_does_not_repolish_on_every_selection(qtbot, qapp, monkeypatch):
    apply_theme(qapp)
    editor = FieldEditor(FIELD_BY_KEY["driver_sex"])
    qtbot.addWidget(editor)
    original = editor.control.style()
    calls = []

    class StyleSpy:
        def unpolish(self, widget):
            calls.append("unpolish")
            original.unpolish(widget)

        def polish(self, widget):
            calls.append("polish")
            original.polish(widget)

    monkeypatch.setattr(editor.control, "style", lambda: StyleSpy())
    editor.set_invalid(None)
    calls.clear()
    for _ in range(20):
        editor.set_invalid(None)
    assert not calls
    editor.set_invalid("Error")
    assert calls == ["unpolish", "polish"]
    calls.clear()
    editor.set_invalid("Another error")
    assert not calls
    assert editor.control.toolTip() == "Another error"
    editor.set_invalid(None)
    assert calls == ["unpolish", "polish"]


def test_checkbox_text_ignores_font_changes_left_by_indicator_style(qtbot, qapp, monkeypatch):
    apply_theme(qapp)
    host = QWidget()
    host.setProperty("investigationField", True)
    layout = QVBoxLayout(host)
    checkbox = WrappingCheckBox("Wrapping checkbox text", host)
    layout.addWidget(checkbox)
    qtbot.addWidget(host)
    original = checkbox.style()
    painted_sizes = []

    class FontLeakingPainter(QStylePainter):
        def drawControl(self, element, option):
            super().drawControl(element, option)
            font = self.font()
            font.setPixelSize(40)
            self.setFont(font)

    class StyleSpy:
        def pixelMetric(self, *args):
            return original.pixelMetric(*args)

        def drawItemText(self, painter, *args):
            painted_sizes.append(painter.font().pixelSize())
            return original.drawItemText(painter, *args)

    monkeypatch.setattr("investigator.widgets.responsive_choices.QStylePainter", FontLeakingPainter)
    monkeypatch.setattr(checkbox, "style", lambda: StyleSpy())
    host.resize(360, 100)
    host.show()
    qapp.processEvents()
    checkbox.grab()
    assert painted_sizes and set(painted_sizes) == {13}


@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
def test_font_size_is_stable_at_windows_display_scales(scale):
    source = """
import json
from PySide6.QtWidgets import QApplication
from investigator.theme import apply_theme
from investigator.widgets.form_panel import InvestigationPanel
app = QApplication([])
app.setStyle('Fusion')
apply_theme(app)
panel = InvestigationPanel()
panel.resize(448, 900)
panel.show()
app.processEvents()
combo = panel.editors['driver_sex'].control
checkbox = panel.editors['site_types'].checkboxes[0][0]
for index in range(20):
    combo.setCurrentIndex(1 + index % 3)
    checkbox.toggle()
    app.processEvents()
combo.showPopup()
app.processEvents()
print(json.dumps([combo.font().pixelSize(), combo.view().font().pixelSize(), checkbox.font().pixelSize()]))
combo.hidePopup()
panel.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True,
        text=True,
        timeout=25,
        check=True,
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [13, 13, 13]
