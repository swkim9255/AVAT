"""소스 실행과 EXE(PyInstaller) 실행의 경로 기준·worker 명령 분기."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

import investigator.detection_process as process
import investigator.runtime_paths as runtime_paths
from investigator.detection_process import DetectionSettings, _load_runtime_manifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def frozen_exe(tmp_path, monkeypatch):
    exe = tmp_path / "AVAT" / "AVAT.exe"
    exe.parent.mkdir()
    exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return exe


def test_source_run_keeps_python_module_command():
    args = ["-u", "-m", runtime_paths.DECODER_WORKER, "--path", "a.mp4"]
    assert runtime_paths.worker_command(sys.executable, args) == (sys.executable, args)
    assert runtime_paths.application_root() == ROOT


def test_frozen_exe_runs_itself_as_worker(frozen_exe):
    program, args = runtime_paths.worker_command(
        str(frozen_exe), ["-u", "-m", runtime_paths.DECODER_WORKER, "--path", "a.mp4"]
    )
    assert program == str(frozen_exe)
    assert args == [runtime_paths.WORKER_FLAG, runtime_paths.DECODER_WORKER, "--path", "a.mp4"]
    _, args = runtime_paths.worker_command(
        str(frozen_exe), ["-m", runtime_paths.DETECTION_WORKER, "--video", "a.mp4"]
    )
    assert args[:2] == [runtime_paths.WORKER_FLAG, runtime_paths.DETECTION_WORKER]
    assert runtime_paths.application_root() == frozen_exe.parent


def test_frozen_exe_keeps_explicit_external_python(frozen_exe, tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    args = ["-m", runtime_paths.DETECTION_WORKER, "--video", "a.mp4"]
    assert runtime_paths.worker_command(str(python), args) == (str(python), args)


def test_frozen_exe_rejects_unknown_worker_module(frozen_exe):
    with pytest.raises(ValueError, match="worker"):
        runtime_paths.worker_command(str(frozen_exe), ["-m", "os", "--help"])


def test_worker_dispatch_ignores_normal_app_arguments():
    assert runtime_paths.run_worker_from_argv(["AVAT.exe"]) is None
    assert runtime_paths.run_worker_from_argv(["AVAT.exe", "--workspace", "D:/x"]) is None
    assert runtime_paths.run_worker_from_argv(["AVAT.exe", runtime_paths.WORKER_FLAG, "os"]) == 3


def test_worker_dispatch_strips_flag_before_worker_argparse(monkeypatch):
    import investigator.video_decode_worker as decoder

    seen = []
    monkeypatch.setattr(decoder, "main", lambda: seen.append(list(sys.argv)) or 7)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    code = runtime_paths.run_worker_from_argv(
        ["AVAT.exe", runtime_paths.WORKER_FLAG, runtime_paths.DECODER_WORKER, "--path", "a.mp4"]
    )
    assert code == 7
    assert seen == [["AVAT.exe", "--path", "a.mp4"]]


def test_main_entry_dispatches_worker_without_starting_gui(tmp_path):
    missing = tmp_path / "missing.mp4"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), runtime_paths.WORKER_FLAG,
         runtime_paths.DECODER_WORKER, "--path", str(missing)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    assert completed.returncode == 1
    event = json.loads(completed.stdout.strip().splitlines()[-1])
    assert event["event"] == "error"


def test_manifest_without_python_uses_running_app(tmp_path, monkeypatch):
    model = tmp_path / "runtime" / "models" / "grounding-dino-base"
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    manifest = tmp_path / process.RUNTIME_MANIFEST
    manifest.write_text(json.dumps({
        "version": 1, "backend": "grounding_dino",
        "model": "runtime/models/grounding-dino-base", "device": "auto", "offline": True,
    }), encoding="utf-8")
    settings, error = _load_runtime_manifest(manifest)
    assert error == ""
    assert settings.python == ""
    assert settings.executable == sys.executable
    assert settings.model == str(model.resolve())

    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    loaded = DetectionSettings.load(store)
    assert not loaded.config_error
    assert loaded.executable == sys.executable


def test_packaged_exe_manifest_is_offline_grounding_dino_without_python(tmp_path):
    data = json.loads((ROOT / "packaging" / "detection-runtime.json").read_text(encoding="utf-8"))
    assert "python" not in data
    assert data["backend"] == "grounding_dino"
    assert data["model"] == "runtime/models/grounding-dino-base"
    assert data["offline"] is True
    assert data["device"] == "auto"


def test_frozen_detection_job_starts_exe_worker(frozen_exe, tmp_path, qtbot):
    from investigator.detection_process import DetectionJob, DetectionRequest
    from investigator.object_detection import DETECTION_CLASSES

    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    request = DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, [DETECTION_CLASSES[0].key], "common")
    job = DetectionJob(request, DetectionSettings(model=str(model), offline=True))
    calls = {}
    job._process.setProgram = lambda program: calls.update(program=program)
    job._process.setArguments = lambda arguments: calls.update(arguments=arguments)
    job._process.start = lambda: calls.update(started=True)
    job.start()
    assert calls["started"] is True
    assert calls["program"] == str(frozen_exe)
    assert calls["arguments"][:2] == [runtime_paths.WORKER_FLAG, runtime_paths.DETECTION_WORKER]
    assert "-m" not in calls["arguments"]
    job.deleteLater()
