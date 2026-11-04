"""사용자 데이터 위치: 프로그램 파일과 분리해 systems/사용자데이터 한 폴더에 모은다.

앱을 새 버전으로 바꿀 때는 이 폴더만 보존하면 된다. 작업 폴더별 데이터(백업,
삭제한 영상)는 ``<작업 폴더>/systems/사용자데이터``에, EXE 실행 설정(검출 설정,
영상 탭 상태, 로그)은 EXE가 있는 systems 폴더의 ``사용자데이터``에 둔다. EXE
배포에서는 작업 폴더가 EXE 상위 폴더이므로 두 위치가 같은 한 폴더가 된다.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from investigator.runtime_paths import is_frozen, runtime_data_root

SYSTEMS_DIRECTORY = "systems"
USER_DATA_DIRECTORY = "사용자데이터"
BACKUP_DIRECTORY = "백업"
QUARANTINE_DIRECTORY = "삭제사고"
LATEST_BACKUP_FILENAME = "작업관리_백업.db"
SETTINGS_FILENAME = "settings.ini"
PROJECT_TABS_FILENAME = "workspace.json"
LOG_DIRECTORY = "logs"
# 이전 버전이 자동으로 만든 백업 이름(수동 백업 파일은 건드리지 않는다).
_LEGACY_AUTOMATIC_BACKUP_PREFIXES = ("삭제전-", "영상경로변경전-")

logger = logging.getLogger(__name__)


def workspace_user_data_directory(workspace: str | Path) -> Path:
    return Path(workspace) / SYSTEMS_DIRECTORY / USER_DATA_DIRECTORY


def workspace_backup_directory(workspace: str | Path) -> Path:
    return workspace_user_data_directory(workspace) / BACKUP_DIRECTORY


def latest_backup_path(workspace: str | Path) -> Path:
    """작업 폴더마다 하나만 유지하는 최신 조사 데이터 백업 파일."""
    return workspace_backup_directory(workspace) / LATEST_BACKUP_FILENAME


def app_user_data_directory() -> Path | None:
    """EXE 실행 설정 폴더. 소스 실행은 기존처럼 사용자 프로필(%LOCALAPPDATA%)을 쓴다."""
    return runtime_data_root() / USER_DATA_DIRECTORY if is_frozen() else None


def _move_if_absent(source: Path, target: Path) -> bool:
    """대상이 없을 때만 옮긴다. 기존 데이터는 절대 덮어쓰지 않는다."""
    if not source.exists() or target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        # 다른 볼륨 등 rename이 안 되는 경우에만 복사 후 원본을 지운다.
        if source.is_dir():
            shutil.copytree(source, target)
            shutil.rmtree(source)
        else:
            shutil.copy2(source, target)
            source.unlink()
    return True


def migrate_app_user_data() -> list[str]:
    """이전 EXE 배치(systems 바로 아래)의 실행 설정·로그를 사용자데이터로 옮긴다.

    로그 설정 전에 호출되므로 실패해도 예외를 밖으로 내보내지 않는다.
    """
    target = app_user_data_directory()
    if target is None:
        return []
    moved: list[str] = []
    root = runtime_data_root()
    for name in (SETTINGS_FILENAME, PROJECT_TABS_FILENAME, LOG_DIRECTORY):
        try:
            if _move_if_absent(root / name, target / name):
                moved.append(name)
        except OSError:
            logger.warning("app_user_data_migration_failed item=%s", name, exc_info=True)
    return moved


def migrate_workspace_user_data(workspace: str | Path) -> list[str]:
    """작업 폴더의 이전 배치(systems/backups, systems/삭제사고)를 사용자데이터로 옮긴다.

    백업은 '가장 최신 1개만 유지' 정책에 따라 가장 최근 자동 백업을 최신 백업 파일로
    옮기고, 이전 버전이 자동으로 만든 나머지 백업만 정리한다. 삭제사고 폴더는
    그대로 옮겨 삭제한 영상과 이동기록을 보존한다.
    """
    root = Path(workspace) / SYSTEMS_DIRECTORY
    moved: list[str] = []
    try:
        if _move_if_absent(root / QUARANTINE_DIRECTORY,
                           workspace_user_data_directory(workspace) / QUARANTINE_DIRECTORY):
            moved.append(QUARANTINE_DIRECTORY)
    except OSError:
        logger.warning("workspace_quarantine_migration_failed", exc_info=True)
    legacy_backups = root / "backups"
    if legacy_backups.is_dir():
        try:
            automatic = sorted(
                (path for path in legacy_backups.glob("*.db")
                 if path.name.startswith(_LEGACY_AUTOMATIC_BACKUP_PREFIXES) and path.is_file()),
                key=lambda path: path.stat().st_mtime_ns,
            )
            latest = latest_backup_path(workspace)
            # 새 배치에 이미 최신 백업이 있으면 이전 폴더는 건드리지 않는다.
            if automatic and not latest.exists() and _move_if_absent(automatic[-1], latest):
                moved.append(LATEST_BACKUP_FILENAME)
                for path in automatic[:-1]:
                    path.unlink(missing_ok=True)
                # 사용자가 넣어 둔 다른 파일이 없을 때만 빈 폴더를 지운다.
                if not any(legacy_backups.iterdir()):
                    legacy_backups.rmdir()
        except OSError:
            logger.warning("workspace_backup_migration_failed", exc_info=True)
    return moved


__all__ = [
    "BACKUP_DIRECTORY",
    "LATEST_BACKUP_FILENAME",
    "PROJECT_TABS_FILENAME",
    "QUARANTINE_DIRECTORY",
    "SETTINGS_FILENAME",
    "USER_DATA_DIRECTORY",
    "app_user_data_directory",
    "latest_backup_path",
    "migrate_app_user_data",
    "migrate_workspace_user_data",
    "workspace_backup_directory",
    "workspace_user_data_directory",
]
