"""Workspace-local native runtime selection without altering user preferences."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

import investigator.detection_process as process
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    python = tmp_path / ".venv-sam3" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    model = tmp_path / "runtime" / "models" / "sam3"
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    data = {
        "version": 1,
        "python": ".venv-sam3/Scripts/python.exe",
        "model": "runtime/models/sam3",
        "device": "auto",
        "offline": True,
    }
    manifest = tmp_path / process.RUNTIME_MANIFEST
    manifest.write_text(json.dumps(data), encoding="utf-8")
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return tmp_path, manifest, data, store


def test_manifest_uses_native_paths_and_does_not_save_preferences(runtime):
    root, _, _, store = runtime
    settings = DetectionSettings.load(store)
    assert settings.executable == str(root / ".venv-sam3" / "Scripts" / "python.exe")
    assert settings.model == str(root / "runtime" / "models" / "sam3")
    assert settings.offline is True
    assert settings.device == "auto"
    assert not settings.config_error
    assert not store.allKeys()


def test_manifest_relative_paths_do_not_depend_on_cwd(runtime, monkeypatch, tmp_path):
    root, _, _, store = runtime
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert DetectionSettings.load(store).model == str(root / "runtime" / "models" / "sam3")


def test_empty_python_and_stale_legacy_values_do_not_override_native_defaults(runtime):
    _, _, _, store = runtime
    store.setValue(process.SETTINGS_PYTHON, "  ")
    store.setValue(process.SETTINGS_MODEL, "old/model")
    store.setValue(process.SETTINGS_DEVICE, "cpu")
    store.setValue(process.SETTINGS_OFFLINE, False)
    settings = DetectionSettings.load(store)
    assert settings.device == "auto"
    assert settings.offline is True
    assert settings.model != "old/model"


def test_user_python_and_device_win_but_model_and_offline_stay_bundled(runtime):
    root, _, _, store = runtime
    DetectionSettings(python=sys.executable, model="other/model", device="cpu").save(store)
    settings = DetectionSettings.load(store)
    assert settings.python == sys.executable
    assert settings.device == "cpu"
    # 모델은 고를 수 없다. 매니페스트(번들)의 모델 경로를 그대로 쓴다.
    assert settings.model == str(root / "runtime" / "models" / "sam3")
    assert settings.offline is True
    DetectionSettings(python=sys.executable, offline=False).save(store)
    assert DetectionSettings.load(store).offline is True


def test_invalid_unused_manifest_does_not_block_explicit_user_runtime(runtime):
    _, manifest, _, store = runtime
    manifest.write_text("broken", encoding="utf-8")
    DetectionSettings(python=sys.executable, model="other/model").save(store)
    settings = DetectionSettings.load(store)
    assert settings.python == sys.executable
    assert not settings.config_error


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("version", 2, "설정 버전"),
        ("version", True, "설정 버전"),
        ("python", "missing.exe", "Python 파일이 없습니다"),
        ("python", [], "python 경로"),
        ("model", "missing-model", "모델 폴더가 없습니다"),
        ("model", "", "model 경로"),
        ("device", "metal", "device"),
        ("device", {}, "device"),
    ],
)
def test_invalid_manifest_reports_exact_config_error(runtime, key, value, expected):
    _, manifest, data, store = runtime
    data[key] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")
    settings = DetectionSettings.load(store)
    assert str(manifest) in settings.config_error
    assert expected in settings.config_error


@pytest.mark.parametrize("content", ["[]", "null", "{not-json}", "\ufeff{broken}"])
def test_invalid_json_is_not_silently_replaced_with_gui_python(runtime, content):
    _, manifest, _, store = runtime
    manifest.write_text(content, encoding="utf-8")
    assert "객체 검출 실행 설정을 읽을 수 없습니다" in DetectionSettings.load(store).config_error


def test_utf8_bom_manifest_supported(runtime):
    _, manifest, data, store = runtime
    manifest.write_text(json.dumps(data), encoding="utf-8-sig")
    assert not DetectionSettings.load(store).config_error


def test_missing_manifest_uses_bundled_base_model_offline(tmp_path, monkeypatch, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings = DetectionSettings.load(store)
    assert settings.executable == sys.executable
    assert settings.model == str(bundled_dino_model)
    assert settings.device == "auto"
    assert settings.offline is True
    assert not settings.config_error


@pytest.mark.parametrize("with_manifest", [True, False])
@pytest.mark.parametrize("offline", [True, "true", "1", False, "false", "0"])
def test_old_tiny_or_online_settings_are_ignored(runtime, monkeypatch, bundled_dino_model, with_manifest, offline):
    root, manifest, _, store = runtime
    if not with_manifest:
        manifest.unlink()
    store.setValue(process.SETTINGS_PYTHON, sys.executable)
    store.setValue(process.SETTINGS_BACKEND, "grounding_dino")
    store.setValue(process.SETTINGS_MODEL, "IDEA-Research/grounding-dino-tiny")
    store.setValue(process.SETTINGS_OFFLINE, offline)
    settings = DetectionSettings.load(store)
    expected = root / "runtime" / "models" / "sam3" if with_manifest else bundled_dino_model
    assert settings.model == str(expected)
    assert settings.offline is True
    assert not settings.config_error


@pytest.mark.parametrize("value", [False, "false", "yes", 3])
def test_manifest_offline_key_is_ignored(runtime, value):
    _, manifest, data, store = runtime
    data["offline"] = value
    manifest.write_text(json.dumps(data), encoding="utf-8")
    settings = DetectionSettings.load(store)
    assert not settings.config_error
    assert settings.offline is True


def test_save_removes_old_model_and_offline_keys(runtime):
    _, _, _, store = runtime
    store.setValue(process.SETTINGS_MODEL, "IDEA-Research/grounding-dino-tiny")
    store.setValue(process.SETTINGS_OFFLINE, False)
    DetectionSettings(python=sys.executable, model="other/model", offline=False).save(store)
    store.sync()
    assert not store.contains(process.SETTINGS_MODEL)
    assert not store.contains(process.SETTINGS_OFFLINE)
    assert store.contains(process.SETTINGS_PYTHON)


def _job(tmp_path: Path, settings: DetectionSettings) -> DetectionJob:
    return DetectionJob(
        DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, ["passenger"], "trigger"), settings
    )


@pytest.mark.parametrize("offline", [True, False])
def test_offline_environment_is_always_applied(qtbot, tmp_path, monkeypatch, offline):
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    model = tmp_path / "local-dino"
    model.mkdir()
    (model / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    job = _job(tmp_path, DetectionSettings(python=sys.executable, offline=offline, model=str(model)))
    monkeypatch.setattr(job._process, "start", lambda: None)
    job.start()
    env = job._process.processEnvironment()
    # 설정 값과 관계없이 모델 다운로드를 막는 환경 변수를 항상 넣는다.
    assert all(env.value(name) == "1" for name in names)
    assert env.value("PYTHONIOENCODING") == "utf-8"
    job.kill()


def test_manifest_error_fails_before_start_and_cleans_temporary_files(qtbot, runtime):
    root, manifest, _, store = runtime
    manifest.write_text("broken", encoding="utf-8")
    settings = DetectionSettings.load(store)
    job = _job(root, settings)
    with qtbot.waitSignal(job.failed, timeout=1000) as blocker:
        job.start()
    assert settings.config_error == blocker.args[0]
    assert not job.is_running
    assert not job._temp_dir.exists()
