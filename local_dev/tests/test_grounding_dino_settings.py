"""Grounding DINO settings are opt-in and do not alter existing backends."""

from __future__ import annotations

import json
import sys

import pytest
from PySide6.QtCore import QSettings

import investigator.detection_process as process
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings
from investigator.widgets.detection_panel import DetectionSettingsDialog


def dino_folder(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps({"model_type": "grounding-dino"}), encoding="utf-8")
    return path


def job_for(tmp_path, settings):
    return DetectionJob(DetectionRequest(tmp_path / "clip.mp4", 600, 609, 1, ["vehicle"], "trigger"), settings)


def test_new_fields_preserve_old_positional_constructor_and_configured_default():
    legacy = DetectionSettings("python", "model", "cpu", True, "", "sam3")
    assert legacy.backend == "sam3"
    assert legacy.dino_chunk_size == 8
    assert legacy.dino_box_threshold == 0.3
    assert legacy.dino_text_threshold == 0.25
    assert legacy.dino_fp16 is True
    assert DetectionSettings().backend == process.DEFAULT_BACKEND
    # 기본 모델은 배포에 포함된 Base 폴더이고, 항상 오프라인이다.
    assert DetectionSettings().model == str(process.default_dino_model_path())
    assert DetectionSettings().offline is True


def test_dino_qsettings_roundtrip(qtbot, tmp_path, monkeypatch, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    configured = DetectionSettings(sys.executable, str(bundled_dino_model), "cuda", True,
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
    model = dino_folder(tmp_path / "grounding-dino-base")
    job = job_for(tmp_path, DetectionSettings(backend="grounding_dino", model=str(model),
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
        assert arguments[arguments.index("--model") + 1] == str(model)
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


def test_remote_model_id_is_rejected_before_process_start(qtbot, tmp_path, monkeypatch):
    # Hugging Face 모델 ID는 다운로드가 필요하므로 받지 않는다(로컬 폴더만 허용).
    job = job_for(tmp_path, DetectionSettings(model="IDEA-Research/grounding-dino-base", backend="grounding_dino"))
    called = []
    monkeypatch.setattr(job._process, "start", lambda: called.append(True))
    with qtbot.waitSignal(job.failed) as signal:
        job.start()
    assert signal.args[0].startswith("Grounding DINO Base 모델 폴더가 없습니다: ")
    assert signal.args[0].endswith(
        "\n프로그램 폴더의 systems\\models\\grounding-dino-base를 확인해 주세요."
    )
    assert not called


def test_dino_controls_always_show_and_preserve_custom_values(qtbot, tmp_path, monkeypatch, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    dialog = DetectionSettingsDialog(DetectionSettings(backend="sam3", model="original-sam"))
    qtbot.addWidget(dialog)
    assert not dialog.dino_options_widget.isHidden()
    assert dialog.settings().model == str(bundled_dino_model)
    dialog.dino_chunk_spin.setValue(6)
    dialog.dino_box_spin.setValue(0.24)
    dialog.dino_text_spin.setValue(0.18)
    dialog.dino_fp16_check.setChecked(False)
    result = dialog.settings()
    assert result.model == str(bundled_dino_model)
    assert result.backend == "grounding_dino" and result.offline is True
    assert result.dino_chunk_size == 6
    assert result.dino_box_threshold == 0.24
    assert result.dino_text_threshold == 0.18
    assert result.dino_fp16 is False


def test_dino_dialog_preserves_passed_preset_without_a_model_switch(qtbot, manifest, monkeypatch):
    path, data = manifest
    path = path.with_name(process.RUNTIME_MANIFEST)
    path.write_text(json.dumps(data), encoding="utf-8")
    # 설정 창의 모델은 작업 공간 매니페스트(번들)의 모델 경로로 고정된다.
    monkeypatch.setattr(process, "REPOSITORY_ROOT", path.parent)
    settings, error = process._load_runtime_manifest(path)
    assert not error
    dialog = DetectionSettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.settings().python == str(path.parent / "python.exe")
    assert dialog.settings().model == str(path.parent / "model")
    assert dialog.settings().offline is True
    assert dialog.settings().dino_chunk_size == 7
    assert dialog.settings().dino_fp16 is False
    assert dialog.settings().backend == "grounding_dino"
