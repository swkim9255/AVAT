from __future__ import annotations

import json
from pathlib import Path

import pytest

import investigator.detection_taxonomy as taxonomy


def original():
    return json.loads((Path(taxonomy.__file__).parent / "assets/detection_taxonomy.json").read_text(encoding="utf-8"))


def test_packaged_taxonomy_validates_without_reading_any_investigation_form():
    taxonomy.validate_taxonomy(original())


@pytest.mark.parametrize("mode", ["version", "class", "subtype", "prompt", "default", "mapping", "ratio", "alias"])
def test_invalid_taxonomy_fails_before_inference(mode):
    data = original()
    if mode == "version":
        data["version"] = True
    elif mode == "class":
        data["classes"][1]["key"] = data["classes"][0]["key"]
    elif mode == "subtype":
        data["classes"][1]["subtypes"][0]["key"] = "passenger"
    elif mode == "prompt":
        data["classes"][1]["prompts"][0] = "car"
    elif mode == "default":
        data["classes"][0]["default_subtype"] = ["invalid"]
    elif mode == "mapping":
        data["classes"][0]["prompt_defaults"]["car"] = "adult"
    elif mode == "ratio":
        data["classes"][0]["threshold"] = float("nan")
    else:
        data["legacy_aliases"]["old"] = "does_not_exist"
    with pytest.raises(ValueError):
        taxonomy.validate_taxonomy(data)
