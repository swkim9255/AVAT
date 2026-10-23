"""Box-only models share filtering/tracking input without fabricating masks."""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from investigator.detection_worker.backend import RawInstance
from investigator.detection_worker.classes import ClassConfig, ClassDef
from investigator.detection_worker.postprocess import build_detections
from local_dev.tools.compare_segmentation_overlay import (
    comparison_panels,
    render_backend,
    segmentation_overlay,
)


def vehicle_config(*, minimum_area=0.0, nms=0.5):
    cls = ClassDef("vehicle", "차량", ("car", "van", "truck"), threshold=0.4,
                   min_area_ratio=minimum_area, nms_iou=nms)
    return ClassConfig((cls,))


def detections(raw, *, config=None):
    config = config or vehicle_config()
    return build_detections(raw, config.enabled(), config, (10, 20))


def test_box_only_uses_original_pixel_xyxy_with_no_mask_or_polygon(monkeypatch):
    def unexpected_polygon(_mask):
        pytest.fail("A box-only prediction must not create a segmentation polygon")

    monkeypatch.setattr("investigator.detection_worker.postprocess.mask_to_polygon", unexpected_polygon)
    result = detections({"car": [RawInstance(None, 0.8, (1.25, 2.5, 11.75, 8.5))]})
    assert len(result) == 1
    det = result[0]
    assert det.bbox_xywh == (1.25, 2.5, 10.5, 6.0)
    assert det.area == 63.0
    assert det.mask is None
    assert det.polygon == []


def test_box_overflow_is_clipped_to_source_frame_before_area_filter():
    result = detections({"car": [RawInstance(None, 0.8, (-5, -3, 25, 11))]})
    assert result[0].bbox_xywh == (0.0, 0.0, 20.0, 10.0)
    assert result[0].area == 200.0
    # Visible 2x2 area is below 5 pixels, despite a much larger un-clipped box.
    assert detections({"car": [RawInstance(None, 0.8, (-5, -5, 2, 2))]},
                      config=vehicle_config(minimum_area=0.025)) == []


def test_fractional_box_area_is_not_rounded_before_minimum_filter():
    result = detections({"car": [RawInstance(None, 0.8, (0, 0, 1.5, 1.5))]},
                        config=vehicle_config(minimum_area=0.011))
    assert result[0].area == 2.25


@pytest.mark.parametrize("box", [
    (21, 2, 25, 4), (-5, 2, -1, 4), (2, 11, 4, 15), (2, -5, 4, -1),
    (20, 2, 25, 4),
])
def test_boxes_wholly_outside_frame_have_no_visible_area(box):
    assert detections({"car": [RawInstance(None, 0.8, box)]}) == []


@pytest.mark.parametrize("box", [
    None, (1, 2, 3), (1, 2, 3, 4, 5), (1, 1, 1, 4), (1, 4, 3, 2),
    (float("nan"), 0, 2, 2), (0, 0, float("inf"), 2), (None, 0, 2, 2),
])
def test_malformed_nonfinite_and_nonpositive_boxes_are_rejected(box):
    with pytest.raises(ValueError, match="박스"):
        detections({"car": [RawInstance(None, 0.8, box)]})


def test_box_only_nms_uses_bbox_iou_across_prompts_of_same_coarse_class():
    result = detections({
        "van": [RawInstance(None, 0.9, (1, 1, 9, 9))],
        "truck": [RawInstance(None, 0.8, (1, 1, 9, 9))],
        "car": [RawInstance(None, 0.7, (12, 1, 19, 9))],
    })
    assert [item.prompt for item in result] == ["van", "car"]


def test_box_only_keeps_class_threshold_and_minimum_area_policies():
    assert detections({"car": [RawInstance(None, 0.39, (0, 0, 20, 10))]}) == []
    assert detections({"car": [RawInstance(None, 0.8, (0, 0, 2, 2))]},
                      config=vehicle_config(minimum_area=0.03)) == []


def test_cross_class_suppression_groups_use_box_iou_without_masks():
    classes = tuple(ClassDef(key, key, (key,), suppression_group="wall", min_area_ratio=0)
                    for key in ("wall", "building_wall"))
    config = ClassConfig(classes, cross_class_iou=0.6)
    raw = {"wall": [RawInstance(None, 0.8, (1, 1, 9, 9))],
           "building_wall": [RawInstance(None, 0.9, (1, 1, 9, 9))]}
    result = build_detections(raw, classes, config, (10, 20))
    assert len(result) == 1
    assert result[0].class_key == "building_wall"


def test_mixed_mask_and_box_nms_falls_back_to_bbox_iou():
    mask = np.zeros((10, 20), dtype=bool)
    mask[1:9, 1:9] = True
    result = detections({"van": [RawInstance(mask, 0.9)],
                         "truck": [RawInstance(None, 0.8, (1, 1, 9, 9))]})
    assert len(result) == 1
    assert result[0].prompt == "van"
    assert result[0].polygon


def test_mask_mask_nms_preserves_pixel_iou_even_when_boxes_overlap():
    # Checkerboard masks share their bounding box but have no common pixels.
    first = np.indices((10, 20)).sum(axis=0) % 2 == 0
    second = ~first
    result = detections({"van": [RawInstance(first, 0.9)],
                         "truck": [RawInstance(second, 0.8)]})
    assert len(result) == 2
    assert [item.area for item in result] == [100, 100]
    assert all(item.mask is not None and item.polygon for item in result)


def test_mask_box_is_still_calculated_from_true_mask_not_backend_box():
    mask = np.zeros((10, 20), dtype=bool)
    mask[2:5, 3:8] = True
    result = detections({"car": [RawInstance(mask, 0.9, (0, 0, 20, 10))]})
    assert result[0].bbox_xywh == (3.0, 2.0, 5.0, 3.0)
    assert result[0].area == 15


def overlay_args(tmp_path, backend="grounding_dino"):
    return SimpleNamespace(worker=backend, output=tmp_path, model="not-loaded",
                           alpha=0.4, frames="600,601", video=tmp_path / "not-read.mp4")


def test_grounding_dino_overlay_skips_without_loading_model_or_video(tmp_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Box-only overlay skip must not load model or video")

    monkeypatch.setattr("investigator.detection_worker.backend.create_backend", unexpected)
    monkeypatch.setattr("local_dev.tools.compare_segmentation_overlay.cv2.VideoCapture", unexpected)
    render_backend(overlay_args(tmp_path))
    metadata = json.loads((tmp_path / "grounding_dino/metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "skipped"
    assert metadata["skip_reason"] == "box_only_backend"
    assert metadata["supports_segmentation"] is False
    assert metadata["frame_indices_zero_based"] == [600, 601]
    assert metadata["views"] == {}
    assert list(tmp_path.rglob("*.png")) == []
    assert comparison_panels(tmp_path, 0.4, ["grounding_dino"]) == []


def test_overlay_respects_instance_capability_for_unknown_box_only_model(tmp_path, monkeypatch):
    monkeypatch.setattr("investigator.detection_worker.backend.create_backend",
                        lambda *_args, **_kwargs: SimpleNamespace(supports_segmentation=False))
    render_backend(overlay_args(tmp_path, backend="sam3"))
    metadata = json.loads((tmp_path / "sam3/metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "skipped"


def test_low_level_overlay_never_substitutes_box_for_missing_mask():
    result = detections({"car": [RawInstance(None, 0.8, (1, 1, 9, 9))]})
    with pytest.raises(ValueError, match="Segmentation mask"):
        segmentation_overlay(np.zeros((10, 20, 3), dtype=np.uint8), result, {"vehicle": (1, 2, 3)})
