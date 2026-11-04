r"""Compare the old and single-thread OpenCV seek paths without editing media.

Run from the repository root::

    .venv\Scripts\python.exe local_dev/tools/benchmark_seek.py --video movie.mp4 --output local_dev/reports/seek.json

Each capture lives in a disposable child process: a native decoder crash does
not terminate the benchmark coordinator. Only seeking/decoding is timed; these
numbers do not represent end-to-end Qt timeline latency or a stability proof.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PHASES = ("jump", "drag", "backward")
MODE_LABELS = {
    "original": "기존 방식 · OpenCV 기본 병렬 디코딩",
    "current": "현재 방식 · FFmpeg 단일 스레드",
}


def sample_frames(total_frames: int) -> dict[str, list[int]]:
    """Use identical deterministic targets in both modes, even for short clips."""
    if total_frames < 1:
        raise ValueError("영상 프레임 수를 확인할 수 없습니다.")
    def clamp(frame: float) -> int:
        return max(0, min(total_frames - 1, int(frame)))

    return {
        "jump": [clamp(total_frames * ratio) for ratio in
                 (.95, .10, .60, .20, .80, .05, .40, .99, .30, .70, .15, .85)],
        "drag": [clamp(total_frames * (.05 + index * .90 / 39))
                 for index in range(40)],
        "backward": [clamp(total_frames // 2 - index) for index in range(20)],
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(float(row["total_ms"]) for row in rows)
    if not values:
        return {"mean_ms": None, "median_ms": None, "p95_ms": None,
                "max_ms": None, "calls": 0}
    return {
        "mean_ms": round(statistics.mean(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3),
        "max_ms": round(values[-1], 3),
        "calls": len(values),
    }


def _emit(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)


def _suppress_windows_crash_dialogs() -> None:
    if os.name == "nt":
        import ctypes
        # Process-local settings, applied before importing any native decoder.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        with suppress(OSError, AttributeError):
            ctypes.CDLL("ucrtbase.dll")._set_abort_behavior(0, 0x1 | 0x2)


def _worker(video: str, mode: str) -> int:
    _suppress_windows_crash_dialogs()
    capture = None
    try:
        import cv2
        started = time.perf_counter()
        if mode == "original":
            capture = cv2.VideoCapture(video)
        else:
            capture = cv2.VideoCapture(
                video, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
        open_ms = (time.perf_counter() - started) * 1000
        if not capture.isOpened():
            raise RuntimeError("영상을 열 수 없습니다.")
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        thread_prop = getattr(cv2, "CAP_PROP_N_THREADS", None)
        actual_threads = capture.get(thread_prop) if thread_prop is not None else None
        fourcc = int(capture.get(cv2.CAP_PROP_FOURCC))
        codec = "".join(chr((fourcc >> (8 * index)) & 255) for index in range(4))
        _emit("metadata", backend=capture.getBackendName(),
              actual_threads=actual_threads, open_ms=round(open_ms, 3),
              opencv=cv2.__version__, video={
                  "name": Path(video).name,
                  "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                  "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                  "fps": capture.get(cv2.CAP_PROP_FPS),
                  "total_frames": total,
                  "codec": codec.strip("\x00"),
              })
        # Exclude decoder startup/first-frame allocation from seek measurements.
        if not capture.read()[0]:
            raise RuntimeError("첫 프레임을 읽을 수 없습니다.")
        for phase, targets in sample_frames(total).items():
            for frame in targets:
                before = time.perf_counter()
                positioned = capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
                after_set = time.perf_counter()
                if not positioned:
                    raise RuntimeError(f"프레임 {frame} 탐색에 실패했습니다.")
                ok, image = capture.read()
                after_read = time.perf_counter()
                if not ok or image is None:
                    raise RuntimeError(f"프레임 {frame} 읽기에 실패했습니다.")
                _emit("sample", phase=phase, row={
                    "frame": frame,
                    "set_ms": round((after_set - before) * 1000, 3),
                    "read_ms": round((after_read - after_set) * 1000, 3),
                    "total_ms": round((after_read - before) * 1000, 3),
                })
        capture.release()
        capture = None
        _emit("done")
        return 0
    except Exception as exc:
        _emit("error", message=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if capture is not None:
            capture.release()


def decode_worker_output(stdout: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Keep completed samples even if the next native operation aborted."""
    run: dict[str, Any] = {"backend": None, "actual_threads": None,
                           "open_ms": None, "phases": {key: [] for key in PHASES},
                           "completed": False, "errors": []}
    metadata = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind == "metadata":
            metadata = event
            for key in ("backend", "actual_threads", "open_ms"):
                run[key] = event.get(key)
        elif kind == "sample" and event.get("phase") in PHASES:
            run["phases"][event["phase"]].append(event["row"])
        elif kind == "done":
            run["completed"] = True
        elif kind == "error":
            run["errors"].append(event.get("message", "디코더 오류"))
    return run, metadata


def run_isolated(video: Path, mode: str, timeout: float) -> tuple[dict[str, Any], Any]:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", mode,
               "--video", str(video)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", errors="replace", creationflags=flags)
    timed_out = False
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        stdout, stderr = child.communicate()
    run, metadata = decode_worker_output(stdout)
    run["exit_code"] = child.returncode
    if timed_out:
        run["errors"].append(f"디코더 제한 시간({timeout:g}초)을 초과했습니다.")
    elif child.returncode:
        run["errors"].append(f"디코더 프로세스 종료 코드: {child.returncode}")
    elif not run["completed"]:
        run["errors"].append("디코더가 완료 신호 없이 종료되었습니다.")
    if stderr.strip():
        run["diagnostics"] = stderr[-4000:]
    return run, metadata


def benchmark(video: Path, repeats: int, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "video": {"name": video.name},
        "environment": {"python": platform.python_version(), "opencv": None,
                        "platform": platform.platform(), "cpu_count": os.cpu_count()},
        "methodology": {
            "repeats": repeats, "timeout_seconds_per_run": timeout,
            "sample_counts_per_run": {"jump": 12, "drag": 40, "backward": 20},
            "notes": [
                "각 모드·반복은 별도 프로세스에서 실행하며 원본 영상은 읽기만 합니다.",
                "기존 방식은 cv2.VideoCapture(path), 현재 방식은 FFmpeg 스레드 1개입니다.",
                "반복마다 실행 순서를 교대합니다. 최초 모드의 파일 캐시 영향은 남을 수 있습니다.",
                "시작 프레임 1개를 예열한 뒤 set(POS_FRAMES)+read 시간을 측정합니다.",
                "프레임 번호는 0부터 시작하며 모든 모드가 동일한 목표를 사용합니다.",
                "드래그는 40개 목표의 직렬 탐색 비용으로, 실제 GUI·요청 병합·입력 지연을 측정하지 않습니다.",
                "프레임 변환·렌더링·오디오·조사 저장은 제외되며 안정성을 보증하는 시험이 아닙니다.",
                "부분 측정은 보존하되 오류가 있는 모드는 정상 비교 결과로 취급하지 마세요.",
            ],
        },
        "modes": [{"id": key, "label": MODE_LABELS[key],
                   "threads_requested": "auto" if key == "original" else 1,
                   "status": "ok", "runs": [], "summaries": {}, "errors": []}
                  for key in MODE_LABELS],
    }
    modes = {mode["id"]: mode for mode in result["modes"]}
    for repeat in range(repeats):
        order = ("original", "current") if repeat % 2 == 0 else ("current", "original")
        for key in order:
            print(f"[{repeat + 1}/{repeats}] {MODE_LABELS[key]}", file=sys.stderr, flush=True)
            run, metadata = run_isolated(video, key, timeout)
            run["repeat"] = repeat + 1
            modes[key]["runs"].append(run)
            modes[key]["errors"].extend(run["errors"])
            if run["errors"]:
                modes[key]["status"] = "error"
            if metadata:
                result["video"] = metadata["video"]
                result["environment"]["opencv"] = metadata["opencv"]
    for mode in result["modes"]:
        mode["summaries"] = {
            phase: summarize([row for run in mode["runs"] for row in run["phases"][phase]])
            for phase in PHASES
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="결과 JSON 경로 (생략하면 표준 출력)")
    parser.add_argument("--repeats", type=int, default=2, help="모드별 반복 수, 1~10 (기본 2)")
    parser.add_argument("--timeout", type=float, default=90,
                        help="한 디코더 실행 제한 시간, 1~600초 (기본 90)")
    parser.add_argument("--worker", choices=tuple(MODE_LABELS), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return _worker(str(args.video), args.worker)
    if not args.video.is_file():
        parser.error("영상 파일이 존재하지 않습니다.")
    if not 1 <= args.repeats <= 10 or not 1 <= args.timeout <= 600:
        parser.error("repeats는 1~10, timeout은 1~600 범위여야 합니다.")
    if args.output:
        if args.output.suffix.lower() != ".json":
            parser.error("결과 파일 확장자는 .json이어야 합니다.")
        if args.output.resolve() == args.video.resolve():
            parser.error("원본 영상에 결과를 저장할 수 없습니다.")
    result = benchmark(args.video.resolve(), args.repeats, args.timeout)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(f"측정 결과: {args.output.resolve()}", file=sys.stderr)
    else:
        print(encoded)
    return 0 if all(mode["status"] == "ok" for mode in result["modes"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
