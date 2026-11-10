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
    # 앱이 쓰는 설정·상태·로그는 프로그램 파일과 분리해 systems/사용자데이터에 모은다.
    user_data = frozen_root / "systems/사용자데이터"
    assert default_project_tabs_path() == user_data / "workspace.json"
    assert default_log_directory() == user_data / "logs"
    settings = detection_settings_store()
    assert Path(settings.fileName()) == user_data / "settings.ini"
    DetectionSettings(model="local DINO model").save()
    settings.sync()
    assert (user_data / "settings.ini").is_file()
    assert not (frozen_root / "systems/settings.ini").exists()
    assert not (frozen_root / "settings.ini").exists()


def test_frozen_database_is_under_video_root_not_with_program_files(frozen_root):
    repository = CaseRepository(default_workspace())
    repository.save([])
    assert repository.database_path == frozen_root / "01_사고동영상/작업관리/작업관리.db"
    assert repository.database_path.is_file()
    assert not (frozen_root / "systems/작업관리.db").exists()
    assert repository.backup_directory == frozen_root / "systems/사용자데이터/백업"
    assert repository.latest_backup_path == repository.backup_directory / "작업관리_백업.db"


def test_legacy_systems_user_data_moves_into_single_user_data_folder(frozen_root):
    from investigator.user_data import migrate_app_user_data, migrate_workspace_user_data

    systems = frozen_root / "systems"
    systems.mkdir(exist_ok=True)
    (systems / "settings.ini").write_text("[detection]\n", encoding="utf-8")
    (systems / "workspace.json").write_text("{}", encoding="utf-8")
    (systems / "logs").mkdir()
    (systems / "logs" / "avat.log").write_text("log", encoding="utf-8")
    quarantine = systems / "삭제사고" / "20260101-000000-abc" / "이동기록.json"
    quarantine.parent.mkdir(parents=True)
    quarantine.write_text("{}", encoding="utf-8")
    backups = systems / "backups"
    backups.mkdir()
    import os
    for index, name in enumerate(("삭제전-a.db", "영상경로변경전-b.db", "삭제전-c.db")):
        path = backups / name
        path.write_bytes(name.encode("utf-8"))
        os.utime(path, ns=(1_000_000_000 * (index + 1),) * 2)
    manual = systems / "investigations_backup_수동.db"
    manual.write_bytes(b"manual")

    assert set(migrate_app_user_data()) == {"settings.ini", "workspace.json", "logs"}
    assert set(migrate_workspace_user_data(frozen_root)) == {"삭제사고", "작업관리_백업.db"}
    user_data = systems / "사용자데이터"
    assert (user_data / "settings.ini").is_file() and not (systems / "settings.ini").exists()
    assert (user_data / "logs" / "avat.log").read_text(encoding="utf-8") == "log"
    assert (user_data / "삭제사고" / "20260101-000000-abc" / "이동기록.json").is_file()
    # 최신 자동 백업 1개만 남기고 이전 버전의 자동 백업은 정리한다.
    assert (user_data / "백업" / "작업관리_백업.db").read_bytes() == "삭제전-c.db".encode("utf-8")
    assert not backups.exists()
    # 사용자가 직접 만든 백업 파일은 건드리지 않는다.
    assert manual.read_bytes() == b"manual"
    # 다시 실행해도 아무것도 덮어쓰지 않는다.
    assert migrate_app_user_data() == [] and migrate_workspace_user_data(frozen_root) == []


def test_frozen_caches_are_redirected_without_creating_external_directories(frozen_root, monkeypatch):
    names = ("HF_HOME", "TORCH_HOME", "XDG_CACHE_HOME", "YOLO_CONFIG_DIR")
    for name in names:
        monkeypatch.setenv(name, "old external cache")
    runtime_paths.configure_runtime_environment()
    import os
    for name in names:
        assert Path(os.environ[name]).is_relative_to(frozen_root / "systems")


def test_portable_saved_model_never_overrides_bundled_base(frozen_root, monkeypatch):
    native = DetectionSettings(backend="grounding_dino", model="bundled DINO", offline=True)
    monkeypatch.setattr(process, "_load_runtime_manifest", lambda path: (native, ""))
    DetectionSettings(model="saved DINO Tiny", offline=False).save()
    restored = DetectionSettings.load()
    assert restored.backend == "grounding_dino"
    assert restored.model == "bundled DINO"
    assert restored.offline is True
    assert restored.executable == sys.executable


def test_portable_without_manifest_uses_systems_models_base(frozen_root, monkeypatch):
    monkeypatch.setattr(process, "_load_runtime_manifest", lambda path: (None, ""))
    DetectionSettings(model="saved local DINO").save()
    restored = DetectionSettings.load()
    assert restored.backend == "grounding_dino"
    # EXE에서는 systems/models/grounding-dino-base가 번들 Base 모델이다.
    assert restored.model == str(frozen_root / "systems" / "models" / "grounding-dino-base")
    assert restored.offline is True


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
