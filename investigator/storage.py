"""Transactional SQLite persistence, legacy JSON migration, and CSV export."""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from investigator.case_numbering import REPORT_ACCIDENT_NUMBER_COLUMN, build_case_number_map
from investigator.csv_specification import csv_export_headers
from investigator.form_definition import FormDefinition
from investigator.models import SCHEMA_VERSION, InvestigationCase
from investigator.speed_csv import speed_csv_path, write_speed_csv

DATABASE_FILENAME = "investigations.db"
LEGACY_DATABASE_FILENAME = "investigations.json"
DATABASE_VERSION = 1
# The physical SQL layout is unchanged. This payload version prevents older
# AVAT builds from silently discarding independent detector annotations.
OBJECT_WORKSPACE_SCHEMA_VERSION = 3
DATABASE_APPLICATION_ID = 0x41564154  # AVAT, not an arbitrary SQLite database.
_UNSET = object()
_REQUIRED_METADATA = {
    "schema_version", "form_definition", "initial_form_definition", "active_case_uid",
    "auto_import_exclusions",
}
logger = logging.getLogger(__name__)
AUTO_IMPORT_EXCLUSIONS_KEY = "auto_import_exclusions"
DATA_SPECIFICATION_FILENAME = "AVAT 데이터 명세서.xlsx"


def data_specification_path(csv_path: str | Path) -> Path:
    destination = Path(csv_path)
    return destination.with_name(f"{destination.stem}_데이터명세서.xlsx")


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
    """Persist cases independently; connections never escape an operation's thread."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.database_path = self.workspace / DATABASE_FILENAME
        self.legacy_database_path = self.workspace / LEGACY_DATABASE_FILENAME
        self.auto_import_exclusions: set[str] = set()
        self.form_definition = FormDefinition.default()
        self.initial_form_definition = FormDefinition.default()
        self._active_case_uid: str | None = None
        self._metadata_loaded = False

    def ensure_workspace(self) -> None:
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"작업 폴더를 만들 수 없습니다: {self.workspace}") from exc
        if not self.workspace.is_dir():
            raise WorkspaceError(f"작업 폴더를 만들 수 없습니다: {self.workspace}")

    def load(self, *, require_existing: bool = False) -> list[InvestigationCase]:
        if require_existing:
            try:
                if not self.workspace.is_dir():
                    raise WorkspaceError("저장된 프로젝트 폴더를 찾을 수 없습니다. 폴더나 외장 드라이브 연결 상태를 확인해 주세요.")
            except OSError as exc:
                raise WorkspaceError("저장된 프로젝트 폴더에 접근할 수 없습니다. 연결 상태와 읽기 권한을 확인해 주세요.") from exc
        else:
            self.ensure_workspace()
        if not self.database_path.exists():
            if self.legacy_database_path.exists():
                self._migrate_legacy()
            else:
                self._adopt_metadata([], FormDefinition.default(), FormDefinition.default(), set(), None)
                return []
        try:
            with self._connection() as connection, connection:
                connection.execute("BEGIN")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise WorkspaceError("조사 데이터베이스의 무결성 검사에 실패했습니다.")
                payload = self._read_payload(connection)
            result = self._decode_payload(payload)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise WorkspaceError(
                f"조사 데이터베이스를 읽을 수 없습니다: {self.database_path}\n"
                "기존 JSON으로 자동 전환하지 않습니다. 백업을 확인해 주세요."
            ) from exc
        self._adopt_metadata(*result)
        return result[0]

    @staticmethod
    def _decode_payload(data: Any) -> tuple[
        list[InvestigationCase], FormDefinition, FormDefinition, set[str], str | None
    ]:
        """Validate legacy and database data through exactly the same model path."""
        if not isinstance(data, dict) or data.get("schema_version") not in {SCHEMA_VERSION, 2, OBJECT_WORKSPACE_SCHEMA_VERSION}:
            raise WorkspaceError("지원하지 않는 조사 데이터 형식입니다.")
        raw_cases = data.get("cases")
        try:
            if data.get("schema_version") in {2, OBJECT_WORKSPACE_SCHEMA_VERSION} and not isinstance(
                data.get("form_definition"), dict
            ):
                raise ValueError("사용자 정의 조사 양식이 누락되었습니다.")
            definition = FormDefinition.from_dict(data.get("form_definition"))
            initial_definition = FormDefinition.from_dict(data.get("initial_form_definition"))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise WorkspaceError(f"조사 양식을 읽을 수 없습니다: {exc}") from exc
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
                case = InvestigationCase.from_dict(item, form_definition=definition)
                case.values = definition.migrate_values(case.values)
                case.form_definition = definition
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
                str(case.values.get("institution", "")).strip().casefold(),
                str(case.values.get("accident_classification", "")).strip(),
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
        active = data.get("active_case_uid")
        return cases, definition, initial_definition, exclusions, str(active) if active else None

    def _adopt_metadata(
        self,
        cases: list[InvestigationCase],
        definition: FormDefinition,
        initial_definition: FormDefinition,
        exclusions: set[str],
        active: str | None,
    ) -> None:
        self.form_definition = definition
        self.initial_form_definition = initial_definition
        self.auto_import_exclusions = exclusions
        self._active_case_uid = active
        self._metadata_loaded = True

    @contextmanager
    def _connection(
        self, path: Path | None = None, *, initialize: bool = False, wal: bool = True
    ) -> Iterator[sqlite3.Connection]:
        target = path or self.database_path
        # mode=rw refuses to create a blank DB if the workspace disappears.
        uri = target.as_uri() + ("?mode=rwc" if initialize else "?mode=rw")
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if initialize:
                connection.executescript(
                    f"""
                    PRAGMA application_id = {DATABASE_APPLICATION_ID};
                    PRAGMA user_version = {DATABASE_VERSION};
                    CREATE TABLE workspace_metadata (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE cases (
                        uid TEXT PRIMARY KEY,
                        position INTEGER NOT NULL CHECK (position >= 0),
                        institution TEXT NOT NULL,
                        accident_number TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX cases_position ON cases(position);
                    CREATE INDEX cases_identity ON cases(institution, accident_number);
                    CREATE INDEX cases_status ON cases(status);
                    """
                )
            elif (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != DATABASE_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != DATABASE_VERSION
            ):
                raise WorkspaceError("지원하지 않는 AVAT 데이터베이스 형식입니다.")
            if wal:
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode.lower() != "wal":
                    raise WorkspaceError("데이터베이스의 안전한 저장 모드를 설정할 수 없습니다.")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _serialize(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("조사 데이터에 저장할 수 없는 값이 있습니다.") from exc

    @staticmethod
    def _read_payload(connection: sqlite3.Connection) -> dict[str, Any]:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key, value FROM workspace_metadata")
        }
        if not _REQUIRED_METADATA.issubset(metadata):
            raise WorkspaceError("조사 데이터베이스의 작업공간 설정이 누락되었습니다.")
        raw_cases = []
        positions: set[int] = set()
        for row in connection.execute("SELECT uid, position, payload FROM cases ORDER BY position, uid"):
            case = json.loads(row["payload"])
            if not isinstance(case, dict) or case.get("uid") != row["uid"]:
                raise WorkspaceError("사고 UID와 저장 데이터가 일치하지 않습니다.")
            if row["position"] in positions:
                raise WorkspaceError("저장된 사고 표시 순서가 올바르지 않습니다.")
            positions.add(row["position"])
            raw_cases.append(case)
        return {**metadata, "cases": raw_cases}

    @staticmethod
    def _remove_temporary(path: Path | None) -> None:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Cleanup must not mask the original I/O or validation error.
                logger.warning("임시 저장 파일을 정리하지 못했습니다: %s", path, exc_info=True)

    @staticmethod
    def _identity(case: InvestigationCase) -> tuple[str, str]:
        return (
            str(case.values.get("institution", "")).strip().casefold(),
            str(case.values.get("accident_classification", "")).strip(),
        )

    @classmethod
    def _write_case(cls, connection: sqlite3.Connection, case: InvestigationCase, position: int) -> None:
        institution, number = cls._identity(case)
        connection.execute(
            """INSERT INTO cases(uid, position, institution, accident_number, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                position=excluded.position, institution=excluded.institution,
                accident_number=excluded.accident_number, status=excluded.status,
                payload=excluded.payload
            WHERE cases.position != excluded.position OR cases.payload != excluded.payload
                OR cases.institution != excluded.institution
                OR cases.accident_number != excluded.accident_number
                OR cases.status != excluded.status""",
            (case.uid, position, institution, number, case.status, cls._serialize(case.to_dict())),
        )

    @classmethod
    def _write_metadata(cls, connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            """INSERT INTO workspace_metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            WHERE workspace_metadata.value != excluded.value""",
            (key, cls._serialize(value)),
        )

    def _create_database(
        self,
        cases: list[InvestigationCase],
        definition: FormDefinition,
        initial_definition: FormDefinition,
        exclusions: set[str],
        active: str | None,
    ) -> None:
        """Build and verify one standalone file before atomically activating it."""
        metadata = {
            "schema_version": (
                OBJECT_WORKSPACE_SCHEMA_VERSION if any(case.objects_by_video for case in cases)
                else SCHEMA_VERSION if definition == FormDefinition.default() else 2
            ),
            "form_definition": definition.to_dict(),
            "initial_form_definition": initial_definition.to_dict(),
            "active_case_uid": active,
            AUTO_IMPORT_EXCLUSIONS_KEY: sorted(exclusions),
        }
        expected = {**metadata, "cases": [case.to_dict() for case in cases]}
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.workspace, prefix=".investigations.", suffix=".migration.db", delete=False) as handle:
                temporary_path = Path(handle.name)
            # DELETE journal keeps activation atomic; no WAL file is stranded at the old name.
            with self._connection(temporary_path, initialize=True, wal=False) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for key, value in metadata.items():
                        self._write_metadata(connection, key, value)
                    for position, case in enumerate(cases):
                        self._write_case(connection, case, position)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise WorkspaceError("이전된 데이터베이스의 무결성 검사에 실패했습니다.")
                actual = self._read_payload(connection)
                if actual != expected:
                    raise WorkspaceError("이전된 조사 데이터 검증에 실패했습니다.")
                self._decode_payload(actual)
            with temporary_path.open("r+b") as handle:
                os.fsync(handle.fileno())
            if self.database_path.exists():
                raise WorkspaceError("다른 작업에서 데이터베이스를 생성했습니다. 작업 폴더를 다시 열어 주세요.")
            # On Windows rename fails instead of replacing a concurrently created DB.
            os.rename(temporary_path, self.database_path)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise WorkspaceError(
                f"조사 데이터베이스를 만들 수 없습니다: {self.database_path}\n"
                "기존 JSON 파일은 변경하지 않았습니다."
            ) from exc
        finally:
            self._remove_temporary(temporary_path)

    def _migrate_legacy(self) -> None:
        """Optional migration hook shared by load/save/backup."""
        migrator = None
        # 자동 이전 제거: 아래 import 한 줄을 주석 처리한 뒤
        # investigator/legacy_migration.py 파일만 삭제하세요. 나머지는 유지합니다.
        from investigator.legacy_migration import migrate_legacy as migrator  # noqa: F811

        if migrator is None:
            raise WorkspaceError(
                "JSON에서 DB로 이전이 필요한 작업 폴더입니다. "
                "이 배포본에서는 자동 이전 기능이 비활성화되어 있습니다. "
                "이전 기능이 있는 버전으로 변환하거나 이전된 investigations.db를 사용해 주세요. "
                "기존 JSON은 변경하지 않았습니다."
            )
        migrator(self)

    def _ensure_database(self) -> None:
        self.ensure_workspace()
        if not self.database_path.exists():
            if self.legacy_database_path.exists():
                self._migrate_legacy()
            else:
                self._create_database([], self.form_definition, self.initial_form_definition, self.auto_import_exclusions, self._active_case_uid)

    def save(
        self,
        cases: list[InvestigationCase],
        *,
        active_case_uid: str | None = None,
        auto_import_exclusions: set[str] | None = None,
        form_definition: FormDefinition | None = None,
    ) -> None:
        """Compatibility replacement save, without rewriting unchanged case rows."""
        self._save_changes(
            cases, replace_all=True, order=[case.uid for case in cases],
            active_case_uid=active_case_uid,
            auto_import_exclusions=auto_import_exclusions,
            form_definition=form_definition,
        )

    def save_changes(
        self,
        upserts: list[InvestigationCase],
        *,
        deleted_uids: list[str] | None = None,
        order: list[str] | None = None,
        active_case_uid: str | None | object = _UNSET,
        auto_import_exclusions: set[str] | None = None,
        form_definition: FormDefinition | None = None,
    ) -> None:
        """Commit changed cases and optional workspace state as one transaction.

        Omitted metadata is preserved. Explicit ``active_case_uid=None`` clears
        the selection. ``order``, when supplied, must list all remaining UIDs.
        Only form changes require validation of the other persisted cases.
        """
        self._save_changes(
            upserts, deleted_uids=deleted_uids, order=order,
            active_case_uid=active_case_uid,
            auto_import_exclusions=auto_import_exclusions,
            form_definition=form_definition,
        )

    @staticmethod
    def _prepare_cases(cases: list[InvestigationCase], definition: FormDefinition) -> None:
        try:
            definition.validate()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise WorkspaceError(f"조사 양식 설정이 올바르지 않습니다: {exc}") from exc
        seen: set[tuple[str, str]] = set()
        seen_uids: set[str] = set()
        for case in cases:
            case.form_definition = definition
            case.values = definition.migrate_values(case.values)
            if not case.defaults_applied:
                definition.apply_defaults(case.values)
                # Preserve the normal date/injury derivation for configured defaults.
                case.__post_init__()
                case.defaults_applied = True
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

    @staticmethod
    def _normalize_exclusions(requested_exclusions: set[str]) -> set[str]:
        try:
            if any(not isinstance(path, str) or not path.strip() for path in requested_exclusions):
                raise ValueError("제외 경로는 비어 있지 않은 문자열이어야 합니다.")
            return {
                str(Path(path).expanduser().resolve()).casefold() for path in requested_exclusions
            }
        except (OSError, ValueError) as exc:
            raise WorkspaceError("자동 영상 검색 제외 경로를 저장할 수 없습니다.") from exc

    def _save_changes(
        self,
        upserts: list[InvestigationCase],
        *,
        replace_all: bool = False,
        deleted_uids: list[str] | None = None,
        order: list[str] | None = None,
        active_case_uid: str | None | object = _UNSET,
        auto_import_exclusions: set[str] | None = None,
        form_definition: FormDefinition | None = None,
    ) -> None:
        self._ensure_database()
        try:
            self._backup_before_object_upgrade(upserts, form_definition)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise WorkspaceError("객체 데이터 형식 변경 전 백업을 만들지 못했습니다. 기존 데이터는 변경하지 않았습니다.") from exc
        normalized_exclusions = (
            None if auto_import_exclusions is None
            else self._normalize_exclusions(auto_import_exclusions)
        )
        deleted = set(deleted_uids or [])
        if any(not isinstance(uid, str) or not uid for uid in deleted):
            raise WorkspaceError("삭제할 사고 UID가 올바르지 않습니다.")
        if deleted.intersection(case.uid for case in upserts):
            raise WorkspaceError("같은 사고를 동시에 저장하고 삭제할 수 없습니다.")
        try:
            with self._connection() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                metadata = {
                    row["key"]: json.loads(row["value"])
                    for row in connection.execute("SELECT key, value FROM workspace_metadata")
                }
                if not _REQUIRED_METADATA.issubset(metadata):
                    raise WorkspaceError("조사 데이터베이스의 작업공간 설정이 누락되었습니다.")
                # Everything needed after COMMIT is parsed before making changes.
                # A corrupt baseline must not turn a committed save into a reported failure.
                _, previous_definition, initial_definition, stored_exclusions, stored_active = self._decode_payload({**metadata, "cases": []})
                definition = form_definition or previous_definition
                self._prepare_cases(upserts, definition)
                changed_by_uid = {case.uid: case for case in upserts}
                if replace_all:
                    deleted.update(
                        row[0] for row in connection.execute("SELECT uid FROM cases")
                        if row[0] not in changed_by_uid
                    )
                for uid in deleted:
                    connection.execute("DELETE FROM cases WHERE uid=?", (uid,))

                definition_changed = definition.to_dict() != previous_definition.to_dict()
                if definition_changed and not replace_all:
                    # A form edit can alter validation and stored choice values.
                    # Ordinary one-case saves never enter this all-case path.
                    other_cases = []
                    for row in connection.execute("SELECT uid, payload FROM cases"):
                        if row["uid"] not in changed_by_uid:
                            raw = json.loads(row["payload"])
                            raw["values"] = definition.migrate_values(raw.get("values", {}), previous_definition)
                            other_cases.append(InvestigationCase.from_dict(raw, form_definition=definition))
                    self._prepare_cases(other_cases, definition)
                    changed_by_uid.update((case.uid, case) for case in other_cases)

                next_position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM cases").fetchone()[0]
                for case in changed_by_uid.values():
                    existing = connection.execute("SELECT position FROM cases WHERE uid=?", (case.uid,)).fetchone()
                    position = existing[0] if existing else next_position
                    self._write_case(connection, case, position)
                    if existing is None:
                        next_position += 1
                # Validate final identities, allowing atomic swaps in the same batch.
                for case in changed_by_uid.values():
                    identity = self._identity(case)
                    if all(identity) and connection.execute(
                        "SELECT uid FROM cases WHERE institution=? AND accident_number=? AND uid!=? LIMIT 1",
                        (*identity, case.uid),
                    ).fetchone():
                        raise WorkspaceError(f"중복 사고 식별자는 저장할 수 없습니다: {case.display_id}")

                if order is not None:
                    existing_uids = {row[0] for row in connection.execute("SELECT uid FROM cases")}
                    if len(order) != len(set(order)) or set(order) != existing_uids:
                        raise WorkspaceError("사고 표시 순서는 모든 사고 UID를 한 번씩 포함해야 합니다.")
                    connection.executemany(
                        "UPDATE cases SET position=? WHERE uid=? AND position!=?",
                        [(position, uid, position) for position, uid in enumerate(order)],
                    )
                if active_case_uid is not _UNSET:
                    if active_case_uid is not None and not isinstance(active_case_uid, str):
                        raise WorkspaceError("선택 사고 UID가 올바르지 않습니다.")
                    metadata["active_case_uid"] = active_case_uid
                    stored_active = active_case_uid
                    self._write_metadata(connection, "active_case_uid", active_case_uid)
                elif metadata.get("active_case_uid") in deleted:
                    metadata["active_case_uid"] = None
                    stored_active = None
                    self._write_metadata(connection, "active_case_uid", None)
                if normalized_exclusions is not None:
                    metadata[AUTO_IMPORT_EXCLUSIONS_KEY] = sorted(normalized_exclusions)
                    self._write_metadata(connection, AUTO_IMPORT_EXCLUSIONS_KEY, sorted(normalized_exclusions))
                    stored_exclusions = normalized_exclusions
                if definition_changed:
                    metadata["form_definition"] = definition.to_dict()
                    self._write_metadata(connection, "form_definition", definition.to_dict())
                version = max(
                    metadata.get("schema_version", SCHEMA_VERSION),
                    OBJECT_WORKSPACE_SCHEMA_VERSION
                    if any(case.objects_by_video for case in changed_by_uid.values())
                    else SCHEMA_VERSION if definition == FormDefinition.default() else 2,
                )
                self._write_metadata(connection, "schema_version", version)
                # Initial settings are immutable across ordinary form edits.
            self._adopt_metadata(
                [], definition, initial_definition, stored_exclusions, stored_active,
            )
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise WorkspaceError(f"조사 데이터를 저장할 수 없습니다: {self.database_path}\n{exc}") from exc

    def _backup_before_object_upgrade(
        self, upserts: list[InvestigationCase], definition: FormDefinition | None,
    ) -> None:
        """Keep a verified pre-upgrade snapshot without rewriting cases on load."""
        if not any(case.objects_by_video for case in upserts) and definition is None:
            return
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM workspace_metadata WHERE key='schema_version'"
            ).fetchone()
            version = json.loads(row[0]) if row else SCHEMA_VERSION
            if version >= OBJECT_WORKSPACE_SCHEMA_VERSION:
                return
            upgrading = any(case.objects_by_video for case in upserts)
            if not upgrading and definition is not None:
                old = connection.execute(
                    "SELECT value FROM workspace_metadata WHERE key='form_definition'"
                ).fetchone()
                if old and json.loads(old[0]) != definition.to_dict():
                    upgrading = any(
                        json.loads(row[0]).get("objects_by_video")
                        for row in connection.execute("SELECT payload FROM cases")
                    )
        if upgrading:
            self.backup(self.workspace / ".backups" / f"investigations.pre-object-v2-{uuid4().hex}.db")

    def active_case_uid(self) -> str | None:
        """Return cached selection; only old callers before load incur a lookup."""
        if not self._metadata_loaded:
            self.load()
        return self._active_case_uid

    def backup(self, path: str | Path) -> Path:
        """Create a consistent single-file backup, including committed WAL data."""
        self._ensure_database()
        destination = Path(path).expanduser().resolve()
        if destination in {self.database_path, self.legacy_database_path} or str(destination).casefold() in {
            (str(self.database_path) + "-wal").casefold(),
            (str(self.database_path) + "-shm").casefold(),
        }:
            raise WorkspaceError("원본 데이터 파일을 백업 대상으로 지정할 수 없습니다.")
        self._check_backup_destination(destination)
        temporary_path: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".avat-backup.", suffix=".db", delete=False) as handle:
                temporary_path = Path(handle.name)
            with self._connection() as source:
                target = sqlite3.connect(temporary_path)
                try:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise WorkspaceError("백업 데이터베이스 검증에 실패했습니다.")
                finally:
                    target.close()
            with temporary_path.open("r+b") as handle:
                os.fsync(handle.fileno())
            self._check_backup_destination(destination)
            os.replace(temporary_path, destination)
        except (OSError, sqlite3.Error) as exc:
            raise WorkspaceError(f"작업공간을 백업할 수 없습니다: {destination}") from exc
        finally:
            self._remove_temporary(temporary_path)
        return destination

    @staticmethod
    def _check_backup_destination(destination: Path) -> None:
        if any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise WorkspaceError(
                "백업 대상에 사용 중이거나 복구가 필요한 데이터베이스 보조 파일이 있습니다. "
                "다른 이름으로 백업해 주세요."
            )

    def export_csv(
        self,
        path: str | Path,
        cases: list[InvestigationCase],
        *,
        include_specification: bool = False,
    ) -> Path:
        destination = Path(path).expanduser().resolve()
        headers = csv_export_headers(self.form_definition)
        report_numbers = build_case_number_map(cases)
        temporary_path: Path | None = None
        speed_temporary_path: Path | None = None
        specification_temporary_path: Path | None = None
        speed_destination = speed_csv_path(destination)
        specification_destination = data_specification_path(destination)
        csv_saved = False
        speed_saved = False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if include_specification:
                # Always the packaged original, byte for byte: form edits change
                # the CSV columns but never the specification workbook.
                content = files("investigator").joinpath(
                    "assets", DATA_SPECIFICATION_FILENAME
                ).read_bytes()
                with tempfile.NamedTemporaryFile(
                    "wb", dir=destination.parent, prefix=".specification.",
                    suffix=".tmp", delete=False,
                ) as specification_handle:
                    specification_temporary_path = Path(specification_handle.name)
                    specification_handle.write(content)
                    specification_handle.flush()
                    os.fsync(specification_handle.fileno())
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8-sig",
                newline="",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
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
                                or bool(marker.get("speed_points"))
                                for marker in case.markers_by_video.values()
                            )
                            else None
                        ),
                        **self.form_definition.csv_row(case.values),
                        REPORT_ACCIDENT_NUMBER_COLUMN: report_numbers.get(case.uid),
                    }
                    writer.writerow({header: _safe_csv_text(row.get(header)) for header in headers})
                handle.flush()
                os.fsync(handle.fileno())
            # Stage every output before replacing any existing export. In
            # particular, an I/O failure while generating speed analysis must
            # not discard the user's previous investigation CSV.
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8-sig",
                newline="",
                prefix=f".{speed_destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as speed_handle:
                speed_temporary_path = Path(speed_handle.name)
                write_speed_csv(speed_handle, cases, report_numbers=report_numbers)
                speed_handle.flush()
                os.fsync(speed_handle.fileno())
            os.replace(temporary_path, destination)
            csv_saved = True
            os.replace(speed_temporary_path, speed_destination)
            speed_saved = True
            if specification_temporary_path is not None:
                os.replace(specification_temporary_path, specification_destination)
        except OSError as exc:
            if csv_saved:
                if not speed_saved:
                    pending_specification = (
                        f"\n데이터 명세서도 저장하지 못했습니다: {specification_destination}"
                        if include_specification else ""
                    )
                    raise WorkspaceError(
                        f"CSV는 저장했습니다: {destination}\n"
                        f"속도 분석 CSV는 저장하지 못했습니다: {speed_destination}"
                        f"{pending_specification}\n"
                        "내보낼 파일이 다른 프로그램에서 열려 있는지 확인해 주세요."
                    ) from exc
                raise WorkspaceError(
                    f"CSV는 저장했습니다: {destination}\n"
                    f"속도 분석 CSV도 저장했습니다: {speed_destination}\n"
                    f"데이터 명세서는 저장하지 못했습니다: {specification_destination}\n"
                    "명세서가 다른 프로그램에서 열려 있는지 확인해 주세요."
                ) from exc
            if include_specification:
                raise WorkspaceError(
                    f"CSV와 데이터 명세서를 내보내지 못했습니다: {destination}\n"
                    f"속도 분석 CSV도 저장하지 못했습니다: {speed_destination}\n{exc}"
                ) from exc
            raise WorkspaceError(
                f"CSV 파일을 저장할 수 없습니다: {destination}\n"
                f"속도 분석 CSV도 저장하지 못했습니다: {speed_destination}\n{exc}"
            ) from exc
        finally:
            self._remove_temporary(temporary_path)
            self._remove_temporary(speed_temporary_path)
            self._remove_temporary(specification_temporary_path)
        return destination


__all__ = [
    "AUTO_IMPORT_EXCLUSIONS_KEY",
    "CaseRepository",
    "DATA_SPECIFICATION_FILENAME",
    "DATABASE_FILENAME",
    "LEGACY_DATABASE_FILENAME",
    "WorkspaceError",
    "data_specification_path",
]
