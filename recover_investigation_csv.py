"""Restore an AVAT investigation CSV into a NEW 작업관리.db.

Place this file beside the investigator source directory. Python 3.11+ is
required; the imported AVAT form/model modules use the standard library,
not Qt or the detector libraries. Original CSV/media/existing DBs are untouched.

python recover_investigation_csv.py --source investigation.csv --workspace D:/AVAT --dry-run
python recover_investigation_csv.py --source investigation.csv --workspace D:/AVAT
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import ntpath
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from investigator.csv_specification import METADATA_HEADERS
    from investigator.form_definition import FormDefinition, new_field
    from investigator.models import InvestigationCase
    from investigator.video_import import parse_accident_video_name, scan_video_tree
except ImportError as exc:
    raise SystemExit("AVAT 소스의 investigator 폴더 옆에 이 스크립트를 두고 Python 3.11 이상으로 실행하세요.\n" + str(exc)) from exc

NULLS = {"", "null", "none", "nan"}
META = {*METADATA_HEADERS, "report_accident_number"}


class CsvRecoveryError(ValueError):
    pass


def _filled(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().casefold() != "null"
    if isinstance(value, dict):
        return any(_filled(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_filled(item) for item in value)
    return True  # Zero and False are meaningful entered values.


def _path_key(path: str) -> str:
    return ntpath.normpath(path.replace("/", "\\")).casefold()


@dataclass(frozen=True, slots=True)
class _DuplicateCaseConsolidation:
    cases: list[InvestigationCase]
    uid_mapping: dict[str, str]
    removed_count: int
    changed_uids: frozenset[str]


def _consolidate_duplicate_cases(cases: list[InvestigationCase]) -> _DuplicateCaseConsolidation:
    """Consolidate CSV recovery rows only; the running app never invokes this.

    Rank by required missing count, entered field count, then CSV row order.
    Keep the winner's survey, UID, workflow, dates and existing video records.
    Additional distinct full video paths from losing cases stay attached, with
    their annotations. Never compare video basenames or combine survey values.
    """
    groups: dict[tuple[str, str], list[tuple[int, InvestigationCase]]] = defaultdict(list)
    for index, case in enumerate(cases):
        institution = str(case.values.get("institution") or "").strip().casefold()
        number = str(case.values.get("accident_classification") or "").strip()
        if institution and number:
            groups[institution, number].append((index, case))
    replacements: dict[str, InvestigationCase] = {}
    uid_mapping: dict[str, str] = {}
    changed: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        _, winner = min(group, key=lambda item: (
            len(item[1].missing),
            -sum(_filled(value) for key, value in item[1].values.items()
                 if key not in {"institution", "accident_classification"}),
            item[0],
        ))
        retained = copy.copy(winner)
        retained.values = copy.deepcopy(winner.values)
        retained.videos = list(winner.videos)
        retained.markers_by_video = copy.deepcopy(winner.markers_by_video)
        retained.rotation_by_video = copy.deepcopy(winner.rotation_by_video)
        retained.objects_by_video = copy.deepcopy(winner.objects_by_video)
        known = {_path_key(path) for path in retained.videos}
        for _, loser in group:
            if loser.uid == winner.uid:
                continue
            uid_mapping[loser.uid] = winner.uid
            for path in loser.videos:
                key = _path_key(path)
                if key in known:
                    continue
                known.add(key)
                retained.videos.append(path)
                for attribute in ("markers_by_video", "rotation_by_video", "objects_by_video"):
                    source = getattr(loser, attribute)
                    target = getattr(retained, attribute)
                    for metadata_path, value in source.items():
                        if metadata_path != "__case__" and _path_key(metadata_path) == key:
                            target[path] = copy.deepcopy(value)
        for _, case in group:
            replacements[case.uid] = retained
        changed.add(winner.uid)
    # Keep the group's original list position, without renumbering accidents.
    result: list[InvestigationCase] = []
    seen: set[str] = set()
    for case in cases:
        retained = replacements.get(case.uid, case)
        if retained.uid not in seen:
            seen.add(retained.uid)
            result.append(retained)
    return _DuplicateCaseConsolidation(result, uid_mapping, len(uid_mapping), frozenset(changed))


def cell(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value.casefold() in NULLS else value


def read_csv(path: Path, encoding: str | None = None) -> tuple[list[str], list[dict[str, str]]]:
    text = None
    for codec in ([encoding] if encoding else ["utf-8-sig", "cp949"]):
        try:
            text = path.read_text(encoding=codec)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise CsvRecoveryError("CSV 인코딩을 읽지 못했습니다. --encoding 옵션을 지정하세요.")
    reader = csv.DictReader(text.splitlines(keepends=True))
    headers = reader.fieldnames or []
    if not headers or len(headers) != len(set(headers)) or any(not header.strip() for header in headers):
        raise CsvRecoveryError("CSV 칼럼명이 비어 있거나 중복됩니다.")
    rows = []
    for number, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            raise CsvRecoveryError(f"CSV {number}행의 칼럼 수가 맞지 않습니다.")
        if any(cell(value) for value in row.values()):
            rows.append(row)
    if not rows:
        raise CsvRecoveryError("CSV에 복구할 사고가 없습니다.")
    return headers, rows


def columns(record: dict) -> list[str]:
    return list(dict.fromkeys(filter(None, [record.get("csv_column"), *record.get("aliases", []), record["key"]])))


def build_form(headers: list[str], form_json: Path | None) -> tuple[FormDefinition, list[str]]:
    if form_json:
        raw = json.loads(form_json.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise CsvRecoveryError("조사 양식 JSON은 객체여야 합니다.")
        definition = FormDefinition.from_dict(raw.get("form_definition", raw))
    else:
        definition = FormDefinition.default()
    known = set(META)
    for record in definition.records():
        known.update(columns(record))
        for choice in record["choices"]:
            known.update(filter(None, [choice.get("csv_column"), *choice.get("csv_aliases", [])]))
    unknown = [header for header in headers if header not in known]
    if unknown:
        fields = []
        for header in unknown:
            record = new_field()
            record.update(key="csv_recovery_" + hashlib.sha256(header.encode()).hexdigest()[:16],
                          label=header, csv_column=header)
            fields.append(record)
        definition.tabs.append({"key": "csv_recovery", "title": "CSV 복구 추가 항목", "short_title": "CSV 복구",
                                "columns": 1, "fields": fields})
    definition.validate()
    return definition, unknown


def choice_value(record: dict, token: str) -> str:
    matches = []
    for choice in record["choices"]:
        aliases = {choice["value"], choice["label"], choice.get("id", choice["value"]),
                   *choice.get("value_aliases", [])}
        if record["key"] == "collision_targets":
            aliases.add(choice["label"].split(" · ", 1)[-1])
        if token in aliases:
            matches.append(choice["value"])
    if len(set(matches)) > 1:
        raise CsvRecoveryError(f"{record['label']}: '{token}'이 여러 선택지에 대응합니다. --form-json으로 원래 양식을 지정하세요.")
    return matches[0] if matches else token


def row_values(row: dict[str, str], definition: FormDefinition) -> dict[str, Any]:
    values = {}
    for record in definition.records():
        text = next((cell(row[column]) for column in columns(record) if column in row and cell(row[column])), "")
        # Undo only the spreadsheet-safety prefix added by AVAT's CSV exporter.
        if len(text) >= 2 and text[0] == "'" and text[1] in "=+-@":
            text = text[1:]
        kind, key = record["kind"], record["key"]
        selected = []
        has_binary = False
        for choice in record["choices"]:
            for column in filter(None, [choice.get("csv_column"), *choice.get("csv_aliases", [])]):
                if column not in row:
                    continue
                has_binary = True
                value = cell(row[column]).casefold()
                if value in {"1", "1.0", "true", "yes", "y", "유"}:
                    selected.append(choice["value"])
                elif value not in {"", "0", "0.0", "false", "no", "n", "무"}:
                    raise CsvRecoveryError(f"{column}: 선택 칼럼에 0/1이 아닌 값이 있습니다: {row[column]}")
        if selected:
            values[key] = list(dict.fromkeys(selected)) if kind == "multi" else selected[0]
        elif text:
            if key == "incident_damage_unknown" and text in {"0", "1", "0.0", "1.0"}:
                values[key] = "확인불가" if text in {"1", "1.0"} else "확인 가능"
            elif kind == "multi":
                values[key] = list(dict.fromkeys(choice_value(record, token.strip()) for token in text.split("|") if token.strip()))
            else:
                values[key] = choice_value(record, text) if kind == "single" else text
        elif has_binary and kind == "multi":
            # No selection is intentionally left unset, not fabricated as input.
            continue
    return values


def json_cell(row: dict, key: str) -> dict:
    text = cell(row.get(key))
    if not text:
        return {}
    result = json.loads(text)
    if not isinstance(result, dict):
        raise CsvRecoveryError(f"{key}는 JSON 객체여야 합니다.")
    return result


def write_database(path: Path, workspace: Path, cases: list[InvestigationCase], definition: FormDefinition) -> None:
    """Write the AVAT v4 payload without importing video/GUI dependencies."""
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    def stored_path(value: str) -> str:
        if value == "__case__":
            return value
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                return os.path.relpath(candidate, workspace).replace("\\", "/")
            except ValueError:
                # An explicitly allowed missing video may be on another drive.
                return value.replace("\\", "/")
        return value.replace("\\", "/")

    metadata = {"schema_version": 4, "path_storage": "workspace-relative-v1",
                "form_definition": definition.to_dict(), "initial_form_definition": definition.to_dict(),
                "active_case_uid": cases[0].uid if cases else None, "auto_import_exclusions": []}
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            PRAGMA application_id=1096171860;
            PRAGMA user_version=1;
            CREATE TABLE workspace_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE cases(uid TEXT PRIMARY KEY, position INTEGER NOT NULL CHECK(position>=0),
                institution TEXT NOT NULL, accident_number TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX cases_position ON cases(position);
            CREATE INDEX cases_identity ON cases(institution, accident_number);
            CREATE INDEX cases_status ON cases(status);
        """)
        with connection:
            connection.executemany("INSERT INTO workspace_metadata VALUES (?,?)",
                                   [(key, encode(value)) for key, value in metadata.items()])
            for position, case in enumerate(cases):
                payload = case.to_dict()
                payload["videos"] = [stored_path(value) for value in payload["videos"]]
                for key in ("markers_by_video", "rotation_by_video", "objects_by_video"):
                    if key in payload:
                        payload[key] = {stored_path(value): data for value, data in payload[key].items()}
                # Verify round-trip model decoding before committing any output.
                InvestigationCase.from_dict(json.loads(encode(payload)), form_definition=definition)
                connection.execute("INSERT INTO cases VALUES (?,?,?,?,?,?)", (
                    case.uid, position, case.values["institution"], case.values["accident_classification"],
                    case.status, encode(payload)))
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CsvRecoveryError("생성 DB의 무결성 검사에 실패했습니다.")
    finally:
        connection.close()


def restore_csv(source: Path, workspace: Path, output: Path, *, encoding: str | None = None,
                form_json: Path | None = None, allow_missing: bool = False, dry_run: bool = False) -> dict:
    source, workspace, output = source.resolve(), workspace.resolve(), output.resolve()
    if not source.is_file():
        raise CsvRecoveryError(f"CSV 파일이 없습니다: {source}")
    if output.exists() or output == source or output.suffix.casefold() != ".db":
        raise CsvRecoveryError(f"기존 파일을 덮어쓰거나 .db 이외의 파일로 저장할 수 없습니다: {output}")
    if any(Path(str(output) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise CsvRecoveryError("출력 DB의 보조 파일이 남아 있습니다. AVAT를 종료하고 별도의 출력 위치를 선택하세요.")
    headers, rows = read_csv(source, encoding)
    if not {"accident_classification", "case_id"}.intersection(headers):
        raise CsvRecoveryError("조사 CSV가 아닙니다. accident_classification 또는 case_id 칼럼이 필요합니다.")
    definition, custom_columns = build_form(headers, form_json)
    video_root = workspace / "01_사고동영상"
    if (not video_root.is_dir() or video_root.is_symlink()
            or getattr(video_root.stat(), "st_file_attributes", 0) & 0x400):
        raise CsvRecoveryError(f"01_사고동영상 폴더를 찾을 수 없습니다: {video_root}")
    videos, _ = scan_video_tree(video_root)
    by_name: dict[str, list[str]] = {}
    by_number: dict[str, list[str]] = {}
    for path in videos:
        by_name.setdefault(path.name.casefold(), []).append(str(path))
        parsed = parse_accident_video_name(path)
        if parsed:
            by_number.setdefault(parsed[0], []).append(str(path))
    warnings, cases, downgraded = [], [], []

    def map_video(old: str) -> str:
        choices = by_name.get(ntpath.basename(old).casefold(), [])
        if len(choices) == 1:
            return choices[0]
        if len(choices) > 1:
            raise CsvRecoveryError(f"동명 영상이 여러 개여서 선택할 수 없습니다: {old}")
        if not allow_missing:
            raise CsvRecoveryError(f"영상을 찾지 못했습니다: {old} (연결을 보류하려면 --allow-missing-videos)")
        warnings.append(f"미연결 유지: {old}")
        return old

    for line, row in enumerate(rows, 2):
        try:
            values = row_values(row, definition)
            fallback_id = cell(row.get("case_id"))
            if "accident_classification" not in values and "-" in fallback_id:
                institution, number = fallback_id.split("-", 1)
                values.setdefault("institution", institution.lower())
                values["accident_classification"] = number
            values["institution"] = str(values.get("institution") or "samsung").lower()
            number = str(values.get("accident_classification") or "").strip()
            if not number or re.fullmatch(r"\d+(?:\.\d+)?[eE][+-]?\d+", number):
                raise CsvRecoveryError("사고번호가 없거나 과학 표기입니다. 원본 CSV의 사고번호를 확인하세요.")
            markers = json_cell(row, "video_markers")
            old_videos = [part.strip() for part in cell(row.get("video_files")).split("|") if part.strip()]
            old_videos = list(dict.fromkeys([*old_videos, *(key for key in markers if key != "__case__")]))
            if not old_videos:
                old_videos = [key for key in markers if key != "__case__"] or by_number.get(number, [])
            mapping = {old: map_video(old) for old in old_videos}
            mapped = list(dict.fromkeys(mapping.values()))
            mapped_markers = {}
            for old, value in markers.items():
                target = "__case__" if old == "__case__" else mapping.get(old) or map_video(old)
                if target in mapped_markers and mapped_markers[target] != value:
                    raise CsvRecoveryError(f"같은 영상으로 연결되는 마커 기록이 서로 다릅니다: {old}")
                mapped_markers[target] = value
            active_old = cell(row.get("active_video_file"))
            active = mapping.get(active_old, mapped[0] if mapped else None)
            if active_old and active_old not in mapping:
                warnings.append(f"{line}행: active_video_file이 영상 목록에 없어 첫 영상을 선택했습니다.")
            if not markers and (cell(row.get("trigger_frame")) or cell(row.get("collision_frames"))):
                target = active or "__case__"
                trigger = cell(row.get("trigger_frame"))
                mapped_markers[target] = {"trigger_frame": int(trigger) if trigger else None,
                    "collision_frames": [int(value) for value in cell(row.get("collision_frames")).split("|") if value]}
            case = InvestigationCase(values=values, videos=mapped,
                active_video_index=mapped.index(active) if active in mapped else 0,
                markers_by_video=mapped_markers, form_definition=definition, defaults_applied=True)
            status = cell(row.get("workflow_status")).casefold()
            if status in {"completed", "완료"}:
                if case.can_complete:
                    case.complete()
                else:
                    downgraded.append({"row": line, "case_id": case.display_id,
                                       "missing": case.missing, "errors": case.errors})
            elif status not in {"", "draft", "임시저장", "미완료"}:
                warnings.append(f"{line}행: 알 수 없는 완료 상태 '{status}'를 draft로 복구했습니다.")
            if case.errors:
                warnings.append(f"{line}행: 입력 형식 확인 필요: {case.errors}")
            cases.append(case)
        except (ValueError, TypeError, KeyError) as exc:
            raise CsvRecoveryError(f"CSV {line}행: {exc}") from exc
    consolidation = _consolidate_duplicate_cases(cases)
    cases = consolidation.cases
    report = {"source": str(source), "output": str(output), "dry_run": dry_run,
              "csv_rows": len(rows), "restored_cases": len(cases), "duplicate_cases_removed": consolidation.removed_count,
              "video_links": sum(len(case.videos) for case in cases),
              "completed": sum(case.status == "completed" for case in cases),
              "completion_downgraded": downgraded, "custom_columns": custom_columns,
              "warnings": warnings,
              "limitations": "CSV에 없는 객체 박스·회전·기존 UID·원래 저장 시각은 복원할 수 없습니다."}
    if dry_run:
        return report
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".avat-csv-recovery-") as staging:
        staging_path = Path(staging) / "작업관리.db"
        write_database(staging_path, workspace, cases, definition)
        if os.name == "nt":
            os.rename(staging_path, output)
        else:
            os.link(staging_path, output)
            staging_path.unlink()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path, help="AVAT 조사 CSV")
    parser.add_argument("--workspace", required=True, type=Path, help="01_사고동영상이 들어 있는 작업 폴더")
    parser.add_argument("--output", type=Path, help="새 DB 경로. 기존 파일은 덮어쓰지 않음")
    parser.add_argument("--encoding", help="CSV 인코딩. 기본 UTF-8/CP949 자동 확인")
    parser.add_argument("--form-json", type=Path, help="사용자 정의 조사 양식 JSON(선택)")
    parser.add_argument("--allow-missing-videos", action="store_true", help="없는 영상은 미연결 상태로 조사값 복구")
    parser.add_argument("--dry-run", action="store_true", help="저장하지 않고 복구 계획만 확인")
    args = parser.parse_args()
    output = args.output or args.workspace / "01_사고동영상/작업관리/작업관리.db"
    try:
        report = restore_csv(args.source, args.workspace, output, encoding=args.encoding, form_json=args.form_json,
                             allow_missing=args.allow_missing_videos, dry_run=args.dry_run)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"CSV 복구 중단: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
