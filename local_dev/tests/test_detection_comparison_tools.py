"""Comparison tools remain reproducible with coarse and legacy vocabularies."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from investigator.detection_worker.classes import load_config
from investigator.detection_worker.yoloe_backend import checkpoint_sha256, read_embeddings
from local_dev.tools.benchmark_detection import selected_modes, summary
from local_dev.tools.cache_yoloe_embeddings import write_cache
from local_dev.tools.compare_segmentation_overlay import comparison_panels, write_png


def test_vehicle_mode_supports_new_and_frozen_old_configuration():
    current = selected_modes(load_config(None), "all,vehicle")
    assert [item.key for item in current[1][1]] == ["vehicle"]
    legacy = load_config(Path("local_dev/examples/detection-taxonomy-legacy33.json"))
    modes = selected_modes(legacy, "all,vehicle,passenger")
    assert len(modes[0][1]) == 26
    assert len({prompt for cls in modes[0][1] for prompt in cls.prompts}) == 33
    assert [item.key for item in modes[1][1]] == ["passenger", "van", "truck", "special"]


def test_summary_reports_detections_and_ids_by_coarse_class():
    rows = [{"decode": 1, "predict": 2, "postprocess": 3, "total": 6,
             "detections": [{"class": "van", "track_id": 1}, {"class": "truck", "track_id": 2}]},
            {"decode": 1, "predict": 2, "postprocess": 3, "total": 6,
             "detections": [{"class": "van", "track_id": 1}, {"class": "adult", "track_id": 3}]}]
    result = summary(rows)
    assert result["coarse_detection_counts"] == {"vehicle": 3, "pedestrian": 1}
    assert result["coarse_unique_track_counts"] == {"vehicle": 2, "pedestrian": 1}


def test_embedding_cache_export_model_bound_and_no_overwrite(tmp_path):
    model = tmp_path / "weights.pt"
    model.write_bytes(b"test-weights")
    path = tmp_path / "test.npz"
    array = np.ones((1, 2, 8), dtype=np.float32)
    write_cache(path, model, ["car", "person"], array)
    np.testing.assert_array_equal(read_embeddings(path, checkpoint_sha256(model), ["person"]), array[:, :1])
    with pytest.raises(FileExistsError):
        write_cache(path, model, ["car", "person"], array)


def test_segmentation_panels_accept_three_backends_and_coarse_modes(tmp_path):
    pytest.importorskip("PIL")
    for name in ("sam3", "efficientsam3", "yoloe"):
        folder = tmp_path / name
        folder.mkdir()
        write_png(folder / "all.png", np.zeros((24, 32, 3), dtype=np.uint8))
        report = {
            "backend": name, "alpha": 0.4, "frame_indices_zero_based": [0],
            "class_thresholds": {"vehicle": 0.4}, "mask_threshold": 0.5,
            "palette_rgb": {"vehicle": [255, 0, 0]}, "labels": {"vehicle": "차량"},
            "width": 32, "height": 24, "video_fps": 30,
            "views": {"all": [{"frame_index": 0, "instances": 1, "classes": ["vehicle"], "image": "all.png"}]},
        }
        (folder / "metadata.json").write_text(json.dumps(report), encoding="utf-8")
    paths = comparison_panels(tmp_path, 0.4, ["sam3", "efficientsam3", "yoloe"])
    assert len(paths) == 1 and paths[0].name == "all-frame-0.png"
    from PIL import Image

    with Image.open(paths[0]) as image:
        assert image.width == 2880
