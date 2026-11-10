"""객체 검출 설정 기능을 없애고 실행 값을 기본값으로 고정한다."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QToolButton

import investigator.detection_process as process
from investigator.detection_process import DetectionSettings
from investigator.widgets import detection_panel as panel_module
from investigator.widgets.detection_panel import DetectionPanel

ROOT = Path(__file__).resolve().parents[2]
# 예전 버전의 설정 창이 settings.ini에 저장하던 검출 설정
LEGACY_VALUES = {
    "detection/backend": "grounding_dino",
    "detection/python": "old-python.exe",
    "detection/model": "IDEA-Research/grounding-dino-tiny",
    "detection/device": "cpu",
    "detection/offline": False,
    "detection/dino_chunk_size": 1,
    "detection/dino_box_threshold": 0.9,
    "detection/dino_text_threshold": 0.9,
    "detection/dino_fp16": False,
}
FIXED_VALUES = {
    "python": "",
    "device": "auto",
    "offline": True,
    "backend": "grounding_dino",
    "dino_chunk_size": 8,
    "dino_box_threshold": 0.3,
    "dino_text_threshold": 0.25,
    "dino_fp16": True,
}


def _values(settings: DetectionSettings) -> dict:
    return {key: getattr(settings, key) for key in FIXED_VALUES}


def test_settings_dialog_and_save_are_removed():
    assert not hasattr(panel_module, "DetectionSettingsDialog")
    assert not hasattr(DetectionSettings, "save")


def test_panel_has_no_settings_button(qtbot):
    panel = DetectionPanel()
    qtbot.addWidget(panel)
    assert not hasattr(panel, "settings_button")
    assert not hasattr(DetectionPanel, "settings_requested")
    assert all("객체 검출 환경" not in button.accessibleName() for button in panel.findChildren(QToolButton))
    assert all("객체 검출 환경" not in button.toolTip() for button in panel.findChildren(QToolButton))


def test_load_without_manifest_uses_fixed_defaults(tmp_path, monkeypatch, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    settings = DetectionSettings.load()
    assert _values(settings) == FIXED_VALUES
    assert settings.executable == sys.executable
    assert settings.model == str(bundled_dino_model)
    assert not settings.config_error


def test_previously_saved_settings_are_ignored_and_left_untouched(tmp_path, monkeypatch, bundled_dino_model):
    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    path = tmp_path / "settings.ini"
    store = QSettings(str(path), QSettings.Format.IniFormat)
    for key, value in LEGACY_VALUES.items():
        store.setValue(key, value)
    store.sync()
    monkeypatch.setattr(process, "detection_settings_store", lambda: store)
    before = path.read_bytes()
    settings = DetectionSettings.load()
    assert _values(settings) == FIXED_VALUES
    assert settings.model == str(bundled_dino_model)
    store.sync()
    assert path.read_bytes() == before


def test_packaged_manifest_uses_the_same_fixed_values(tmp_path):
    # EXE에 들어가는 detection-runtime.json도 고정 기본값과 같은 값이어야 한다.
    manifest = tmp_path / process.RUNTIME_MANIFEST
    shutil.copyfile(ROOT / "packaging" / process.RUNTIME_MANIFEST, manifest)
    model = tmp_path / json.loads(manifest.read_text(encoding="utf-8"))["model"]
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    settings, error = process._load_runtime_manifest(manifest)
    assert not error
    assert _values(settings) == FIXED_VALUES
    assert settings.model == str(model.resolve())
