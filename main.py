"""Convenient source-tree entry point (and the PyInstaller EXE entry script)."""

import sys

from investigator.runtime_paths import run_worker_from_argv

if __name__ == "__main__":
    # EXE가 디코더·검출 worker로 다시 실행된 경우 Qt·진단 설정을 불러오기 전에 분기한다.
    worker_exit = run_worker_from_argv(sys.argv)
    if worker_exit is not None:
        raise SystemExit(worker_exit)

    from investigator.app import main

    raise SystemExit(main())
