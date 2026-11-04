"""Run only Grounding DINO on the exact frames in a saved YOLOE test.

Does not run another model or create a comparison report. All output goes to a
new directory; source videos, investigation data and user settings are read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path

import cv2

from investigator.detection_worker.backend import create_backend
from investigator.detection_worker.classes import load_config
from investigator.detection_worker.events import CancelToken, EventWriter
from investigator.detection_worker.runner import RunOptions, run, write_atomic


class Tee:
    def __init__(self, file):
        self.file = file

    def write(self, text):
        import sys

        self.file.write(text)
        sys.stdout.write(text)

    def flush(self):
        import sys

        self.file.flush()
        sys.stdout.flush()


def reference_input(path: Path) -> tuple[Path, str, list[int]]:
    reference = json.loads(path.read_text(encoding="utf-8"))
    yoloe = next(item for item in reference["backends"] if item["backend"] == "yoloe")
    frames = [row["frame"] for row in yoloe["modes"]["all"]["rows"]]
    if (len(frames) < 2 or any(isinstance(f, bool) or not isinstance(f, int) for f in frames)
            or frames[0] < 0):
        raise ValueError("Reference must contain at least two valid frame indices")
    step = frames[1] - frames[0]
    if step < 1 or frames != list(range(frames[0], frames[-1] + 1, step)):
        raise ValueError("Reference frames must form an increasing, regular interval")
    video = Path(reference["video"])
    with video.open("rb") as video_file:
        checksum = hashlib.file_digest(video_file, "sha256").hexdigest()
    if checksum != reference["video_sha256"]:
        raise ValueError("Video checksum does not match the previous YOLOE test")
    return video, checksum, frames


def save_box_previews(result: dict, output: Path, frames: list[int]) -> list[str]:
    """Draw boxes only, never fabricated segmentation masks."""
    chosen = sorted({frames[0], frames[len(frames) // 2], frames[-1]})
    cap = cv2.VideoCapture(result["video"]["path"])
    paths = []
    try:
        for index in chosen:
            if not cap.set(cv2.CAP_PROP_POS_FRAMES, index):
                raise RuntimeError(f"Cannot seek preview frame {index}")
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"Cannot read preview frame {index}")
            detections = [d for d in result["detections"] if d["frame_index"] == index]
            for detection in detections:
                x, y, w, h = detection["bbox_xywh"]
                color = (52, 205, 245)
                cv2.rectangle(frame, (round(x), round(y)), (round(x + w), round(y + h)), color, 2)
                label = f"{detection['object_id']} {detection['source_prompt']} {detection['score']:.2f}"
                cv2.putText(frame, label, (round(x), max(18, round(y) - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2, cv2.LINE_AA)
            cv2.putText(frame, f"Grounding DINO | frame {index} (zero-based) | boxes {len(detections)}",
                        (20, 32), cv2.FONT_HERSHEY_SIMPLEX, .75, (255, 255, 255), 2, cv2.LINE_AA)
            name = f"boxes-frame-{index:06d}.png"
            success, encoded = cv2.imencode(".png", frame)
            if not success:
                raise RuntimeError("Cannot encode preview PNG")
            (output / name).write_bytes(encoded.tobytes())
            paths.append(name)
    finally:
        cap.release()
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="runtime/models/grounding-dino-base")
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--box-threshold", type=float, default=.3)
    parser.add_argument("--text-threshold", type=float, default=.25)
    parser.add_argument("--no-half", action="store_true")
    args = parser.parse_args(argv)
    video, checksum, frames = reference_input(args.reference)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(filename=args.output_dir / "inference.log", encoding="utf-8", level=logging.INFO)
    config = load_config(None)
    classes = config.enabled()
    with (args.output_dir / "events.jsonl").open("w", encoding="utf-8") as event_file:
        events = EventWriter(Tee(event_file))
        events.emit("loading_model", model_id=args.model)
        backend = create_backend(args.model, "cuda", not args.no_half, "grounding_dino",
                                 chunk_size=args.chunk_size, box_threshold=args.box_threshold,
                                 text_threshold=args.text_threshold)
        if backend.device != "cuda":
            raise RuntimeError("This test requires CUDA; CPU fallback is not accepted")
        result = run(RunOptions(video_path=video, output_path=args.output_dir / "result.json",
                                frame_step=frames[1] - frames[0], start_frame=frames[0],
                                end_frame=frames[-1] + 1, include_polygons=False),
                     backend, config, classes, events, CancelToken())
        if result["sampling"]["processed_frames"] != frames or result["video"]["sha256"] != checksum:
            raise RuntimeError("Processed frames/video do not match reference")
        result["input_reference"] = str(args.reference.resolve())
        write_atomic(result, args.output_dir / "result.json")
        with (args.output_dir / "label-mapping-failures.jsonl").open("w", encoding="utf-8") as failure_file:
            for failure in result["mapping_failures"]:
                failure_file.write(json.dumps(failure, ensure_ascii=False, allow_nan=False) + "\n")
        previews = save_box_previews(result, args.output_dir, frames)
        summary = {**result["detection_summary"], "coarse_class_labels": {
            c.key: c.label for c in classes}, "previews": previews,
            "processed_frame_indices": frames, "prompt_count": len({p for c in classes for p in c.prompts}),
            "video_sha256": checksum}
        write_atomic(summary, args.output_dir / "summary.json")
        events.emit("completed", **summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
