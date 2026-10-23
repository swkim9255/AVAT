"""Independent detector taxonomy, frame annotations, and explicit form bridging."""

from __future__ import annotations

from copy import deepcopy

import pytest

from investigator.detection_taxonomy import (
    SUBTYPE_BY_KEY,
    SUBTYPES,
    coarse_class_key,
    subtype_options,
    suggest_subtype,
    taxonomy_hash,
    taxonomy_records,
)
from investigator.form_definition import FormDefinition
from investigator.object_detection import (
    DETECTION_CLASSES,
    ROLE_COLLISION,
    ROLE_TRIGGER,
    DetectedObject,
    LabelRef,
    ObjectBox,
    VideoObjects,
    add_manual_object,
    delete_box,
    effective_annotation,
    form_updates,
    merge_detection_result,
    object_display_text,
    object_subtypes,
    set_objects_reviewed,
    set_objects_role,
    set_objects_subtype,
    split_object_frames,
    worker_class_config,
)
from investigator.object_history import ObjectEditHistory


def _detect(key="vehicle", *, prompts=("car", "truck"), role=ROLE_TRIGGER):
    result = {
        "detections": [
            {
                "frame_index": frame,
                "class_key": key,
                "object_id": "track1",
                "bbox_xywh": [1.123456, 2.345678, 20.234567, 30.456789],
                "score": 0.9 - frame / 100,
                "source_prompt": prompt,
            }
            for frame, prompt in enumerate(prompts)
        ]
    }
    return merge_detection_result(
        VideoObjects(),
        result,
        role=role,
        definition=FormDefinition.default(),
        start_frame=0,
        end_frame=10,
        frame_step=1,
        classes=[key],
    )[0]


def test_fixed_taxonomy_has_coarse_union_and_removed_prompts():
    assert len(DETECTION_CLASSES) == 21
    assert len(SUBTYPES) == 26
    prompts = [prompt for item in DETECTION_CLASSES for prompt in item.prompts]
    assert len(prompts) == len(set(prompts)) == 28
    assert "person" in prompts
    assert not {
        "child",
        "adult person",
        "tow truck",
        "fire truck",
        "ambulance",
        "electric kick scooter",
    }.intersection(prompts)
    assert {item.key for item in subtype_options("vehicle")} == {
        "passenger",
        "van",
        "truck",
        "special",
    }
    assert {item.key for item in subtype_options("two_wheeler")} == {"motorcycle", "pm"}


def test_taxonomy_records_detached_and_worker_metadata_present():
    digest = taxonomy_hash()
    records = taxonomy_records()
    records[0]["prompts"].append("do not leak")
    assert taxonomy_hash() == digest
    assert "do not leak" not in taxonomy_records()[0]["prompts"]
    config = worker_class_config(["vehicle", "pedestrian"])
    assert config["taxonomy_hash"] == digest
    assert config["classes"][0]["nms_iou"] == 0.6
    assert config["classes"][1]["prompts"] == ["person"]


@pytest.mark.parametrize(
    ("key", "prompt", "expected"),
    [
        ("vehicle", "car", "passenger"),
        ("vehicle", "van", "van"),
        ("vehicle", "bus", "van"),
        ("vehicle", "truck", "truck"),
        ("vehicle", "excavator", "special"),
        ("vehicle", None, None),
        ("two_wheeler", "scooter", "motorcycle"),
        ("pedestrian", "person", None),
        ("bicycle", "bicycle", "bicycle"),
    ],
)
def test_prompt_defaults(key, prompt, expected):
    assert suggest_subtype(key, prompt) == expected


def test_merge_preserves_per_frame_prompts_and_best_observation_default():
    objects = _detect(prompts=("car", "truck", "bus"))
    item = objects.objects[0]
    assert item.class_key == item.model_class_key == "vehicle"
    assert item.subtype_key == item.suggested_subtype_key == "passenger"
    assert item.subtype_source == "default" and not item.reviewed
    assert [box.source_prompt for box in item.boxes] == ["car", "truck", "bus"]
    assert item.source_prompt == "car"
    assert VideoObjects.from_dict(objects.to_dict()).to_dict() == objects.to_dict()


def test_form_changes_do_not_rename_detector_options_or_raw_storage():
    objects = _detect()
    before = objects.to_dict()
    definition = FormDefinition.default()
    record = next(
        record for record in definition.records() if record["key"] == "obstacle_vehicle_types"
    )
    car = next(choice for choice in record["choices"] if choice["id"] == "승용")
    car.update(label="사용자 승용", value="custom.car")
    assert SUBTYPE_BY_KEY["passenger"].label == "승용"
    assert object_display_text(objects.objects[0]) == "차량 · 승용"
    updates = form_updates(objects.objects, ROLE_TRIGGER, definition, {})
    assert updates[0].stored_values == ["custom.car"]
    record["choices"].remove(car)
    with pytest.raises(ValueError, match="선택지가 없어"):
        form_updates(objects.objects, ROLE_TRIGGER, definition, {})
    assert objects.to_dict() == before
    assert len(subtype_options("vehicle")) == 4


def test_pedestrian_without_subtype_blocks_complete_apply_and_preserves_values():
    pedestrian = _detect("pedestrian", prompts=("person",))
    vehicle = _detect()
    values = {"remarks": "원래 메모", "obstacle_vehicle_types": ["화물"]}
    snapshot = deepcopy(values)
    with pytest.raises(ValueError, match="세분류를 먼저 선택"):
        form_updates(
            [*vehicle.objects, *pedestrian.objects], ROLE_TRIGGER, FormDefinition.default(), values
        )
    assert values == snapshot
    assert object_display_text(pedestrian.objects[0]) == "보행자 · 세분류 미지정"


def test_pm_trigger_blocks_but_collision_has_pm_and_role_preserves_subtype():
    objects = _detect("two_wheeler", prompts=("scooter",))
    uid = objects.objects[0].uid
    objects = set_objects_subtype(objects, [uid], "pm")
    definition = FormDefinition.default()
    with pytest.raises(ValueError, match=r"\[객체\] 탭에는 PM 선택지가 없어 반영할 수 없습니다\."):
        form_updates(objects.objects, ROLE_TRIGGER, definition, {})
    collision = set_objects_role(objects, [uid], ROLE_COLLISION, definition)
    assert collision.find(uid).subtype_key == "pm"
    assert form_updates(collision.objects, ROLE_COLLISION, definition, {})[0].stored_values == [
        "vehicle.pm",
    ]


def test_current_frame_edit_creates_mixed_id_and_blocks_even_other_frame_apply():
    objects = _detect()
    uid = objects.objects[0].uid
    before = objects.to_dict()
    edited = set_objects_subtype(objects, [uid], "truck", frame=1)
    item = edited.find(uid)
    assert object_subtypes(item) == {"passenger", "truck"}
    assert effective_annotation(item, 0).subtype_key == "passenger"
    assert effective_annotation(item, 1).subtype_key == "truck"
    assert "혼합" in object_display_text(item)
    assert objects.to_dict() == before
    with pytest.raises(ValueError, match="같은 ID에 서로 다른 세분류"):
        form_updates([item], ROLE_TRIGGER, FormDefinition.default(), {})
    assert (
        VideoObjects.from_dict(edited.to_dict()).find(uid).frame_annotations
        == item.frame_annotations
    )


def test_whole_id_assignment_resolves_mixed_and_does_not_touch_geometry():
    objects = _detect()
    uid = objects.objects[0].uid
    original_boxes = objects.find(uid).boxes
    edited = set_objects_subtype(objects, [uid], "truck", frame=1)
    unified = set_objects_subtype(edited, [uid], "van")
    item = unified.find(uid)
    assert object_subtypes(item) == {"van"} and not item.frame_annotations
    assert not item.reviewed and item.subtype_source == "user"
    assert item.boxes == original_boxes
    assert form_updates([item], ROLE_TRIGGER, FormDefinition.default(), {})


def test_split_and_delete_each_resolve_mixed_id_without_changing_boxes():
    objects = _detect()
    uid = objects.objects[0].uid
    edited = set_objects_subtype(objects, [uid], "truck", frame=1)
    split, new = split_object_frames(edited, uid, [1])
    assert split.find(uid).object_id == "V001" and new.object_id == "V002"
    assert new.uid != uid
    assert object_subtypes(split.find(uid)) == {"passenger"}
    assert object_subtypes(new) == {"truck"}
    assert (
        sorted([box for obj in split.objects for box in obj.boxes], key=lambda box: box.frame)
        == edited.find(uid).boxes
    )
    assert form_updates(split.objects, ROLE_TRIGGER, FormDefinition.default(), {})
    deleted = delete_box(edited, uid, 1)
    assert not deleted.find(uid).frame_annotations
    assert form_updates(deleted.objects, ROLE_TRIGGER, FormDefinition.default(), {})


def test_invalid_category_and_special_detail_fail_atomically():
    objects = _detect()
    before = objects.to_dict()
    with pytest.raises(ValueError, match="대분류"):
        set_objects_subtype(objects, [objects.objects[0].uid], "adult")
    with pytest.raises(ValueError, match="특수 세분류"):
        set_objects_subtype(
            objects, [objects.objects[0].uid], "passenger", special_vehicle_type="ambulance"
        )
    assert objects.to_dict() == before
    special = set_objects_subtype(
        objects, [objects.objects[0].uid], "special", special_vehicle_type="ambulance"
    )
    assert special.objects[0].special_vehicle_type == "ambulance"


def test_manual_object_can_choose_fixed_subtype_without_survey_option():
    objects, item = add_manual_object(VideoObjects(), 0, (0, 0, 20, 20), ROLE_TRIGGER, None)
    objects = set_objects_subtype(objects, [item.uid], "pm")
    assert objects.find(item.uid).class_key == "two_wheeler"
    assert objects.find(item.uid).subtype_key == "pm"


@pytest.mark.parametrize(
    ("old", "coarse", "subtype", "detail"),
    [
        ("child", "pedestrian", "child", None),
        ("adult", "pedestrian", "adult", None),
        ("passenger", "vehicle", "passenger", None),
        ("pm", "two_wheeler", "pm", None),
        ("ambulance", "vehicle", "special", "ambulance"),
        ("special", "vehicle", "special", None),
    ],
)
def test_legacy_migration_preserves_identity_coordinates_review(old, coarse, subtype, detail):
    raw = {
        "uid": "keep",
        "object_id": "old-id",
        "class_key": old,
        "role": ROLE_TRIGGER,
        "source": "automatic_detection",
        "reviewed": True,
        "boxes": [{"frame": 7, "bbox": [1.123456, 2.2, 30.34, 40.45], "score": 0.87}],
    }
    before = deepcopy(raw)
    item = DetectedObject.from_dict(raw)
    assert item.uid == "keep" and item.object_id == "old-id"
    assert item.class_key == coarse and item.subtype_key == subtype
    assert item.special_vehicle_type == detail
    assert item.reviewed and item.source_prompt is None
    assert item.boxes[0].bbox == tuple(raw["boxes"][0]["bbox"])
    assert raw == before
    assert DetectedObject.from_dict(item.to_dict()).to_dict() == item.to_dict()
    assert coarse_class_key(old) == coarse


def test_legacy_corrected_label_wins_and_unknown_user_label_survives():
    raw = {
        "uid": "old",
        "object_id": "V001",
        "class_key": "passenger",
        "role": ROLE_TRIGGER,
        "label": LabelRef("obstacle_vehicle_types", "화물").to_dict(),
        "boxes": [ObjectBox(0, (1, 2, 3, 4)).to_dict()],
    }
    item = DetectedObject.from_dict(raw)
    assert item.class_key == "vehicle" and item.subtype_key == "truck"
    raw["label"] = LabelRef("custom", "custom-id").to_dict()
    item = DetectedObject.from_dict(raw)
    assert item.label == LabelRef("custom", "custom-id") and item.subtype_key is None
    assert item.subtype_source == "legacy_unmapped"
    assert DetectedObject.from_dict(item.to_dict()).label == item.label


def test_per_frame_review_survives_rerun_and_deleted_overrides_are_pruned():
    objects = _detect()
    uid = objects.objects[0].uid
    objects = set_objects_reviewed(objects, [uid], True, frame=1)
    result, added, replaced = merge_detection_result(
        objects,
        {"detections": []},
        role=ROLE_TRIGGER,
        definition=FormDefinition.default(),
        start_frame=0,
        end_frame=1,
        frame_step=1,
        classes=["vehicle"],
    )
    assert (added, replaced) == (0, 1)
    assert result.find(uid).frames == [1]
    assert effective_annotation(result.find(uid), 1).reviewed
    assert VideoObjects.from_dict(result.to_dict()).to_dict() == result.to_dict()


def test_user_subtype_is_not_removed_by_rerun_after_unchecking_review():
    objects = _detect()
    uid = objects.objects[0].uid
    objects = set_objects_subtype(objects, [uid], "truck", frame=1)
    objects = set_objects_reviewed(objects, [uid], False)
    result, _, _ = merge_detection_result(
        objects,
        {"detections": []},
        role=ROLE_TRIGGER,
        definition=FormDefinition.default(),
        start_frame=0,
        end_frame=1,
        frame_step=1,
        classes=["vehicle"],
    )
    assert result.find(uid).frames == [1]
    assert effective_annotation(result.find(uid), 1).subtype_key == "truck"
    assert not effective_annotation(result.find(uid), 1).reviewed


def test_subtype_edit_and_review_are_independent_and_noop_preserves_review():
    objects = _detect()
    uid = objects.objects[0].uid
    reviewed = set_objects_reviewed(objects, [uid], True)
    noop = set_objects_subtype(reviewed, [uid], "passenger")
    assert noop.to_dict() == reviewed.to_dict()
    edited = set_objects_subtype(reviewed, [uid], "truck", frame=1)
    assert effective_annotation(edited.find(uid), 0).reviewed
    assert not effective_annotation(edited.find(uid), 1).reviewed
    confirmed = set_objects_reviewed(edited, [uid], True, frame=1)
    assert effective_annotation(confirmed.find(uid), 1).reviewed
    assert set_objects_subtype(confirmed, [uid], "truck", frame=1).to_dict() == confirmed.to_dict()
    unified = set_objects_subtype(confirmed, [uid], "van")
    assert not unified.find(uid).reviewed
    assert not unified.find(uid).frame_annotations


def test_annotation_split_undo_redo_roundtrip():
    objects = _detect()
    uid = objects.objects[0].uid
    edited = set_objects_subtype(objects, [uid], "truck", frame=1)
    split, new = split_object_frames(edited, uid, [1])
    history = ObjectEditHistory()
    key = ("workspace", "case", "video")
    assert history.record(
        key, edited, split, before_selection=[uid], after_selection=[new.uid], frame=1
    )
    undone = history.undo(key, split)
    assert undone.objects.to_dict() == edited.to_dict()
    assert history.redo(key, undone.objects).objects.to_dict() == split.to_dict()


def test_unknown_future_version_and_orphan_annotation_are_rejected():
    objects = _detect()
    raw = objects.to_dict()
    raw["object_schema_version"] = 3
    with pytest.raises(ValueError, match="버전"):
        VideoObjects.from_dict(raw)
    raw = objects.objects[0].to_dict()
    raw["frame_annotations"]["999"] = {"subtype_key": "truck"}
    with pytest.raises(ValueError, match="박스가 없는 프레임"):
        DetectedObject.from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("subtype_key", "not-a-subtype", ValueError),
        ("suggested_subtype_key", "not-a-subtype", ValueError),
        ("reviewed", "false", TypeError),
    ],
)
def test_v2_invalid_annotation_fields_not_silently_discarded(field, value, error):
    raw = _detect().objects[0].to_dict()
    raw[field] = value
    with pytest.raises(error):
        DetectedObject.from_dict(raw)


def test_outer_v2_and_missing_inner_version_preserve_explicit_missing_subtype():
    raw = _detect().to_dict()
    item = raw["objects"][0]
    del item["object_schema_version"]
    item["subtype_key"] = None
    item["subtype_source"] = "user"
    assert VideoObjects.from_dict(raw).objects[0].subtype_key is None
    assert DetectedObject.from_dict(item).subtype_key is None
