"""Discover accident videos whose filenames encode a 13-digit case number."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = frozenset({".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".webm"})
ACCIDENT_VIDEO_STEM = re.compile(r"^(?P<number>\d{13})(?:_(?P<sequence>[1-9]\d*))?$")
MANAGED_VIDEO_DIRECTORY = "01_사고동영상"
# These directories contain application data, not newly imported videos. In
# particular, scanning a backup must not resurrect deleted accident entries.
IGNORED_VIDEO_DIRECTORIES = frozenset(
    name.casefold() for name in (
        "작업관리", "작업관리DB", "systems", "작업 폴더 관리 DB", "csv내보내기", "02_csv내보내기",
        "03_Report내보내기", "삭제사고", ".backups", ".git", "runtime", "models", "_internal", "licenses",
    )
)


def scan_video_tree(folder: str | Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return videos and directories recursively, without following links.

    Directory paths are also returned so a QFileSystemWatcher can subscribe to
    every nested folder (a watch on a parent alone is not recursive on Windows).
    """

    source = Path(folder).expanduser().resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    videos: list[Path] = []
    directories: list[Path] = []
    pending = [source]
    while pending:
        directory = pending.pop()
        directories.append(directory)
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
        nested: list[Path] = []
        for entry in children:
            # Windows junctions are reparse points rather than ordinary Python
            # symlinks. Avoid both kinds to stay inside the selected video tree.
            if entry.is_symlink():
                continue
            stat = entry.stat(follow_symlinks=False)
            if getattr(stat, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if not entry.name.startswith(".") and entry.name.casefold() not in IGNORED_VIDEO_DIRECTORIES:
                    nested.append(path)
            elif entry.is_file(follow_symlinks=False) and path.suffix.casefold() in VIDEO_EXTENSIONS:
                videos.append(path)
        pending.extend(reversed(nested))
    return tuple(videos), tuple(directories)


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
    """Scan the nested video tree and return deterministic accident groups.

    Only filenames carrying the existing 13-digit accident identifier are
    automatically grouped; generic camera names must still be attached to a
    user-selected accident, not guessed from an arbitrary folder name.
    """

    grouped: dict[str, list[tuple[int, str, str]]] = {}
    ignored: list[str] = []
    seen_paths: set[str] = set()
    videos, _ = scan_video_tree(folder)
    for entry in videos:
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
    "MANAGED_VIDEO_DIRECTORY",
    "IGNORED_VIDEO_DIRECTORIES",
    "VideoFolderScan",
    "parse_accident_video_name",
    "scan_video_folder",
    "scan_video_tree",
]
