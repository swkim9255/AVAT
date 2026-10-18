"""Stage imported videos in a project root, without replacing existing files."""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from investigator.video_import import parse_accident_video_name


@dataclass
class CopiedVideos:
    paths: list[str] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)

    def rollback(self) -> None:
        for path in reversed(self.created):
            path.unlink(missing_ok=True)


def copy_videos(files: list[str], workspace: str | Path) -> CopiedVideos:
    destination = Path(workspace).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    result = CopiedVideos()
    sources = list(dict.fromkeys(Path(path).resolve() for path in files))
    try:
        for source in sources:
            if source.parent == destination:
                result.paths.append(str(source))
                continue
            parsed = parse_accident_video_name(source)
            base = parsed[0] if parsed else source.stem
            sequence = parsed[1] + 1 if parsed else 1
            target = destination / source.name
            before = source.stat()
            while target.exists():
                if target.is_file() and filecmp.cmp(source, target, shallow=False):
                    break
                target = destination / f"{base}_{sequence}{source.suffix}"
                sequence += 1
            else:
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=destination, prefix=".avat-import-", suffix=".part", delete=False
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
