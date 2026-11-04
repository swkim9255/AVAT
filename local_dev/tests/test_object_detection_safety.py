"""Persistence, source-coordinate, and human-review safety checks."""

from __future__ import annotations

from copy import deepcopy

import pytest

from investigator.form_definition import FormDefinition
from investigator.models import InvestigationCase
from investigator.object_detection import (
    ROLE_COLLISION,
    ROLE_TRIGGER,
    LabelRef,
    ObjectBox,
    VideoObjects,
    add_manual_object,
    form_updates,
    label_options,
    label_text,
    merge_detection_result,
    set_objects_label,
    set_objects_role,
)
from investigator.storage import CaseRepository


def _merge(current=None, *, role=ROLE_TRIGGER, key="passenger", document=None):
    return merge_detection_result(
        current or VideoObjects(),
        document or {"detections": [{"frame_index": 2, "class_key": key,
                                    "object_id": "track1", "bbox_xywh": [1, 2, 20, 30],
                                    "score": 0.9}]},
        role=role, definition=FormDefinition.default(),
        start_frame=0, end_frame=5, frame_step=1, classes=[key],
    )[0]


def test_rerun_preserves_other_role_and_does_not_mutate_input():
    current = _merge(role=ROLE_COLLISION)
    old_uid = current.objects[0].uid
    before = current.to_dict()
    result = _merge(current)
    assert result.find(old_uid) is not None
    assert current.to_dict() == before
    assert [item.object_id for item in result.objects] == ["V001", "V002"]


def test_full_replacement_does_not_reuse_replaced_id():
    current = _merge()
    result = _merge(current)
    assert len(result.objects) == 1
    assert result.objects[0].object_id == "V002"


def test_corrected_label_wins_over_automatic_class_on_role_change():
    definition = FormDefinition.default()
    objects = _merge()
    uid = objects.objects[0].uid
    corrected = set_objects_label(
        objects, [uid], LabelRef("obstacle_vehicle_types", "화물"), definition
    )
    converted = set_objects_role(corrected, [uid], ROLE_COLLISION, definition)
    assert converted.objects[0].label == LabelRef("collision_targets", "vehicle.truck")
    assert objects.objects[0].label == LabelRef("obstacle_vehicle_types", "승용")


def test_landscape_role_conversion_retains_detected_subtype():
    definition = FormDefinition.default()
    objects = _merge(key="streettree")
    converted = set_objects_role(objects, [objects.objects[0].uid], ROLE_COLLISION, definition)
    assert converted.objects[0].label == LabelRef("collision_targets", "building.streettree")


def test_coordinate_precision_is_unchanged_by_label_edits():
    bbox = (1.123456, 2.234567, 20.345678, 30.456789)
    objects, item = add_manual_object(VideoObjects(), 4, bbox, ROLE_TRIGGER, None)
    renamed = set_objects_label(
        objects, [item.uid], LabelRef("obstacle_vehicle_types", "승용"), FormDefinition.default()
    )
    assert renamed.objects[0].boxes[0].bbox == bbox
    assert VideoObjects.from_dict(renamed.to_dict()).objects[0].boxes[0].bbox == bbox


@pytest.mark.parametrize("bbox", [(-1, 0, 20, 20), (0, -1, 20, 20), (0, 0, 0, 20),
                                  (0, 0, 20, float("nan")), (float("inf"), 0, 20, 20)])
def test_invalid_source_coordinates_rejected(bbox):
    with pytest.raises(ValueError):
        ObjectBox(0, bbox)


@pytest.mark.parametrize("frame", [-1, 1.5, float("nan"), True])
def test_invalid_frame_rejected(frame):
    with pytest.raises(ValueError):
        ObjectBox(frame, (0, 0, 10, 10))


@pytest.mark.parametrize("score", [-0.1, 1.1, float("inf")])
def test_invalid_score_rejected(score):
    with pytest.raises(ValueError):
        ObjectBox(0, (0, 0, 10, 10), score)


def test_invalid_result_never_modifies_existing_objects():
    objects = _merge()
    before = objects.to_dict()
    document = {"video": {"width": 10, "height": 10}, "detections": [
        {"frame_index": 2, "class_key": "passenger", "bbox_xywh": [0, 0, 20, 20]}
    ]}
    with pytest.raises(ValueError, match="원본 영상"):
        _merge(objects, document=document)
    assert objects.to_dict() == before


def test_custom_labels_and_values_use_stable_ids_without_altering_form_values():
    definition = FormDefinition.default()
    record = next(r for r in definition.records() if r["key"] == "collision_targets")
    choice = next(c for c in record["choices"] if c["id"] == "vehicle.passenger")
    choice.update(label="사용자 제작 승용차", value="custom.car")
    definition.validate()
    objects = _merge(role=ROLE_COLLISION)
    ref = objects.objects[0].label
    assert label_text(definition, ROLE_COLLISION, ref) == "사용자 제작 승용차"
    values = {"collision_targets": ["vehicle.truck"], "remarks": "원래 메모"}
    before = deepcopy(values)
    updates = form_updates(objects.objects, ROLE_COLLISION, definition, values)
    assert updates[0].stored_values == ["custom.car", "vehicle.truck"]
    assert values == before


def test_non_multi_custom_form_is_not_overwritten():
    definition = FormDefinition.default()
    record = next(r for r in definition.records() if r["key"] == "collision_targets")
    record["kind"] = "select"
    assert not label_options(definition, ROLE_COLLISION)
    with pytest.raises(ValueError, match="선택지가 없어"):
        form_updates(_merge(role=ROLE_COLLISION).objects, ROLE_COLLISION,
                     definition, {"collision_targets": "vehicle.truck"})


def test_case_keys_are_case_insensitive_detached_and_noop_keeps_completed(tmp_path):
    video = str(tmp_path / "Test.avi")
    objects = _merge()
    case = InvestigationCase(videos=[video], objects_by_video={video.upper(): objects.to_dict()})
    assert case.video_objects().objects
    assert case.objects_for_video(video.upper()).objects
    detached = case.video_objects()
    detached.objects.clear()
    assert case.video_objects().objects
    case.status, case.completed_at = "completed", "saved"
    before = case.to_dict()
    case.set_video_objects(case.video_objects(), video_path=video.upper())
    assert case.to_dict() == before
    case.remove_video(0)
    assert not case.objects_by_video


def test_database_roundtrip_keeps_video_specific_objects_and_original_values(tmp_path):
    case = InvestigationCase(videos=[str(tmp_path / "a.avi"), str(tmp_path / "b.avi")],
                             values={"remarks": "아직 조사하지 않음"})
    case.set_video_objects(_merge())
    case.active_video_index = 1
    case.set_video_objects(_merge(role=ROLE_COLLISION, key="truck"))
    before_values = deepcopy(case.values)
    repository = CaseRepository(tmp_path)
    repository.save([case], active_case_uid=case.uid)
    restored = repository.load()[0]
    assert restored.video_objects().objects[0].class_key == "vehicle"
    assert restored.video_objects().objects[0].subtype_key == "truck"
    restored.active_video_index = 0
    assert restored.video_objects().objects[0].class_key == "vehicle"
    assert restored.video_objects().objects[0].subtype_key == "passenger"
    assert restored.values == before_values
    restored.remove_video(0)
    assert len(restored.objects_by_video) == 1
    assert restored.video_objects().objects[0].subtype_key == "truck"


def test_raw_annotations_do_not_change_csv_or_legacy_case_payload(tmp_path):
    case = InvestigationCase(videos=[str(tmp_path / "clip.avi")], values={"remarks": "기존 조사"})
    original = case.to_dict()
    assert "objects_by_video" not in original
    assert InvestigationCase.from_dict(original).to_dict() == original
    repository = CaseRepository(tmp_path)
    baseline, annotated = tmp_path / "before.csv", tmp_path / "after.csv"
    repository.export_csv(baseline, [case], include_specification=False)
    case.set_video_objects(_merge())
    repository.export_csv(annotated, [case], include_specification=False)
    assert baseline.read_bytes() == annotated.read_bytes()
