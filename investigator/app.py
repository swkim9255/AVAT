"""Application entry point."""

from __future__ import annotations

import argparse
import sys
from ctypes import WinDLL
from pathlib import Path

from investigator.diagnostics import configure_diagnostics

# Install native/Python crash diagnostics before loading Qt or other native DLLs.
configure_diagnostics()

_icuuc = WinDLL(r"C:/Windows/System32/icuuc.dll")
_icuin = WinDLL(r"C:/Windows/System32/icuin.dll")

from PySide6.QtWidgets import QApplication  # noqa: E402 -- native preload must run first

from investigator.main_window import MainWindow  # noqa: E402
from investigator.theme import apply_theme  # noqa: E402


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="사고 영상 조사 도구")
    parser.add_argument("--workspace", type=Path, help="조사 데이터와 CSV를 저장할 작업 폴더")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    qt_arguments = [sys.argv[0]] if arguments is not None else sys.argv
    app = QApplication(qt_arguments)
    app.setApplicationName("KATRI Video Investigator")
    app.setApplicationDisplayName("AVAT")
    app.setOrganizationName("KATRI")
    app.setStyle("Fusion")
    apply_theme(app)
    window = MainWindow(args.workspace)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
