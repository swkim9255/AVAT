"""공유 배포 예시는 로컬 Grounding DINO와 독립 실행 환경만 지정한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from investigator.detection_process import _load_runtime_manifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", [
    "detection-runtime.example.json",
    "detection-runtime.grounding-dino.example.json",
])
def test_deployment_example_is_offline_grounding_dino(name, tmp_path):
    data = json.loads((ROOT / name).read_text(encoding="utf-8"))
    assert data["backend"] == "grounding_dino"
    assert data["python"] == ".venv/Scripts/python.exe"
    assert data["model"] == "runtime/models/grounding-dino-base"
    assert data["device"] == "auto"
    assert data["offline"] is True

    # 가중치를 저장소에 넣지 않고 설정 파일의 상대 경로 해석만 검사한다.
    executable = tmp_path / data["python"]
    executable.parent.mkdir(parents=True)
    executable.touch()
    model = tmp_path / data["model"]
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type": "grounding-dino"}', encoding="utf-8")
    manifest = tmp_path / "detection-runtime.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    settings, error = _load_runtime_manifest(manifest)
    assert error == ""
    assert settings is not None
    assert settings.backend == "grounding_dino"
    assert settings.python == str(executable.resolve())
    assert settings.model == str(model.resolve())
    assert settings.offline is True
    assert settings.dino_chunk_size == 8
    assert settings.dino_box_threshold == 0.3
    assert settings.dino_text_threshold == 0.25
    assert settings.dino_fp16 is True
