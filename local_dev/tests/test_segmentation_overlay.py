"""Pixel-accurate overlays must not replace segmentation masks with boxes."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from local_dev.tools.compare_segmentation_overlay import (
    class_palette,
    segmentation_overlay,
    write_png,
)


def detection(key, mask):
    return SimpleNamespace(class_key=key, mask=mask)


def test_only_true_mask_pixels_are_colored_holes_and_background_stay_unchanged():
    frame = np.full((12, 12, 3), 100, dtype=np.uint8)
    mask = np.zeros((12, 12), dtype=bool)
    mask[2:10, 2:10] = True
    mask[4:8, 4:8] = False  # Interior hole must not be filled like a polygon/box.
    original_mask = mask.copy()
    result = segmentation_overlay(frame, [detection("car", mask)], {"car": (200, 40, 80)},
                                  0.5, contours=False)
    assert np.array_equal(result[~mask], frame[~mask])
    assert np.array_equal(result[2, 2], [90, 70, 150])  # RGB legend -> BGR image
    assert np.array_equal(result[5, 5], frame[5, 5])
    assert np.all(frame == 100)
    assert np.array_equal(mask, original_mask)


def test_small_object_overlay_wins_over_large_background_regardless_of_input_order():
    frame = np.zeros((12, 12, 3), dtype=np.uint8)
    large = np.ones((12, 12), dtype=bool)
    small = np.zeros_like(large)
    small[4:8, 4:8] = True
    wall, car = detection("wall", large), detection("car", small)
    palette = {"wall": (255, 0, 0), "car": (0, 255, 0)}
    a = segmentation_overlay(frame, [wall, car], palette, 0.5, contours=False)
    b = segmentation_overlay(frame, [car, wall], palette, 0.5, contours=False)
    assert np.array_equal(a, b)
    assert np.array_equal(a[5, 5], [0, 128, 0])
    assert np.array_equal(a[0, 0], [0, 0, 128])


@pytest.mark.parametrize("alpha", [0, 0.4, 1])
def test_no_detection_returns_original_image_not_black_mask(alpha):
    frame = np.arange(120, dtype=np.uint8).reshape(4, 10, 3)
    result = segmentation_overlay(frame, [], {}, alpha)
    assert np.array_equal(result, frame)
    assert result is not frame


def test_zero_opacity_preserves_original_including_boundaries():
    frame = np.full((8, 8, 3), 70, dtype=np.uint8)
    mask = np.ones((8, 8), dtype=bool)
    result = segmentation_overlay(frame, [detection("car", mask)], {"car": (255, 0, 0)}, 0)
    assert np.array_equal(result, frame)


@pytest.mark.parametrize("alpha", [-1, 1.1, float("nan"), float("inf")])
def test_invalid_opacity_is_rejected(alpha):
    with pytest.raises(ValueError):
        segmentation_overlay(np.zeros((2, 2, 3), dtype=np.uint8), [], {}, alpha)


@pytest.mark.parametrize("mask", [None, np.zeros((4, 4), dtype=bool), np.zeros((2, 2), dtype=np.uint8)])
def test_misaligned_or_nonboolean_masks_are_not_silently_resized(mask):
    with pytest.raises(ValueError):
        segmentation_overlay(np.zeros((2, 2, 3), dtype=np.uint8), [detection("car", mask)], {"car": (1, 2, 3)})


def test_palette_is_shared_even_if_model_has_no_detections_for_a_class():
    palette = class_palette(["car", "wall", "person"])
    assert palette == class_palette(["car", "wall", "person"])
    assert len(set(palette.values())) == 3


def test_png_output_never_overwrites_previous_comparison(tmp_path):
    path = tmp_path / "overlay.png"
    image = np.full((4, 4, 3), 100, dtype=np.uint8)
    write_png(path, image)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_png(path, np.zeros_like(image))
    assert path.read_bytes() == original
