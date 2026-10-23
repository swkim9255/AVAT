"""명령행 진입점: python -m investigator.detection_worker --video ... --output ...

종료 코드: 0 성공, 1 오류, 2 취소, 3 인자 오류.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from contextlib import suppress
from pathlib import Path

from .classes import load_config
from .events import CancelToken, EventWriter
from .runner import Cancelled, RunOptions, run, write_atomic

EXIT_OK, EXIT_ERROR, EXIT_CANCELLED, EXIT_USAGE = 0, 1, 2, 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="investigator.detection_worker", description="AVAT 텍스트 프롬프트 객체 검출 worker"
    )
    p.add_argument("--video", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path, help="결과 JSON 경로(성공 시에만 생성)")
    p.add_argument("--config", type=Path, default=None, help="클래스·프롬프트 JSON(기본 내장 설정)")
    p.add_argument(
        "--classes", default=None, help="쉼표로 구분한 클래스 key. 생략하면 enabled 클래스"
    )
    p.add_argument("--sample-fps", type=float, default=2.0)
    p.add_argument(
        "--frame-step", type=int, default=None, help="N프레임마다 처리(--sample-fps 대신)"
    )
    p.add_argument("--start-frame", type=int, default=0)
    p.add_argument("--end-frame", type=int, default=None, help="이 프레임은 포함하지 않음")
    p.add_argument("--mask-threshold", type=float, default=0.5)
    p.add_argument("--model", default=None, help="모델 ID 또는 로컬 모델 경로")
    p.add_argument("--backend", choices=["grounding_dino", "sam3", "efficientsam3", "yoloe"], default="grounding_dino")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    precision = p.add_mutually_exclusive_group()
    precision.add_argument("--half", action="store_true", default=None, help="CUDA 혼합 정밀도 사용 (DINO: FP16)")
    precision.add_argument("--no-half", dest="half", action="store_false", help="FP32 사용")
    p.add_argument("--dino-chunk-size", type=int, default=8)
    p.add_argument("--dino-box-threshold", type=float, default=.3)
    p.add_argument("--dino-text-threshold", type=float, default=.25)
    p.add_argument("--no-polygons", action="store_true")
    p.add_argument("--cancel-file", type=Path, default=None)
    p.add_argument("--stdin-cancel", action="store_true", help="표준 입력의 'cancel' 줄로 취소")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, backend=None) -> int:
    events = EventWriter()
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK

    try:
        config = load_config(args.config)
        keys = [k.strip() for k in args.classes.split(",") if k.strip()] if args.classes else None
        classes = config.enabled(keys)
        if not classes:
            raise ValueError("선택된 클래스가 없습니다.")
        if not args.video.is_file():
            raise ValueError(f"영상 파일이 없습니다: {args.video}")
    except (ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
        events.emit("error", stage="arguments", message=str(exc))
        return EXIT_USAGE

    cancel = CancelToken(args.cancel_file, watch_stdin=args.stdin_cancel)
    opts = RunOptions(
        video_path=args.video,
        output_path=args.output,
        sample_fps=args.sample_fps,
        frame_step=args.frame_step,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
        mask_threshold=args.mask_threshold,
        include_polygons=not args.no_polygons,
    )
    try:
        opts.validate()
    except ValueError as exc:
        events.emit("error", stage="arguments", message=str(exc))
        return EXIT_USAGE
    try:
        if cancel.cancelled:
            raise Cancelled()
        device_note = None
        if backend is None:
            if args.model is None:
                args.model = {"sam3": "facebook/sam3", "grounding_dino": "IDEA-Research/grounding-dino-base"}.get(args.backend, "")
            if not args.model:
                raise ValueError("검출 모델의 로컬 경로를 지정해 주세요.")
            events.emit("loading_model", model_id=args.model)
            from .backend import create_backend

            half = args.half if args.half is not None else args.backend == "grounding_dino"
            backend = create_backend(
                args.model, args.device, half, args.backend,
                chunk_size=args.dino_chunk_size, box_threshold=args.dino_box_threshold,
                text_threshold=args.dino_text_threshold,
            )
            device_note = backend.fallback_reason
        doc = run(opts, backend, config, classes, events, cancel, device_note)
        if cancel.cancelled:
            raise Cancelled()
        write_atomic(doc, args.output)
        events.emit(
            "completed",
            result_path=str(args.output),
            device=backend.device,
            detections=len(doc["detections"]),
            objects=len({d["object_id"] for d in doc["detections"]}),
        )
        return EXIT_OK
    except Cancelled:
        events.emit("cancelled", message="취소되어 결과를 저장하지 않았습니다.")
        return EXIT_CANCELLED
    except Exception as exc:  # noqa: BLE001 - GUI에 오류를 전달하는 최상위 경계
        events.emit("error", stage="run", message=str(exc), traceback=traceback.format_exc(limit=5))
        return EXIT_ERROR


def _lower_process_priority() -> None:
    """검출 프로세스를 '보통 미만' 우선순위로 낮춘다.

    CPU 추론과 디코딩이 모든 코어를 오래 쓰더라도 AVAT 화면·오디오·시스템 서비스가
    먼저 실행되게 한다. 남는 코어가 있으면 검출 속도는 달라지지 않는다.
    """
    if os.name == "nt":
        import ctypes

        with suppress(Exception):
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL


if __name__ == "__main__":
    # 프로세스로 실행될 때만 적용한다. 테스트처럼 main()을 직접 부르는 호출자는 그대로 둔다.
    _lower_process_priority()
    sys.exit(main())
