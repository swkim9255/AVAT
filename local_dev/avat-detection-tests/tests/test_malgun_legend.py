from __future__ import annotations

import pytest
from PySide6.QtGui import QFontDatabase, QFontInfo
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton

from investigator.theme import apply_theme, material_font, ui_font_family
from investigator.widgets.form_panel import InvestigationPanel
from investigator.widgets.playback_panel import PlaybackPanel


def assert_malgun(widget):
    widget.ensurePolished()
    assert widget.font().family() == "Malgun Gothic"
    if "Malgun Gothic" in QFontDatabase.families():
        assert QFontInfo(widget.font()).family() == "Malgun Gothic"


def test_malgun_ui_preserves_sizes_and_separate_icon_font(qtbot, qapp):
    apply_theme(qapp)
    assert ui_font_family() == "Malgun Gothic"
    assert qapp.font().family() == "Malgun Gothic"
    assert qapp.font().pixelSize() == 13
    ordinary_label = QLabel("일반 화면")
    qtbot.addWidget(ordinary_label)
    assert_malgun(ordinary_label)
    assert ordinary_label.font().pixelSize() == 13
    ordinary_button = QPushButton("일반 버튼")
    qtbot.addWidget(ordinary_button)
    assert_malgun(ordinary_button)
    assert ordinary_button.font().pixelSize() == 13

    panel = InvestigationPanel()
    qtbot.addWidget(panel)
    panel.show()
    qapp.processEvents()
    assert panel.save_button.font().pixelSize() == 13
    assert panel.complete_button.font().pixelSize() == 13
    assert panel.findChild(QLabel, "sectionTitle").font().pixelSize() == 16
    assert panel.case_title.font().pixelSize() == 20
    for editor in panel.editors.values():
        assert_malgun(editor)
        assert editor.font().pixelSize() == 13
        for control in editor.findChildren(QLineEdit):
            assert_malgun(control)
            assert control.font().pixelSize() == 13
        for checkbox, _ in editor.checkboxes:
            assert_malgun(checkbox)
            assert checkbox.font().pixelSize() == 13
        if isinstance(editor.control, QComboBox):
            assert_malgun(editor.control)
            assert_malgun(editor.control.view())
            assert editor.control.view().font().pixelSize() == 13
    completer_popup = panel.editors["vehicle_manufacturer"].control.completer().popup()
    assert_malgun(completer_popup)
    assert completer_popup.font().pixelSize() == 13
    assert material_font(20).family() != "Malgun Gothic"
    assert material_font(20).pixelSize() == 20


@pytest.mark.parametrize("width", [400, 640, 1000])
def test_timeline_legend_stays_one_line_at_existing_size(qtbot, qapp, width):
    apply_theme(qapp)
    panel = PlaybackPanel()
    qtbot.addWidget(panel)
    panel.resize(width, 230)
    panel.show()
    qapp.processEvents()
    legend = panel.timeline_legend
    assert_malgun(legend)
    assert legend.text() == "◆ T 트리거 ▲ C 충돌  | 속도 지점"
    assert not legend.wordWrap()
    assert not legend.hasHeightForWidth()
    assert legend.font().pixelSize() == 11
    assert legend.width() >= legend.fontMetrics().horizontalAdvance(legend.text())
    assert legend.height() < 2 * legend.fontMetrics().height()
