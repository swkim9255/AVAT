"""Independent detector labels and explicit, atomic investigation bridging."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from investigator.detection_controller import DetectionMixin
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.object_detection import (
    ROLE_COLLISION,
    ROLE_TRIGGER,
    DetectedObject,
    ObjectBox,
    VideoObjects,
    effective_annotation,
    form_updates,
    object_subtypes,
    set_objects_subtype,
    split_object_frames,
)
from investigator.widgets.detection_panel import DetectionPanel


def tracked(group="pedestrian", subtype=None, role=ROLE_TRIGGER):
    return DetectedObject(
        "track", "P001", role, None,
        [ObjectBox(0, (10, 20, 30, 40), .9, "person"),
         ObjectBox(1, (12, 20, 30, 40), .9, "person")],
        class_key=group, subtype_key=subtype,
    )


@pytest.fixture()
def panel(qtbot):
    widget = DetectionPanel()
    qtbot.addWidget(widget)
    widget.set_video(30, 30)
    return widget


def options(combo):
    return {combo.itemData(index): combo.itemText(index) for index in range(combo.count())}


def test_fixed_subtype_options_ignore_form_choice_renames_and_deletions(panel):
    definition = FormDefinition.default()
    records = definition.records()
    for record in records:
        if record["key"] == "obstacle_pedestrians":
            record["choices"][0]["label"] = "조사 양식에서 변경한 이름"
    changed = FormDefinition.from_dict({**definition.to_dict(), "fields": records})
    panel.set_objects(VideoObjects([tracked()]), changed)
    panel.select_uids(["track"])
    assert options(panel.label_combo) == {None: "세분류 미지정", "child": "어린이", "adult": "어른"}
    assert panel.label_combo.currentData() is None


def test_pm_stays_available_independently_of_trigger_form(panel):
    panel.set_objects(VideoObjects([tracked("two_wheeler", "motorcycle")]), FormDefinition.default())
    panel.select_uids(["track"])
    assert set(options(panel.label_combo)) == {None, "motorcycle", "pm"}


def test_frame_scope_and_id_scope_emit_distinct_requests(panel):
    panel.set_objects(VideoObjects([tracked(subtype="child")]), FormDefinition.default())
    panel.select_uids(["track"])
    events = []
    panel.subtype_change_requested.connect(lambda *args: events.append(args))
    index = panel.label_combo.findData("adult")
    panel._label_chosen(index)
    assert events[-1] == (["track"], "adult", None, None)
    panel.all_id_frames.setChecked(False)
    panel.set_current_frame(1)
    panel._label_chosen(panel.label_combo.findData("adult"))
    assert events[-1] == (["track"], "adult", 1, None)


def test_mixed_id_is_visible_and_can_be_split_without_changing_coordinates(panel):
    original = VideoObjects([tracked(subtype="child")])
    mixed = set_objects_subtype(original, ["track"], "adult", frame=1)
    panel.set_objects(mixed, FormDefinition.default())
    panel.select_uids(["track"])
    assert "혼합" in panel.table.item(0, 2).text()
    with pytest.raises(ValueError, match="같은 ID"):
        form_updates(mixed.objects, ROLE_TRIGGER, FormDefinition.default(), {})
    separated, new_item = split_object_frames(mixed, "track", [1])
    assert new_item.object_id != "P001"
    assert new_item.boxes[0] == original.objects[0].boxes[1]
    assert object_subtypes(new_item) == {"adult"}
    assert all(len(object_subtypes(item)) == 1 for item in separated.objects)
    assert form_updates(separated.objects, ROLE_TRIGGER, FormDefinition.default(), {})


def test_whole_id_unifies_overrides_and_does_not_auto_review(panel):
    objects = set_objects_subtype(VideoObjects([tracked(subtype="child")]), ["track"], "adult", frame=1)
    unified = set_objects_subtype(objects, ["track"], "adult")
    assert object_subtypes(unified.objects[0]) == {"adult"}
    assert all(effective_annotation(unified.objects[0], frame).subtype_key == "adult" for frame in (0, 1))
    assert not unified.objects[0].reviewed


@pytest.mark.parametrize("mode", ["missing", "mixed", "pm"])
def test_controller_blocks_before_touching_any_investigation_value(monkeypatch, mode):
    objects = VideoObjects([tracked()])
    if mode == "mixed":
        objects = set_objects_subtype(objects, ["track"], "child", frame=0)
        objects = set_objects_subtype(objects, ["track"], "adult", frame=1)
    elif mode == "pm":
        objects = VideoObjects([tracked("two_wheeler", "pm")])
    case = InvestigationCase(videos=["video.mp4"], values={"obstacle_exists": "무"})
    case.set_video_objects(objects)
    before = dict(case.values)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))

    class Window(DetectionMixin):
        repository = SimpleNamespace(form_definition=FormDefinition.default())
        investigation = SimpleNamespace(editors={})

        def _detection_case(self):
            return case, True

        def _field_changed(self, *_args):
            pytest.fail("Invalid detection must not change the investigation")

    Window().apply_objects_to_form(ROLE_TRIGGER)
    assert warnings
    assert case.values == before
    if mode == "pm":
        assert "PM 선택지가 없어" in warnings[0]


def test_pm_collision_bridge_is_supported_without_trigger_schema_change():
    objects = [tracked("two_wheeler", "pm", ROLE_COLLISION)]
    updates = form_updates(objects, ROLE_COLLISION, FormDefinition.default(), {})
    assert updates[0].field_key == "collision_targets"
    assert "vehicle.pm" in updates[0].stored_values
