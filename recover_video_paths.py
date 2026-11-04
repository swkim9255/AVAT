"""AVAT DB 영상 경로 복구 도구. Python 표준 라이브러리만 필요합니다.

먼저 AVAT를 종료하세요. 원본 DB와 영상은 변경하지 않으며 기존 출력 파일도
덮어쓰지 않습니다. --dry-run으로 먼저 확인할 수 있습니다.

python recover_video_paths.py --source investigations.db --workspace D:/AVAT --dry-run
python recover_video_paths.py --source investigations.db --workspace D:/AVAT \
    --conflict most-complete --output D:/AVAT/01_사고동영상/작업관리/작업관리.db

미입력 연결은 작업 폴더의 '미입력 삭제 영상/investigations.db'에 원래 형식으로
분리합니다. 실제 영상 파일은 옮기거나 삭제하지 않습니다.
같은 이름의 실제 파일이 여러 개이거나 파일을 못 찾으면 중단합니다. 여러 옛
경로가 하나의 파일에 대응할 때 중복 연결은 하나로 만들며, 주석 충돌은 기본적으로
중단합니다. most-complete 옵션은 한 옛 경로의 주석 세트를 통째로 선택합니다.
사고 UID·조사 입력·사고 순서·완료 상태는 바꾸거나 통합하지 않습니다.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import ntpath
import os
import sqlite3
import stat
import sys
import tempfile
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any

VIDEO_ROOT = "01_사고동영상"
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".webm"}
IGNORED = {"작업관리", "작업관리db", "systems", "삭제사고", "삭제영상", "models", "runtime", "_internal"}
PATH_FIELDS = ("markers_by_video", "rotation_by_video", "objects_by_video")
APPLICATION_ID = 0x41564154

# Default form snapshot for v1 DBs with no saved form. Standard-library-only
# decoding keeps this utility independent of investigator/PySide6 and network.
DEFAULT_FORM_ZLIB_BASE64 = (
    "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"
    "n0pSd/xpu2nFtlbcbsq2dcnu58uv/g+s/VtY"
)


class RecoveryError(ValueError):
    pass


def normalize_legacy_case(case: dict) -> dict:
    """Attach old case-level markers to their original active video."""
    result = copy.deepcopy(case)
    legacy = result.pop("markers", None)
    if isinstance(legacy, dict) and not result.get("markers_by_video"):
        videos = result.get("videos", [])
        active = int(result.get("active_video_index") or 0)
        key = videos[min(max(active, 0), len(videos) - 1)] if videos else "__case__"
        result["markers_by_video"] = {key: legacy}
    return result


def _name(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise RecoveryError("DB에 올바르지 않은 영상 경로가 있습니다.")
    return ntpath.basename(path.replace("/", "\\")).casefold()


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def index_videos(workspace: Path) -> dict[str, list[str]]:
    root = workspace / VIDEO_ROOT
    if not root.is_dir() or _linked(root):
        raise RecoveryError(f"영상 폴더가 없거나 링크·정션입니다: {root}")
    index: dict[str, list[str]] = defaultdict(list)
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path.name.startswith(".") or _linked(path):
                continue
            if path.is_dir():
                if path.name.casefold() not in IGNORED:
                    pending.append(path)
            elif path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS:
                index[path.name.casefold()].append(path.relative_to(workspace).as_posix())
    return dict(index)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _entered(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_entered(item) for item in value.values())
    if isinstance(value, list):
        return any(_entered(item) for item in value)
    return not _empty(value) and not (isinstance(value, str) and not value.strip())


def split_unentered(case: dict) -> tuple[dict | None, dict | None]:
    """Separate blank links, without mistaking identifier defaults for work."""
    survey = any(_entered(value) for key, value in case.get("values", {}).items()
                 if key not in {"institution", "accident_classification"})

    def worked(path: str) -> bool:
        markers = case.get("markers_by_video", {})
        objects = case.get("objects_by_video", {}).get(path, {})
        return (survey or _entered(markers.get(path, {})) or _entered(markers.get("__case__", {}))
                or bool(case.get("rotation_by_video", {}).get(path))
                or bool(objects.get("objects")) or bool(objects.get("runs")))

    videos = case.get("videos", [])
    keep = [path for path in videos if worked(path)]
    blank = [path for path in videos if not worked(path)]

    def subset(paths: list[str]) -> dict:
        result = copy.deepcopy(case)
        result["videos"] = paths
        active = case.get("active_video_index", 0)
        selected = videos[active] if isinstance(active, int) and 0 <= active < len(videos) else None
        result["active_video_index"] = paths.index(selected) if selected in paths else 0
        keys = {ntpath.normpath(path.replace("/", "\\")).casefold() for path in paths}
        for field in PATH_FIELDS:
            if field in result:
                result[field] = {path: value for path, value in result[field].items()
                                 if path == "__case__" or ntpath.normpath(path.replace("/", "\\")).casefold() in keys}
        return result

    if not videos:
        return (copy.deepcopy(case), None) if survey or worked("__case__") else (None, copy.deepcopy(case))
    return (subset(keep) if keep else None, subset(blank) if blank else None)


def _score(case: dict, path: str) -> tuple[int, int]:
    markers = case.get("markers_by_video", {}).get(path, {})
    points = markers.get("speed_points", []) or []
    entered = int(markers.get("trigger_frame") is not None) + bool(markers.get("collision_frames"))
    entered += sum(1 + int(point.get("distance_m") is not None) for point in points)
    entered += bool(case.get("rotation_by_video", {}).get(path))
    objects = case.get("objects_by_video", {}).get(path, {}).get("objects", [])
    entered += sum(bool(obj.get("reviewed")) or obj.get("source") == "manual" for obj in objects)
    return entered, len(objects)


def remap_case(case: dict, mapping: dict[str, str], policy: str) -> tuple[dict, list[dict]]:
    result = copy.deepcopy(case)
    groups: dict[str, list[str]] = defaultdict(list)
    paths = list(case.get("videos", []))
    for field in PATH_FIELDS:
        paths.extend(path for path in case.get(field, {}) if path != "__case__")
    for path in dict.fromkeys(paths):
        groups[mapping[path]].append(path)
    old_videos = case.get("videos", [])
    result["videos"] = list(dict.fromkeys(mapping[path] for path in old_videos))
    active = case.get("active_video_index", 0)
    if old_videos:
        if not isinstance(active, int) or not 0 <= active < len(old_videos):
            raise RecoveryError(f"잘못된 활성 영상 인덱스: {case.get('uid')}")
        result["active_video_index"] = result["videos"].index(mapping[old_videos[active]])
    for field in PATH_FIELDS:
        if field in case:
            result[field] = {"__case__": copy.deepcopy(case[field]["__case__"])} if "__case__" in case[field] else {}
    conflicts = []
    for target, aliases in groups.items():
        conflicting = []
        for field in PATH_FIELDS:
            values = [case.get(field, {})[path] for path in aliases
                      if path in case.get(field, {}) and not _empty(case[field][path])]
            if values and any(value != values[0] for value in values[1:]):
                conflicting.append(field)
        winner = max(aliases, key=lambda path: _score(case, path)) if conflicting else None
        if conflicting:
            conflicts.append({"case_uid": case.get("uid"), "target": target, "fields": conflicting,
                              "old_paths": aliases, "selected": winner if policy == "most-complete" else None})
        for field in PATH_FIELDS:
            records = case.get(field, {})
            if conflicting:
                # Do not mix the T marker from one revision with the collision
                # marker or detector labels from another revision.
                if winner in records:
                    result[field][target] = copy.deepcopy(records[winner])
            else:
                present = [records[path] for path in aliases if path in records]
                if present:
                    result[field][target] = copy.deepcopy(next((v for v in present if not _empty(v)), present[0]))
    return result, conflicts


def _write_database(connection: sqlite3.Connection, temporary: Path, rows: list[tuple[str, dict]], updates: dict) -> None:
    target = sqlite3.connect(temporary)
    try:
        connection.backup(target)
        with target:
            retained = {uid for uid, _ in rows}
            deleted = [uid for uid, in target.execute("SELECT uid FROM cases") if uid not in retained]
            target.executemany("DELETE FROM cases WHERE uid=?", [(uid,) for uid in deleted])
            for uid, case in rows:
                target.execute("UPDATE cases SET payload=? WHERE uid=?", (json.dumps(case, ensure_ascii=False), uid))
            for key, value in updates.items():
                target.execute("INSERT OR REPLACE INTO workspace_metadata VALUES(?,?)", (key, json.dumps(value)))
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryError("출력 DB의 무결성 검사에 실패했습니다.")
    finally:
        target.close()
    with temporary.open("r+b") as handle:
        os.fsync(handle.fileno())


def _publish(temporary: Path, output: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, output)
    else:
        os.link(temporary, output)
        temporary.unlink()


def recover(source: Path, workspace: Path, output: Path, *, conflict: str = "error", dry_run: bool = False,
            archive_output: Path | None = None) -> dict:
    source, workspace, output = source.resolve(), workspace.resolve(), output.resolve()
    archive_output = (archive_output or workspace / "미입력 삭제 영상" / "investigations.db").resolve()
    if archive_output in {source, output}:
        raise RecoveryError("미입력 DB의 위치는 원본·복구 DB와 달라야 합니다.")
    if conflict not in {"error", "most-complete"}:
        raise RecoveryError("알 수 없는 충돌 처리 옵션입니다.")
    if not source.is_file():
        raise RecoveryError(f"원본 DB가 없습니다: {source}")
    if source == output or output.exists():
        raise RecoveryError(f"원본 또는 기존 파일을 덮어쓸 수 없습니다: {output}")
    for database in (source, output):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise RecoveryError(f"AVAT를 종료하고 DB 보조 파일 상태를 확인하세요: {sidecar}")
    index = index_videos(workspace)
    connection = sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)
    temporary: Path | None = None
    archive_temporary: Path | None = None
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise RecoveryError("지원하는 AVAT SQLite DB가 아닙니다.")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RecoveryError("지원하지 않는 SQLite 테이블 버전입니다.")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RecoveryError("원본 DB의 무결성 검사에 실패했습니다.")
        metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM workspace_metadata")}
        raw_version = metadata.get("schema_version")
        version = int(raw_version) if isinstance(raw_version, str) and raw_version in {"1", "3", "4"} else raw_version
        if type(version) is not int or version not in {1, 3, 4}:
            raise RecoveryError(f"지원하지 않는 데이터 버전입니다: {raw_version!r} (지원: 1, 3, 4)")
        legacy_metadata = {}
        if version == 1:
            definition = metadata.get("form_definition")
            if definition is None:
                definition = json.loads(zlib.decompress(base64.b64decode(DEFAULT_FORM_ZLIB_BASE64)))
            legacy_metadata = {"schema_version": 1, "form_definition": definition,
                               "initial_form_definition": metadata.get("initial_form_definition") or copy.deepcopy(definition),
                               "auto_import_exclusions": metadata.get("auto_import_exclusions", [])}
        rows = [(uid, json.loads(payload)) for uid, payload in connection.execute("SELECT uid,payload FROM cases ORDER BY position")]
        paths = set()
        kept_rows, archived_rows = [], []
        for uid, case in rows:
            if case.get("uid") != uid:
                raise RecoveryError("사고 UID와 payload가 다릅니다.")
            if version == 1:
                case = normalize_legacy_case(case)
            known = {ntpath.normpath(path.replace("/", "\\")).casefold() for path in case.get("videos", [])}
            for field in PATH_FIELDS:
                if any(path != "__case__" and ntpath.normpath(path.replace("/", "\\")).casefold() not in known
                       for path in case.get(field, {})):
                    raise RecoveryError(f"영상 목록에 없는 주석 경로가 있어 확인이 필요합니다: {uid}, {field}")
            kept, archived = split_unentered(case)
            if archived is not None:
                archived_rows.append((uid, archived))
            if kept is None:
                continue
            case = kept
            kept_rows.append((uid, kept))
            paths.update(case.get("videos", []))
            for field in PATH_FIELDS:
                paths.update(path for path in case.get(field, {}) if path != "__case__")
        mapping, problems = {}, []
        for old in sorted(paths):
            candidates = index.get(_name(old), [])
            if len(candidates) == 1:
                mapping[old] = candidates[0]
            else:
                problems.append({"path": old, "reason": "없음" if not candidates else "동명 파일 여러 개", "candidates": candidates})
        if problems:
            raise RecoveryError("일부 영상을 확정할 수 없습니다. 출력 DB를 만들지 않았습니다.\n"
                                + json.dumps(problems, ensure_ascii=False, indent=2))
        converted, conflicts = [], []
        for uid, case in kept_rows:
            updated, found = remap_case(case, mapping, conflict)
            converted.append((uid, updated))
            conflicts.extend(found)
        report = {"source": str(source), "output": str(output), "dry_run": dry_run,
                  "source_schema_version": version, "output_schema_version": 4,
                  "default_form_added": version == 1 and metadata.get("form_definition") is None,
                  "cases": len(rows), "path_mapping": mapping,
                  "video_links_before": sum(len(case.get("videos", [])) for _, case in rows),
                  "video_links_after": sum(len(case.get("videos", [])) for _, case in converted),
                  "cases_after": len(converted),
                  "archive_output": str(archive_output) if archived_rows else None,
                  "archived_cases": len(archived_rows),
                  "archived_video_links": sum(len(case.get("videos", [])) for _, case in archived_rows),
                  "conflict_policy": conflict, "conflicts": conflicts}
        if conflicts and conflict == "error":
            raise RecoveryError("같은 파일로 연결되는 작업 기록이 충돌합니다. 출력 DB를 만들지 않았습니다.\n"
                                "기록 선택을 허용하려면 --conflict most-complete를 명시하세요.\n"
                                + json.dumps(conflicts, ensure_ascii=False, indent=2))
        if dry_run:
            return report
        if archived_rows:
            if archive_output.exists() or any(Path(str(archive_output) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise RecoveryError(f"미입력 DB를 덮어쓸 수 없습니다: {archive_output}")
            archive_output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=archive_output.parent, prefix=".avat-unentered-", suffix=".db", delete=False) as handle:
                archive_temporary = Path(handle.name)
            active = metadata.get("active_case_uid")
            archive_active = active if active in {uid for uid, _ in archived_rows} else archived_rows[0][0]
            _write_database(connection, archive_temporary, archived_rows,
                            {**legacy_metadata, "active_case_uid": archive_active})
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".avat-recovery-", suffix=".db", delete=False) as handle:
            temporary = Path(handle.name)
        exclusions = []
        for path in metadata.get("auto_import_exclusions", []):
            matches = index.get(_name(path), [])
            if len(matches) == 1:
                exclusions.append(matches[0])
            elif ntpath.isabs(path):
                try:
                    exclusions.append(ntpath.relpath(path, str(workspace)).replace("\\", "/"))
                except ValueError:
                    exclusions.append(path)
            else:
                exclusions.append(path)
        active = metadata.get("active_case_uid")
        used_targets = set(mapping.values())
        for _, archived in archived_rows:
            for path in archived.get("videos", []):
                exclusions.extend(target for target in index.get(_name(path), []) if target not in used_targets)
        exclusions = list(dict.fromkeys(exclusions))
        current = active if active in {uid for uid, _ in converted} else converted[0][0] if converted else None
        _write_database(connection, temporary, converted,
                        {**legacy_metadata, "schema_version": 4, "path_storage": "workspace-relative-v1",
                         "auto_import_exclusions": exclusions, "active_case_uid": current})
        if archive_temporary is not None:
            _publish(archive_temporary, archive_output)
            archive_temporary = None
        _publish(temporary, output)
        temporary = None
        return report
    finally:
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if archive_temporary is not None:
            archive_temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=Path("investigations.db"), help="원본 DB (변경하지 않음)")
    parser.add_argument("--workspace", type=Path, required=True, help="01_사고동영상 폴더가 들어 있는 작업 폴더")
    parser.add_argument("--output", type=Path, help="새 출력 DB. 기존 파일은 덮어쓰지 않음")
    parser.add_argument("--conflict", choices=("error", "most-complete"), default="error", help="작업 기록 충돌 처리")
    parser.add_argument("--dry-run", action="store_true", help="복구 계획만 확인. DB를 생성하지 않음")
    parser.add_argument("--archive-output", type=Path, help="미입력 연결 보관 DB (기본: 작업 폴더/미입력 삭제 영상/investigations.db)")
    args = parser.parse_args()
    output = args.output or args.workspace / VIDEO_ROOT / "작업관리" / "작업관리.db"
    try:
        report = recover(args.source, args.workspace, output, conflict=args.conflict, dry_run=args.dry_run,
                         archive_output=args.archive_output)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(f"복구 중단: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
