"""Convert an already acquired Meta SAM 3 checkpoint for offline Windows use.

No network request is made. Run with the separate detection interpreter and an
audited copy of Hugging Face's official SAM 3 conversion module. The original
checkpoint is never modified, and an existing output directory is never reused.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

MAPPING_SOURCE = (
    "https://github.com/huggingface/transformers/blob/main/"
    "src/transformers/models/sam3/convert_sam3_to_hf.py"
)
_UNUSED_BUFFER = re.compile(
    r"vision_encoder\.backbone\.layers\.\d+\.rotary_emb\.rope_embeddings"
)
_DETECTOR_PREFIXES = ("detector.", "sam3_model.")
_TRACKER_PREFIXES = ("tracker.", "sam2_predictor.")
# Meta's geometry encoder has separate point-only projections. Transformers'
# Sam3GeometryEncoder implements box/text prompts, not point prompts. AVAT's
# worker uses text prompts; excluding these does not replace any HF parameter.
# See sam3/model/geometry_encoders.py and the installed HF modeling_sam3.py.
_UNSUPPORTED_POINT_PARAMETERS = {
    f"geometry_encoder.{projection}.{parameter}"
    for projection in ("points_direct_project", "points_pool_project", "points_pos_enc_project")
    for parameter in ("weight", "bias")
}
# Sam3DualViTDetNeck returns independent SAM3 `convs` and SAM2 `sam2_convs`
# feature maps. The latter are enabled only for instance interactivity/tracking;
# Meta's image builder defaults enable_inst_interactivity=False. HF's image
# detector has one SAM3 FPN, with no SAM2 branch. Enumerate only these known
# layers, not every unknown key under a permissive prefix.
# https://github.com/facebookresearch/sam3/blob/main/sam3/model/necks.py
_UNSUPPORTED_SAM2_NECK_PARAMETERS = {
    f"backbone.vision_backbone.sam2_convs.{index}.{layer}.{parameter}"
    for index, layers in {
        0: ("dconv_2x2_0", "dconv_2x2_1", "conv_1x1", "conv_3x3"),
        1: ("dconv_2x2", "conv_1x1", "conv_3x3"),
        2: ("conv_1x1", "conv_3x3"),
        3: ("conv_1x1", "conv_3x3"),
    }.items()
    for layer in layers
    for parameter in ("weight", "bias")
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unwrap_state_dict(checkpoint: object) -> Mapping:
    if not isinstance(checkpoint, Mapping):
        raise ValueError("Checkpoint must contain a tensor state dictionary.")
    for key in ("model", "state_dict"):
        nested = checkpoint.get(key)
        if isinstance(nested, Mapping):
            checkpoint = nested
            break
    if not checkpoint or any(not isinstance(key, str) for key in checkpoint):
        raise ValueError("Checkpoint contains no valid named weights.")
    return checkpoint


def select_detector_weights(state: Mapping) -> tuple[dict, int]:
    """Strip a known detector wrapper and omit only known tracker namespaces."""
    prefixes = [prefix for prefix in _DETECTOR_PREFIXES if any(k.startswith(prefix) for k in state)]
    if len(prefixes) > 1:
        raise ValueError("Mixed detector prefixes are not supported.")
    if not prefixes:
        if any(k.startswith(_TRACKER_PREFIXES) for k in state):
            raise ValueError("Tracker weights are present without a known detector prefix.")
        return dict(state), 0
    prefix = prefixes[0]
    unknown = [k for k in state if not k.startswith((prefix, *_TRACKER_PREFIXES))]
    if unknown:
        raise ValueError(f"Unknown checkpoint namespaces: {unknown[:10]}")
    selected = {k[len(prefix) :]: v for k, v in state.items() if k.startswith(prefix)}
    return selected, len(state) - len(selected)


def load_mapping(path: Path):
    spec = importlib.util.spec_from_file_location("avat_audited_sam3_mapping", path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot import the local conversion mapping.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("convert_old_keys_to_new_keys", "split_qkv"):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"The conversion mapping has no callable {name}.")
    return module


def convert_detector_weights(state: Mapping, mapping) -> dict:
    names = list(state)
    renamed = mapping.convert_old_keys_to_new_keys(names)
    if set(renamed) != set(names):
        raise ValueError("The upstream mapping did not account for every original key.")
    converted = {}
    for old_name in names:
        name = renamed[old_name]
        if name in converted:
            raise ValueError(f"Conversion key collision: {name}")
        tensor = state[old_name]
        if name == "vision_encoder.backbone.embeddings.position_embeddings":
            if len(tensor.shape) != 3 or tensor.shape[1] != 577:
                raise ValueError("Unexpected SAM 3 positional embedding shape.")
            tensor = tensor[:, 1:, :]
        converted[name] = tensor
    converted = mapping.split_qkv(converted)
    name = "text_encoder.text_projection.weight"
    if name in converted:
        converted[name] = converted[name].T
    return converted


def validate_weights(converted: dict, expected: Mapping) -> list[str]:
    """Require all HF weights/shapes; exclude only audited unused branches/data."""
    omitted = []
    for name in list(converted):
        if name not in expected and (
            _UNUSED_BUFFER.fullmatch(name)
            or name in _UNSUPPORTED_POINT_PARAMETERS
            or name in _UNSUPPORTED_SAM2_NECK_PARAMETERS
        ):
            omitted.append(name)
            del converted[name]
    missing = sorted(set(expected) - set(converted))
    unexpected = sorted(set(converted) - set(expected))
    mismatched = [
        (name, tuple(converted[name].shape), tuple(expected[name].shape))
        for name in sorted(set(expected) & set(converted))
        if tuple(converted[name].shape) != tuple(expected[name].shape)
    ]
    if missing or unexpected or mismatched:
        raise ValueError(
            "Strict checkpoint validation failed. "
            f"Missing ({len(missing)}): {missing[:12]}; "
            f"unexpected ({len(unexpected)}): {unexpected[:12]}; "
            f"shape mismatches ({len(mismatched)}): {mismatched[:12]}"
        )
    return omitted


def clip_assets_from_bpe(path: Path, directory: Path) -> tuple[Path, Path]:
    """Reconstruct CLIP's standard 49,408-entry vocabulary from Meta's BPE."""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        lines = stream.read().split("\n")
    merges = lines[1 : 49152 - 256 - 2 + 1]
    if len(merges) != 48894 or any(len(line.split()) != 2 for line in merges):
        raise ValueError("The local BPE does not contain the standard CLIP merges.")
    byte_values = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    characters = byte_values.copy()
    next_codepoint = 256
    for value in range(256):
        if value not in byte_values:
            byte_values.append(value)
            characters.append(next_codepoint)
            next_codepoint += 1
    alphabet = [chr(value) for value in characters]
    vocabulary = alphabet + [value + "</w>" for value in alphabet]
    vocabulary += ["".join(line.split()) for line in merges]
    vocabulary += ["<|startoftext|>", "<|endoftext|>"]
    if len(vocabulary) != 49408 or len(set(vocabulary)) != 49408:
        raise ValueError("The reconstructed CLIP vocabulary is invalid.")
    directory.mkdir(parents=True, exist_ok=False)
    vocab_path = directory / "vocab.json"
    merges_path = directory / "merges.txt"
    vocab_path.write_text(
        json.dumps(dict(zip(vocabulary, range(len(vocabulary)), strict=True)), ensure_ascii=False),
        encoding="utf-8",
    )
    merges_path.write_text("#version: 0.2\n" + "\n".join(merges) + "\n", encoding="utf-8")
    return vocab_path, merges_path


def convert(checkpoint: Path, output: Path, mapping_path: Path, tokenizer: Path | None, bpe: Path | None):
    if os.path.lexists(output):
        raise FileExistsError(f"Output already exists; choose a new directory: {output}")
    for path in (checkpoint, mapping_path, bpe):
        if path is not None and not path.is_file():
            raise FileNotFoundError(path)
    if tokenizer is not None and not tokenizer.is_dir():
        raise NotADirectoryError(tokenizer)
    if (tokenizer is None) == (bpe is None):
        raise ValueError("Specify exactly one local --tokenizer directory or --bpe file.")
    # Enforce offline operation before importing Hugging Face libraries/mapping.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    import transformers
    from transformers import (
        CLIPTokenizerFast,
        Sam3Config,
        Sam3ImageProcessor,
        Sam3Model,
        Sam3Processor,
    )

    mapping = load_mapping(mapping_path)
    print("Hashing original checkpoint...", flush=True)
    digest = sha256_file(checkpoint)
    print("Loading original checkpoint on CPU (weights_only, mmap)...", flush=True)
    original = unwrap_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True, mmap=True))
    original_count = len(original)
    if any(not isinstance(value, torch.Tensor) for value in original.values()):
        raise ValueError("Checkpoint contains non-tensor entries.")
    detector, tracker_count = select_detector_weights(original)
    detector_count = len(detector)
    converted = convert_detector_weights(detector, mapping)
    config = Sam3Config()
    config.architectures = ["Sam3Model"]
    with torch.device("meta"):
        model = Sam3Model(config)
    omitted = validate_weights(converted, model.state_dict())
    model.load_state_dict(converted, strict=True, assign=True)
    if any(tensor.is_meta for tensor in model.state_dict().values()):
        raise ValueError("A saved model tensor remains on the meta device.")
    buffer_count = sum(bool(_UNUSED_BUFFER.fullmatch(name)) for name in omitted)
    point_count = sum(name in _UNSUPPORTED_POINT_PARAMETERS for name in omitted)
    sam2_count = sum(name in _UNSUPPORTED_SAM2_NECK_PARAMETERS for name in omitted)
    print(
        f"Validated {len(converted)} saved tensors; excluded {buffer_count} precomputed buffers, "
        f"{point_count} unsupported point-prompt tensors, {sam2_count} SAM2-only neck tensors.",
        flush=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-converting-", dir=output.parent))
    try:
        if tokenizer is not None:
            text_processor = CLIPTokenizerFast.from_pretrained(
                str(tokenizer), local_files_only=True, model_max_length=32, max_length=32,
            )
        else:
            # Only temporary files created by this conversion are cleaned up.
            with tempfile.TemporaryDirectory(prefix="avat-tokenizer-", dir=stage) as temp:
                vocab, merges = clip_assets_from_bpe(bpe, Path(temp) / "clip")
                text_processor = CLIPTokenizerFast(
                    vocab_file=str(vocab), merges_file=str(merges), model_max_length=32,
                    bos_token="<|startoftext|>", eos_token="<|endoftext|>",
                    unk_token="<|endoftext|>", pad_token="<|endoftext|>",
                )
        if (text_processor.vocab_size, text_processor.bos_token_id, text_processor.eos_token_id) != (49408, 49406, 49407):
            raise ValueError("Local tokenizer does not match SAM 3's CLIP vocabulary.")
        processor = Sam3Processor(image_processor=Sam3ImageProcessor(), tokenizer=text_processor)
        processor.save_pretrained(stage)
        model.save_pretrained(stage, safe_serialization=True)
        provenance = {
            "original_sha256": digest,
            "transformers_version": transformers.__version__,
            "mapping_source": MAPPING_SOURCE,
            "mapping_sha256": sha256_file(mapping_path),
            "key_counts": {
                "original": original_count, "detector": detector_count,
                "excluded_tracker": tracker_count, "saved": len(converted),
                "excluded_precomputed_buffers": buffer_count,
                "excluded_point_prompt_parameters": point_count,
                "excluded_sam2_neck_parameters": sam2_count,
            },
        }
        (stage / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        if os.path.lexists(output):
            raise FileExistsError(f"Output was created by another process: {output}")
        stage.rename(output)
    except BaseException:
        print(f"Conversion failed. New staging files retained for inspection: {stage}", file=sys.stderr)
        raise
    print(f"Saved offline model: {output}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tokenizer", type=Path, help="Already downloaded local CLIP tokenizer directory")
    source.add_argument("--bpe", type=Path, help="Local original bpe_simple_vocab_16e6.txt.gz")
    options = parser.parse_args(argv)
    convert(options.checkpoint, options.output, options.mapping, options.tokenizer, options.bpe)


if __name__ == "__main__":
    main()
