"""Non-destructive confidence display filtering and large-result selection."""

from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtCore import QSettings, Qt

from investigator.form_definition import FormDefinition
from investigator.object_detection import (
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    SOURCE_MANUAL,
    DetectedObject,
    LabelRef,
    ObjectBox,
    VideoObjects,
)
from investigator.widgets import detection_panel as panel_module
from investigator.widgets.detection_panel import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    MAX_EXPANDED_TABLE_ROWS,
    SETTINGS_CONFIDENCE_THRESHOLD,
    DetectionPanel,
)


def _object(uid, scores, *, source=SOURCE_AUTOMATIC, reviewed=False):
    return DetectedObject(
        uid=uid,
        object_id=uid,
        role=ROLE_TRIGGER,
        label=LabelRef("obstacle_vehicle_types", "승용"),
        boxes=[ObjectBox(frame, (10, 10, 30, 30), score) for frame, score in scores],
        source=source,
        reviewed=reviewed,
    )


@pytest.fixture()
def panel(qtbot):
    widget = DetectionPanel()
    widget.set_video(100, 30.0)
    qtbot.addWidget(widget)
    return widget


def _mixed_objects():
    return VideoObjects(
        [
            _object("low", [(0, 0.2), (1, 0.4)], reviewed=True),
            _object("mixed", [(0, 0.9), (1, 0.2), (2, 0.8)]),
            _object("exact", [(1, 0.5)]),
            _object("manual", [(1, 0.1)], source=SOURCE_MANUAL),
            _object("unknown-score", [(1, None)]),
        ]
    )


def test_confidence_default_and_control_explain_non_destructive_filter(panel):
    assert panel.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD == 0.5
    assert panel.confidence_spin.minimum() == 0.0
    assert panel.confidence_spin.maximum() == 1.0
    assert panel.confidence_spin.singleStep() == 0.05
    assert "삭제하지 않으며" in panel.confidence_spin.toolTip()
    assert "직접 그린 박스" in panel.confidence_spin.toolTip()


def test_filter_is_per_box_including_reviewed_automatic_results_and_keeps_raw_data(panel):
    objects = _mixed_objects()
    raw = deepcopy(objects.to_dict())
    panel.set_objects(objects, FormDefinition.default())
    assert panel._table_uids == ("mixed", "exact", "manual", "unknown-score")
    row = panel._table_rows_by_uid["mixed"]
    assert panel.table.item(row, 3).text() == "1~3"
    assert panel.table.item(row, 4).text() == "2"
    assert panel.table.item(row, 5).text() == "0.85"
    assert "검출 객체 5개 · 표시 4개" in panel.objects_title.text()
    panel.confidence_spin.setValue(0.0)
    assert panel.table.rowCount() == 5
    assert panel.table.item(panel._table_rows_by_uid["mixed"], 4).text() == "3"
    assert objects.to_dict() == raw


def test_current_frame_filter_uses_that_frames_box_score_not_object_average(panel):
    panel.set_objects(_mixed_objects(), FormDefinition.default())
    panel.current_only.setChecked(True)
    panel.set_current_frame(1)
    assert panel._table_uids == ("exact", "manual", "unknown-score")
    panel.set_current_frame(0)
    assert panel._table_uids == ("mixed",)
    panel.confidence_spin.setValue(0.95)
    assert panel.table.rowCount() == 0
    panel.set_current_frame(1)
    assert panel._table_uids == ("manual", "unknown-score")
    panel.current_only.setChecked(False)
    assert panel._table_uids == ("manual", "unknown-score")


def test_lowering_threshold_restores_hidden_objects_and_clears_hidden_selection(panel, qtbot):
    panel.set_objects(_mixed_objects(), FormDefinition.default())
    panel.select_uids(["exact"])
    with qtbot.waitSignal(panel.confidence_threshold_changed) as signal:
        panel.confidence_spin.setValue(0.7)
    assert signal.args == [0.7]
    assert panel.selected_uids() == []
    panel.confidence_spin.setValue(0.0)
    assert panel.table.rowCount() == 5


def test_threshold_persists_and_does_not_trigger_detection(panel, qtbot):
    calls = []
    panel.run_requested.connect(lambda: calls.append(True))
    panel.confidence_spin.setValue(0.75)
    assert QSettings().value(SETTINGS_CONFIDENCE_THRESHOLD, type=float) == 0.75
    reopened = DetectionPanel()
    qtbot.addWidget(reopened)
    assert reopened.confidence_threshold == 0.75
    assert calls == []


@pytest.mark.parametrize("stored", ["bad", "nan", "inf", []])
def test_invalid_persisted_threshold_uses_safe_default(qtbot, stored):
    QSettings().setValue(SETTINGS_CONFIDENCE_THRESHOLD, stored)
    panel = DetectionPanel()
    qtbot.addWidget(panel)
    assert panel.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD


def test_double_click_seeks_nearest_visible_box_not_hidden_box(panel, qtbot):
    panel.set_objects(_mixed_objects(), FormDefinition.default())
    panel.set_current_frame(1)
    row = panel._table_rows_by_uid["mixed"]
    with qtbot.waitSignal(panel.seek_requested) as signal:
        panel.table.cellDoubleClicked.emit(row, 0)
    assert signal.args == [0]


def test_large_table_caches_label_options_and_click_selects_without_recreating_cells(
    panel, monkeypatch
):
    options_calls = []
    original_options = panel_module.subtype_options

    def counted_options(group):
        options_calls.append(group)
        return original_options(group)

    monkeypatch.setattr(panel_module, "subtype_options", counted_options)
    objects = VideoObjects(
        [_object(f"V{index:04}", [(0, 0.9)]) for index in range(2255)]
    )
    panel.set_objects(objects, FormDefinition.default())
    assert len(options_calls) == len(panel_module.DETECTION_CLASSES)
    assert panel.table.rowCount() == 2255
    assert panel.table.height() < 3000
    assert panel.table.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert panel.table.rowCount() > MAX_EXPANDED_TABLE_ROWS
    first_cell = panel.table.item(0, 0)
    last_cell = panel.table.item(2254, 0)
    selection_signals = []
    panel.selection_changed.connect(selection_signals.append)

    def forbidden_rebuild(*args, **kwargs):
        pytest.fail("Selecting an existing canvas object must not rebuild the object table")

    monkeypatch.setattr(panel, "_rebuild_table", forbidden_rebuild)
    panel.select_uids(["V2254"])
    assert panel.selected_uids() == ["V2254"]
    assert selection_signals == [["V2254"]]
    assert panel.table.item(0, 0) is first_cell
    assert panel.table.item(2254, 0) is last_cell
    assert len(options_calls) == len(panel_module.DETECTION_CLASSES)
    panel.select_uids(["V2254"])
    assert selection_signals == [["V2254"]]
    panel.select_uids([])
    assert panel.selected_uids() == []
    assert selection_signals == [["V2254"], []]


def test_deleted_label_uses_cached_fallback_without_form_copies(panel):
    item = _object("missing", [(0, 0.9)])
    item.label = LabelRef("removed-field", "removed-choice")
    panel.set_objects(VideoObjects([item]), FormDefinition.default())
    assert "세분류 미지정" in panel.table.item(0, 2).text()
