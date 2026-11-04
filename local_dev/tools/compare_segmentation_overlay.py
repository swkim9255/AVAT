"""Render real SAM3 / EfficientSAM3 / YOLOE segmentation overlays.

No boxes or filled polygon approximations are used. Each backend is isolated,
and neither project data nor original videos are changed. Run with a local
detection Python containing the selected backends (default: SAM3 and YOLOE).
Box-only backends such as Grounding DINO are skipped with explicit metadata.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from local_dev.tools.benchmark_detection import (  # noqa: E402
    BACKEND_LABELS as DETECTION_BACKEND_LABELS,
)
from local_dev.tools.benchmark_detection import selected_modes  # noqa: E402

BACKEND_LABELS = {**DETECTION_BACKEND_LABELS, "grounding_dino": "Grounding DINO"}


def class_palette(keys: list[str]) -> dict[str, tuple[int, int, int]]:
    """Stable RGB colors shared by both models, even when a class is absent."""
    return {key: tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(
        (0.57 + index * 0.38196601125) % 1, 0.72, 0.96,
    )) for index, key in enumerate(keys)}


def segmentation_overlay(frame: np.ndarray, detections, palette: dict,
                         alpha: float = 0.4, *, contours: bool = True) -> np.ndarray:
    """Blend true pixel masks once; small foreground regions win overlaps.

    The source frame and masks are never modified. Non-detected pixels keep
    their original color (apart from an optional thin segmentation outline).
    """
    if (not math.isfinite(alpha) or not 0 <= alpha <= 1 or frame.ndim != 3
            or frame.shape[2] != 3 or frame.dtype != np.uint8):
        raise ValueError("Expected BGR uint8 frame and finite opacity between 0 and 1")
    detections = list(detections)
    for det in detections:
        if not isinstance(det.mask, np.ndarray) or det.mask.dtype != np.bool_ or det.mask.shape != frame.shape[:2]:
            raise ValueError("Segmentation mask must be boolean and match the source frame")
    ordered = sorted(detections, key=lambda item: -int(item.mask.sum()))
    colors = np.zeros_like(frame)
    covered = np.zeros(frame.shape[:2], dtype=bool)
    for det in ordered:
        colors[det.mask] = palette[det.class_key][::-1]  # RGB legend -> OpenCV BGR
        covered |= det.mask
    out = frame.copy()
    out[covered] = np.rint((1 - alpha) * frame[covered] + alpha * colors[covered]).astype(np.uint8)
    if contours and alpha > 0:
        for det in ordered:
            edges, _ = cv2.findContours(det.mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(out, edges, -1, palette[det.class_key][::-1], 2, cv2.LINE_AA)
    return out


def write_png(path: Path, image: np.ndarray) -> None:
    encoded, buffer = cv2.imencode(".png", image)
    if not encoded:
        raise OSError(f"Could not encode overlay: {path}")
    with path.open("xb") as stream:
        stream.write(buffer.tobytes())


def render_backend(args) -> None:
    from investigator.detection_worker.backend import create_backend
    from investigator.detection_worker.classes import load_config
    from investigator.detection_worker.postprocess import build_detections

    def skip_box_only_backend() -> None:
        output = args.output / args.worker
        output.mkdir()
        report = {
            "backend": args.worker, "model": args.model, "alpha": args.alpha,
            "frame_indices_zero_based": [int(value) for value in args.frames.split(",")],
            "supports_segmentation": False, "status": "skipped",
            "skip_reason": "box_only_backend", "views": {},
        }
        with (output / "metadata.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(f"{args.worker}: segmentation overlay skipped (box-only backend)", flush=True)

    # A box-only result must never be displayed as a segmentation overlay.
    # Skip the known model without loading its weights or reading the video.
    if args.worker == "grounding_dino":
        skip_box_only_backend()
        return
    config = load_config(getattr(args, "config", None))
    classes = config.enabled()
    colors = class_palette([item.key for item in classes])
    prompts = sorted({prompt for item in classes for prompt in item.prompts})
    backend = create_backend(args.model, "cuda", backend=args.worker)
    if not getattr(backend, "supports_segmentation", True):
        skip_box_only_backend()
        return
    prepare = getattr(backend, "prepare", None)
    if callable(prepare):
        prepare(prompts)
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise RuntimeError("Could not open video")
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = [int(value) for value in args.frames.split(",")]
    report = {"backend": args.worker, "model": backend.model_id, "device": backend.device,
              "status": "completed", "supports_segmentation": True,
              "alpha": args.alpha, "frame_indices_zero_based": frames,
              "video_fps": cap.get(cv2.CAP_PROP_FPS), "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
              "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
              "mask_threshold": 0.5, "class_thresholds": {item.key: item.threshold for item in classes},
              "taxonomy": config.notes,
              "palette_rgb": colors, "labels": {item.key: item.label for item in classes}, "views": {}}
    output = args.output / args.worker
    output.mkdir()
    try:
        for frame_index in frames:
            if not 0 <= frame_index < count or not cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
                raise ValueError(f"Invalid frame index: {frame_index}")
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"Could not read frame {frame_index}")
            raw = backend.predict(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), prompts,
                                  min(item.threshold for item in classes), 0.5)
            for mode, selected in selected_modes(config, getattr(args, "modes", "all,vehicle")):
                detections = build_detections(raw, selected, config, frame.shape[:2])
                image = segmentation_overlay(frame, detections, colors, args.alpha)
                filename = f"{mode}-frame-{frame_index}.png"
                write_png(output / filename, image)
                rows = report["views"].setdefault(mode, [])
                rows.append({"frame_index": frame_index, "display_frame": frame_index + 1,
                             "image": filename, "instances": len(detections),
                             "classes": sorted({item.class_key for item in detections})})
                print(f"{args.worker} {mode} index={frame_index} regions={len(detections)}", flush=True)
    finally:
        cap.release()
    with (output / "metadata.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)


def comparison_panels(folder: Path, alpha: float, backends: list[str] | None = None) -> list[Path]:
    backends = backends or [name for name in BACKEND_LABELS if (folder / name / "metadata.json").is_file()]
    reports = [json.loads((folder / backend / "metadata.json").read_text(encoding="utf-8"))
               for backend in backends]
    if not reports:
        raise ValueError("No backend overlay metadata found")
    reports = [report for report in reports
               if report.get("status") != "skipped" and report.get("supports_segmentation", True)]
    if not reports:
        return []
    from PIL import Image, ImageDraw, ImageFont

    if any(report["alpha"] != alpha for report in reports):
        raise ValueError("Cannot compare overlays with different opacity")
    for key in ("frame_indices_zero_based", "class_thresholds", "mask_threshold", "palette_rgb", "width", "height"):
        if any(reports[0][key] != report[key] for report in reports[1:]):
            raise ValueError(f"Model comparison settings differ: {key}")
    font_path = Path("C:/Windows/Fonts/malgun.ttf")
    title_font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default(size=22)
    small_font = ImageFont.truetype(str(font_path), 16) if font_path.exists() else ImageFont.load_default(size=16)
    outputs = []
    modes = list(reports[0]["views"])
    if any(list(report["views"]) != modes for report in reports):
        raise ValueError("Cannot compare different overlay modes")
    for mode in modes:
        for rows in zip(*(report["views"][mode] for report in reports), strict=True):
            left = rows[0]
            if any(left["frame_index"] != row["frame_index"] for row in rows[1:]):
                raise ValueError("Cannot compare different video frames")
            panel_width = 960
            panel_height = round(panel_width * reports[0]["height"] / reports[0]["width"])
            keys = sorted({key for row in rows for key in row["classes"]})
            total_width = panel_width * len(reports)
            legend_columns = max(1, total_width // 380)
            legend_rows = math.ceil(len(keys) / legend_columns)
            footer_height = 40 + legend_rows * 28
            image = Image.new("RGB", (total_width, panel_height + 94 + footer_height), "#F5F7FA")
            draw = ImageDraw.Draw(image)
            heading = f"전체 {len(reports[0]['class_thresholds'])}종" if mode == "all" else {
                "vehicle": "차량", "passenger": "승용차만",
            }.get(mode, reports[0]["labels"].get(mode, mode))
            frame = left["frame_index"]
            timestamp = frame / reports[0]["video_fps"]
            draw.text((16, 10), f"Segmentation overlay · {heading} · Frame {frame+1} (index {frame}) · {timestamp:.2f}s",
                      font=title_font, fill="#1B1F24")
            for column, (report, row) in enumerate(zip(reports, rows, strict=True)):
                name = BACKEND_LABELS[report["backend"]]
                x = column * panel_width
                draw.text((x + 16, 49), f"{name} · {row['instances']}개 영역", font=title_font, fill="#1B1F24")
                with Image.open(folder / report["backend"] / row["image"]) as overlay:
                    image.paste(overlay.resize((panel_width, panel_height), Image.Resampling.LANCZOS), (x, 94))
            y = panel_height + 106
            draw.text((16, y), f"불투명도 {alpha:.0%} · 같은 색 = 같은 클래스 · 실제 픽셀 마스크 사용 · 겹친 영역은 작은 객체 우선",
                      font=small_font, fill="#5B636E")
            for index, key in enumerate(keys):
                x, row_y = 16 + (index % legend_columns) * 380, y + 32 + (index // legend_columns) * 28
                draw.rectangle((x, row_y + 3, x + 16, row_y + 19), fill=tuple(reports[0]["palette_rgb"][key]))
                draw.text((x + 24, row_y), reports[0]["labels"][key], font=small_font, fill="#1B1F24")
            output = folder / f"{mode}-frame-{frame}.png"
            with output.open("xb") as stream:
                image.save(stream, format="PNG")
            outputs.append(output)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--frames", default="100,600,1000", help="Comma-separated zero-based indices")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; never overwrites")
    parser.add_argument("--alpha", type=float, default=0.4)
    parser.add_argument("--sam3-model", default=str(ROOT / "runtime/models/sam3"))
    parser.add_argument("--efficient-model", default=str(ROOT / "runtime/models/efficientsam3-tv-m"))
    parser.add_argument("--yoloe-model", default=str(ROOT / "runtime/models/yoloe-11l/yoloe-11l-seg.pt"))
    parser.add_argument("--grounding-dino-model", default=str(ROOT / "runtime/models/grounding-dino-base"))
    parser.add_argument("--backends", default="sam3,yoloe")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--modes", default="all,vehicle")
    parser.add_argument("--worker", choices=list(BACKEND_LABELS))
    parser.add_argument("--model")
    args = parser.parse_args()
    args.video, args.output = args.video.resolve(), args.output.resolve()
    try:
        frames = [int(value) for value in args.frames.split(",")]
        if not frames or min(frames) < 0 or len(set(frames)) != len(frames):
            raise ValueError
    except ValueError:
        parser.error("frames must be distinct nonnegative integer indices")
    if not args.video.is_file() or not math.isfinite(args.alpha) or not 0 <= args.alpha <= 1:
        parser.error("Specify an existing video and opacity between 0 and 1")
    if args.worker:
        render_backend(args)
        return 0
    backends = args.backends.split(",")
    if not backends or len(set(backends)) != len(backends) or set(backends) - BACKEND_LABELS.keys():
        parser.error("Specify distinct supported --backends")
    args.output.mkdir(parents=True, exist_ok=False)
    models = {"sam3": args.sam3_model, "efficientsam3": args.efficient_model, "yoloe": args.yoloe_model,
              "grounding_dino": args.grounding_dino_model}
    for backend in backends:
        model = models[backend]
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", backend, "--model", model,
                   "--video", str(args.video), "--frames", args.frames, "--output", str(args.output),
                   "--alpha", str(args.alpha), "--modes", args.modes]
        if args.config:
            command.extend(["--config", str(args.config.resolve())])
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONIOENCODING": "utf-8"}
        subprocess.run(command, env=env, cwd=ROOT, check=True)
    for path in comparison_panels(args.output, args.alpha, backends):
        print(path, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
