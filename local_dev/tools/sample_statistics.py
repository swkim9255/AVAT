"""Import the supplied statistics CSV into an isolated, non-destructive project.

The CSV is not a full investigation export. Preserve its source workflow status
as data and leave incomplete investigations in draft; never fabricate victims,
video files, FPS, markers, dates, or missing answers to bypass completion checks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DEV = ROOT / "local_dev"
DEFAULT_SOURCE = LOCAL_DEV / "samples" / "avat_sample_cases.csv"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from investigator.form_definition import FormDefinition, field_record  # noqa: E402
from investigator.models import InvestigationCase  # noqa: E402
from investigator.schema import Choice, FieldKind, FieldSpec  # noqa: E402
from investigator.storage import CaseRepository  # noqa: E402

COLUMNS = (
    "sample_id", "workflow_status", "light_condition", "collision_target",
    "damage_level", "trigger_speed_kph", "trigger_to_collision_s", "vehicle_mileage_10k_km",
)
LABELS = {
    "sample_id": "샘플 ID (CSV)",
    "workflow_status": "작업상태 (원본 CSV)",
    "light_condition": "조명 상태 (CSV)",
    "collision_target": "충돌대상 (CSV)",
    "damage_level": "피해 정도 (CSV)",
    "trigger_speed_kph": "T 시점 속도 (CSV, km/h)",
    "trigger_to_collision_s": "트리거 → 충돌 시간 (CSV, 초)",
    "vehicle_mileage_10k_km": "주행거리 (CSV, 만 km)",
}
NUMERIC = {"trigger_speed_kph", "trigger_to_collision_s", "vehicle_mileage_10k_km"}
CATEGORICAL = {"workflow_status", "light_condition", "collision_target", "damage_level"}
DEFAULT_PROJECT = LOCAL_DEV / "samples" / "AVAT_통계샘플"


def read_source(path: Path) -> tuple[list[dict[str, str]], str]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError("샘플 CSV의 8개 칼럼과 순서를 확인해 주세요.")
        rows = list(reader)
    seen = set()
    for line, row in enumerate(rows, 2):
        if None in row or any(row.get(column) in (None, "") for column in COLUMNS):
            raise ValueError(f"{line}행에 누락되거나 여분인 값이 있습니다.")
        if not re.fullmatch(r"SAMPLE-\d{3}", row["sample_id"]) or row["sample_id"] in seen:
            raise ValueError(f"{line}행의 샘플 ID가 중복되거나 형식이 잘못되었습니다.")
        seen.add(row["sample_id"])
        if row["workflow_status"] not in {"completed", "draft"}:
            raise ValueError(f"{line}행의 작업상태가 올바르지 않습니다.")
        for column in NUMERIC:
            try:
                number = Decimal(row[column])
            except InvalidOperation as exc:
                raise ValueError(f"{line}행 {column}에 숫자가 아닌 값이 있습니다.") from exc
            if not number.is_finite() or number < 0:
                raise ValueError(f"{line}행 {column}에 유한한 0 이상 수치가 필요합니다.")
        mileage = Decimal(row["vehicle_mileage_10k_km"]) * 10_000
        if mileage != mileage.to_integral_value():
            raise ValueError(f"{line}행의 만 km를 정수 km로 정확히 변환할 수 없습니다.")
    if not rows:
        raise ValueError("샘플 CSV가 비어 있습니다.")
    return rows, hashlib.sha256(path.read_bytes()).hexdigest()


def sample_definition(rows: list[dict[str, str]]) -> FormDefinition:
    definition = FormDefinition.default()
    records = []
    for column in COLUMNS:
        kind = FieldKind.DECIMAL if column in NUMERIC else FieldKind.SINGLE if column in CATEGORICAL else FieldKind.TEXT
        choices = tuple(
            Choice({"completed": "완료", "draft": "임시저장"}.get(value, value) if column == "workflow_status" else value, value)
            for value in sorted({row[column] for row in rows})
        ) if column in CATEGORICAL else ()
        records.append(field_record(FieldSpec(
            "csv_" + column, LABELS[column], kind, "source_" + column,
            choices=choices, required=True,
        )))
    definition.tabs.insert(1, {
        "key": "csv_statistics_sample", "title": "샘플 CSV 원본값",
        "short_title": "CSV 샘플", "columns": 2, "fields": records,
    })
    definition.validate()
    return definition


def import_project(source: Path, destination: Path) -> tuple[CaseRepository, bool]:
    rows, digest = read_source(source)
    destination = destination.resolve()
    if destination == ROOT or destination == source.parent and destination != DEFAULT_PROJECT:
        raise ValueError("원본 프로젝트가 아닌 별도 샘플 폴더를 지정해 주세요.")
    repository = CaseRepository(destination)
    if repository.database_path.exists():
        cases = repository.load()
        if len(cases) != len(rows) or any(case.values.get("_sample_csv_sha256") != digest for case in cases):
            raise ValueError("대상 폴더에 다른 데이터가 있습니다. 기존 데이터는 변경하지 않았습니다.")
        return repository, False
    if destination.exists() and any(path.name != "README.md" for path in destination.iterdir()):
        raise ValueError("빈 샘플 폴더를 사용해 주세요. 기존 파일은 변경하지 않았습니다.")
    definition = sample_definition(rows)
    cases = []
    for row in rows:
        values = {"csv_" + column: value for column, value in row.items()}
        values.update(
            institution="samsung",
            accident_classification=row["sample_id"].split("-")[-1].zfill(13),
            vehicle_mileage=str(int(Decimal(row["vehicle_mileage_10k_km"]) * 10_000)),
            remarks=(
                f"통계 확인용 샘플. 원본: {source.name}, {row['sample_id']}. "
                "CSV에 없는 조사 값은 미입력으로 보존했습니다. "
                "원본 작업상태는 CSV 샘플 탭에서 확인합니다. 실제 영상·FPS·마커는 없습니다. "
                "주행거리만 만 km × 10,000으로 km에 정확히 환산했습니다."
            ),
            _sample_csv_sha256=digest,
        )
        cases.append(InvestigationCase(
            uid="avat-csv-" + row["sample_id"].lower(), values=values,
            status="draft", form_definition=definition,
        ))
    repository.save(cases, active_case_uid=cases[0].uid, form_definition=definition)
    restored = repository.load()
    assert len(restored) == len(rows)
    for case, row in zip(restored, rows, strict=True):
        assert all(case.values["csv_" + column] == row[column] for column in COLUMNS)
        assert Decimal(case.values["vehicle_mileage"]) == Decimal(row["vehicle_mileage_10k_km"]) * 10_000
        assert not case.videos and not case.markers_by_video and case.status == "draft"
    return repository, True


def open_statistics(workspace: Path) -> int:
    # Use the normal entry point's ICU preload and diagnostics before Qt loads.
    from investigator.app import MainWindow, QApplication, apply_theme
    from investigator.statistics import StatsCondition

    app = QApplication([sys.argv[0]])
    app.setApplicationDisplayName("AVAT · CSV 통계 샘플")
    app.setStyle("Fusion")
    apply_theme(app)
    window = MainWindow(workspace)
    window.mode_tabs.setCurrentIndex(1)
    panel = window.statistics_panel
    panel.pinned_fields = ["csv_light_condition", "csv_collision_target", "csv_damage_level", "csv_workflow_status"]
    panel._row_key, panel._column_key = "csv_light_condition", "csv_damage_level"
    panel._item_key = "csv_light_condition"
    panel.status.setCurrentIndex(panel.status.findData("all"))
    panel.conditions = [StatsCondition("csv_workflow_status", "completed")]
    panel.view_tabs.setCurrentIndex(2)
    window.show()
    return app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(description="원본 CSV를 보존하는 AVAT 통계 샘플 프로젝트")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    repository, created = import_project(args.source, args.workspace)
    cases = repository.load()
    print("created" if created else "existing", repository.workspace)
    print("records", len(cases), "source_status", dict(Counter(case.values["csv_workflow_status"] for case in cases)))
    return open_statistics(repository.workspace) if args.open else 0


if __name__ == "__main__":
    raise SystemExit(main())
