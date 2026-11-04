"""소스 실행과 PyInstaller EXE 실행의 공통 진입점."""

import multiprocessing
import sys

from investigator.runtime_paths import configure_runtime_environment, run_worker_from_argv

if __name__ == "__main__":
    multiprocessing.freeze_support()
    configure_runtime_environment()
    # worker는 Qt 창과 진단 설정을 초기화하기 전에 별도 진입점으로 분기한다.
    worker_exit = run_worker_from_argv(sys.argv)
    if worker_exit is not None:
        raise SystemExit(worker_exit)

    from investigator.app import main

    raise SystemExit(main())
