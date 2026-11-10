"""The application/worker/portable build no longer select previous models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

import investigator.detection_process as process
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings
from investigator.detection_worker.__main__ import parse_args
from investigator.detection_worker.backend import create_backend


@pytest.mark.parametrize("old_backend", [None, "sam3", "efficientsam3", "yoloe"])
def test_previous_settings_never_select_an_old_model(tmp_path, monkeypatch, old_backend, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    settings = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    settings.setValue("detection/python", "old-model-python.exe")
    settings.setValue("detection/model", "old-model-weights")
    if old_backend:
        settings.setValue("detection/backend", old_backend)
    monkeypatch.setattr(process, "detection_settings_store", lambda: settings)
    before = {key: settings.value(key) for key in settings.allKeys()}
    loaded = DetectionSettings.load()
    assert loaded.backend == "grounding_dino"
    assert loaded.model == str(bundled_dino_model)
    assert loaded.offline is True
    assert loaded.python == ""
    assert {key: settings.value(key) for key in settings.allKeys()} == before


@pytest.mark.parametrize("old_backend", ["sam3", "efficientsam3", "yoloe"])
def test_old_models_are_not_executable(qtbot, tmp_path, old_backend):
    settings = DetectionSettings(backend=old_backend, model="old model")
    with pytest.raises(ValueError, match="Grounding DINO"):
        create_backend("old model", backend=old_backend)
    job = DetectionJob(DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, ["vehicle"], "common"), settings)
    with qtbot.waitSignal(job.failed, timeout=1000) as result:
        job.start()
    assert "Grounding DINO" in result.args[0]
    assert not job.is_running
    job.deleteLater()


def test_worker_defaults_to_dino_and_rejects_previous_models():
    args = ["--video", "clip.mp4", "--output", "result.json"]
    assert parse_args(args).backend == "grounding_dino"
    with pytest.raises(SystemExit):
        parse_args([*args, "--backend", "sam3"])


def test_manifest_without_backend_is_dino_not_sam3(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text(json.dumps({"model_type": "grounding-dino"}), encoding="utf-8")
    path = tmp_path / "detection-runtime.json"
    path.write_text(json.dumps({"model": "model", "offline": True}), encoding="utf-8")
    settings, error = process._load_runtime_manifest(path)
    assert not error
    assert settings.backend == "grounding_dino"
    assert Path(settings.model) == model
