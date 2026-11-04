"""Stage videos in the project's managed video tree without replacing files."""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from investigator.video_import import MANAGED_VIDEO_DIRECTORY, parse_accident_video_name


@dataclass
class CopiedVideos:
    paths: list[str] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    created_directories: list[Path] = field(default_factory=list)

    def rollback(self) -> None:
        for path in reversed(self.created):
            path.unlink(missing_ok=True)
        for directory in reversed(self.created_directories):
            # Never remove a pre-existing or non-empty user directory.
            with suppress(OSError):
                directory.rmdir()


def copy_videos(
    files: list[str], workspace: str | Path, *, source_root: str | Path | None = None,
) -> CopiedVideos:
    """Copy into ``workspace/영상폴더``, preserving the selected folder tree.

    Folder imports should supply ``source_root`` to keep a stable relative
    hierarchy on repeated imports. For individual-file selection, the common
    source parent is used, so identical camera names in different subfolders
    are not flattened into one directory. Existing managed videos are reused.
    """

    project = Path(workspace).resolve()
    project.mkdir(parents=True, exist_ok=True)
    destination = project / MANAGED_VIDEO_DIRECTORY
    if not destination.resolve().is_relative_to(project):
        raise OSError("영상폴더가 작업 폴더 외부에 연결되어 있습니다.")
    result = CopiedVideos()
    sources = list(dict.fromkeys(Path(path).resolve() for path in files))
    if not sources:
        return result
    if source_root is not None:
        origin = Path(source_root).resolve()
        if not origin.is_dir() or any(not source.is_relative_to(origin) for source in sources):
            raise OSError("선택한 영상이 가져오기 기준 폴더에 포함되지 않습니다.")
    else:
        try:
            origin = Path(os.path.commonpath([str(source.parent) for source in sources]))
        except ValueError:
            # Selecting files across Windows drives has no common directory.
            origin = None

    def ensure_directory(directory: Path) -> None:
        if not directory.resolve().is_relative_to(destination.resolve()):
            raise OSError("영상 복사 경로가 영상폴더 외부를 가리킵니다.")
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        for current in reversed(missing):
            current.mkdir()
            result.created_directories.append(current)

    try:
        for source in sources:
            before = source.stat()
            if not source.is_file():
                raise OSError(f"영상 파일이 아닙니다: {source}")
            if source.is_relative_to(destination.resolve()):
                result.paths.append(str(source))
                continue
            relative = source.relative_to(origin) if origin is not None else Path(source.parent.name) / source.name
            parent = destination / relative.parent
            ensure_directory(parent)
            parsed = parse_accident_video_name(source)
            base = parsed[0] if parsed else source.stem
            sequence = parsed[1] + 1 if parsed else 1
            target = parent / source.name
            while target.exists() or target.is_symlink():
                if not target.is_symlink() and target.is_file() and filecmp.cmp(source, target, shallow=False):
                    break
                target = parent / f"{base}_{sequence}{source.suffix}"
                sequence += 1
            else:
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=parent, prefix=".avat-import-", suffix=".part", delete=False
                    ) as handle:
                        temporary = Path(handle.name)
                        with source.open("rb") as reader:
                            shutil.copyfileobj(reader, handle, length=1024 * 1024)
                        handle.flush()
                        os.fsync(handle.fileno())
                    after = source.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise OSError("복사 중 원본 영상이 변경되었습니다. 복사가 끝난 후 다시 가져와 주세요.")
                    # Windows rename refuses an existing target. On POSIX link
                    # provides the same no-overwrite publication guarantee.
                    if os.name == "nt":
                        os.rename(temporary, target)
                    else:
                        os.link(temporary, target)
                        temporary.unlink()
                    result.created.append(target)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            result.paths.append(str(target))
        return result
    except BaseException:
        result.rollback()
        raise
