"""Synthetic-only storage/list benchmark: python -m local_dev.tests.benchmark_storage.

No existing workspace or video is opened. Temporary datasets live below the
repository's ignored .pytest_cache directory and are removed after each run.
The JSON baseline measures full serialization + atomic fsync/replace, excluding
the old implementation's additional full-dataset validation cost.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from investigator.diagnostics import process_memory_bytes  # noqa: E402
from investigator.models import InvestigationCase  # noqa: E402
from investigator.storage import CaseRepository  # noqa: E402
from investigator.widgets.case_list import CaseListPanel  # noqa: E402
from .helpers import valid_values  # noqa: E402


def elapsed_ms(operation):
    started = time.perf_counter()
    result = operation()
    return (time.perf_counter() - started) * 1000, result


def legacy_save(repository, cases, destination):
    payload = {
        "schema_version": 1,
        "form_definition": repository.form_definition.to_dict(),
        "initial_form_definition": repository.initial_form_definition.to_dict(),
        "active_case_uid": cases[0].uid,
        "auto_import_exclusions": [],
        "cases": [case.to_dict() for case in cases],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", dir=destination.parent, encoding="utf-8", newline="", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def benchmark(count: int, repeats: int, parent: Path, app: QApplication) -> dict:
    with tempfile.TemporaryDirectory(prefix="avat-benchmark-", dir=parent) as temporary:
        workspace = Path(temporary)
        repository = CaseRepository(workspace)
        cases = []
        for index in range(count):
            values = valid_values()
            values.update(
                institution="samsung",
                accident_classification=f"{index + 1:013d}",
                remarks="성능 검증용 가상 사고 정보입니다. " * 20,
            )
            case = InvestigationCase(
                uid=f"synthetic-{index:06d}",
                values=values,
                videos=[str(workspace / f"{index + 1:013d}.mp4")],
                form_definition=repository.form_definition,
            )
            cases.append(case)
        initial_save_ms, _ = elapsed_ms(lambda: repository.save(cases))
        load_ms, loaded = elapsed_ms(repository.load)
        assert len(loaded) == count
        panel = CaseListPanel()
        panel.resize(330, 700)
        list_initial_ms, _ = elapsed_ms(lambda: panel.set_cases(cases, cases[0].uid))
        app.processEvents()
        json_samples, sqlite_samples, row_samples = [], [], []
        for iteration in range(repeats):
            cases[0].set_value("remarks", f"가상 수정 {iteration}")
            duration, _ = elapsed_ms(
                lambda: legacy_save(repository, cases, workspace / "legacy-baseline.json")
            )
            json_samples.append(duration)
            duration, _ = elapsed_ms(lambda: repository.save_changes([cases[0]]))
            sqlite_samples.append(duration)
            duration, updated = elapsed_ms(lambda: panel.update_case(cases[0]))
            assert updated
            row_samples.append(duration)
        result = {
            "cases": count,
            "repeats": repeats,
            "legacy_full_json_median_ms": round(statistics.median(json_samples), 3),
            "sqlite_single_case_median_ms": round(statistics.median(sqlite_samples), 3),
            "sqlite_initial_save_ms": round(initial_save_ms, 3),
            "sqlite_load_ms": round(load_ms, 3),
            "list_initial_build_ms": round(list_initial_ms, 3),
            "list_single_row_median_ms": round(statistics.median(row_samples), 3),
            "memory_bytes": process_memory_bytes(),
            "legacy_json_bytes": (workspace / "legacy-baseline.json").stat().st_size,
            "database_bytes": repository.database_path.stat().st_size,
        }
        panel.close()
        panel.deleteLater()
        app.processEvents()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[100, 1000, 5000])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1 or any(count < 1 for count in args.counts):
        parser.error("counts and repeats must be positive")
    app = QApplication.instance() or QApplication([])
    parent = Path(__file__).resolve().parents[2] / ".pytest_cache"
    parent.mkdir(exist_ok=True)
    for count in args.counts:
        print(json.dumps(benchmark(count, args.repeats, parent, app), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
