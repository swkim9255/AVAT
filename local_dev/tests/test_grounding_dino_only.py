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
from investigator.widgets.detection_panel import DetectionSettingsDialog


@pytest.mark.parametrize("old_backend", [None, "sam3", "efficientsam3", "yoloe"])
def test_previous_settings_never_select_an_old_model(tmp_path, monkeypatch, old_backend):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    settings = QSettings(str(tmp_path / "old.ini"), QSettings.Format.IniFormat)
    settings.setValue(process.SETTINGS_PYTHON, "old-model-python.exe")
    settings.setValue(process.SETTINGS_MODEL, "old-model-weights")
    if old_backend:
        settings.setValue(process.SETTINGS_BACKEND, old_backend)
    before = {key: settings.value(key) for key in settings.allKeys()}
    loaded = DetectionSettings.load(settings)
    assert loaded.backend == "grounding_dino"
    assert loaded.model == process.DINO_BASE_MODEL
    assert loaded.python == ""
    assert {key: settings.value(key) for key in settings.allKeys()} == before


def test_dino_settings_roundtrip_including_tiny_and_thresholds(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    configured = DetectionSettings(model=process.DINO_TINY_MODEL, device="cpu", offline=False,
                                   dino_chunk_size=5, dino_box_threshold=.4, dino_text_threshold=.2)
    configured.save(store)
    assert DetectionSettings.load(store) == configured


@pytest.mark.parametrize("old_backend", ["sam3", "efficientsam3", "yoloe"])
def test_old_models_are_not_executable_or_saveable(qtbot, tmp_path, old_backend):
    settings = DetectionSettings(backend=old_backend, model="old model")
    with pytest.raises(ValueError, match="Grounding DINO"):
        settings.save(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    with pytest.raises(ValueError, match="Grounding DINO"):
        create_backend("old model", backend=old_backend)
    job = DetectionJob(DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, ["vehicle"], "common"), settings)
    with qtbot.waitSignal(job.failed, timeout=1000) as result:
        job.start()
    assert "Grounding DINO" in result.args[0]
    assert not job.is_running
    job.deleteLater()


def test_settings_dialog_only_offers_dino_base_and_tiny(qtbot):
    dialog = DetectionSettingsDialog(DetectionSettings(model=process.DINO_TINY_MODEL))
    qtbot.addWidget(dialog)
    assert dialog.backend_combo.count() == 1
    assert dialog.backend_combo.currentData() == "grounding_dino"
    assert not dialog.backend_combo.isEnabled()
    assert dialog.dino_model_combo.findData(process.DINO_BASE_MODEL) >= 0
    assert dialog.dino_model_combo.findData(process.DINO_TINY_MODEL) >= 0
    assert dialog.settings().backend == "grounding_dino"
    assert dialog.settings().model == process.DINO_TINY_MODEL


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
