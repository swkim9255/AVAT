"""Local verification only: simulate an interpreter with no Accelerate package."""

import importlib.abc
import importlib.util
import os
import runpy
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
original_find_spec = importlib.util.find_spec


def without_accelerate(name, *args, **kwargs):
    if name == "accelerate" or name.startswith("accelerate."):
        return None
    return original_find_spec(name, *args, **kwargs)


class BlockAccelerate(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "accelerate" or fullname.startswith("accelerate."):
            raise ModuleNotFoundError("Accelerate deliberately unavailable during this check")
        return None


importlib.util.find_spec = without_accelerate
sys.meta_path.insert(0, BlockAccelerate())
sys.argv = ["investigator.detection_worker", *sys.argv[1:]]
runpy.run_module("investigator.detection_worker", run_name="__main__")
