from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import threading
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

import cv2
import numpy as np
import pytest

from investigator import video_decode_worker as worker


class Capture:
    def __init__(self, *, total_frames=8, reported_fps=25.0):
        self.total_frames = total_frames
        self.reported_fps = reported_fps
        self.position = 0
        self.reads = []
        self.seeks = []
        self.released = False
        self.fail_read = False
        self.fail_seek = False

    def get(self, prop):
        return {
            cv2.CAP_PROP_FPS: self.reported_fps,
            cv2.CAP_PROP_FRAME_COUNT: self.total_frames,
            cv2.CAP_PROP_N_THREADS: 4,
        }.get(prop, 0)

    def set(self, prop, value):
        assert prop == cv2.CAP_PROP_POS_FRAMES
        self.seeks.append(value)
        if self.fail_seek:
            return False
        self.position = value
        return True

    def read(self):
        self.reads.append(self.position)
        if self.fail_read:
            return False, None
        image = np.full((6, 8, 3), [self.position, 30, 70], dtype=np.uint8)
        self.position += 1
        return True, image

    def release(self):
        self.released = True


@pytest.fixture
def session(monkeypatch):
    capture = Capture()
    monkeypatch.setattr(worker, "_open_capture", lambda *_args: capture)
    session = worker.DecoderSession("unused.mp4", 0)
    session.open_message()
    try:
        yield session, capture
    finally:
        session.close()


@pytest.mark.parametrize("threads", [0, 1, 4])
def test_capture_thread_policy_is_an_explicit_open_time_option(monkeypatch, threads):
    capture = Capture()
    capture.isOpened = lambda: True
    requests = []
    monkeypatch.setattr(cv2, "VideoCapture", lambda *args: requests.append(args) or capture)

    assert worker._open_capture("example.mp4", threads) is capture
    # 하드웨어(GPU) 디코딩을 끈 뒤 스레드 수를 지정한다.
    software = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE]
    assert requests == [
        ("example.mp4", cv2.CAP_FFMPEG, [*software, cv2.CAP_PROP_N_THREADS, threads]),
    ]


def test_startup_metadata_and_one_reused_rgb_shared_buffer(session):
    session, capture = session
    message = session.open_message()
    assert message["fps"] == message["reported_fps"] == 25
    assert message["shape"] == [6, 8, 3]
    assert message["threads"] == 4
    assert capture.reads == [0]
    assert not capture.seeks

    memory = SharedMemory(name=message["shm_name"])
    try:
        image = np.ndarray(message["shape"], dtype=np.uint8, buffer=memory.buf)
        assert image[0, 0].tolist() == [70, 30, 0]
        for frame in [3, 1, 7, 2, 4]:
            reply = session.handle({"command": "seek", "request_id": frame, "frame": frame})
            assert reply["frame"] == frame
            assert reply["decode_ms"] >= 0
            assert image[0, 0].tolist() == [70, 30, frame]
            assert session.memory.name == message["shm_name"]
        del image
    finally:
        memory.close()


def test_sequential_and_cached_reads_avoid_native_seek(session):
    session, capture = session
    for frame in [1, 1, 2, 2]:
        session.handle({"command": "read", "request_id": 1, "frame": frame})
    assert capture.reads == [0, 1, 2]
    assert not capture.seeks
    reply = session.handle({"command": "seek", "request_id": 2, "frame": 999})
    assert reply["frame"] == 7
    assert capture.seeks == [7]


def test_failed_read_invalidates_physical_cursor_before_resume(session):
    session, capture = session
    capture.fail_read = True
    assert session.handle({"command": "read", "request_id": 1, "frame": 1}) == {
        "event": "eof", "request_id": 1,
    }
    assert session.next_frame is None
    capture.fail_read = False
    session.handle({"command": "read", "request_id": 2, "frame": 1})
    assert capture.seeks == [1]


def test_read_at_known_end_is_eof_without_decoding(session):
    session, capture = session
    assert session.handle({"command": "read", "request_id": 5, "frame": 8}) == {
        "event": "eof", "request_id": 5,
    }
    assert capture.reads == [0]


def test_unknown_length_and_fps_are_not_fabricated(monkeypatch):
    capture = Capture(total_frames=float("nan"), reported_fps=float("nan"))
    monkeypatch.setattr(worker, "_open_capture", lambda *_args: capture)
    session = worker.DecoderSession("unused.mp4", 0)
    try:
        message = session.open_message()
        assert message["fps"] == message["reported_fps"] == message["total_frames"] == 0
        capture.fail_read = True
        assert session.handle({"command": "read", "request_id": 1, "frame": 1})["event"] == "eof"
    finally:
        session.close()


def test_failed_seek_is_an_error_and_cursor_becomes_unknown(session):
    session, capture = session
    capture.fail_seek = True
    with pytest.raises(RuntimeError, match="이동"):
        session.handle({"command": "seek", "request_id": 1, "frame": 4})
    assert session.next_frame is None


@pytest.mark.parametrize("payload", [
    {"command": "seek", "request_id": True, "frame": 1},
    {"command": "seek", "request_id": 1, "frame": 1.5},
    {"command": "seek", "request_id": 1, "frame": True},
    {"command": "other", "request_id": 1, "frame": 1},
])
def test_malformed_requests_are_rejected_before_native_calls(session, payload):
    session, capture = session
    with pytest.raises(ValueError):
        session.handle(payload)
    assert capture.reads == [0]
    assert not capture.seeks


def test_close_releases_capture_and_shared_memory(session):
    session, capture = session
    name = session.memory.name
    session.close()
    session.close()
    assert capture.released
    assert session.memory is None
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=name)


def test_protocol_recovers_from_malformed_line_and_keeps_stdout_json(monkeypatch):
    capture = Capture()
    monkeypatch.setattr(worker, "_open_capture", lambda *_args: capture)
    source = io.StringIO('not-json\n[]\n{"command":"seek","request_id":2,"frame":4}\n{"command":"close"}\n')
    output = io.StringIO()
    assert worker.run("unused.mp4", 0, source, output) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["event"] for message in messages] == ["opened", "error", "error", "frame"]
    assert messages[-1]["frame"] == 4
    assert capture.released


def test_protocol_ignores_blank_lines_but_reports_nonblank_corruption(monkeypatch, capsys):
    capture = Capture()
    monkeypatch.setattr(worker, "_open_capture", lambda *_args: capture)
    source = io.StringIO(
        '\n\r\n  \t\nnot-json\n{"command":"seek","request_id":2,"frame":4}\n{"command":"close"}\n'
    )
    output = io.StringIO()
    assert worker.run("unused.mp4", 0, source, output) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["event"] for message in messages] == ["opened", "error", "frame"]
    assert "request_id" not in messages[1]
    assert messages[-1]["request_id"] == 2
    assert messages[-1]["frame"] == 4
    diagnostic = capsys.readouterr().err
    assert "decoder_protocol_error" in diagnostic
    assert "JSONDecodeError" in diagnostic
    assert "not-json" not in diagnostic


def test_startup_error_is_a_protocol_error(monkeypatch):
    def fail(*_args):
        raise RuntimeError("open failed")

    monkeypatch.setattr(worker, "_open_capture", fail)
    output = io.StringIO()
    assert worker.run("missing.mp4", 0, io.StringIO(), output) == 1
    assert json.loads(output.getvalue()) == {"event": "error", "message": "open failed"}


@pytest.mark.parametrize("threads", [0, 1])
def test_real_worker_handshake_and_frame_accurate_shared_memory(tmp_path, threads):
    if not cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG):
        pytest.skip("This OpenCV build does not include FFmpeg.")
    path = tmp_path / "decoder.avi"
    writer = cv2.VideoWriter(
        str(path), cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"MJPG"), 25.0, (64, 48),
    )
    if not writer.isOpened():
        writer.release()
        pytest.skip("This FFmpeg build cannot create the MJPEG test video.")
    try:
        for frame in range(20):
            writer.write(np.full((48, 64, 3), frame * 10, dtype=np.uint8))
    finally:
        writer.release()

    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "investigator.video_decode_worker", "--path", str(path), "--threads", str(threads)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
    reader.start()
    memory = None
    image = None
    try:
        opened = json.loads(lines.get(timeout=20))
        assert opened["event"] == "opened"
        assert opened["total_frames"] == 20
        if threads:
            assert opened["threads"] == threads
        memory = SharedMemory(name=opened["shm_name"])
        image = np.ndarray(opened["shape"], dtype=np.uint8, buffer=memory.buf)
        assert image.mean() == pytest.approx(0, abs=3)
        for request_id, frame in enumerate([19, 0, 10, 4, 5, 5, 17, 1, 9, 3], 1):
            process.stdin.write(json.dumps({"command": "seek", "request_id": request_id, "frame": frame}) + "\n")
            process.stdin.flush()
            reply = json.loads(lines.get(timeout=20))
            assert reply["event"] == "frame"
            assert reply["request_id"] == request_id
            assert reply["frame"] == frame
            assert image.mean() == pytest.approx(frame * 10, abs=3)
        image = None
        memory.close()
        memory = None
        process.stdin.write('{"command":"close"}\n')
        process.stdin.flush()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
    finally:
        image = None
        if memory is not None:
            memory.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for pipe in [process.stdin, process.stdout, process.stderr]:
            pipe.close()
