"""Worker safety and QProcess integration without torch or model downloads."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QProcess

from investigator.detection_process import DetectionJob, DetectionRequest, DetectionSettings
from investigator.detection_worker import __main__ as cli
from investigator.detection_worker.backend import RawInstance
from investigator.detection_worker.classes import load_config
from investigator.detection_worker.events import CancelToken
from investigator.detection_worker.postprocess import build_detections
from investigator.detection_worker.runner import Cancelled, RunOptions, _sha256, plan_frames


class EmptyBackend:
    device = "cpu"
    model_id = "test/empty"

    def predict(self, *_args):
        return {}


@pytest.fixture()
def clip(tmp_path):
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    assert writer.isOpened()
    for _ in range(8):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    return path


@pytest.mark.parametrize(
    "values",
    [
        {"frame_step": 0}, {"frame_step": -1}, {"sample_fps": 0},
        {"sample_fps": float("nan")}, {"sample_fps": float("inf")},
        {"start_frame": -1}, {"start_frame": 3, "end_frame": 3},
        {"mask_threshold": -0.1}, {"mask_threshold": 1.1},
    ],
)
def test_invalid_options_are_rejected(values):
    with pytest.raises(ValueError):
        plan_frames(20, 10, RunOptions(Path("x"), Path("y"), **values))


def test_worker_import_does_not_load_qt_or_torch():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import investigator.detection_worker.__main__; "
         "assert 'PySide6' not in sys.modules; assert 'torch' not in sys.modules"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_empty_explicit_class_list_is_not_all_classes(clip, tmp_path, capsys):
    code = cli.main(["--video", str(clip), "--output", str(tmp_path / "r.json"),
                     "--classes", ",,"], backend=EmptyBackend())
    assert code == cli.EXIT_USAGE
    assert "선택된 클래스" in capsys.readouterr().out


def test_decode_failure_keeps_existing_result(clip, tmp_path, monkeypatch, capsys):
    real_capture = cv2.VideoCapture

    class FailingCapture:
        def __init__(self, *args):
            self.cap = real_capture(*args)
            self.reads = 0

        def __getattr__(self, name):
            return getattr(self.cap, name)

        def read(self):
            self.reads += 1
            return (False, None) if self.reads == 2 else self.cap.read()

    monkeypatch.setattr(cv2, "VideoCapture", FailingCapture)
    target = tmp_path / "r.json"
    target.write_text("existing result", encoding="utf-8")
    code = cli.main(["--video", str(clip), "--output", str(target), "--frame-step", "1"],
                    backend=EmptyBackend())
    assert code == cli.EXIT_ERROR
    assert target.read_text(encoding="utf-8") == "existing result"
    assert "2번 프레임" in capsys.readouterr().out


def test_hash_can_be_cancelled(clip):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        _sha256(clip, token)


@pytest.mark.parametrize("bad", ["score", "shape"])
def test_invalid_model_output_rejected(bad):
    config = load_config(None)
    instances = [RawInstance(np.ones((48, 64), bool), float("nan"))] if bad == "score" else [
        RawInstance(np.ones((24, 32), bool), 0.9)
    ]
    with pytest.raises(ValueError):
        build_detections({"car": instances}, config.enabled(["vehicle"]), config, (48, 64))


def make_job(tmp_path):
    return DetectionJob(
        DetectionRequest(tmp_path / "clip.avi", 0, 5, 1, ["passenger"], "trigger"),
        DetectionSettings(python=sys.executable),
    )


def test_protocol_noise_is_ignored(qtbot, tmp_path):
    job = make_job(tmp_path)
    for raw in (b"not json", b"[]", b"null", b'"str"',
                b'{"type":"started", "total_frames":[]}',
                b'{"type":"progress", "total":"bad"}'):
        job._handle_line(raw)
    job.kill()


def test_cancel_wins_over_completed_result(qtbot, tmp_path):
    job = make_job(tmp_path)
    job._output.write_text('{"detections":[]}', encoding="utf-8")
    job._completed = True
    job._cancel_requested = True
    completed, cancelled = [], []
    job.finished.connect(completed.append)
    job.cancelled.connect(lambda: cancelled.append(True))
    job._process_finished(0, QProcess.ExitStatus.NormalExit)
    assert not completed and cancelled
    assert not job._temp_dir.exists()


def test_real_qprocess_with_fake_protocol(qtbot, tmp_path, monkeypatch):
    job = make_job(tmp_path)
    code = (
        "import json; from pathlib import Path; "
        f"Path({str(job._output)!r}).write_text(json.dumps({{'detections': []}}), encoding='utf-8'); "
        "print(json.dumps({'type':'started','total_frames':1,'device':'cpu'}),flush=True); "
        "print(json.dumps({'type':'progress','processed':1,'total':1,'frame_index':0}),flush=True); "
        "print(json.dumps({'type':'completed'}),flush=True)"
    )
    monkeypatch.setattr(job, "arguments", lambda: ["-c", code])
    progress = []
    job.progress.connect(lambda *values: progress.append(values))
    with qtbot.waitSignal(job.finished, timeout=10000) as blocker:
        job.start()
    assert blocker.args == [{"detections": []}]
    assert progress == [(1, 1, 0)]
    assert not job._temp_dir.exists()


def test_real_qprocess_missing_python_reports_error(qtbot, tmp_path):
    job = make_job(tmp_path)
    job.settings.python = str(tmp_path / "missing-python.exe")
    with qtbot.waitSignal(job.failed, timeout=10000) as blocker:
        job.start()
    assert "검출용 Python" in blocker.args[0]
    assert not job._temp_dir.exists()


def test_real_qprocess_cancel_preserves_no_result(qtbot, tmp_path, monkeypatch):
    job = make_job(tmp_path)
    code = (
        "import sys,json,time; from pathlib import Path; "
        f"cancel=Path({str(job._cancel_file)!r}); "
        "print(json.dumps({'type':'started','total_frames':1}),flush=True); "
        "\nwhile not cancel.exists(): time.sleep(0.01)\nsys.exit(2)"
    )
    monkeypatch.setattr(job, "arguments", lambda: ["-c", code])
    job.started.connect(lambda _total: job.cancel())
    with qtbot.waitSignal(job.cancelled, timeout=10000):
        job.start()
    assert not job._temp_dir.exists()
