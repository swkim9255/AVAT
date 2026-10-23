"""소스 실행(python main.py)과 PyInstaller EXE 실행의 경로·worker 명령을 한곳에서 정한다.

EXE에서는 ``sys.executable``이 Python이 아니라 ``AVAT.exe``이므로 ``-m <모듈>``을
쓸 수 없다. 같은 EXE를 ``--avat-worker <모듈>`` 인자로 다시 실행하고, 진입점이
Qt를 불러오기 전에 해당 worker로 분기한다. 이 모듈은 PySide6·OpenCV를 불러오지 않는다.
"""

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
    """detection-runtime.json과 runtime/models 상대 경로의 기준 폴더.

    EXE는 내부 라이브러리 폴더(_internal)가 아니라 AVAT.exe가 있는 폴더를 쓴다.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_current_executable(executable: str) -> bool:
    try:
        return os.path.samefile(executable, sys.executable)
    except OSError:
        return os.path.normcase(os.path.abspath(executable)) == os.path.normcase(
            os.path.abspath(sys.executable)
        )


def worker_command(executable: str, arguments: Sequence[str]) -> tuple[str, list[str]]:
    """Python용 ``[-u] -m <모듈> ...`` 인자를 실제 실행 명령으로 바꾼다.

    소스 실행이거나 별도 Python을 지정했다면 그대로 둔다. EXE가 자기 자신을
    실행할 때만 ``--avat-worker <모듈> ...``로 바꾼다(표준 입출력 버퍼링은
    EXE 빌드 설정의 unbuffered 옵션과 worker의 flush가 맡는다).
    """
    args = list(arguments)
    if not is_frozen() or not _is_current_executable(executable):
        return executable, args
    while args and args[0] == "-u":
        args.pop(0)
    if len(args) < 2 or args[0] != "-m" or args[1] not in WORKER_MODULES:
        raise ValueError(f"EXE에서 실행할 수 없는 worker 명령입니다: {' '.join(args[:2])}")
    return executable, [WORKER_FLAG, *args[1:]]


def _use_utf8_stdio() -> None:
    """부모(GUI)는 worker 출력을 UTF-8로 읽는다. EXE는 PYTHONIOENCODING을 무시할 수 있어
    한국어 Windows의 기본 cp949 대신 UTF-8로 직접 맞춘다."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def run_worker_from_argv(argv: Sequence[str]) -> int | None:
    """``--avat-worker <모듈>``이면 해당 worker를 실행해 종료 코드를, 아니면 None을 돌려준다."""
    if len(argv) < 2 or argv[1] != WORKER_FLAG:
        return None
    module = argv[2] if len(argv) > 2 else ""
    if module not in WORKER_MODULES:
        print(f"알 수 없는 AVAT worker입니다: {module!r}", file=sys.stderr)
        return 3
    # worker의 argparse는 sys.argv[1:]을 읽으므로 분기 인자를 제거한다.
    sys.argv = [argv[0], *argv[3:]]
    _use_utf8_stdio()
    if module == DECODER_WORKER:
        from investigator.video_decode_worker import main

        return main()
    from investigator.detection_worker.__main__ import _lower_process_priority, main

    _lower_process_priority()
    return main()
