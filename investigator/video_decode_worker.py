"""Native video decoder subprocess with a single, bounded RGB shared buffer.

Run with ``python -u -m investigator.video_decode_worker --path FILE --threads 0``.
Only newline-delimited JSON protocol messages are written to stdout.  The parent
must copy each announced image before sending the next request: that handshake
keeps the one shared frame buffer from being overwritten while it is being read.
This module deliberately imports no Qt/application modules.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from contextlib import suppress
from multiprocessing.shared_memory import SharedMemory
from time import perf_counter
from typing import TextIO

import cv2
import numpy as np


def _software_decoding_params() -> list[int]:
    """Request CPU decoding explicitly, so no D3D11/DXVA path enters the GPU driver.

    OpenCV's FFmpeg backend already defaults to software decoding; pinning it
    keeps another OpenCV build or OPENCV_FFMPEG_CAPTURE_OPTIONS from changing it.
    """
    hw_property = getattr(cv2, "CAP_PROP_HW_ACCELERATION", None)
    software = getattr(cv2, "VIDEO_ACCELERATION_NONE", None)
    return [] if hw_property is None or software is None else [hw_property, software]


def _open_capture(path: str, threads: int) -> cv2.VideoCapture:
    """Open exactly one FFmpeg capture, with parallelism selected at open time."""
    if threads < 0:
        raise ValueError("Decoder thread count cannot be negative.")
    thread_property = getattr(cv2, "CAP_PROP_N_THREADS", None)
    if thread_property is None and threads:
        raise RuntimeError("OpenCV does not support configuring FFmpeg decoder threads.")
    capture = None
    try:
        params = _software_decoding_params()
        if thread_property is not None:
            params += [thread_property, threads]
        if params:
            capture = cv2.VideoCapture(path, cv2.CAP_FFMPEG, params)
        else:
            capture = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
        if not capture.isOpened():
            raise RuntimeError("영상을 열 수 없습니다.")
        return capture
    except Exception:
        if capture is not None:
            with suppress(Exception):
                capture.release()
        raise


def _positive_float(value: float) -> float:
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _codec_tag(capture: cv2.VideoCapture) -> str:
    """Four-character codec tag (for example avc1, hev1) for diagnostics only."""
    with suppress(Exception):
        tag = int(capture.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode("ascii")
        tag = tag.strip("\0 ")
        if tag.isprintable():
            return tag
    return ""


class DecoderSession:
    """Serial capture ownership and one shared-memory image, owned by this process."""

    def __init__(self, path: str, threads: int) -> None:
        self.capture = _open_capture(path, threads)
        self.memory: SharedMemory | None = None
        self._shared_image: np.ndarray | None = None
        self._shape: tuple[int, ...] | None = None
        self.current_frame = -1
        self.next_frame: int | None = None
        try:
            self.fps = _positive_float(self.capture.get(cv2.CAP_PROP_FPS))
            self.total_frames = int(_positive_float(self.capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            thread_property = getattr(cv2, "CAP_PROP_N_THREADS", None)
            self.threads = int(self.capture.get(thread_property)) if thread_property is not None else 0
            self.codec = _codec_tag(self.capture)
        except Exception:
            self.capture.release()
            raise

    def open_message(self) -> dict:
        if not self._decode(0):
            raise RuntimeError("영상의 첫 프레임을 읽을 수 없습니다.")
        assert self.memory is not None and self._shape is not None
        return {
            "event": "opened",
            "fps": self.fps,
            "reported_fps": self.fps,
            "total_frames": self.total_frames,
            "width": self._shape[1],
            "height": self._shape[0],
            "shm_name": self.memory.name,
            "shape": list(self._shape),
            "frame": 0,
            "threads": self.threads,
            "codec": self.codec,
        }

    def _decode(self, frame: int) -> bool:
        if frame == self.current_frame:
            return True
        try:
            needs_seek = frame != self.next_frame and not (
                frame == 0 and self.current_frame == -1 and self.next_frame is None
            )
            if needs_seek and not self.capture.set(cv2.CAP_PROP_POS_FRAMES, frame):
                raise RuntimeError("요청한 영상 프레임으로 이동할 수 없습니다.")
            ok, bgr = self.capture.read()
            if not ok or bgr is None or bgr.size == 0:
                self.next_frame = None
                return False
            rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
                raise RuntimeError("지원하지 않는 영상 프레임 형식입니다.")
            if rgb.nbytes > 256 * 1024 * 1024:
                raise RuntimeError("공유 프레임의 최대 크기(256MiB)를 초과했습니다.")
            if self.memory is None:
                self.memory = SharedMemory(create=True, size=rgb.nbytes)
                self._shape = rgb.shape
                self._shared_image = np.ndarray(rgb.shape, dtype=np.uint8, buffer=self.memory.buf)
            if rgb.shape != self._shape:
                raise RuntimeError("영상 재생 중 프레임 해상도가 변경되었습니다.")
            assert self._shared_image is not None
            self._shared_image[:] = rgb
            self.current_frame = frame
            self.next_frame = frame + 1
            return True
        except Exception:
            self.next_frame = None
            raise

    def handle(self, request: dict) -> dict:
        request_id = request.get("request_id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            raise ValueError("A request must include an integer request_id.")
        command = request.get("command")
        if command not in {"seek", "read"}:
            raise ValueError("Unknown decoder command.")
        target = request.get("frame")
        if not isinstance(target, int) or isinstance(target, bool):
            raise ValueError("A frame must be an integer.")
        target = max(0, target)
        if self.total_frames:
            if command == "read" and target >= self.total_frames:
                return {"event": "eof", "request_id": request_id}
            target = min(target, self.total_frames - 1)
        sequential_read = command == "read" and target == self.next_frame
        started = perf_counter()
        if not self._decode(target):
            if sequential_read:
                return {"event": "eof", "request_id": request_id}
            raise RuntimeError("요청한 영상 프레임을 읽을 수 없습니다.")
        return {
            "event": "frame",
            "request_id": request_id,
            "frame": target,
            "decode_ms": (perf_counter() - started) * 1000.0,
        }

    def close(self) -> None:
        with suppress(Exception):
            self.capture.release()
        # Drop the exported numpy view before closing its underlying buffer.
        self._shared_image = None
        if self.memory is not None:
            memory, self.memory = self.memory, None
            with suppress(Exception):
                memory.close()
            with suppress(FileNotFoundError):
                memory.unlink()


def _send(output: TextIO, message: dict) -> None:
    output.write(json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n")
    output.flush()


def _suppress_windows_error_dialogs() -> None:
    if os.name == "nt":
        # A native decoder crash must reach the parent as process termination,
        # not block on a Windows Error Reporting/modal crash dialog.
        import ctypes

        with suppress(Exception):
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)


def _lower_process_priority() -> None:
    if os.name == "nt":
        # Parallel decoding can briefly occupy every core while scrubbing.
        # Below-normal priority lets the GUI, audio and system services run
        # first; it changes nothing while any core is idle.
        import ctypes

        with suppress(Exception):
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL


def run(path: str, threads: int, source: TextIO, output: TextIO) -> int:
    session = None
    try:
        session = DecoderSession(path, threads)
        _send(output, session.open_message())
        for line in source:
            if not line.strip():
                continue
            request_id = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("A decoder request must be an object.")
                request_id = request.get("request_id")
                if request.get("command") == "close":
                    break
                _send(output, session.handle(request))
            except Exception as error:
                response = {"event": "error", "message": str(error)}
                if isinstance(request_id, int) and not isinstance(request_id, bool):
                    response["request_id"] = request_id
                else:
                    # An uncorrelated error is a protocol/transport failure,
                    # not a decoder failure for a particular frame. Retain a
                    # bounded diagnostic on stderr, never protocol stdout.
                    print(
                        f"decoder_protocol_error type={type(error).__name__} "
                        f"detail={str(error)[:512]!r}",
                        file=sys.stderr, flush=True,
                    )
                _send(output, response)
        return 0
    except Exception as error:
        with suppress(BrokenPipeError, OSError):
            _send(output, {"event": "error", "message": str(error)})
        return 1
    finally:
        if session is not None:
            session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--threads", type=int, default=0)
    args = parser.parse_args()
    _suppress_windows_error_dialogs()
    _lower_process_priority()
    return run(args.path, args.threads, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
