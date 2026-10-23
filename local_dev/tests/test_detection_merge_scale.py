"""Large detection merges retain IDs and boxes without per-track rescans."""

from __future__ import annotations

from collections import Counter

import pytest

from investigator import object_detection as detection
from investigator.form_definition import FormDefinition
from investigator.object_detection import (
    ROLE_COLLISION,
    ROLE_TRIGGER,
    SOURCE_AUTOMATIC,
    SOURCE_MANUAL,
    DetectedObject,
    ObjectBox,
    VideoObjects,
    merge_detection_result,
)


def _detection(frame: int, track: str, *, key: str = "passenger", score=0.8, x=10):
    return {
        "frame_index": frame,
        "class_key": key,
        "object_id": track,
        "bbox_xywh": [x, 10, 20, 30],
        "score": score,
    }


def _merge(current: VideoObjects, detections: list[dict], *, classes=None, end=733):
    return merge_detection_result(
        current,
        {"detections": detections},
        role=ROLE_TRIGGER,
        definition=FormDefinition.default(),
        start_frame=0,
        end_frame=end,
        frame_step=1,
        classes=classes or ["passenger"],
    )


def test_thousands_of_tracks_share_prefix_and_resolve_labels_once(monkeypatch):
    calls: Counter[str] = Counter()
    original = detection.default_label

    def counted_label(definition, key, role):
        calls[key] += 1
        return original(definition, key, role)

    def no_rescan(*_args):
        pytest.fail("A large merge must not rescan every track to allocate each ID.")

    monkeypatch.setattr(detection, "default_label", counted_label)
    monkeypatch.setattr(detection, "next_object_id", no_rescan)
    detections = [
        _detection(index % 734, str(index), key="passenger" if index % 2 else "truck")
        for index in range(2255)
    ]
    merged, added, replaced = _merge(
        VideoObjects(), detections, classes=["passenger", "truck"]
    )
    assert (added, replaced) == (2255, 0)
    assert [item.object_id for item in merged.objects] == [
        f"V{index + 1:03d}" for index in range(2255)
    ]
    # Detector labels no longer inspect editable form definitions during merge.
    assert not calls
    assert merged.runs[-1].object_count == 2255


def test_long_track_deduplicates_frames_without_linear_box_lookup(monkeypatch):
    def no_box_scan(*_args):
        pytest.fail("Merging a track must index existing boxes by frame.")

    monkeypatch.setattr(DetectedObject, "box_at", no_box_scan)
    merged, added, replaced = _merge(
        VideoObjects(), [_detection(frame, "track") for frame in reversed(range(734))]
    )
    assert (added, replaced) == (1, 0)
    assert [box.frame for box in merged.objects[0].boxes] == list(range(734))


def test_duplicate_frame_keeps_highest_score_and_first_tie():
    merged, *_ = _merge(
        VideoObjects(),
        [
            _detection(2, "track", score=None, x=10),
            _detection(2, "track", score=0, x=20),
            _detection(2, "track", score=0.9, x=30),
            _detection(2, "track", score=0.8, x=40),
            _detection(2, "track", score=0.9, x=50),
            _detection(1, "track", score=None, x=60),
            _detection(1, "track", score=0, x=70),
        ],
    )
    boxes = merged.objects[0].boxes
    assert [(box.frame, box.bbox[0], box.score) for box in boxes] == [
        (1, 60.0, None),
        (2, 30.0, 0.9),
    ]


def _object(uid, object_id, *, source=SOURCE_AUTOMATIC, reviewed=False, role=ROLE_TRIGGER):
    return DetectedObject(
        uid=uid,
        object_id=object_id,
        role=role,
        label=None,
        boxes=[ObjectBox(1, (10, 10, 20, 20), 0.9)],
        source=source,
        reviewed=reviewed,
    )


def test_id_highwater_includes_removed_manual_reviewed_and_other_role_objects():
    current = VideoObjects(
        [
            _object("removed", "V1099"),
            _object("manual", "V1100", source=SOURCE_MANUAL),
            _object("reviewed", "V1101", reviewed=True),
            _object("other_role", "V1102", role=ROLE_COLLISION),
            _object("removed_largest", "V1200"),
            _object("ignored_suffix", "V9000x", source=SOURCE_MANUAL),
            _object("pedestrian", "P007", source=SOURCE_MANUAL),
        ]
    )
    before = current.to_dict()
    merged, added, replaced = _merge(
        current,
        [
            _detection(1, "car"),
            _detection(1, "truck", key="truck"),
            _detection(1, "person", key="adult"),
        ],
        classes=["passenger", "truck", "adult"],
    )
    assert (added, replaced) == (3, 2)
    assert current.to_dict() == before
    assert merged.find("removed") is None and merged.find("removed_largest") is None
    for uid in ("manual", "reviewed", "other_role", "ignored_suffix", "pedestrian"):
        assert merged.find(uid).to_dict() == current.find(uid).to_dict()
    assert [item.object_id for item in merged.objects[-3:]] == ["V1201", "V1202", "P008"]


def test_invalid_late_detection_does_not_modify_input():
    current = VideoObjects([_object("original", "V005")])
    before = current.to_dict()
    with pytest.raises(ValueError, match="박스"):
        _merge(
            current,
            [_detection(1, "good"), {**_detection(2, "bad"), "bbox_xywh": [0, 0, -1, 5]}],
        )
    assert current.to_dict() == before
