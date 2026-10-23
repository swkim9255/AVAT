"""빌드한 AVAT.exe를 인터넷이 차단된 상태로 실제 실행해 확인한다(Windows 빌드 서버용).

    python packaging/smoke_test_exe.py --exe package/AVAT/AVAT.exe --detect
        [--model <grounding-dino-base 폴더>] [--image sample.jpg] [--require-detections]

1. 영상 디코더 worker: AVAT.exe --avat-worker로 합성 영상을 열고 탐색한다.
2. 객체 검출 worker: EXE 옆 detection-runtime.json(또는 --model)을 AVAT와 같은 방식으로 읽고,
   AVAT 화면이 만드는 것과 같은 인자로 Grounding DINO를 CPU에서 실행한다.
   HF_HUB_OFFLINE과 연결할 수 없는 프록시를 지정해 네트워크 접근 시 실패하게 한다.
3. GUI: AVAT.exe를 실행해 "AVAT" 창이 뜨고 진단 로그가 남는지 확인한다.

이 스크립트는 빌드 서버의 Python(PySide6·OpenCV 설치)에서 실행하며 배포본에는 넣지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from investigator.runtime_paths import (  # noqa: E402
    DECODER_WORKER,
    DETECTION_WORKER,
    WORKER_FLAG,
)

OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    # 네트워크를 시도하면 바로 실패하도록 닫힌 포트를 프록시로 지정한다.
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "",
}


def make_video(path: Path, image: Path | None, frames: int = 8) -> None:
    if image is not None and image.is_file():
        frame = cv2.imread(str(image))
    else:
        frame = np.full((360, 640, 3), 90, np.uint8)
        cv2.rectangle(frame, (120, 160), (420, 300), (40, 40, 200), -1)
    height, width = frame.shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (width, height))
    for index in range(frames):
        shifted = np.roll(frame, index * 4, axis=1)
        writer.write(shifted)
    writer.release()
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit("합성 영상을 만들지 못했습니다.")


def check_decoder(exe: Path, video: Path) -> None:
    process = subprocess.Popen(
        [str(exe), WORKER_FLAG, DECODER_WORKER, "--path", str(video), "--threads", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **OFFLINE_ENV},
    )
    try:
        opened = json.loads(process.stdout.readline())
        assert opened.get("event") == "opened", opened
        assert opened["total_frames"] >= 2, opened
        process.stdin.write(b'{"command":"seek","request_id":1,"frame":3}\n')
        process.stdin.flush()
        frame = json.loads(process.stdout.readline())
        assert frame.get("event") == "frame" and frame.get("request_id") == 1, frame
        process.stdin.write(b'{"command":"close"}\n')
        process.stdin.close()
        code = process.wait(timeout=30)
        assert code == 0, f"디코더 종료 코드 {code}: {process.stderr.read().decode(errors='replace')}"
    finally:
        if process.poll() is None:
            process.kill()
    print(f"[통과] 디코더 worker: {opened['width']}x{opened['height']}, {opened['total_frames']}프레임")


def detection_settings(exe: Path, model: Path | None):
    """--model이 없으면 AVAT.exe가 쓰는 프로그램 폴더의 detection-runtime.json을 그대로 읽는다."""
    from investigator.detection_process import DetectionSettings, _load_runtime_manifest

    if model is None:
        manifest = exe.parent / "detection-runtime.json"
        settings, error = _load_runtime_manifest(manifest)
        if settings is None:
            raise SystemExit(error or f"설정 파일이 없습니다: {manifest}")
        assert settings.python == "", "배포본 설정은 내장 환경(python 생략)을 써야 합니다."
        assert settings.backend == "grounding_dino" and settings.offline is True
    else:
        settings = DetectionSettings(model=str(model), offline=True)
    # 빌드 서버에는 GPU가 없으므로 CPU·FP32로 확인한다.
    settings.device = "cpu"
    settings.dino_fp16 = False
    print(f"  검출 설정: backend={settings.backend} model={settings.model} offline={settings.offline}")
    return settings


def detection_arguments(video: Path, settings, work: Path) -> list[str]:
    """AVAT의 DetectionJob이 만드는 python -m 인자를 그대로 쓴다."""
    from investigator.detection_process import DetectionJob, DetectionRequest
    from investigator.object_detection import DETECTION_CLASSES

    request = DetectionRequest(video, 0, 3, 1, [c.key for c in DETECTION_CLASSES], "common")
    job = DetectionJob(request, settings)
    arguments = job.arguments()
    assert arguments[:2] == ["-m", DETECTION_WORKER], arguments[:2]
    # DetectionJob의 임시 폴더 대신 검사용 폴더에 결과를 쓴다.
    output = arguments.index("--output") + 1
    arguments[output] = str(work / "result.json")
    return arguments[1:]


def check_detection(exe: Path, video: Path, model: Path | None, work: Path, require: bool) -> None:
    arguments = detection_arguments(video, detection_settings(exe, model), work)
    started = time.monotonic()
    completed = subprocess.run(
        [str(exe), WORKER_FLAG, *arguments],
        capture_output=True, timeout=1800, env={**os.environ, **OFFLINE_ENV, "HF_HOME": str(work / "hf")},
    )
    stdout = completed.stdout.decode("utf-8")  # worker 이벤트가 UTF-8인지도 함께 확인
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    for event in events:
        if event.get("type") in {"error", "completed", "loading_model"}:
            print("  ", json.dumps(event, ensure_ascii=False)[:600])
    if completed.returncode != 0:
        print(completed.stderr.decode("utf-8", errors="replace")[-4000:])
        raise SystemExit(f"검출 worker 종료 코드 {completed.returncode}")
    assert events and events[-1].get("type") == "completed", events[-1:]
    document = json.loads((work / "result.json").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for detection in document["detections"]:
        counts[detection["class_key"]] = counts.get(detection["class_key"], 0) + 1
    print(f"[통과] 검출 worker(오프라인 CPU): {time.monotonic() - started:.0f}초, "
          f"검출 {len(document['detections'])}개 {counts}")
    if require and not document["detections"]:
        raise SystemExit("샘플 이미지에서 검출 결과가 없습니다.")


def _window_titles(pid: int) -> list[str]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buffer, 512)
            if buffer.value:
                titles.append(buffer.value)
        return True

    user32.EnumWindows(callback, 0)
    return titles


def check_gui(exe: Path, work: Path) -> None:
    local = work / "localappdata"
    env = {**os.environ, **OFFLINE_ENV, "LOCALAPPDATA": str(local)}
    process = subprocess.Popen([str(exe), "--workspace", str(work / "workspace")], env=env)
    try:
        deadline = time.monotonic() + 90
        titles: list[str] = []
        while time.monotonic() < deadline and process.poll() is None:
            titles = _window_titles(process.pid)
            if "AVAT" in titles:
                break
            time.sleep(1)
        assert process.poll() is None, f"GUI가 종료됐습니다(코드 {process.returncode})."
        assert "AVAT" in titles, f"AVAT 창을 찾지 못했습니다. 보이는 창: {titles}"
        time.sleep(5)
        assert process.poll() is None, f"GUI가 시작 직후 종료됐습니다(코드 {process.returncode})."
    finally:
        process.kill()
        process.wait(timeout=30)
    logs = list((local / "AVAT" / "logs").glob("*.log*")) if (local / "AVAT").is_dir() else []
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in logs)
    print(f"[통과] GUI 창 표시, 진단 로그 {len(logs)}개")
    if "Traceback" in text:
        print(text[-4000:])
        raise SystemExit("GUI 진단 로그에 Traceback이 있습니다.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--detect", action="store_true", help="Grounding DINO 실제 추론 점검")
    parser.add_argument("--model", type=Path, help="모델 폴더(생략 시 EXE 옆 detection-runtime.json 사용)")
    parser.add_argument("--image", type=Path, help="합성 영상에 쓸 사진(차량·보행자 등)")
    parser.add_argument("--require-detections", action="store_true")
    parser.add_argument("--skip-gui", action="store_true")
    args = parser.parse_args()
    exe = args.exe.resolve()
    if not exe.is_file():
        raise SystemExit(f"EXE가 없습니다: {exe}")
    with tempfile.TemporaryDirectory(prefix="avat-smoke-") as temp:
        work = Path(temp)
        video = work / "sample.mp4"
        make_video(video, args.image)
        check_decoder(exe, video)
        if args.detect:
            model = args.model.resolve() if args.model is not None else None
            check_detection(exe, video, model, work, args.require_detections)
        if not args.skip_gui:
            check_gui(exe, work)
    print("모든 EXE 점검을 통과했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
