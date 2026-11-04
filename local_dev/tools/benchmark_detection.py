"""Compare real AVAT backends in isolated processes, without changing cases.

Run using a local detection environment containing the selected backends:
  python local_dev/tools/benchmark_detection.py --video clip.mp4 --output comparison.json
Model load, prompt preparation and warmup are separate; frames use CUDA sync.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PREFIX = "AVAT_BENCHMARK "
BACKEND_LABELS = {"sam3": "SAM3", "efficientsam3": "EfficientSAM3 TV-M", "yoloe": "YOLOE 11L-seg"}


def selected_modes(config, requested: str):
    """Resolve current coarse modes and explicit legacy baselines separately."""
    available = {item.key for item in config.classes}
    modes = []
    for mode in requested.split(","):
        if mode == "all":
            classes = config.enabled()
        elif mode == "vehicle":
            keys = ["vehicle"] if "vehicle" in available else [
                key for key in ("passenger", "van", "truck", "special") if key in available
            ]
            classes = config.enabled(keys)
        else:
            classes = config.enabled([mode])
        if not classes or any(name == mode for name, _ in modes):
            raise ValueError("Benchmark modes must select distinct, nonempty class sets")
        modes.append((mode, classes))
    return modes


def summary(rows: list[dict]) -> dict:
    from investigator.detection_taxonomy import coarse_class_key

    class_counts: dict[str, int] = {}
    tracks: dict[str, set[int]] = {}
    for row in rows:
        for det in row["detections"]:
            key = det.get("coarse_class") or coarse_class_key(det["class"]) or det["class"]
            class_counts[key] = class_counts.get(key, 0) + 1
            if "track_id" in det:
                tracks.setdefault(key, set()).add(det["track_id"])
    return {
        "samples": len(rows),
        **{f"{key}_mean_s": statistics.mean(row[key] for row in rows)
           for key in ("decode", "predict", "postprocess", "total")},
        "total_median_s": statistics.median(row["total"] for row in rows),
        "total_min_s": min(row["total"] for row in rows),
        "total_max_s": max(row["total"] for row in rows),
        "detections": sum(len(row["detections"]) for row in rows),
        "coarse_detection_counts": class_counts,
        "coarse_unique_track_counts": {key: len(ids) for key, ids in tracks.items()},
    }


def measure(args) -> dict:
    import cv2
    import torch

    from investigator.detection_worker.backend import create_backend
    from investigator.detection_worker.classes import load_config
    from investigator.detection_worker.postprocess import build_detections
    from investigator.detection_worker.tracking import SimpleTracker

    config = load_config(getattr(args, "config", None))
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise RuntimeError("Cannot open benchmark video")
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = [int(value) for value in args.frames.split(",")]
    if len(set(frames)) != len(frames) or any(frame < 0 or frame >= count for frame in frames):
        raise ValueError("Benchmark frames must be distinct valid zero-based indices")
    report = {
        "backend": args.worker, "model": args.model,
        "gpu": torch.cuda.get_device_name(), "python": sys.version,
        "versions": {name: importlib.metadata.version(name)
                     for name in ("torch", "torchvision", "numpy", "pillow")},
        "config_path": str(args.config.resolve()) if getattr(args, "config", None) else None,
        "taxonomy": config.notes,
        "video": {"path": str(args.video), "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                  "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), "frame_count": count,
                  "fps": cap.get(cv2.CAP_PROP_FPS)},
        "modes": {},
    }
    try:
        for mode, classes in selected_modes(config, getattr(args, "modes", "all,vehicle")):
            # A model has one immutable ordered vocabulary per run. Recreate it
            # between modes instead of reusing mismatched class-index mappings.
            before = time.perf_counter()
            backend = create_backend(args.model, "cuda", backend=args.worker)
            torch.cuda.synchronize()
            load_seconds = time.perf_counter() - before
            prompts = sorted({prompt for cls in classes for prompt in cls.prompts})
            before = time.perf_counter()
            prepare = getattr(backend, "prepare", None)
            if callable(prepare):
                prepare(prompts)
            torch.cuda.synchronize()
            prompt_seconds = time.perf_counter() - before
            model = backend.model if callable(getattr(backend.model, "parameters", None)) else backend.model.model
            metadata = {
                "model_load_seconds": load_seconds, "prompt_prepare_seconds": prompt_seconds,
                "model_parameters": sum(parameter.numel() for parameter in model.parameters()),
                "autocast": str(getattr(backend, "_autocast_dtype", None)),
                "half_weights": bool(getattr(backend, "_half", False)),
                "tf32_matmul": torch.backends.cuda.matmul.allow_tf32,
                "tf32_cudnn": torch.backends.cudnn.allow_tf32,
                "inference_imgsz": getattr(backend, "inference_imgsz", None),
            }
            if "model_load_seconds" not in report:
                report.update(metadata)
            if args.worker == "yoloe":
                report["versions"]["ultralytics"] = importlib.metadata.version("ultralytics")
            else:
                report["versions"]["transformers"] = importlib.metadata.version("transformers")
            tracker = SimpleTracker()
            rows, warmup = [], []
            next_sequential = 0
            torch.cuda.reset_peak_memory_stats()
            for index, frame_index in enumerate([frames[0]] * args.warmup + frames):
                if index == args.warmup:
                    tracker = SimpleTracker()
                torch.cuda.synchronize()
                t0 = time.perf_counter()
                if frame_index != next_sequential and not cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
                    raise RuntimeError(f"Seek failed: {frame_index}")
                ok, bgr = cap.read()
                next_sequential = frame_index + 1
                if not ok:
                    raise RuntimeError(f"Read failed: {frame_index}")
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                t1 = time.perf_counter()
                raw = backend.predict(rgb, prompts, min(cls.threshold for cls in classes), 0.5)
                torch.cuda.synchronize()
                t2 = time.perf_counter()
                detections = build_detections(raw, classes, config, rgb.shape[:2])
                track_ids = tracker.update(index, [(det.class_key, det.bbox_xywh) for det in detections])
                t3 = time.perf_counter()
                row = {"frame": frame_index, "decode": t1 - t0, "predict": t2 - t1,
                       "postprocess": t3 - t2, "total": t3 - t0,
                       "detections": [{"class": det.class_key, "box": list(det.bbox_xywh),
                                       "score": det.score, "prompt": det.prompt, "track_id": track_id}
                                      for det, track_id in zip(detections, track_ids, strict=True)]}
                (warmup if index < args.warmup else rows).append(row)
                print(f"{args.worker} {mode} {'warmup' if index < args.warmup else 'sample'} "
                      f"frame={frame_index} total={row['total']:.3f}s boxes={len(detections)}", flush=True)
            report["modes"][mode] = {"classes": len(classes), "prompts": len(prompts),
                                     **metadata,
                                     "thresholds": {cls.key: cls.threshold for cls in classes},
                                     "warmup": warmup, "rows": rows, "summary": summary(rows),
                                     "peak_cuda_allocated_mb": torch.cuda.max_memory_allocated() / 2**20}
            del model, backend
            gc.collect()
            torch.cuda.empty_cache()
    finally:
        cap.release()
    return report


def agreement(reference: list[dict], candidate: list[dict]) -> dict:
    """Box agreement is NOT accuracy: SAM3 is a reference, not ground truth."""
    from investigator.detection_worker.postprocess import box_iou

    matched, reference_count, candidate_count = 0, 0, 0
    overlaps = []
    for left, right in zip(reference, candidate, strict=True):
        if left["frame"] != right["frame"]:
            raise ValueError("Cannot compare different frames")
        a, b = left["detections"], right["detections"]
        reference_count += len(a)
        candidate_count += len(b)
        pairs = sorted(((box_iou(x["box"], y["box"]), i, j) for i, x in enumerate(a)
                        for j, y in enumerate(b) if x["class"] == y["class"]), reverse=True)
        used_a, used_b = set(), set()
        for overlap, i, j in pairs:
            if overlap >= 0.5 and i not in used_a and j not in used_b:
                used_a.add(i)
                used_b.add(j)
                overlaps.append(overlap)
                matched += 1
    return {"reference_boxes": reference_count, "candidate_boxes": candidate_count,
            "same_class_iou_50_matches": matched,
            "reference_unmatched": reference_count - matched,
            "candidate_unmatched": candidate_count - matched,
            "matched_mean_iou": statistics.mean(overlaps) if overlaps else None,
            "not_ground_truth_accuracy": True}


def previews(report: dict, folder: Path) -> None:
    import cv2
    import numpy as np

    folder.mkdir()
    cap = cv2.VideoCapture(report["video"])
    try:
        mode = "all" if "all" in report["backends"][0]["modes"] else next(iter(report["backends"][0]["modes"]))
        rows = [item["modes"][mode]["rows"] for item in report["backends"]]
        for index in (0, len(rows[0]) // 2, len(rows[0]) - 1):
            frame = rows[0][index]["frame"]
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, image = cap.read()
            if not ok:
                raise RuntimeError("Preview frame read failed")
            panels = []
            for backend, detections in zip(report["backends"], rows, strict=True):
                name = BACKEND_LABELS[backend["backend"]]
                canvas = image.copy()
                for det in detections[index]["detections"]:
                    x, y, w, h = [round(value) for value in det["box"]]
                    cv2.rectangle(canvas, (x, y), (x+w, y+h), (0, 200, 255), 2)
                    cv2.putText(canvas, f"{det['class']} {det['score']:.2f}", (x, max(18, y-4)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)
                canvas = cv2.resize(canvas, (960, 540))
                header = np.full((36, 960, 3), 245, dtype=np.uint8)
                cv2.putText(header, f"{name} | frame {frame} | {len(detections[index]['detections'])} boxes",
                            (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (30, 30, 30), 1, cv2.LINE_AA)
                panels.append(np.concatenate([header, canvas]))
            if not cv2.imwrite(str(folder / f"frame-{frame}.jpg"), np.concatenate(panels, axis=1)):
                raise OSError("Failed to write comparison preview")
    finally:
        cap.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--frames", default="100,200,300,400,500,600,700,800,900,1000")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--config", type=Path, help="Explicit frozen legacy class configuration, if required")
    parser.add_argument("--modes", default="all,vehicle", help="all,vehicle or explicit class keys (legacy: passenger)")
    parser.add_argument("--backends", default="sam3,yoloe", help="Comma-separated backend names")
    parser.add_argument("--sam3-model", default=str(ROOT / "runtime/models/sam3"))
    parser.add_argument("--efficient-model", default=str(ROOT / "runtime/models/efficientsam3-tv-m"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--yoloe-model", default=str(ROOT / "runtime/models/yoloe-11l/yoloe-11l-seg.pt"))
    parser.add_argument("--worker", choices=list(BACKEND_LABELS))
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.warmup < 1:
        parser.error("warmup must be >= 1")
    args.video = args.video.resolve()
    if args.worker:
        print(PREFIX + json.dumps(measure(args), ensure_ascii=True), flush=True)
        return 0
    if args.output is None or args.output.exists() or args.output.with_suffix("").exists():
        parser.error("Specify a new --output JSON path; existing artifacts are never overwritten")
    results = []
    backends = args.backends.split(",")
    if not backends or len(set(backends)) != len(backends) or set(backends) - BACKEND_LABELS.keys():
        parser.error("Specify distinct supported --backends")
    models = {"sam3": args.sam3_model, "efficientsam3": args.efficient_model, "yoloe": args.yoloe_model}
    for backend in backends:
        model = models[backend]
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", backend,
                   "--model", model, "--video", str(args.video), "--frames", args.frames,
                   "--warmup", str(args.warmup), "--modes", args.modes]
        if args.config:
            command.extend(["--config", str(args.config.resolve())])
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONIOENCODING": "utf-8"}
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None, text=True,
                                 encoding="utf-8", env=env, cwd=ROOT)
        result = None
        for line in child.stdout:
            if line.startswith(PREFIX):
                result = json.loads(line[len(PREFIX):])
            else:
                print(line.rstrip(), flush=True)
        if child.wait() or result is None:
            raise RuntimeError(f"{backend} benchmark failed; no comparison written")
        results.append(result)
    comparisons = {}
    for candidate in results[1:]:
        pair = {}
        for mode in results[0]["modes"]:
            a, b = results[0]["modes"][mode], candidate["modes"][mode]
            pair[mode] = {"speedup": a["summary"]["total_mean_s"] / b["summary"]["total_mean_s"],
                          "agreement": agreement(a["rows"], b["rows"])}
        comparisons[f"{results[0]['backend']}_vs_{candidate['backend']}"] = pair
    with args.video.open("rb") as stream:
        video_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"video": str(args.video), "video_sha256": video_hash,
              "measurement": "CUDA synchronized; load+prompt preparation+warmup excluded; no case/database writes; "
                             "includes decode, preprocessing, masks, NMS, polygons, tracker; "
                             "excludes final JSON/DB/UI updates and whole-file hashing",
              "backends": results, "comparison": comparisons}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    previews(report, args.output.with_suffix(""))
    print(json.dumps(comparisons, indent=2), flush=True)
    print(f"Report: {args.output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
