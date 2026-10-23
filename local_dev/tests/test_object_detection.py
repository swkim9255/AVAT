from __future__ import annotations

import json

import pytest

from investigator.detection_taxonomy import SUBTYPES
from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.object_detection import (
    CLASS_BY_KEY,
    DETECTION_CLASSES,
    ROLE_COLLISION,
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    LabelRef,
    VideoObjects,
    add_manual_object,
    delete_box,
    form_updates,
    label_options,
    label_text,
    merge_detection_result,
    set_objects_label,
    set_objects_reviewed,
    set_objects_role,
)


@pytest.fixture()
def definition() -> FormDefinition:
    return FormDefinition.default()


def _doc(*detections):  # type: ignore[no-untyped-def]
    return {
        "provenance": {"detector": "sam3", "model_id": "fake", "device": "cpu"},
        "detections": list(detections),
    }


def _det(frame, key, object_id, x=10.0, score=0.9):  # type: ignore[no-untyped-def]
    return {
        "frame_index": frame,
        "class_key": key,
        "object_id": object_id,
        "bbox_xywh": [x, 20.0, 50.0, 40.0],
        "score": score,
    }


def test_class_set_is_union_of_obstacles_and_collision_targets(definition):
    trigger = {option.ref for option in label_options(definition, ROLE_TRIGGER)}
    collision = {option.ref for option in label_options(definition, ROLE_COLLISION)}
    # Detection has independent coarse classes; survey bridging uses subtypes.
    assert {LabelRef(*item.collision) for item in SUBTYPES} == collision
    assert all(LabelRef(*item.trigger) in trigger for item in SUBTYPES if item.key != "pm")
    assert LabelRef("obstacle_vehicle_types", "PM") not in trigger
    assert len(collision) == len(SUBTYPES) == 26
    assert len(DETECTION_CLASSES) == 21


def test_merge_groups_by_track_and_uses_role_labels(definition):
    doc = _doc(
        _det(3, "passenger", "V001"),
        _det(4, "passenger", "V001", x=12),
        _det(3, "streettree", "R001"),
    )
    objects, added, replaced = merge_detection_result(
        VideoObjects(),
        doc,
        role=ROLE_COLLISION,
        definition=definition,
        start_frame=3,
        end_frame=4,
        frame_step=1,
        classes=["passenger", "streettree"],
    )
    assert (added, replaced) == (2, 0)
    car = next(item for item in objects.objects if item.subtype_key == "passenger")
    assert car.class_key == "vehicle"
    assert car.frames == [3, 4] and car.object_id == "V001"
    assert label_text(definition, car.role, car.label) == "차량 · 승용"
    tree = next(item for item in objects.objects if item.class_key == "streettree")
    assert tree.label == LabelRef("collision_targets", "building.streettree")
    assert len(objects.runs) == 1


def test_rerun_replaces_only_unreviewed_automatic_boxes_in_range(definition):
    first, *_ = merge_detection_result(
        VideoObjects(),
        _doc(_det(1, "passenger", "V001"), _det(5, "passenger", "V001")),
        role=ROLE_TRIGGER,
        definition=definition,
        start_frame=0,
        end_frame=9,
        frame_step=1,
        classes=["passenger"],
    )
    first, manual = add_manual_object(first, 2, (0, 0, 10, 10), ROLE_TRIGGER, None)
    second, added, replaced = merge_detection_result(
        first,
        _doc(_det(1, "truck", "V001")),
        role=ROLE_TRIGGER,
        definition=definition,
        start_frame=0,
        end_frame=3,
        frame_step=1,
        classes=["truck"],
    )
    assert (added, replaced) == (1, 1)
    old_car = next(item for item in second.objects if item.subtype_key == "passenger")
    assert old_car.frames == [5]  # box outside the range stays
    assert second.find(manual.uid) is not None
    truck = next(item for item in second.objects if item.subtype_key == "truck")
    assert truck.object_id == "V002"  # no ID reuse


def test_reviewed_objects_survive_rerun(definition):
    objects, *_ = merge_detection_result(
        VideoObjects(),
        _doc(_det(1, "adult", "P001")),
        role=ROLE_TRIGGER,
        definition=definition,
        start_frame=1,
        end_frame=1,
        frame_step=1,
        classes=["adult"],
    )
    uid = objects.objects[0].uid
    objects = set_objects_reviewed(objects, [uid], True)
    objects, _, replaced = merge_detection_result(
        objects,
        _doc(),
        role=ROLE_TRIGGER,
        definition=definition,
        start_frame=0,
        end_frame=5,
        frame_step=1,
        classes=["adult"],
    )
    assert replaced == 0 and objects.find(uid) is not None


def test_role_change_converts_label(definition):
    objects, *_ = merge_detection_result(
        VideoObjects(),
        _doc(_det(1, "pole_streetlight", "R001")),
        role=ROLE_TRIGGER,
        definition=definition,
        start_frame=1,
        end_frame=1,
        frame_step=1,
        classes=["pole_streetlight"],
    )
    uid = objects.objects[0].uid
    assert objects.objects[0].label == CLASS_BY_KEY["pole_streetlight"].trigger
    converted = set_objects_role(objects, [uid], ROLE_COLLISION, definition)
    assert converted.objects[0].label == CLASS_BY_KEY["pole_streetlight"].collision
    back = set_objects_role(converted, [uid], ROLE_TRIGGER, definition)
    assert back.objects[0].label == CLASS_BY_KEY["pole_streetlight"].trigger


def test_label_must_match_role(definition):
    objects, item = add_manual_object(VideoObjects(), 0, (0, 0, 20, 20), ROLE_TRIGGER, None)
    with pytest.raises(ValueError):
        set_objects_label(
            objects, [item.uid], LabelRef("collision_targets", "vehicle.van"), definition
        )
    labelled = set_objects_label(
        objects, [item.uid], LabelRef("obstacle_other", "동물"), definition
    )
    assert labelled.objects[0].reviewed


def test_manual_box_rejects_tiny_rect():
    with pytest.raises(ValueError):
        add_manual_object(VideoObjects(), 0, (0, 0, 1, 1), ROLE_TRIGGER, None)


def test_delete_last_box_removes_object():
    objects, item = add_manual_object(VideoObjects(), 4, (0, 0, 20, 20), ROLE_COLLISION, None)
    assert delete_box(objects, item.uid, 4).is_empty()


def test_form_updates_are_additive_and_ordered(definition):
    objects, *_ = merge_detection_result(
        VideoObjects(),
        _doc(_det(1, "truck", "V001"), _det(1, "passenger", "V002"), _det(1, "streettree", "R001")),
        role=ROLE_COLLISION,
        definition=definition,
        start_frame=1,
        end_frame=1,
        frame_step=1,
        classes=["truck", "passenger", "streettree"],
    )
    values = definition.stored_values({"collision_targets": ["vehicle.truck"]})
    updates = form_updates(objects.objects, ROLE_COLLISION, definition, values)
    assert len(updates) == 1
    update = updates[0]
    assert update.field_key == "collision_targets"
    canonical = definition.canonical_values({"collision_targets": update.stored_values})
    assert canonical["collision_targets"] == [
        "vehicle.passenger",
        "vehicle.truck",
        "building.streettree",
    ]
    assert sorted(update.added_texts) == sorted(["차량 · 승용", "건물/조경 · 가로수(수목)"])
    assert form_updates(objects.objects, ROLE_TRIGGER, definition, values) == []


def test_case_round_trip_keeps_objects(tmp_path, definition):
    video = str(tmp_path / "a.mp4")
    case = InvestigationCase(videos=[video])
    assert "objects_by_video" not in case.to_dict()  # legacy payload unchanged
    objects, *_ = merge_detection_result(
        VideoObjects(),
        _doc(_det(2, "bicycle", "B001")),
        role=ROLE_TRIGGER,
        definition=definition,
        start_frame=2,
        end_frame=2,
        frame_step=1,
        classes=["bicycle"],
    )
    case.set_video_objects(objects)
    restored = InvestigationCase.from_dict(json.loads(json.dumps(case.to_dict())))
    item = restored.video_objects().objects[0]
    assert item.source == SOURCE_AUTOMATIC and item.frames == [2]
    assert item.label == CLASS_BY_KEY["bicycle"].trigger
    restored.remove_video(0)
    assert restored.objects_by_video == {}
