r"""Run a real detector through AVAT's Qt process boundary without changing a project.

Run with AVAT's regular Python, not the detection Python:
  .venv\Scripts\python.exe local_dev/tools/smoke_native_detection_gui.py \
      --video path/to/clip.mp4 --classes vehicle --start-frame 0

Frames are zero-based and both bounds are inclusive. The workspace's
detection-runtime.json is used with empty, temporary user settings. No fake
backend, video rewrite, project save, or persistent QSettings write is involved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WATCHDOG_MS = 120_000


def _emit(kind: str, **values: Any) -> None:
    print(json.dumps({"type": kind, **values}, ensure_ascii=False), flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--classes", default="vehicle", help="Comma-separated detector class keys")
    parser.add_argument("--start-frame", type=int, default=0, help="Zero-based, inclusive")
    parser.add_argument("--end-frame", type=int, default=None, help="Inclusive; defaults to start")
    parser.add_argument("--frame-step", type=int, default=1)
    parser.add_argument("--output", type=Path, help="Optional new JSON audit artifact (no overwrite)")
    parser.add_argument("--runtime-manifest", type=Path, default=ROOT / "detection-runtime.json")
    args = parser.parse_args(argv)
    args.video = args.video.expanduser().resolve()
    if not args.video.is_file():
        parser.error(f"Video not found: {args.video}")
    args.end_frame = args.start_frame if args.end_frame is None else args.end_frame
    if args.start_frame < 0 or args.end_frame < args.start_frame:
        parser.error("Frame bounds must be nonnegative and end >= start")
    if args.frame_step < 1:
        parser.error("--frame-step must be >= 1")
    args.classes = list(dict.fromkeys(key.strip() for key in args.classes.split(",") if key.strip()))
    if not args.classes:
        parser.error("Select at least one class")
    if args.output:
        args.output = args.output.expanduser().resolve()
        if args.output.exists():
            parser.error(f"Refusing to overwrite existing output: {args.output}")
    if not args.runtime_manifest.is_file():
        parser.error(f"Workspace runtime manifest not found: {args.runtime_manifest}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication

    from investigator.detection_process import (
        DetectionJob,
        DetectionRequest,
        DetectionSettings,
        _load_runtime_manifest,
    )
    from investigator.form_definition import FormDefinition
    from investigator.object_detection import (
        CLASS_BY_KEY,
        ROLE_TRIGGER,
        VideoObjects,
        merge_detection_result,
    )
    from investigator.qt_font_setup import configure_text_rendering

    unknown = set(args.classes) - CLASS_BY_KEY.keys()
    if unknown:
        _emit("failed", message=f"Unknown AVAT classes: {', '.join(sorted(unknown))}")
        return 2
    configure_text_rendering()
    app = QApplication.instance() or QApplication(["avat-native-detection-smoke"])
    app.setQuitOnLastWindowClosed(False)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="avat-native-smoke-") as directory:
        # Explicitly isolated settings make the manifest authoritative without
        # clearing, reading, or changing the real GUI user's chosen interpreter.
        store = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
        if args.runtime_manifest.resolve() == (ROOT / "detection-runtime.json").resolve():
            settings = DetectionSettings.load(store)
        else:
            settings, error = _load_runtime_manifest(args.runtime_manifest.resolve())
            if settings is None:
                _emit("failed", message=error)
                return 2
        if settings.config_error:
            _emit("failed", message=settings.config_error)
            return 2
        _emit(
            "runtime",
            python=settings.executable,
            model=settings.model,
            device=settings.device,
            offline=settings.offline,
            backend=settings.backend,
        )
        request = DetectionRequest(
            video_path=args.video,
            start_frame=args.start_frame,
            end_frame=args.end_frame,
            frame_step=args.frame_step,
            classes=args.classes,
            role=ROLE_TRIGGER,
        )
        job = DetectionJob(request, settings)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        state = {"exit_code": 1, "done": False}

        def finish(code: int) -> None:
            if state["done"]:
                return
            state["done"] = True
            state["exit_code"] = code
            watchdog.stop()
            app.exit(code)

        def failed(message: str) -> None:
            _emit("failed", message=message, elapsed_seconds=round(time.perf_counter() - started, 3))
            finish(1)

        def completed(document: dict[str, Any]) -> None:
            try:
                initial = VideoObjects()
                before = initial.to_dict()
                merged, added, replaced = merge_detection_result(
                    initial,
                    document,
                    role=request.role,
                    definition=FormDefinition.default(),
                    start_frame=request.start_frame,
                    end_frame=request.end_frame,
                    frame_step=request.frame_step,
                    classes=request.classes,
                )
                domain = merged.to_dict()
                if VideoObjects.from_dict(domain).to_dict() != domain:
                    raise ValueError("Domain serialization round-trip changed the result")
                if initial.to_dict() != before or replaced:
                    raise ValueError("Domain merge unexpectedly changed initial data")
                if len(merged.runs) != 1:
                    raise ValueError("Expected exactly one domain detection run")
                summary = {
                    "device": document.get("provenance", {}).get("device"),
                    "processed_frames": document.get("sampling", {}).get("processed_frames"),
                    "detections": len(document["detections"]),
                    "objects": len(merged.objects),
                    "added": added,
                    "domain_roundtrip": "passed",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
                if args.output:
                    artifact = {"summary": summary, "worker_result": document, "domain": domain}
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with args.output.open("x", encoding="utf-8") as handle:
                        json.dump(artifact, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    summary["artifact"] = str(args.output)
                _emit("completed", **summary)
                finish(0)
            except (OSError, TypeError, ValueError, KeyError) as exc:
                failed(f"Result/domain validation failed: {exc}")

        def cancelled() -> None:
            _emit("cancelled")
            finish(2)

        def timed_out() -> None:
            _emit("failed", message="Real detection exceeded the 120-second watchdog")
            finish(124)
            job.kill()

        job.status.connect(lambda message: _emit("status", message=message))
        job.started.connect(lambda count: _emit("started", frames=count))
        job.progress.connect(
            lambda done, total, frame: _emit("progress", processed=done, total=total, frame=frame)
        )
        job.finished.connect(completed)
        job.failed.connect(failed)
        job.cancelled.connect(cancelled)
        watchdog.timeout.connect(timed_out)
        watchdog.start(WATCHDOG_MS)
        # Defer startup until the event loop is active, including synchronous
        # configuration/argument failures emitted before QProcess starts.
        QTimer.singleShot(0, job.start)
        try:
            app.exec()
        finally:
            watchdog.stop()
            job.kill()
        return state["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
