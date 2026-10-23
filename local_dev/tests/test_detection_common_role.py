"""One shared inference result is independently applicable to either form tab."""

from __future__ import annotations

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.object_detection import (
    ROLE_BOTH,
    ROLE_COLLISION,
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    DetectedObject,
    ObjectBox,
    VideoObjects,
    add_manual_object,
    applies_to_role,
    form_updates,
    merge_detection_result,
    set_objects_subtype,
)
from investigator.storage import CaseRepository


def document(*rows):
    return {"detections": [
        {"frame_index": frame, "class_key": key, "object_id": oid,
         "source_prompt": prompt, "bbox_xywh": [10, 20, 30, 40], "score": .9}
        for frame, key, oid, prompt in rows
    ]}


def merge(current, doc, role=ROLE_BOTH):
    return merge_detection_result(current, doc, role=role, definition=FormDefinition.default(),
                                  start_frame=2, end_frame=4, frame_step=1,
                                  classes=["vehicle", "pedestrian", "two_wheeler"])


def shared_car():
    objects, *_ = merge(VideoObjects(), document((2, "vehicle", "V001", "car"),
                                                 (3, "vehicle", "V001", "van")))
    return objects


def test_one_worker_result_produces_one_common_track_and_one_run():
    result = shared_car()
    assert len(result.objects) == len(result.runs) == 1
    item = result.objects[0]
    assert item.role == result.runs[0].role == ROLE_BOTH
    assert item.frames == [2, 3]
    assert item.subtype_key == "passenger"
    assert item.label is None  # no one tab's LabelRef is privileged
    assert applies_to_role(item, ROLE_TRIGGER)
    assert applies_to_role(item, ROLE_COLLISION)


def test_same_shared_record_bridges_to_each_tab_without_data_mutation():
    objects = shared_car()
    before = objects.to_dict()
    definition = FormDefinition.default()
    trigger = form_updates(objects.objects, ROLE_TRIGGER, definition, {})
    collision = form_updates(objects.objects, ROLE_COLLISION, definition, {})
    assert [(u.field_key, u.stored_values) for u in trigger] == [("obstacle_vehicle_types", ["승용"])]
    assert [(u.field_key, u.stored_values) for u in collision] == [("collision_targets", ["vehicle.passenger"])]
    assert objects.to_dict() == before


def test_common_role_roundtrips_with_run_and_case_data():
    result = shared_car()
    assert VideoObjects.from_dict(result.to_dict()).to_dict() == result.to_dict()
    case = InvestigationCase(videos=["sample.mp4"])
    case.set_video_objects(result)
    restored = InvestigationCase.from_dict(case.to_dict())
    assert restored.video_objects().to_dict() == result.to_dict()


@pytest.mark.parametrize("role", [ROLE_TRIGGER, ROLE_COLLISION])
def test_old_role_specific_records_remain_unchanged_and_specific(role):
    old = merge(VideoObjects(), document((2, "vehicle", "V001", "car")), role)[0]
    snapshot = old.to_dict()
    restored = VideoObjects.from_dict(snapshot)
    assert restored.to_dict() == snapshot
    item = restored.objects[0]
    other = ROLE_COLLISION if role == ROLE_TRIGGER else ROLE_TRIGGER
    assert applies_to_role(item, role)
    assert not applies_to_role(item, other)
    assert form_updates(restored.objects, other, FormDefinition.default(), {}) == []


def test_common_rerun_replaces_old_automatic_results_of_both_roles_once():
    old = VideoObjects([
        DetectedObject("t", "V001", ROLE_TRIGGER, None, [ObjectBox(2, (1, 1, 10, 10))],
                       class_key="vehicle", source=SOURCE_AUTOMATIC, subtype_key="passenger"),
        DetectedObject("c", "V002", ROLE_COLLISION, None, [ObjectBox(2, (1, 1, 10, 10))],
                       class_key="vehicle", source=SOURCE_AUTOMATIC, subtype_key="passenger"),
    ])
    before = old.to_dict()
    result, added, replaced = merge(old, document((2, "vehicle", "V001", "car")))
    assert (added, replaced) == (1, 2)
    assert len(result.objects) == 1
    assert result.objects[0].role == ROLE_BOTH
    assert result.objects[0].object_id == "V003"
    assert old.to_dict() == before


def test_shared_rerun_preserves_reviewed_manual_and_outside_range_boxes():
    old = VideoObjects([
        DetectedObject("reviewed", "V001", ROLE_TRIGGER, None, [ObjectBox(2, (1, 1, 10, 10))],
                       class_key="vehicle", source=SOURCE_AUTOMATIC, subtype_key="passenger", reviewed=True),
        DetectedObject("outside", "V002", ROLE_COLLISION, None,
                       [ObjectBox(0, (1, 1, 10, 10)), ObjectBox(2, (1, 1, 10, 10))],
                       class_key="vehicle", source=SOURCE_AUTOMATIC, subtype_key="passenger"),
    ])
    old, manual = add_manual_object(old, 2, (1, 1, 10, 10), ROLE_BOTH, None)
    result, *_ = merge(old, document())
    assert result.find("reviewed").frames == [2]
    assert result.find("outside").frames == [0]
    assert result.find(manual.uid).role == ROLE_BOTH


@pytest.mark.parametrize("role", [ROLE_TRIGGER, ROLE_COLLISION])
def test_shared_unassigned_pedestrian_is_blocked_in_either_tab(role):
    result, *_ = merge(VideoObjects(), document((2, "pedestrian", "P001", "person")))
    with pytest.raises(ValueError, match="세분류를 먼저 선택"):
        form_updates(result.objects, role, FormDefinition.default(), {})


def test_shared_pm_is_allowed_in_collision_but_still_warns_for_trigger():
    result, *_ = merge(VideoObjects(), document((2, "two_wheeler", "M001", "scooter")))
    result = set_objects_subtype(result, [result.objects[0].uid], "pm")
    with pytest.raises(ValueError, match="PM 선택지가 없어"):
        form_updates(result.objects, ROLE_TRIGGER, FormDefinition.default(), {})
    updates = form_updates(result.objects, ROLE_COLLISION, FormDefinition.default(), {})
    assert updates[0].stored_values == ["vehicle.pm"]


@pytest.mark.parametrize("role", [ROLE_TRIGGER, ROLE_COLLISION])
def test_shared_mixed_id_is_blocked_in_either_tab(role):
    result = shared_car()
    result = set_objects_subtype(result, [result.objects[0].uid], "truck", frame=3)
    with pytest.raises(ValueError, match="같은 ID에 서로 다른 세분류"):
        form_updates(result.objects, role, FormDefinition.default(), {})


def test_common_role_is_not_itself_a_form_tab():
    with pytest.raises(ValueError, match="조사 탭"):
        form_updates(shared_car().objects, ROLE_BOTH, FormDefinition.default(), {})


def test_common_annotations_survive_database_reload_without_changing_form(tmp_path):
    repository = CaseRepository(tmp_path / "project")
    case = InvestigationCase(videos=["sample.mp4"],
                             values={"institution": "samsung", "accident_classification": "2020011300001",
                                     "remarks": "keep"})
    repository.save([case])
    values = dict(case.values)
    common = shared_car()
    case.set_video_objects(common)
    repository.save_changes([case])
    restored = CaseRepository(repository.workspace).load()[0]
    assert restored.video_objects().to_dict() == common.to_dict()
    assert restored.values == values
    assert restored.status == "draft" and restored.completed_at is None


def test_common_run_does_not_delete_unselected_classes_of_either_legacy_role():
    previous = VideoObjects([
        DetectedObject("t", "P001", ROLE_TRIGGER, None, [ObjectBox(2, (1, 1, 10, 10))],
                       class_key="pedestrian", source=SOURCE_AUTOMATIC, subtype_key="adult"),
        DetectedObject("c", "P002", ROLE_COLLISION, None, [ObjectBox(2, (1, 1, 10, 10))],
                       class_key="pedestrian", source=SOURCE_AUTOMATIC, subtype_key="child"),
    ])
    result, added, replaced = merge_detection_result(
        previous, document(), role=ROLE_BOTH, definition=FormDefinition.default(),
        start_frame=2, end_frame=4, frame_step=1, classes=["vehicle"],
    )
    assert (added, replaced) == (0, 0)
    assert [item.to_dict() for item in result.objects] == [item.to_dict() for item in previous.objects]
