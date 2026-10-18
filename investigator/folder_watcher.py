"""Watch open project roots and wait for newly arriving videos to settle."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from investigator.video_import import VIDEO_EXTENSIONS


@dataclass
class _Directory:
    seen: set[str] = field(default_factory=set)
    pending: dict[str, tuple[tuple[int, int], float]] = field(default_factory=dict)


class ProjectFolderWatcher(QObject):
    videos_ready = Signal(str, list)
    watch_error = Signal(str)

    def __init__(self, parent: QObject | None = None, *, settle_seconds: float = 2.0,
                 poll_ms: int = 500) -> None:
        super().__init__(parent)
        self.settle_seconds = settle_seconds
        self._directories: dict[str, _Directory] = {}
        self._native = QFileSystemWatcher(self)
        self._native.directoryChanged.connect(self._changed)
        self._rescan = QTimer(self)
        self._rescan.setSingleShot(True)
        self._rescan.setInterval(250)
        self._rescan.timeout.connect(self.rescan)
        self._poll = QTimer(self)
        self._poll.setInterval(poll_ms)
        self._poll.timeout.connect(self._check_pending)

    def add_directory(self, folder: str | Path, *, known_paths: set[str] | None = None) -> None:
        root = str(Path(folder).resolve())
        if root not in self._directories:
            known = self._video_paths(root) if known_paths is None else known_paths
            self._directories[root] = _Directory(seen={str(Path(p).resolve()).casefold() for p in known})
        if root not in self._native.directories() and not self._native.addPath(root):
            self.watch_error.emit("프로젝트 폴더 감시를 시작할 수 없습니다.")
        self._rescan.start()

    def remove_directory(self, folder: str | Path) -> None:
        root = str(Path(folder).resolve())
        self._directories.pop(root, None)
        if root in self._native.directories():
            self._native.removePath(root)
        if not any(state.pending for state in self._directories.values()):
            self._poll.stop()

    @staticmethod
    def _video_paths(root: str) -> set[str]:
        with os.scandir(root) as entries:
            return {entry.path for entry in entries
                    if Path(entry.name).suffix.casefold() in VIDEO_EXTENSIONS and entry.is_file()}

    def _changed(self, folder: str) -> None:
        del folder
        self._rescan.start()

    def rescan(self) -> None:
        for root, state in list(self._directories.items()):
            try:
                paths = self._video_paths(root)
            except OSError:
                self.watch_error.emit("프로젝트 폴더를 읽을 수 없어 자동 검색을 보류했습니다.")
                continue
            current = {path.casefold(): path for path in paths}
            state.seen.intersection_update(current)
            for path in list(state.pending):
                if path.casefold() not in current:
                    state.pending.pop(path)
            for key, path in current.items():
                if key in state.seen:
                    continue
                state.seen.add(key)
                try:
                    stat = Path(path).stat()
                except OSError:
                    state.seen.discard(key)
                    continue
                state.pending[path] = ((stat.st_size, stat.st_mtime_ns), time.monotonic())
        if any(state.pending for state in self._directories.values()):
            self._poll.start()

    def _check_pending(self) -> None:
        now = time.monotonic()
        for root, state in list(self._directories.items()):
            ready = []
            for path, (signature, since) in list(state.pending.items()):
                try:
                    stat = Path(path).stat()
                    current = (stat.st_size, stat.st_mtime_ns)
                    if current != signature:
                        state.pending[path] = (current, now)
                    elif stat.st_size > 0 and now - since >= self.settle_seconds:
                        # A writer that disallows shared reads is still copying.
                        with open(path, "rb"):
                            pass
                        ready.append(path)
                        state.pending.pop(path)
                except FileNotFoundError:
                    state.pending.pop(path, None)
                    state.seen.discard(path.casefold())
                except OSError:
                    state.pending[path] = (signature, now)
            if ready:
                self.videos_ready.emit(root, ready)
        if not any(state.pending for state in self._directories.values()):
            self._poll.stop()

    def shutdown(self) -> None:
        self._rescan.stop()
        self._poll.stop()
        paths = self._native.directories()
        if paths:
            self._native.removePaths(paths)
        self._directories.clear()
