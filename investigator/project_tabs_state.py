"""Small, per-user tab configuration; investigation data stays in each workspace."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QIODevice, QSaveFile

from investigator.user_data import PROJECT_TABS_FILENAME, app_user_data_directory


def default_project_tabs_path() -> Path:
    user_data = app_user_data_directory()
    if user_data is not None:
        # EXE: 프로그램 파일과 분리된 systems/사용자데이터에 둔다.
        return user_data / PROJECT_TABS_FILENAME
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "AVAT" if base else Path.home() / ".avat") / "project_tabs.json"


@dataclass(frozen=True, slots=True)
class ProjectTabsState:
    folders: tuple[Path, ...] = ()
    active_folder: Path | None = None
    video_folder_order: tuple[str, ...] = ()
    active_video_folder: str | None = None

    @classmethod
    def from_dict(cls, payload: object) -> ProjectTabsState:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("지원하지 않는 프로젝트 탭 설정입니다.")
        raw_folders = payload.get("folders")
        if not isinstance(raw_folders, list):
            raise ValueError("프로젝트 탭 폴더 목록이 올바르지 않습니다.")
        folders, seen = [], set()
        for raw in raw_folders:
            if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or not Path(raw).is_absolute():
                raise ValueError("프로젝트 탭 경로가 올바르지 않습니다.")
            path = Path(raw).resolve()
            identity = os.path.normcase(str(path))
            if identity not in seen:
                folders.append(path)
                seen.add(identity)
        raw_active = payload.get("active_folder")
        if raw_active is not None and (not isinstance(raw_active, str) or "\x00" in raw_active or not Path(raw_active).is_absolute()):
            raise ValueError("선택한 프로젝트 탭 경로가 올바르지 않습니다.")
        active = Path(raw_active).resolve() if raw_active is not None else None
        if active not in folders:
            active = folders[0] if folders else None
        order = payload.get("video_folder_order", [])
        selected_video = payload.get("active_video_folder")
        if (not isinstance(order, list) or any(not isinstance(key, str) for key in order)
                or (selected_video is not None and not isinstance(selected_video, str))):
            raise ValueError("영상 폴더 탭 설정이 올바르지 않습니다.")
        return cls(tuple(folders), active, tuple(dict.fromkeys(order)), selected_video)


class ProjectTabsStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_project_tabs_path()

    def load(self) -> ProjectTabsState:
        try:
            payload = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ProjectTabsState()
        return ProjectTabsState.from_dict(json.loads(payload))

    def save(self, state: ProjectTabsState) -> None:
        payload = {
            "version": 1,
            "folders": [str(folder) for folder in state.folders],
            "active_folder": str(state.active_folder) if state.active_folder is not None else None,
            "video_folder_order": list(state.video_folder_order),
            "active_video_folder": state.active_video_folder,
        }
        # Validate writes through the same schema as reads, including absolute paths.
        ProjectTabsState.from_dict(payload)
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = QSaveFile(str(self.path))
        if not target.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(target.errorString())
        if target.write(data) != len(data):
            error = target.errorString()
            target.cancelWriting()
            raise OSError(error)
        if not target.commit():
            raise OSError(target.errorString())
