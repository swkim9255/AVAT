"""Recoverably quarantine explicitly selected videos before removing DB links.

The journal is intentionally retained: an interrupted database save can be
recovered without guessing either the original location or the moved filename.
This module never removes directories or permanently deletes selected videos.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from investigator.video_import import VIDEO_EXTENSIONS

QUARANTINE_VIDEO_DIRECTORY = "systems/삭제사고"
QUARANTINE_MANIFEST = "이동기록.json"


def _absolute(path: str | Path, *, relative_to: Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and relative_to is not None:
        value = relative_to / value
    # Do not resolve symlinks before checking them: that would hide the link.
    return Path(os.path.abspath(value))


def _key(path: Path) -> str:
    return os.path.normpath(str(path)).replace("\\", "/").casefold()


def _check_no_links(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise OSError(f"연결된 폴더·파일은 이동할 수 없습니다: {component}")


def _fingerprint(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _safe_label(label: str | None) -> str | None:
    if label is None:
        return None
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(label)).strip(" .")[:100]
    if not value:
        return None
    if value.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        value = f"사고_{value}"
    return value


def _publish_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish a same-volume file, refusing an existing target."""
    if os.name == "nt":
        os.rename(source, destination)
    else:
        os.link(source, destination)
        try:
            source.unlink()
        except BaseException:
            destination.unlink()
            raise


def _copy_move_no_replace(source: Path, destination: Path) -> None:
    """Cross-volume fallback: complete/fsync a copy before removing its source."""
    before = _fingerprint(source)
    temporary: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".avat-quarantine-", suffix=".part", delete=False,
        ) as writer:
            temporary = Path(writer.name)
            with source.open("rb") as reader:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if _fingerprint(source) != before:
            raise OSError(f"이동 중 원본 영상이 변경되었습니다: {source}")
        shutil.copystat(source, temporary, follow_symlinks=False)
        _publish_no_replace(temporary, destination)
        temporary = None
        published = True
        _check_no_links(source)
        if _fingerprint(source) != before:
            raise OSError(f"이동 중 원본 영상이 변경되었습니다: {source}")
        source.unlink()
    except BaseException:
        if published:
            # Only the destination created by this operation is removed. The
            # original still exists if removing it failed or was not attempted.
            destination.unlink()
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _move_no_replace(source: Path, destination: Path) -> None:
    _check_no_links(source)
    _check_no_links(destination)
    if not source.is_file():
        raise OSError(f"영상 파일이 아닙니다: {source}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"기존 파일을 덮어쓸 수 없습니다: {destination}")
    try:
        _publish_no_replace(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV and getattr(error, "winerror", None) != 17:
            raise
        _copy_move_no_replace(source, destination)


@dataclass
class QuarantinedFile:
    source: Path
    destination: Path
    fingerprint: tuple[int, int, int, int]
    moved: bool = False
    destination_fingerprint: tuple[int, int, int, int] | None = None


class QuarantineRollbackError(OSError):
    """Some videos remain safely quarantined because restoration was blocked."""

    def __init__(self, failures: dict[str, str], operation_directory: Path | None):
        self.failures = failures
        self.operation_directory = operation_directory
        super().__init__(
            "일부 영상을 원래 위치로 복구하지 못했습니다. 삭제사고 폴더의 이동기록을 확인해 주세요. "
            + "; ".join(f"{source} ← {destination}" for source, destination in failures.items())
        )


@dataclass
class QuarantinedVideos:
    workspace: Path
    operation_directory: Path | None = None
    records: list[QuarantinedFile] = field(default_factory=list)
    missing: tuple[str, ...] = ()
    case_label: str | None = None
    committed: bool = False
    journal_error: str | None = None

    @property
    def moved(self) -> list[QuarantinedFile]:
        return [record for record in self.records if record.moved]

    @property
    def mapping(self) -> dict[str, str]:
        return {str(record.source): str(record.destination) for record in self.moved}

    @property
    def manifest_path(self) -> Path | None:
        return self.operation_directory / QUARANTINE_MANIFEST if self.operation_directory else None

    def _validate_destination(self, destination: Path) -> None:
        if self.operation_directory is None or not destination.is_relative_to(self.operation_directory):
            raise OSError("삭제사고 폴더 밖의 파일을 변경할 수 없습니다.")
        quarantine = self.workspace / QUARANTINE_VIDEO_DIRECTORY
        if not self.operation_directory.is_relative_to(quarantine):
            raise OSError("삭제사고 작업 경로가 올바르지 않습니다.")
        _check_no_links(destination)

    def _write_journal(self, status: str) -> None:
        manifest = self.manifest_path
        if manifest is None:
            return
        self._validate_destination(manifest)
        payload = {
            "version": 1,
            "status": status,
            "workspace": str(self.workspace),
            "case_label": self.case_label,
            "missing": list(self.missing),
            "files": [
                {
                    "source": str(item.source),
                    "destination": str(item.destination),
                    "moved": item.moved,
                    "source_fingerprint": item.fingerprint,
                    "destination_fingerprint": item.destination_fingerprint,
                }
                for item in self.records
            ],
        }
        temporary = manifest.with_name(f".이동기록-{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as writer:
                json.dump(payload, writer, ensure_ascii=False, indent=2)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, manifest)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self) -> None:
        """Restore only files moved by this batch; never replace another file."""
        if self.committed:
            raise ValueError("DB에 반영된 영상 이동은 자동 취소할 수 없습니다.")
        failures: dict[str, str] = {}
        for record in reversed(self.records):
            if not record.moved:
                continue
            try:
                self._validate_destination(record.destination)
                _check_no_links(record.source)
                if record.source.exists() or record.source.is_symlink():
                    raise FileExistsError(record.source)
                if not record.source.parent.is_dir():
                    raise FileNotFoundError(record.source.parent)
                if (
                    record.destination_fingerprint is not None
                    and _fingerprint(record.destination) != record.destination_fingerprint
                ):
                    raise OSError(f"이동한 영상이 변경되어 자동 복구할 수 없습니다: {record.destination}")
                _move_no_replace(record.destination, record.source)
                record.moved = False
            except OSError:
                failures[str(record.source)] = str(record.destination)
        try:
            self._write_journal("rollback_failed" if failures else "rolled_back")
        except OSError as error:
            self.journal_error = str(error)
        if failures:
            raise QuarantineRollbackError(failures, self.operation_directory)

    def commit(self) -> bool:
        """Mark a successful DB save; a journal failure must not undo that save."""
        self.committed = True
        try:
            self._write_journal("committed")
        except OSError as error:
            self.journal_error = str(error)
            return False
        return True


def quarantine_videos(
    files: list[str] | tuple[str, ...], workspace: str | Path, *, case_label: str | None = None,
) -> QuarantinedVideos:
    """Move selected existing files into a unique, recoverable quarantine.

    Resolve and validate *all* sources first. Existing non-video files are
    rejected before any move, including DB/export paths in a corrupt record.
    Missing links (even unsupported suffixes) are recorded, not fatal: there is
    no physical file to change and the caller may safely remove the stale link.
    Files outside the project are also moved when explicitly supplied;
    on another volume a fsynced copy is completed before unlinking the source.
    Call ``rollback()`` if saving DB changes fails, ``commit()`` only afterwards.
    The caller must release any video decoder holding these files beforehand.
    """
    project = _absolute(workspace)
    _check_no_links(project)
    if not project.is_dir():
        raise NotADirectoryError(project)
    quarantine = project / QUARANTINE_VIDEO_DIRECTORY
    _check_no_links(quarantine)
    if quarantine.exists() and not quarantine.is_dir():
        raise NotADirectoryError(quarantine)

    sources: list[tuple[Path, tuple[int, int, int, int]]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for supplied in files:
        if not str(supplied).strip():
            raise ValueError("영상 경로가 비어 있습니다.")
        source = _absolute(supplied, relative_to=project)
        if _key(source) in seen:
            continue
        seen.add(_key(source))
        _check_no_links(source)
        if source.is_relative_to(quarantine):
            raise OSError(f"이미 삭제사고 폴더에 있는 영상입니다: {source}")
        if not source.exists():
            missing.append(str(source))
        elif not source.is_file():
            raise OSError(f"영상 파일이 아닙니다: {source}")
        elif source.suffix.casefold() not in VIDEO_EXTENSIONS:
            raise ValueError(f"지원하지 않는 영상 확장자입니다. 파일을 이동하지 않았습니다: {source}")
        else:
            sources.append((source, _fingerprint(source)))

    result = QuarantinedVideos(project, missing=tuple(missing), case_label=_safe_label(case_label))
    if not sources:
        return result
    operation = quarantine / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex}"
    _check_no_links(operation)
    operation.mkdir(parents=True, exist_ok=False)
    result.operation_directory = operation
    destination_root = operation / result.case_label if result.case_label else operation
    for source, fingerprint in sources:
        if source.is_relative_to(project):
            relative = source.relative_to(project)
        else:
            digest = hashlib.sha256(_key(source.parent).encode("utf-8")).hexdigest()[:12]
            parent_label = _safe_label(source.parent.name) or "외부"
            relative = Path("외부영상") / f"{parent_label}-{digest}" / source.name
        target = destination_root / relative
        result._validate_destination(target)
        if target.exists():
            raise FileExistsError(target)
        result.records.append(QuarantinedFile(source, target, fingerprint))
    try:
        result._write_journal("prepared")
        for record in result.records:
            result._validate_destination(record.destination)
            _check_no_links(record.source)
            if _fingerprint(record.source) != record.fingerprint:
                raise OSError(f"삭제할 영상이 검사 후 변경되었습니다: {record.source}")
            record.destination.parent.mkdir(parents=True, exist_ok=True)
            result._validate_destination(record.destination)
            _move_no_replace(record.source, record.destination)
            record.moved = True
            record.destination_fingerprint = _fingerprint(record.destination)
            result._write_journal("moving")
        result._write_journal("awaiting_database_save")
        return result
    except BaseException:
        result.rollback()
        raise
