"""local_dev/tests 공용 fixture."""

from __future__ import annotations

import json

import pytest

import investigator.detection_process as process


@pytest.fixture
def bundled_dino_model(tmp_path, monkeypatch):
    """배포에 포함된 Grounding DINO Base 모델 폴더 대신 쓰는 가짜 로컬 모델 폴더.

    검출은 항상 번들 로컬 모델을 오프라인으로 쓰므로, 실제 가중치가 없는 개발·테스트
    환경에서는 이 폴더를 기본 경로로 바꿔 끼운다(config.json만 있으면 시작 전 확인을 통과).
    """
    folder = tmp_path / "bundled-models" / "grounding-dino-base"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"model_type": "grounding-dino"}), encoding="utf-8")
    monkeypatch.setattr(process, "default_dino_model_path", lambda: folder)
    return folder
