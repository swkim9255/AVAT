"""Grounding DINO 기본 설정과 기존 백엔드 설정의 호환성을 검증한다."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings

import investigator.detection_process as process
import investigator.widgets.detection_panel as panel
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings
from investigator.widgets.detection_panel import DetectionSettingsDialog


def dino_folder(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps({"model_type": "grounding-dino"}), encoding="utf-8")
    return path


def job_for(tmp_path, settings):
    return DetectionJob(DetectionRequest(tmp_path / "clip.mp4", 600, 609, 1, ["vehicle"], "trigger"), settings)


def test_dino_default_preserves_old_positional_constructor_and_backend_model_mapping():
    legacy = DetectionSettings("python", "model", "cpu", True, "", "sam3")
    assert legacy.backend == "sam3"
    assert legacy.dino_chunk_size == 8
    assert legacy.dino_box_threshold == 0.3
    assert legacy.dino_text_threshold == 0.25
    assert legacy.dino_fp16 is True
    assert DetectionSettings().backend == "grounding_dino"
    assert DetectionSettings().model == process.DINO_BASE_MODEL
    assert process.default_model("grounding_dino") == process.DINO_BASE_MODEL
    assert process.default_model("yoloe") == process.YOLOE_MODEL
    assert process.default_model("yoloe").endswith("yoloe-11l-seg.pt")
    assert process.default_model("sam3") == process.SAM3_MODEL


def test_new_preferences_default_to_dino_without_writing_settings(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "new-preferences.ini"), QSettings.Format.IniFormat)
    restored = DetectionSettings.load(store)
    assert restored.backend == "grounding_dino"
    assert restored.model == process.DINO_BASE_MODEL
    assert not store.allKeys()


@pytest.mark.parametrize("backend", ["yoloe", "sam3", "efficientsam3"])
def test_explicit_existing_backend_preferences_remain_unchanged(qtbot, tmp_path, monkeypatch, backend):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "existing-preferences.ini"), QSettings.Format.IniFormat)
    configured = DetectionSettings(sys.executable, "existing-model", "cpu", True, backend=backend)
    configured.save(store)
    assert DetectionSettings.load(store) == configured


def test_old_preferences_without_backend_still_use_sam3(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "legacy-preferences.ini"), QSettings.Format.IniFormat)
    store.setValue(process.SETTINGS_PYTHON, sys.executable)
    store.setValue(process.SETTINGS_MODEL, "legacy-sam-model")
    restored = DetectionSettings.load(store)
    assert restored.backend == "sam3"
    assert restored.model == "legacy-sam-model"
    assert not store.contains(process.SETTINGS_BACKEND)


def test_default_dialog_selects_dino_and_keeps_legacy_choices(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "REPOSITORY_ROOT", tmp_path)
    dialog = DetectionSettingsDialog(DetectionSettings())
    qtbot.addWidget(dialog)
    assert dialog.backend_combo.itemData(0) == "grounding_dino"
    assert dialog.backend_combo.currentData() == "grounding_dino"
    assert "기본" in dialog.backend_combo.currentText()
    assert "기본" not in dialog.backend_combo.itemText(dialog.backend_combo.findData("yoloe"))
    assert not dialog.dino_options_widget.isHidden()
    assert dialog.settings().model == process.DINO_BASE_MODEL


@pytest.mark.parametrize("message", ["No module named 'torch'", "401 unauthorized"])
def test_shared_error_messages_do_not_misidentify_dino_as_sam3(message):
    assert "SAM 3" not in process.friendly_error(message)


def test_worker_defaults_to_dino_base_and_fp16(tmp_path, monkeypatch):
    from investigator.detection_worker import __main__ as cli
    from investigator.detection_worker import backend as factory

    clip = tmp_path / "unused-clip.mp4"
    clip.touch()
    calls = []

    def fake_backend(model, device, half, backend, **options):
        calls.append((model, device, half, backend, options))
        return SimpleNamespace(device="cpu", fallback_reason=None)

    monkeypatch.setattr(factory, "create_backend", fake_backend)
    monkeypatch.setattr(cli, "run", lambda *args: {"detections": []})
    code = cli.main(["--video", str(clip), "--output", str(tmp_path / "result.json")])
    assert code == cli.EXIT_OK
    assert calls == [(process.DINO_BASE_MODEL, "auto", True, "grounding_dino", {
        "chunk_size": 8, "box_threshold": 0.3, "text_threshold": 0.25,
    })]


def test_dino_qsettings_roundtrip(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    configured = DetectionSettings(sys.executable, process.DINO_TINY_MODEL, "cuda", False,
                                   backend="grounding_dino", dino_chunk_size=6,
                                   dino_box_threshold=0.21, dino_text_threshold=0.17, dino_fp16=False)
    configured.save(store)
    restored = DetectionSettings.load(store)
    assert restored == configured


@pytest.fixture
def manifest(tmp_path):
    (tmp_path / "python.exe").touch()
    dino_folder(tmp_path / "model")
    data = {"version": 1, "python": "python.exe", "model": "model", "backend": "grounding_dino",
            "device": "cuda", "offline": True, "dino_chunk_size": 7,
            "dino_box_threshold": 0.28, "dino_text_threshold": 0.22, "dino_fp16": False}
    path = tmp_path / "detection-runtime.grounding-dino.json"
    return path, data


def test_local_dino_manifest_loads_thresholds_without_changing_preferences(manifest):
    path, data = manifest
    path.write_text(json.dumps(data), encoding="utf-8")
    settings, error = process._load_runtime_manifest(path)
    assert not error
    assert settings.backend == "grounding_dino"
    assert settings.model == str(path.parent / "model")
    assert settings.dino_chunk_size == 7
    assert settings.dino_box_threshold == 0.28
    assert settings.dino_text_threshold == 0.22
    assert settings.dino_fp16 is False


@pytest.mark.parametrize(("key", "value"), [
    ("dino_chunk_size", 0), ("dino_chunk_size", True), ("dino_chunk_size", 1.5),
    ("dino_box_threshold", -0.1), ("dino_box_threshold", float("nan")),
    ("dino_text_threshold", 1.1), ("dino_text_threshold", True),
    ("dino_box_threshold", "0.3"), ("dino_fp16", "false"),
])
def test_malformed_dino_manifest_options_fail_before_model_load(manifest, key, value):
    path, data = manifest
    data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    settings, error = process._load_runtime_manifest(path)
    assert settings is None
    assert "Grounding DINO" in error


def test_wrong_architecture_local_model_is_rejected(manifest):
    path, data = manifest
    (path.parent / "model/config.json").write_text('{"model_type":"sam3"}', encoding="utf-8")
    path.write_text(json.dumps(data), encoding="utf-8")
    settings, error = process._load_runtime_manifest(path)
    assert settings is None
    assert "model_type" in error


@pytest.mark.parametrize("fp16", [True, False])
def test_worker_arguments_include_dino_thresholds_and_explicit_precision(qtbot, tmp_path, fp16):
    job = job_for(tmp_path, DetectionSettings(backend="grounding_dino", model=process.DINO_BASE_MODEL,
                                            dino_chunk_size=5, dino_box_threshold=0.23,
                                            dino_text_threshold=0.19, dino_fp16=fp16))
    try:
        arguments = job.arguments()
        for key, value in (("--dino-chunk-size", "5"), ("--dino-box-threshold", "0.23"),
                           ("--dino-text-threshold", "0.19")):
            assert arguments[arguments.index(key) + 1] == value
        assert ("--half" in arguments) is fp16
        assert ("--no-half" in arguments) is not fp16
        assert arguments[arguments.index("--backend") + 1] == "grounding_dino"
    finally:
        job.kill()


@pytest.mark.parametrize("backend", ["sam3", "efficientsam3", "yoloe"])
def test_other_backends_do_not_receive_dino_flags(qtbot, tmp_path, backend):
    job = job_for(tmp_path, DetectionSettings(backend=backend))
    try:
        arguments = job.arguments()
        assert not any(value.startswith("--dino-") for value in arguments)
        assert "--half" not in arguments and "--no-half" not in arguments
    finally:
        job.kill()


def test_bad_dino_local_folder_fails_before_process_start(qtbot, tmp_path, monkeypatch):
    model = tmp_path / "incomplete-model"
    model.mkdir()
    job = job_for(tmp_path, DetectionSettings(model=str(model), backend="grounding_dino"))
    called = []
    monkeypatch.setattr(job._process, "start", lambda: called.append(True))
    with qtbot.waitSignal(job.failed) as signal:
        job.start()
    assert "config.json" in signal.args[0]
    assert not called
    assert not job._temp_dir.exists()


def test_offline_dino_rejects_remote_id_before_process_start(qtbot, tmp_path, monkeypatch):
    job = job_for(tmp_path, DetectionSettings(model=process.DINO_BASE_MODEL, backend="grounding_dino", offline=True))
    called = []
    monkeypatch.setattr(job._process, "start", lambda: called.append(True))
    with qtbot.waitSignal(job.failed) as signal:
        job.start()
    assert "로컬 모델 폴더" in signal.args[0]
    assert not called


def test_dino_controls_only_show_for_dino_and_custom_values_survive_switch(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "REPOSITORY_ROOT", tmp_path)
    dialog = DetectionSettingsDialog(DetectionSettings(backend="sam3", model="original-sam"))
    qtbot.addWidget(dialog)
    assert dialog.dino_options_widget.isHidden()
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("grounding_dino"))
    assert not dialog.dino_options_widget.isHidden()
    assert dialog.settings().model == process.DINO_BASE_MODEL
    dialog.model_edit.setText("custom-dino-model")
    dialog.dino_chunk_spin.setValue(6)
    dialog.dino_box_spin.setValue(0.24)
    dialog.dino_text_spin.setValue(0.18)
    dialog.dino_fp16_check.setChecked(False)
    assert dialog.dino_model_combo.currentData() == ""
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("sam3"))
    assert dialog.dino_options_widget.isHidden()
    assert dialog.settings().model == "original-sam"
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("grounding_dino"))
    result = dialog.settings()
    assert result.model == "custom-dino-model"
    assert result.dino_chunk_size == 6
    assert result.dino_box_threshold == 0.24
    assert result.dino_text_threshold == 0.18
    assert result.dino_fp16 is False


def test_base_tiny_selector_preserves_free_model_edit(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "REPOSITORY_ROOT", tmp_path)
    local_tiny = dino_folder(tmp_path / "runtime/models/grounding-dino-tiny")
    dialog = DetectionSettingsDialog(DetectionSettings(backend="grounding_dino", model=process.DINO_BASE_MODEL))
    qtbot.addWidget(dialog)
    assert dialog.dino_model_combo.currentData() == process.DINO_BASE_MODEL
    dialog.dino_model_combo.setCurrentIndex(dialog.dino_model_combo.findData(process.DINO_TINY_MODEL))
    assert dialog.model_edit.text() == str(local_tiny)
    dialog.dino_model_combo.setCurrentIndex(dialog.dino_model_combo.findData(process.DINO_BASE_MODEL))
    assert dialog.model_edit.text() == process.DINO_BASE_MODEL
    dialog.model_edit.setText("another-custom-dino")
    assert dialog.settings().model == "another-custom-dino"


def test_dino_selection_loads_dedicated_preset_without_preference_writes(qtbot, manifest, monkeypatch):
    path, data = manifest
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(panel, "REPOSITORY_ROOT", path.parent)
    dialog = DetectionSettingsDialog(DetectionSettings(backend="yoloe", model="local-yolo.pt", python="original.exe"))
    qtbot.addWidget(dialog)
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("grounding_dino"))
    assert dialog.settings().python == str(path.parent / "python.exe")
    assert dialog.settings().model == str(path.parent / "model")
    assert dialog.settings().offline is True
    assert dialog.settings().dino_chunk_size == 7
    assert dialog.settings().dino_fp16 is False
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("yoloe"))
    assert dialog.settings().model == "local-yolo.pt"
    assert dialog.settings().python == "original.exe"


def test_dino_selection_uses_main_dino_manifest_if_no_dedicated_preset(qtbot, manifest, monkeypatch):
    path, data = manifest
    main_manifest = path.parent / process.RUNTIME_MANIFEST
    main_manifest.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(panel, "REPOSITORY_ROOT", path.parent)
    dialog = DetectionSettingsDialog(DetectionSettings(backend="yoloe", model="local-yolo.pt"))
    qtbot.addWidget(dialog)
    dialog.backend_combo.setCurrentIndex(dialog.backend_combo.findData("grounding_dino"))
    assert dialog.settings().model == str(path.parent / "model")
    assert dialog.settings().python == str(path.parent / "python.exe")
    assert dialog.settings().offline is True
