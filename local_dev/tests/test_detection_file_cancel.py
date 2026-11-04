"""The GUI uses file cancellation: no stdin reader during native DLL imports."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess

import investigator.detection_process as process
from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings


def _job(tmp_path: Path) -> DetectionJob:
    return DetectionJob(
        DetectionRequest(tmp_path / "clip.mp4", 0, 3, 1, ["passenger"], "trigger"),
        DetectionSettings(python=sys.executable),
    )


def test_gui_arguments_use_own_cancel_file_without_stdin_thread(qtbot, tmp_path):
    job = _job(tmp_path)
    args = job.arguments()
    assert "--stdin-cancel" not in args
    assert args[args.index("--cancel-file") + 1] == str(job._cancel_file)
    assert job._cancel_file.parent == job._temp_dir
    assert not job._cancel_file.exists()
    job.kill()


def test_cancel_creates_marker_once_and_keeps_bounded_kill_fallback(qtbot, tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(job._process, "state", lambda: QProcess.ProcessState.Running)
    written = []
    monkeypatch.setattr(job._process, "write", lambda data: written.append(data))
    job.cancel()
    assert job._cancel_requested
    assert job._cancel_file.is_file()
    assert job._kill_timer.isActive()
    assert not written
    modified = job._cancel_file.stat().st_mtime_ns
    job.cancel()
    assert job._cancel_file.stat().st_mtime_ns == modified
    monkeypatch.setattr(job._process, "state", lambda: QProcess.ProcessState.NotRunning)
    job.kill()
    assert not job._cancel_file.exists()


def test_marker_failure_terminates_instead_of_raising(qtbot, tmp_path, monkeypatch):
    job = _job(tmp_path)
    monkeypatch.setattr(job._process, "state", lambda: QProcess.ProcessState.Running)
    original_touch = Path.touch

    def denied(path, *args, **kwargs):
        if path == job._cancel_file:
            raise PermissionError("marker denied")
        return original_touch(path, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", denied)
    terminated = []
    monkeypatch.setattr(job._process, "terminate", lambda: terminated.append(True))
    job.cancel()
    assert terminated == [True]
    assert job._cancel_requested and job._kill_timer.isActive()
    monkeypatch.setattr(job._process, "state", lambda: QProcess.ProcessState.NotRunning)
    job.kill()


def test_real_qprocess_cancels_during_model_loading_without_stdin(qtbot, tmp_path, monkeypatch):
    job = _job(tmp_path)
    code = (
        "import json,sys,time,threading; "
        "from investigator.detection_worker.events import CancelToken; "
        "before=threading.active_count(); "
        f"token=CancelToken({str(job._cancel_file)!r}); "
        "assert threading.active_count()==before; "
        "print(json.dumps({'type':'loading_model','model_id':'loading-test'}),flush=True); "
        "\nwhile not token.cancelled: time.sleep(0.01)\n"
        "sys.exit(2)"
    )
    monkeypatch.setattr(job, "arguments", lambda: ["-c", code])
    started, completed = [], []
    job.started.connect(started.append)
    job.finished.connect(completed.append)
    job.status.connect(lambda message: job.cancel() if "loading-test" in message else None)
    with qtbot.waitSignal(job.cancelled, timeout=10000):
        job.start()
    assert not started and not completed
    assert not job._temp_dir.exists()


def test_real_qprocess_late_success_after_cancel_is_discarded(qtbot, tmp_path, monkeypatch):
    job = _job(tmp_path)
    code = (
        "import json,time; from pathlib import Path; "
        f"cancel=Path({str(job._cancel_file)!r}); "
        "print(json.dumps({'type':'started','total_frames':1}),flush=True); "
        "\nwhile not cancel.exists(): time.sleep(0.01)\n"
        f"Path({str(job._output)!r}).write_text(json.dumps({{'detections':[]}}),encoding='utf-8'); "
        "print(json.dumps({'type':'completed'}),flush=True)"
    )
    monkeypatch.setattr(job, "arguments", lambda: ["-c", code])
    completed, failed = [], []
    job.finished.connect(completed.append)
    job.failed.connect(failed.append)
    job.started.connect(lambda _count: job.cancel())
    with qtbot.waitSignal(job.cancelled, timeout=10000):
        job.start()
    assert job._completed
    assert not completed and not failed
    assert not job._temp_dir.exists()


def test_real_qprocess_uncooperative_model_is_killed_after_grace(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(process, "CANCEL_GRACE_MS", 30)
    job = _job(tmp_path)
    code = (
        "import json,time; "
        "print(json.dumps({'type':'started','total_frames':1}),flush=True); "
        "time.sleep(30)"
    )
    monkeypatch.setattr(job, "arguments", lambda: ["-c", code])
    job.started.connect(lambda _count: job.cancel())
    with qtbot.waitSignal(job.cancelled, timeout=10000):
        job.start()
    assert not job.is_running
    assert not job._temp_dir.exists()
