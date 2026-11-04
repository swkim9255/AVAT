"""Application entry point."""

from __future__ import annotations

import argparse
import os
import sys
from ctypes import WinDLL
from pathlib import Path

from investigator.diagnostics import configure_diagnostics, log_runtime_environment

# Install native/Python crash diagnostics before loading Qt or other native DLLs.
configure_diagnostics()


def _preload_system_icu() -> tuple[WinDLL, ...]:
    """Load Windows' own ICU first, so Qt6Core never binds a same-named copy.

    Qt6Core imports icuuc.dll by name. Resolve System32 from %SystemRoot%
    instead of assuming C:\\Windows; if a DLL is absent, Qt reports it itself.
    """
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    return tuple(
        WinDLL(str(path))
        for path in (system32 / "icuuc.dll", system32 / "icuin.dll")
        if path.is_file()
    )


_system_icu = _preload_system_icu()

from PySide6.QtWidgets import QApplication  # noqa: E402 -- native preload must run first

from investigator.audio_playback import disable_qt_hardware_decoding  # noqa: E402
from investigator.ffmpeg_logging import restore_qt_ffmpeg_log_level  # noqa: E402
from investigator.main_window import MainWindow  # noqa: E402
from investigator.qt_font_setup import configure_text_rendering  # noqa: E402
from investigator.theme import apply_theme  # noqa: E402


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="사고 영상 조사 도구")
    parser.add_argument("--workspace", type=Path, help="조사 데이터와 CSV를 저장할 작업 폴더")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    qt_arguments = [sys.argv[0]] if arguments is not None else sys.argv
    configure_text_rendering(qt_arguments)
    disable_qt_hardware_decoding()
    app = QApplication(qt_arguments)
    log_runtime_environment()
    app.setApplicationName("AVAT")
    app.setApplicationDisplayName("AVAT")
    app.setOrganizationName("AVAT")
    app.setStyle("Fusion")
    apply_theme(app)
    window = MainWindow(args.workspace)
    # Also release native decoders for app.quit()/event-loop exits that bypass
    # the normal window closeEvent. close_video() is intentionally idempotent.
    app.aboutToQuit.connect(window.video_controller.close_video)
    app.aboutToQuit.connect(restore_qt_ffmpeg_log_level)
    window.show()
    try:
        return app.exec()
    finally:
        shutdown_detection = getattr(window, "_shutdown_detection", None)
        if shutdown_detection is not None:
            shutdown_detection()
        window.video_controller.close_video()
        restore_qt_ffmpeg_log_level()


if __name__ == "__main__":
    raise SystemExit(main())
