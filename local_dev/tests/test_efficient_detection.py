"""Optional EfficientSAM3 selection, validation, and benchmark correctness."""

from __future__ import annotations

import json
import sys

import pytest
from PySide6.QtCore import QSettings

import investigator.detection_process as process
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings
from investigator.detection_worker.backend import create_backend
from investigator.detection_worker.efficient_backend import checkpoint_state, read_profile
from investigator.widgets.detection_panel import DetectionSettingsDialog
from local_dev.tools.benchmark_detection import agreement, summary


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    source = tmp_path / "source"
    (source / "sam3").mkdir(parents=True)
    (source / "sam3/model_builder.py").touch()
    (root / "weights.pt").write_bytes(b"test-not-real-weights")
    data = {"version": 1, "source": "../source", "checkpoint": "weights.pt",
            "backbone_type": "tinyvit", "model_name": "11m"}
    path = root / "avat-efficient.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return root, path, data


def test_profile_paths_resolve_relative_to_model_not_cwd(profile, monkeypatch, tmp_path):
    root, _, _ = profile
    monkeypatch.chdir(tmp_path.parent)
    data = read_profile(root)
    assert data["checkpoint"] == root / "weights.pt"
    assert data["source"] == root.parent / "source"


@pytest.mark.parametrize(("key", "value"), [
    ("version", True), ("version", 2), ("source", "missing"),
    ("checkpoint", "missing.pt"), ("checkpoint", "../outside.pt"),
    ("backbone_type", "sam3"), ("model_name", "unknown"),
    ("backbone_type", []), ("model_name", {}),
])
def test_bad_profiles_fail_before_model_import(profile, key, value):
    root, path, data = profile
    data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        read_profile(root)


def test_checkpoint_keys_are_normalized_without_dropping_any_weights():
    assert checkpoint_state({"model": {"detector.backbone.student_trunk.layer": 1}}) == {
        "backbone.layer": 1,
    }
    with pytest.raises(ValueError, match="중복"):
        checkpoint_state({"detector.a": 1, "a": 2})
    with pytest.raises(ValueError):
        checkpoint_state({"model": {}})


def test_backend_factory_preserves_original_default(monkeypatch):
    import investigator.detection_worker.backend as module

    calls = []
    monkeypatch.setattr(module, "Sam3Backend", lambda *args: calls.append(args))
    create_backend("original-model", "cpu")
    assert calls == [("original-model", "cpu", False)]
    with pytest.raises(ValueError):
        create_backend("x", backend="unsupported")


def test_efficient_backend_settings_and_worker_arguments_roundtrip(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings = DetectionSettings(python=sys.executable, model="efficient-model", backend="efficientsam3", offline=True)
    settings.save(store)
    restored = DetectionSettings.load(store)
    assert restored.backend == "efficientsam3"
    job = DetectionJob(DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, ["passenger"], "trigger"), restored)
    args = job.arguments()
    assert args[args.index("--backend") + 1] == "efficientsam3"
    assert args[args.index("--model") + 1] == "efficient-model"
    job.kill()


def test_efficient_profile_manifest_is_opt_in_and_local(tmp_path):
    (tmp_path / "python.exe").touch()
    (tmp_path / "model").mkdir()
    path = tmp_path / "detection-runtime.efficientsam3.json"
    path.write_text(json.dumps({"python": "python.exe", "model": "model", "offline": True,
                                "backend": "efficientsam3"}), encoding="utf-8")
    settings, error = process._load_runtime_manifest(path)
    assert not error and settings.backend == "efficientsam3"
    assert settings.model == str(tmp_path / "model")


def test_model_switch_restores_each_models_custom_settings(qtbot, monkeypatch, tmp_path):
    import investigator.widgets.detection_panel as panel

    monkeypatch.setattr(panel, "REPOSITORY_ROOT", tmp_path)
    original = DetectionSettings(python="original.exe", model="original-model", offline=True, backend="sam3")
    dialog = DetectionSettingsDialog(original)
    qtbot.addWidget(dialog)
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("efficientsam3"))
    assert dialog.model_edit.text() == ""
    dialog.python_edit.setText("efficient.exe")
    dialog.model_edit.setText("efficient-model")
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("sam3"))
    assert dialog.settings().python == "original.exe"
    assert dialog.settings().model == "original-model"
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("efficientsam3"))
    assert dialog.settings().python == "efficient.exe"
    assert dialog.settings().model == "efficient-model"


def test_empty_efficient_model_fails_without_starting_process(qtbot, tmp_path, monkeypatch):
    job = DetectionJob(DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, ["passenger"], "trigger"),
                       DetectionSettings(backend="efficientsam3", model=""))
    started = []
    monkeypatch.setattr(job._process, "start", lambda: started.append(True))
    with qtbot.waitSignal(job.failed) as signal:
        job.start()
    assert "로컬 모델 폴더" in signal.args[0]
    assert not started


def test_efficient_selection_loads_workspace_preset_without_saving(qtbot, monkeypatch, tmp_path):
    import investigator.widgets.detection_panel as panel

    monkeypatch.setattr(panel, "REPOSITORY_ROOT", tmp_path)
    (tmp_path / "efficient.exe").touch()
    (tmp_path / "model").mkdir()
    (tmp_path / "detection-runtime.efficientsam3.json").write_text(json.dumps({
        "python": "efficient.exe", "model": "model", "backend": "efficientsam3", "offline": True,
    }), encoding="utf-8")
    dialog = DetectionSettingsDialog(DetectionSettings(python="original.exe", model="original-model", backend="sam3"))
    qtbot.addWidget(dialog)
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("efficientsam3"))
    assert dialog.settings().python == str(tmp_path / "efficient.exe")
    assert dialog.settings().model == str(tmp_path / "model")
    assert dialog.settings().offline is True
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("sam3"))
    assert dialog.settings().python == "original.exe"
    assert dialog.settings().model == "original-model"


def test_benchmark_separates_frame_times_from_startup_and_marks_agreement_not_accuracy():
    rows = [{"frame": i, "decode": 1, "predict": 2, "postprocess": 3, "total": 6,
             "detections": [{"class": "car", "box": [0, 0, 10, 10]}]} for i in range(2)]
    assert summary(rows)["total_mean_s"] == 6
    result = agreement(rows, rows)
    assert result["same_class_iou_50_matches"] == 2
    assert result["matched_mean_iou"] == 1
    assert result["not_ground_truth_accuracy"] is True
    changed = [{**row, "detections": [{"class": "person", "box": [0, 0, 10, 10]}]} for row in rows]
    assert agreement(rows, changed)["same_class_iou_50_matches"] == 0
