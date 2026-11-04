"""Video-folder views share one workspace database and original case identities."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from investigator.video_import import (
    IGNORED_VIDEO_DIRECTORIES,
    MANAGED_VIDEO_DIRECTORY,
    VIDEO_EXTENSIONS,
)

UNLINKED_FOLDER_KEY = "__unlinked__"


@dataclass(frozen=True, slots=True)
class VideoFolder:
    key: str
    label: str
    path: Path
    direct_only: bool = False

    def contains(self, video: str | Path) -> bool:
        path = Path(video)
        return path.parent == self.path if self.direct_only else path.is_relative_to(self.path)


def _normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


class VideoFolderIndex:
    """영상 경로가 속한 폴더 탭을 문자열 비교로 한 번만 계산해 캐시한다.

    VideoFolder.contains()는 호출마다 Path 객체를 만들어 수만 건의 사고에서
    탭 전환·목록 범위 계산을 느리게 했다. 저장된 영상 경로는 절대 경로이므로
    대소문자를 정규화한 접두사 비교로 같은 결과를 얻는다.
    """

    def __init__(self, folders: list[VideoFolder] | tuple[VideoFolder, ...] = ()) -> None:
        self._rules: list[tuple[str, str, str, bool]] = []
        for folder in folders:
            if folder.key == UNLINKED_FOLDER_KEY:
                continue
            base = _normalized(folder.path)
            prefix = base if base.endswith(os.sep) else base + os.sep
            self._rules.append((folder.key, base, prefix, folder.direct_only))
        self._cache: dict[str, str | None] = {}

    def key_for(self, video: str | Path) -> str | None:
        """폴더 탭 키를 돌려준다. 어느 폴더에도 없으면 None(미연결)이다."""
        text = str(video)
        try:
            return self._cache[text]
        except KeyError:
            pass
        normalized = _normalized(text)
        parent = os.path.dirname(normalized)
        result = None
        for key, base, prefix, direct_only in self._rules:
            if (parent == base) if direct_only else normalized.startswith(prefix):
                result = key
                break
        self._cache[text] = result
        return result

    def contains(self, folder: VideoFolder, video: str | Path) -> bool:
        key = self.key_for(video)
        if folder.key == UNLINKED_FOLDER_KEY:
            return key is None
        return key == folder.key


def _video_entries(root: Path) -> tuple[list[Path], bool]:
    """Inspect direct video folders/files, excluding management data and links."""
    children: list[Path] = []
    direct = False
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_symlink() or entry.name.startswith("."):
                continue
            if getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() not in IGNORED_VIDEO_DIRECTORIES:
                    children.append(Path(entry.path).resolve())
            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.casefold() in VIDEO_EXTENSIONS:
                direct = True
    return children, direct


def video_root(workspace: Path) -> Path:
    """Prefer the new layout, retaining readable old workspace video layouts."""
    root = workspace / MANAGED_VIDEO_DIRECTORY
    if root.is_symlink() or (root.exists() and getattr(root.stat(), "st_file_attributes", 0) & 0x400):
        raise OSError("영상 폴더의 심볼릭 링크·정션은 지원하지 않습니다.")
    if not root.is_dir():
        return workspace
    children, direct = _video_entries(root)
    if children or direct:
        return root
    # Creating the DB directory alone must not hide videos in an old layout.
    legacy_children, legacy_direct = _video_entries(workspace)
    return workspace if legacy_direct or any(path != root for path in legacy_children) else root


def discover_video_folders(workspace: Path) -> list[VideoFolder]:
    root = video_root(workspace)
    if not root.is_dir():
        return []
    children, direct = _video_entries(root)
    if root == workspace:
        # A management-only 01_사고동영상 is not a legacy video folder tab.
        children = [path for path in children if path.name != MANAGED_VIDEO_DIRECTORY]
    result = [VideoFolder(path.relative_to(workspace).as_posix(), path.name, path)
              for path in sorted(children, key=lambda path: path.name.casefold())]
    if direct or not result:
        result.insert(0, VideoFolder(root.relative_to(workspace).as_posix(),
                                     root.name if root != workspace else MANAGED_VIDEO_DIRECTORY,
                                     root, direct_only=bool(children)))
    return result
