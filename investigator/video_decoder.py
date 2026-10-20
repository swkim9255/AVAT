"""Qt transport for a single-owner native decoder in an expendable process.

Only small JSON messages cross stdout/stdin. One RGB frame is shared, and the
controller never sends the next command until the previous image is copied.
No capture, native decoder thread, or video-frame FIFO lives in the GUI process.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from contextlib import suppress
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtGui import QImage

LOGGER = logging.getLogger("avat.video_decoder")
MAX_FRAME_BYTES = 256 * 1024 * 1024


class ProcessDecoder(QObject):
    frame_decoded = Signal(QImage, int, int, float)  # image, request, frame, native ms
    end_of_stream = Signal(int)
    request_failed = Signal(int, str)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._stdout_ready)
        self.process.readyReadStandardError.connect(self._stderr_ready)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._buffer = bytearray()
        self._shared: SharedMemory | None = None
        self._shape: tuple[int, int, int] | None = None
        self._opening: tuple[dict, QImage] | None = None
        self._error = ""
        self._closing = False
        self._reported_failure = False
        self._busy = False
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(15_000)
        self._watchdog.timeout.connect(self._timed_out)

    @property
    def pid(self) -> int | None:
        return int(self.process.processId()) or None

    def open(self, path: Path, threads: int = 0) -> tuple[dict, QImage] | None:
        """Bound only initial file opening; all subsequent requests are async."""
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        self.process.start(sys.executable, [
            "-u", "-m", "investigator.video_decode_worker", "--path", str(path),
            "--threads", str(threads),
        ])
        if not self.process.waitForStarted(3000):
            self._error = self.process.errorString()
            self.close()
            return None
        deadline = time.monotonic() + 10
        while self._opening is None and not self._error and time.monotonic() < deadline:
            self.process.waitForReadyRead(50)
            self._stdout_ready()
            if self.process.state() == QProcess.ProcessState.NotRunning:
                break
        if self._opening is None:
            LOGGER.warning("decoder_open_failed path=%s error=%s", path.name, self._error)
            self.close()
        return self._opening

    def request(self, request_id: int, frame: int, *, playback: bool = False) -> bool:
        if self._closing or self._busy or self.process.state() != QProcess.ProcessState.Running:
            return False
        self._busy = True
        payload = {"command": "read" if playback else "seek", "request_id": request_id,
                   "frame": frame}
        if self.process.write(json.dumps(payload).encode("utf-8") + b"\n") < 0:
            self._busy = False
            return False
        self._watchdog.start()
        return True

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._watchdog.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.write(b'{"command":"close"}\n')
            self.process.closeWriteChannel()
            if not self.process.waitForFinished(100):
                self.process.kill()
                self.process.waitForFinished(1000)
        if self._shared is not None:
            self._shared.close()
            # On Windows the last handle deletes the mapping; on POSIX also
            # clean up if the worker died before its finally block.
            with suppress(FileNotFoundError):
                self._shared.unlink()
            self._shared = None
        self._buffer.clear()

    def _copy_image(self) -> QImage:
        if self._shared is None or self._shape is None:
            raise ValueError("디코더의 공유 프레임이 없습니다.")
        height, width, _ = self._shape
        return QImage(self._shared.buf, width, height, width * 3,
                      QImage.Format.Format_RGB888).copy()

    def _stdout_ready(self) -> None:
        self._buffer.extend(bytes(self.process.readAllStandardOutput()))
        if self._closing:
            self._buffer.clear()
            return
        if len(self._buffer) > 64 * 1024:
            self._fail("영상 디코더의 응답 형식이 올바르지 않습니다.")
            return
        while b"\n" in self._buffer:
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            try:
                message = json.loads(line)
                event = message["event"]
                if event == "opened":
                    height, width, channels = map(int, message["shape"])
                    if min(height, width) < 1 or channels != 3 or height * width * 3 > MAX_FRAME_BYTES:
                        raise ValueError("invalid frame dimensions")
                    self._shared = SharedMemory(name=message["shm_name"])
                    if self._shared.size < height * width * 3:
                        raise ValueError("short shared frame")
                    self._shape = (height, width, channels)
                    self._opening = message, self._copy_image()
                elif event == "frame":
                    request = int(message["request_id"])
                    frame = int(message["frame"])
                    native_ms = float(message["decode_ms"])
                    if not math.isfinite(native_ms):
                        native_ms = 0.0
                    image = self._copy_image()  # finish copying before another request can write
                    self._busy = False
                    self._watchdog.stop()
                    self.frame_decoded.emit(image, request, frame, native_ms)
                elif event == "eof":
                    self._busy = False
                    self._watchdog.stop()
                    self.end_of_stream.emit(int(message["request_id"]))
                elif event == "error":
                    self._busy = False
                    self._watchdog.stop()
                    LOGGER.warning("decoder_request_error pid=%s %s", self.pid, message)
                    if isinstance(message.get("request_id"), int):
                        self.request_failed.emit(int(message["request_id"]), str(message["message"]))
                    else:
                        self._fail(str(message["message"]))
                else:
                    raise ValueError("unknown decoder event")
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self._fail(f"영상 디코더 응답을 처리할 수 없습니다: {exc}")
                return
            if self._closing:
                return

    def _stderr_ready(self) -> None:
        # Drain Qt's buffer even for noisy/damaged files; do not retain a log FIFO.
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        if text.strip():
            LOGGER.warning("decoder_stderr pid=%s %s", self.pid, text[-4000:].strip())

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._fail("영상 디코더 프로세스를 시작할 수 없습니다.")

    def _finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._stdout_ready()
        if not self._closing:
            self._fail(f"영상 디코더가 종료되었습니다 (코드 {code}).")

    def _fail(self, message: str) -> None:
        self._error = message
        self._watchdog.stop()
        if not self._closing and not self._reported_failure:
            self._reported_failure = True
            LOGGER.warning("decoder_failed pid=%s %s", self.pid, message)
            self.failed.emit(message)

    def _timed_out(self) -> None:
        self._fail("영상 디코더의 응답 시간이 초과되었습니다.")
