"""소스와 EXE 실행의 경로 기준·worker 명령·내장 검출 환경을 검증한다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import investigator.detection_process as process
import investigator.runtime_paths as runtime_paths
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings

ROOT = Path(__file__).resolve().parents[2]
SOURCE_PYTHON = sys.executable


@pytest.fixture
def frozen_exe(tmp_path, monkeypatch):
    exe = tmp_path / "AVAT" / "AVAT.exe"
    exe.parent.mkdir()
    exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return exe


def _dino_model(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    return path


def _manifest(root, **options):
    _dino_model(root / "runtime" / "models" / "grounding-dino-base")
    data = {
        "version": 1,
        "backend": "grounding_dino",
        "model": "runtime/models/grounding-dino-base",
        "device": "auto",
        "offline": True,
        **options,
    }
    path = root / process.RUNTIME_MANIFEST
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("module", sorted(runtime_paths.WORKER_MODULES))
def test_source_run_keeps_python_module_command(module, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    args = ["-u", "-m", module, "--path", "a.mp4"]
    assert runtime_paths.worker_command(sys.executable, args) == (sys.executable, args)
    assert runtime_paths.application_root() == ROOT
    assert args == ["-u", "-m", module, "--path", "a.mp4"]


@pytest.mark.parametrize("module", sorted(runtime_paths.WORKER_MODULES))
@pytest.mark.parametrize("prefix", [[], ["-u"], ["-u", "-u"]])
def test_frozen_exe_runs_itself_as_worker(frozen_exe, module, prefix):
    original = [*prefix, "-m", module, "--path", "한글 영상.mp4"]
    program, args = runtime_paths.worker_command(str(frozen_exe), original)
    assert program == str(frozen_exe)
    assert args == [runtime_paths.WORKER_FLAG, module, "--path", "한글 영상.mp4"]
    assert original == [*prefix, "-m", module, "--path", "한글 영상.mp4"]
    assert runtime_paths.application_root() == frozen_exe.parent


def test_frozen_exe_keeps_explicit_external_python(frozen_exe, tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    args = ["-u", "-m", runtime_paths.DETECTION_WORKER, "--video", "a.mp4"]
    assert runtime_paths.worker_command(str(python), args) == (str(python), args)


@pytest.mark.parametrize("arguments", [[], ["-m"], ["-m", "os"], ["--help"]])
def test_frozen_exe_rejects_unsupported_worker_commands(frozen_exe, arguments):
    with pytest.raises(ValueError, match="worker"):
        runtime_paths.worker_command(str(frozen_exe), arguments)


def test_worker_dispatch_ignores_normal_app_arguments():
    assert runtime_paths.run_worker_from_argv(["AVAT.exe"]) is None
    assert runtime_paths.run_worker_from_argv(["AVAT.exe", "--workspace", "D:/x"]) is None


@pytest.mark.parametrize("tail", [[], ["os"]])
def test_worker_dispatch_rejects_missing_or_unknown_module(tail, capsys):
    assert runtime_paths.run_worker_from_argv(["AVAT.exe", runtime_paths.WORKER_FLAG, *tail]) == 3
    assert "알 수 없는 AVAT worker" in capsys.readouterr().err


def test_decoder_dispatch_strips_flag_before_worker_argparse(monkeypatch):
    import investigator.video_decode_worker as decoder

    seen = []
    monkeypatch.setattr(decoder, "main", lambda: seen.append(list(sys.argv)) or 7)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    code = runtime_paths.run_worker_from_argv(
        ["AVAT.exe", runtime_paths.WORKER_FLAG, runtime_paths.DECODER_WORKER, "--path", "a.mp4"]
    )
    assert code == 7
    assert seen == [["AVAT.exe", "--path", "a.mp4"]]


def test_detection_dispatch_lowers_priority_before_worker_main(monkeypatch):
    import investigator.detection_worker.__main__ as detector

    seen = []
    monkeypatch.setattr(detector, "_lower_process_priority", lambda: seen.append("priority"))
    monkeypatch.setattr(detector, "main", lambda: seen.append(list(sys.argv)) or 2)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    code = runtime_paths.run_worker_from_argv(
        ["AVAT.exe", runtime_paths.WORKER_FLAG, runtime_paths.DETECTION_WORKER, "--video", "a.mp4"]
    )
    assert code == 2
    assert seen == ["priority", ["AVAT.exe", "--video", "a.mp4"]]


@pytest.mark.parametrize(
    ("module", "arguments", "expected_code", "event_key"),
    [
        (runtime_paths.DECODER_WORKER, ["--path", "없는 영상.mp4"], 1, "event"),
        (runtime_paths.DETECTION_WORKER, ["--video", "없는 영상.mp4", "--output", "result.json"], 3, "type"),
    ],
)
def test_main_entry_dispatches_worker_without_starting_gui(tmp_path, module, arguments, expected_code, event_key):
    child_local_data = tmp_path / "local"
    completed = subprocess.run(
        [SOURCE_PYTHON, str(ROOT / "main.py"), runtime_paths.WORKER_FLAG, module, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), "LOCALAPPDATA": str(child_local_data)},
    )
    assert completed.returncode == expected_code, completed.stderr
    event = json.loads(completed.stdout.strip().splitlines()[-1])
    assert event[event_key] == "error"
    if module == runtime_paths.DETECTION_WORKER:
        assert "없는 영상.mp4" in event["message"]
    assert not (tmp_path / "result.json").exists()
    assert not (child_local_data / "AVAT" / "logs").exists()


@pytest.mark.parametrize("options", [{}, {"python": ""}, {"python": "  "}])
def test_manifest_without_python_uses_running_app(tmp_path, monkeypatch, options):
    manifest = _manifest(tmp_path, **options)
    settings, error = process._load_runtime_manifest(manifest)
    assert error == ""
    assert settings.python == ""
    assert settings.executable == sys.executable
    assert settings.model == str((tmp_path / "runtime/models/grounding-dino-base").resolve())
    assert settings.backend == "grounding_dino"
    assert settings.offline is True

    monkeypatch.setattr(process, "REPOSITORY_ROOT", tmp_path)
    loaded = DetectionSettings.load()
    assert not loaded.config_error
    assert loaded.executable == sys.executable


def test_frozen_manifest_without_python_uses_exe(frozen_exe, tmp_path, monkeypatch):
    _manifest(frozen_exe.parent)
    monkeypatch.setattr(process, "REPOSITORY_ROOT", runtime_paths.application_root())
    loaded = DetectionSettings.load()
    assert not loaded.config_error
    assert loaded.python == ""
    assert loaded.executable == str(frozen_exe)


def test_manifest_keeps_explicit_relative_python(frozen_exe, tmp_path):
    python = tmp_path / "external" / "python.exe"
    python.parent.mkdir()
    python.touch()
    manifest = _manifest(tmp_path, python="external/python.exe")
    settings, error = process._load_runtime_manifest(manifest)
    assert not error
    assert settings.executable == str(python.resolve())


@pytest.mark.parametrize("python", [None, False, 1, []])
def test_optional_python_still_rejects_non_string_values(tmp_path, python):
    settings, error = process._load_runtime_manifest(_manifest(tmp_path, python=python))
    assert settings is None
    assert "python 경로는 문자열" in error


@pytest.mark.parametrize("mode", ["source", "frozen", "external_python"])
def test_detection_job_starts_expected_worker_command(frozen_exe, tmp_path, qtbot, monkeypatch, mode):
    from investigator.object_detection import DETECTION_CLASSES

    if mode == "source":
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "executable", SOURCE_PYTHON)
    python = ""
    if mode == "external_python":
        external_python = tmp_path / "python.exe"
        external_python.touch()
        python = str(external_python)
    monkeypatch.setattr(process, "REPOSITORY_ROOT", runtime_paths.application_root())
    model = _dino_model(tmp_path / "model")
    request = DetectionRequest(tmp_path / "clip.mp4", 0, 0, 1, [DETECTION_CLASSES[0].key], "common")
    job = DetectionJob(request, DetectionSettings(python=python, model=str(model), offline=True))
    calls = {}
    monkeypatch.setattr(job._process, "start", lambda: calls.update(started=True))
    try:
        job.start()
        assert calls["started"] is True
        assert job._process.program() == (python or sys.executable)
        arguments = job._process.arguments()
        prefix = runtime_paths.WORKER_FLAG if mode == "frozen" else "-m"
        assert arguments[:2] == [prefix, runtime_paths.DETECTION_WORKER]
        assert job._process.workingDirectory() == str(runtime_paths.application_root())
        assert arguments[arguments.index("--backend") + 1] == "grounding_dino"
        environment = job._process.processEnvironment()
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
            assert environment.value(name) == "1"
        assert environment.value("PYTHONIOENCODING") == "utf-8"
    finally:
        job.kill()
        job.deleteLater()


@pytest.mark.parametrize("frozen", [False, True])
def test_video_decoder_starts_expected_worker_command(frozen_exe, tmp_path, qtbot, monkeypatch, frozen):
    from investigator.video_decoder import ProcessDecoder

    if not frozen:
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(sys, "executable", SOURCE_PYTHON)
    decoder = ProcessDecoder()
    calls = {}
    monkeypatch.setattr(
        decoder.process, "start", lambda program, arguments: calls.update(program=program, arguments=arguments)
    )
    monkeypatch.setattr(decoder.process, "waitForStarted", lambda timeout: False)
    try:
        video = tmp_path / "한글 영상.mp4"
        assert decoder.open(video, threads=2) is None
        assert calls["program"] == sys.executable
        prefix = [runtime_paths.WORKER_FLAG] if frozen else ["-u", "-m"]
        assert calls["arguments"] == [*prefix, runtime_paths.DECODER_WORKER,
                                      "--path", str(video), "--threads", "2"]
        assert decoder.process.workingDirectory() == str(runtime_paths.application_root())
    finally:
        decoder.close()
        decoder.deleteLater()
