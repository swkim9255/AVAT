"""소스와 EXE의 설정 경로 및 영상·검출 worker 실행 명령을 정한다."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

WORKER_FLAG = "--avat-worker"
DECODER_WORKER = "investigator.video_decode_worker"
DETECTION_WORKER = "investigator.detection_worker"
WORKER_MODULES = frozenset({DECODER_WORKER, DETECTION_WORKER})


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def application_root() -> Path:
    """EXE 배포의 작업 폴더는 systems 바깥이며 소스 실행은 저장소 루트이다."""
    if is_frozen():
        executable_directory = Path(sys.executable).resolve().parent
        return (executable_directory.parent if executable_directory.name.casefold() == "systems"
                else executable_directory)
    return Path(__file__).resolve().parent.parent


def runtime_data_root() -> Path:
    """EXE·라이브러리·설정은 같은 systems 폴더에 두고 구형 EXE 배치도 읽는다."""
    root = application_root()
    if not is_frozen():
        return root
    executable_directory = Path(sys.executable).resolve().parent
    return (executable_directory if executable_directory.name.casefold() == "systems"
            else root / "systems")


DINO_MODEL_FOLDER = "grounding-dino-base"


def default_dino_model_path() -> Path:
    """배포에 포함된 Grounding DINO Base 모델 폴더(EXE는 systems/models, 소스는 runtime/models)."""
    return runtime_data_root() / ("models" if is_frozen() else "runtime/models") / DINO_MODEL_FOLDER


def configure_runtime_environment() -> None:
    """Keep model/library caches out of the portable distribution's top level."""
    if not is_frozen():
        return
    root = runtime_data_root()
    for key, suffix in {
        "HF_HOME": "cache/huggingface", "TORCH_HOME": "cache/torch",
        "XDG_CACHE_HOME": "cache", "YOLO_CONFIG_DIR": "settings/ultralytics",
    }.items():
        os.environ[key] = str(root / suffix)


def _is_current_executable(executable: str) -> bool:
    try:
        return os.path.samefile(executable, sys.executable)
    except OSError:
        return os.path.normcase(os.path.abspath(executable)) == os.path.normcase(
            os.path.abspath(sys.executable)
        )


def worker_command(executable: str, arguments: Sequence[str]) -> tuple[str, list[str]]:
    """EXE가 자기 자신을 실행할 때만 Python의 -m 인자를 worker 분기로 바꾼다."""
    args = list(arguments)
    if not is_frozen() or not _is_current_executable(executable):
        return executable, args
    while args and args[0] == "-u":
        args.pop(0)
    if len(args) < 2 or args[0] != "-m" or args[1] not in WORKER_MODULES:
        raise ValueError(f"EXE에서 실행할 수 없는 worker 명령입니다: {' '.join(args[:2])}")
    return executable, [WORKER_FLAG, *args[1:]]


def _use_utf8_stdio() -> None:
    """한국어 Windows에서도 부모가 worker의 JSON 이벤트를 UTF-8로 읽도록 한다."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def run_worker_from_argv(argv: Sequence[str]) -> int | None:
    """worker 요청은 해당 종료 코드를 반환하고 일반 GUI 요청은 None을 반환한다."""
    if len(argv) < 2 or argv[1] != WORKER_FLAG:
        return None
    module = argv[2] if len(argv) > 2 else ""
    if module not in WORKER_MODULES:
        print(f"알 수 없는 AVAT worker입니다: {module!r}", file=sys.stderr)
        return 3
    sys.argv = [argv[0], *argv[3:]]
    _use_utf8_stdio()
    if module == DECODER_WORKER:
        from investigator.video_decode_worker import main

        return main()
    from investigator.detection_worker.__main__ import _lower_process_priority, main

    _lower_process_priority()
    return main()
