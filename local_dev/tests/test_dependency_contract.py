"""Keep exactly one runtime and one development requirements file in sync.

These checks inspect only dependency declarations: no Qt application, model,
CUDA runtime, package installation, or network access is needed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "requirements.txt"
DEVELOPMENT = ROOT / "local_dev" / "requirements-dev.txt"
BASE_ONLY = {"pyside6", "opencv-python", "numpy"}
DETECTION_ONLY = {
    "torch", "torchvision", "transformers", "pillow", "ultralytics", "clip",
    "timm", "einops", "ftfy", "wcwidth", "pycocotools",
}
DEVELOPMENT_ONLY = {"pytest", "pytest-qt", "ruff"}


def _requirements(path: Path, *, include: bool = True) -> dict[str, Requirement]:
    """Resolve the project's plain requirements files and reject duplicate pins."""

    result: dict[str, Requirement] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            if not include:
                continue
            entries = _requirements(path.parent / line[3:].strip())
        else:
            requirement = Requirement(line)
            entries = {canonicalize_name(requirement.name): requirement}
        duplicate = result.keys() & entries.keys()
        assert not duplicate, f"Duplicate dependencies in {path.name}: {sorted(duplicate)}"
        result.update(entries)
    return result


def _named(requirements: list[str]) -> dict[str, Requirement]:
    parsed = [Requirement(item) for item in requirements]
    result = {canonicalize_name(item.name): item for item in parsed}
    assert len(result) == len(parsed), "Duplicate dependency in pyproject.toml"
    return result


def test_only_runtime_and_development_files_exist():
    assert sorted(path.name for path in ROOT.glob("requirements*.txt")) == ["requirements.txt"]
    assert sorted(path.name for path in DEVELOPMENT.parent.glob("requirements*.txt")) == [
        "requirements-dev.txt"
    ]


def test_runtime_file_covers_gui_and_detection_once():
    assert set(_requirements(RUNTIME)) == BASE_ONLY | DETECTION_ONLY


def test_development_file_adds_tools_to_the_runtime_file():
    lines = [line.strip() for line in DEVELOPMENT.read_text(encoding="utf-8-sig").splitlines()]
    assert "-r ../requirements.txt" in lines
    additions = _requirements(DEVELOPMENT, include=False)
    assert set(additions) == DEVELOPMENT_ONLY
    assert _requirements(DEVELOPMENT) == _requirements(RUNTIME) | additions


@pytest.mark.parametrize("path", [RUNTIME, DEVELOPMENT])
def test_korean_comments_declare_utf8_for_pip_on_cp949_windows(path):
    # pip falls back to the locale codec (cp949) without a PEP 263 declaration.
    assert path.read_text(encoding="utf-8-sig").splitlines()[0] == "# -*- coding: utf-8 -*-"


def test_package_metadata_matches_requirement_files():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base = _named(project["dependencies"])
    detection = _named(project["optional-dependencies"]["detection"])
    development = _named(project["optional-dependencies"]["dev"])
    assert set(base) == BASE_ONLY
    assert set(detection) == DETECTION_ONLY
    assert base | detection == _requirements(RUNTIME)
    assert development == _requirements(DEVELOPMENT, include=False)


@pytest.mark.parametrize("name,version", [("numpy", "2.1.3"), ("pillow", "11.1.0")])
def test_target_pc_versions_are_allowed_by_runtime_install(name, version):
    requirements = _requirements(RUNTIME)
    assert requirements[name].specifier.contains(version)


@pytest.mark.parametrize(
    "name,version", [("torch", "2.11.0+cu128"), ("torchvision", "0.26.0+cu128")]
)
def test_cuda_wheels_satisfy_public_pytorch_version_pins(name, version):
    requirements = _requirements(RUNTIME)
    assert requirements[name].specifier.contains(version)


def test_yoloe_dependencies_and_reproducible_tokenizer():
    requirements = _requirements(RUNTIME)
    assert requirements["ultralytics"].specifier.contains("8.3.253")
    assert requirements["clip"].url.endswith("@7ffa84b3bfa40c42ecc1c77147a855e69cb2dd40")
