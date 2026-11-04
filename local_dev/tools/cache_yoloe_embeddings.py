"""Prepare a model-bound YOLOE prompt cache for fully offline inference."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def write_cache(path: Path, model: Path, prompts: list[str], embeddings: np.ndarray) -> None:
    from investigator.detection_worker.yoloe_backend import checkpoint_sha256

    if (embeddings.dtype != np.float32 or embeddings.ndim != 3
            or embeddings.shape[:2] != (1, len(prompts)) or embeddings.shape[2] <= 0
            or not np.isfinite(embeddings).all()):
        raise ValueError("Invalid prompt embeddings")
    # Exclusive creation deliberately prevents replacement of a deployed cache.
    with path.open("xb") as stream:
        np.savez_compressed(stream, format_version=np.asarray(1, dtype=np.int64),
                            model_sha256=np.asarray(checkpoint_sha256(model), dtype=np.str_),
                            prompts=np.asarray(prompts, dtype=np.str_), embeddings=embeddings)


def main() -> int:
    from investigator.detection_worker.backend import create_backend
    from investigator.detection_worker.classes import load_config
    from investigator.detection_worker.yoloe_backend import EMBEDDING_SUFFIX, local_checkpoint

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    checkpoint = local_checkpoint(args.model)
    output = args.output or checkpoint.with_suffix(EMBEDDING_SUFFIX)
    if output.exists():
        parser.error("Choose a new output path; existing embeddings are never overwritten")
    config = load_config(args.config)
    prompts = sorted({prompt for item in config.enabled() for prompt in item.prompts})
    backend = create_backend(str(checkpoint), args.device, backend="yoloe")
    backend.prepare(prompts)
    embeddings = backend.model.model.pe.detach().float().cpu().numpy()
    write_cache(output, checkpoint, prompts, embeddings)
    print(f"Saved {len(prompts)} model-bound prompt embeddings: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
