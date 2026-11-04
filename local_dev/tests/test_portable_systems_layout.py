"""Frozen runtime data/config/cache paths stay under the portable systems folder."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from investigator import detection_process as process
from investigator import runtime_paths
from investigator.detection_process import DetectionSettings, detection_settings_store
from investigator.diagnostics import default_log_directory
from investigator.main_window import default_workspace
from investigator.project_tabs_state import default_project_tabs_path
from investigator.storage import CaseRepository


@pytest.fixture(params=[".", "systems"])
def frozen_root(tmp_path, monkeypatch, request):
    root = tmp_path / "Portable AVAT"
    root.mkdir()
    executable = root / request.param / "AVAT.exe"
    executable.parent.mkdir(exist_ok=True)
    executable.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return root


def test_frozen_workspace_and_support_paths(frozen_root):
    assert default_workspace() == frozen_root
    assert runtime_paths.application_root() == frozen_root
    assert runtime_paths.runtime_data_root() == frozen_root / "systems"
    assert default_project_tabs_path() == frozen_root / "systems/workspace.json"
    assert default_log_directory() == frozen_root / "systems/logs"
    settings = detection_settings_store()
    assert Path(settings.fileName()) == frozen_root / "systems/settings.ini"
    DetectionSettings(model="local DINO model").save()
    settings.sync()
    assert (frozen_root / "systems/settings.ini").is_file()
    assert not (frozen_root / "settings.ini").exists()


def test_frozen_database_is_under_video_root_not_with_program_files(frozen_root):
    repository = CaseRepository(default_workspace())
    repository.save([])
    assert repository.database_path == frozen_root / "01_사고동영상/작업관리/작업관리.db"
    assert repository.database_path.is_file()
    assert not (frozen_root / "systems/작업관리.db").exists()
    assert repository.backup_directory == frozen_root / "systems/backups"


def test_frozen_caches_are_redirected_without_creating_external_directories(frozen_root, monkeypatch):
    names = ("HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "YOLO_CONFIG_DIR")
    for name in names:
        monkeypatch.setenv(name, "old external cache")
    runtime_paths.configure_runtime_environment()
    import os
    for name in names:
        assert Path(os.environ[name]).is_relative_to(frozen_root / "systems")


def test_portable_explicit_dino_model_setting_overrides_bundled_default(frozen_root, monkeypatch):
    native = DetectionSettings(backend="grounding_dino", model="bundled DINO", offline=True)
    monkeypatch.setattr(process, "_load_runtime_manifest", lambda path: (native, ""))
    DetectionSettings(model="saved DINO Tiny", offline=True).save()
    restored = DetectionSettings.load()
    assert restored.backend == "grounding_dino"
    assert restored.model == "saved DINO Tiny"
    assert restored.executable == sys.executable


def test_portable_saved_backend_still_loads_without_a_manifest(frozen_root, monkeypatch):
    monkeypatch.setattr(process, "_load_runtime_manifest", lambda path: (None, ""))
    DetectionSettings(model="saved local DINO").save()
    restored = DetectionSettings.load()
    assert restored.backend == "grounding_dino"
    assert restored.model == "saved local DINO"


def test_spec_and_manifest_use_matching_systems_model_layout():
    root = Path(__file__).resolve().parents[2]
    spec = root / "packaging/AVAT.spec"
    source = spec.read_text(encoding="utf-8")
    ast.parse(source)
    assert 'contents_directory="."' in source
    assert 'entry.rename(systems_root / entry.name)' in source
    assert '"models/grounding-dino-base"' in source
    manifest = json.loads((root / "packaging/detection-runtime.json").read_text(encoding="utf-8"))
    assert manifest["model"] == "models/grounding-dino-base"
    assert manifest["python"] == "" and manifest["offline"] is True
