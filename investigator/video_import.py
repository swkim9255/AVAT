"""Discover accident videos whose filenames encode a 13-digit case number."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = frozenset({".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".webm"})
ACCIDENT_VIDEO_STEM = re.compile(r"^(?P<number>\d{13})(?:_(?P<sequence>[1-9]\d*))?$")


@dataclass(frozen=True, slots=True)
class VideoFolderScan:
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    ignored_videos: tuple[str, ...]

    @property
    def video_count(self) -> int:
        return sum(len(paths) for _, paths in self.groups)


def parse_accident_video_name(path: str | Path) -> tuple[str, int] | None:
    """Return ``(accident_number, sequence)`` for a supported filename."""

    source = Path(path)
    if source.suffix.casefold() not in VIDEO_EXTENSIONS:
        return None
    match = ACCIDENT_VIDEO_STEM.fullmatch(source.stem)
    if match is None:
        return None
    sequence = int(match.group("sequence") or 0)
    return match.group("number"), sequence


def scan_video_folder(folder: str | Path) -> VideoFolderScan:
    """Scan direct child files and return deterministic accident groups."""

    source = Path(folder).expanduser().resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)

    grouped: dict[str, list[tuple[int, str, str]]] = {}
    ignored: list[str] = []
    seen_paths: set[str] = set()
    for entry in source.iterdir():
        if not entry.is_file() or entry.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        resolved = str(entry.resolve())
        path_key = resolved.casefold()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        parsed = parse_accident_video_name(entry)
        if parsed is None:
            ignored.append(resolved)
            continue
        accident_number, sequence = parsed
        grouped.setdefault(accident_number, []).append((sequence, entry.name.casefold(), resolved))

    groups = tuple(
        (
            accident_number,
            tuple(path for _, _, path in sorted(items)),
        )
        for accident_number, items in sorted(grouped.items())
    )
    return VideoFolderScan(groups, tuple(sorted(ignored, key=str.casefold)))


__all__ = [
    "ACCIDENT_VIDEO_STEM",
    "VIDEO_EXTENSIONS",
    "VideoFolderScan",
    "parse_accident_video_name",
    "scan_video_folder",
]
