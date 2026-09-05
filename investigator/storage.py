"""Atomic JSON persistence and deterministic CSV export."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from investigator.models import SCHEMA_VERSION, InvestigationCase
from investigator.schema import csv_headers, values_to_csv

DATABASE_FILENAME = "investigations.json"
AUTO_IMPORT_EXCLUSIONS_KEY = "auto_import_exclusions"


class WorkspaceError(RuntimeError):
    pass


def _safe_csv_text(value: Any) -> str:
    text = str(value) if value is not None else ""
    if not text.strip():
        return "null"
    # Prevent spreadsheet applications from interpreting user text as formulae.
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


class CaseRepository:
    """Persist all investigation cases in one user-selected workspace."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.database_path = self.workspace / DATABASE_FILENAME
        self.auto_import_exclusions: set[str] = set()

    def ensure_workspace(self) -> None:
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"작업 폴더를 만들 수 없습니다: {self.workspace}") from exc
        if not self.workspace.is_dir():
            raise WorkspaceError(f"작업 폴더를 만들 수 없습니다: {self.workspace}")

    def load(self) -> list[InvestigationCase]:
        self.ensure_workspace()
        if not self.database_path.exists():
            self.auto_import_exclusions = set()
            return []
        try:
            data = json.loads(self.database_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                f"조사 데이터 파일을 읽을 수 없습니다: {self.database_path}"
            ) from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceError("지원하지 않는 조사 데이터 형식입니다.")
        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list):
            raise WorkspaceError("조사 데이터의 cases 항목이 올바르지 않습니다.")
        raw_exclusions = data.get(AUTO_IMPORT_EXCLUSIONS_KEY, [])
        if not isinstance(raw_exclusions, list) or any(
            not isinstance(path, str) or not path.strip() for path in raw_exclusions
        ):
            raise WorkspaceError(
                f"조사 데이터의 {AUTO_IMPORT_EXCLUSIONS_KEY} 항목이 올바르지 않습니다."
            )
        try:
            exclusions = {
                str(Path(path).expanduser().resolve()).casefold() for path in raw_exclusions
            }
        except (OSError, ValueError) as exc:
            raise WorkspaceError(
                f"조사 데이터의 {AUTO_IMPORT_EXCLUSIONS_KEY} 경로가 올바르지 않습니다."
            ) from exc
        cases: list[InvestigationCase] = []
        for index, item in enumerate(raw_cases):
            if not isinstance(item, dict):
                raise WorkspaceError(f"cases[{index}] 항목은 JSON 객체여야 합니다.")
            try:
                case = InvestigationCase.from_dict(item)
            except (TypeError, ValueError, OSError) as exc:
                raise WorkspaceError(f"cases[{index}] 조사 데이터가 올바르지 않습니다.") from exc
            cases.append(case)
        seen: set[tuple[str, str]] = set()
        seen_uids: set[str] = set()
        for case in cases:
            if case.uid in seen_uids:
                raise WorkspaceError(f"중복 사고 UID가 있습니다: {case.uid}")
            seen_uids.add(case.uid)
            identity = (
                str(case.values.get("institution", "")).casefold(),
                str(case.values.get("accident_classification", "")),
            )
            if all(identity) and identity in seen:
                raise WorkspaceError(f"중복 사고 식별자가 있습니다: {case.display_id}")
            seen.add(identity)
            if case.status == "completed":
                if case.missing or case.errors:
                    raise WorkspaceError(
                        f"완료 상태인 사고에 미입력 또는 오류가 있습니다: {case.display_id}"
                    )
                for markers in case.markers_by_video.values():
                    trigger = markers.get("trigger_frame")
                    collisions = markers.get("collision_frames", [])
                    if (
                        trigger is not None
                        and collisions
                        and int(trigger) > min(map(int, collisions))
                    ):
                        raise WorkspaceError(
                            f"완료 상태인 사고의 마커 순서가 올바르지 않습니다: {case.display_id}"
                        )
        self.auto_import_exclusions = exclusions
        return cases

    def save(
        self,
        cases: list[InvestigationCase],
        *,
        active_case_uid: str | None = None,
        auto_import_exclusions: set[str] | None = None,
    ) -> None:
        self.ensure_workspace()
        seen: set[tuple[str, str]] = set()
        seen_uids: set[str] = set()
        for case in cases:
            if case.uid in seen_uids:
                raise WorkspaceError(f"중복 사고 UID는 저장할 수 없습니다: {case.uid}")
            seen_uids.add(case.uid)
            identity = (
                str(case.values.get("institution", "")).strip().casefold(),
                str(case.values.get("accident_classification", "")).strip(),
            )
            if all(identity) and identity in seen:
                raise WorkspaceError(f"중복 사고 식별자는 저장할 수 없습니다: {case.display_id}")
            seen.add(identity)
            if case.status == "completed" and not case.can_complete:
                raise WorkspaceError(
                    f"완료 조건을 충족하지 않은 사고는 완료 상태로 저장할 수 없습니다: {case.display_id}"
                )
        requested_exclusions = (
            self.auto_import_exclusions
            if auto_import_exclusions is None
            else auto_import_exclusions
        )
        try:
            normalized_exclusions = {
                str(Path(path).expanduser().resolve()).casefold() for path in requested_exclusions
            }
        except (OSError, ValueError) as exc:
            raise WorkspaceError("자동 영상 검색 제외 경로를 저장할 수 없습니다.") from exc
        payload = {
            "schema_version": SCHEMA_VERSION,
            "active_case_uid": active_case_uid,
            AUTO_IMPORT_EXCLUSIONS_KEY: sorted(normalized_exclusions),
            "cases": [case.to_dict() for case in cases],
        }
        try:
            serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("조사 데이터에 저장할 수 없는 값이 있습니다.") from exc

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="",
                prefix=f".{self.database_path.name}.",
                suffix=".tmp",
                dir=self.workspace,
                delete=False,
            ) as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, self.database_path)
            self.auto_import_exclusions = normalized_exclusions
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise WorkspaceError(f"조사 데이터를 저장할 수 없습니다: {self.database_path}") from exc

    def active_case_uid(self) -> str | None:
        if not self.database_path.exists():
            return None
        try:
            data = json.loads(self.database_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        value = data.get("active_case_uid") if isinstance(data, dict) else None
        return str(value) if value else None

    def export_csv(self, path: str | Path, cases: list[InvestigationCase]) -> Path:
        destination = Path(path).expanduser().resolve()
        metadata_headers = [
            "case_id",
            "workflow_status",
            "video_files",
            "active_video_file",
            "trigger_frame",
            "collision_frames",
            "video_markers",
        ]
        headers = metadata_headers + csv_headers()
        temporary_path: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8-sig",
                newline="",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=headers,
                    restval="null",
                    extrasaction="ignore",
                )
                writer.writeheader()
                for case in cases:
                    row = {
                        "case_id": case.display_id,
                        "workflow_status": case.status,
                        "video_files": "|".join(case.videos),
                        "active_video_file": case.active_video or "",
                        "trigger_frame": ""
                        if case.trigger_frame is None
                        else str(case.trigger_frame),
                        "collision_frames": "|".join(map(str, case.collision_frames)),
                        "video_markers": (
                            json.dumps(
                                case.markers_by_video,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            if any(
                                marker.get("trigger_frame") is not None
                                or bool(marker.get("collision_frames"))
                                for marker in case.markers_by_video.values()
                            )
                            else None
                        ),
                        **values_to_csv(case.values),
                    }
                    writer.writerow({header: _safe_csv_text(row.get(header)) for header in headers})
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, destination)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise WorkspaceError(f"CSV 파일을 저장할 수 없습니다: {destination}") from exc
        return destination


__all__ = [
    "AUTO_IMPORT_EXCLUSIONS_KEY",
    "CaseRepository",
    "DATABASE_FILENAME",
    "WorkspaceError",
]
