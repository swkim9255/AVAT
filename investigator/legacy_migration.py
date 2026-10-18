"""Optional, removable reader for legacy investigations.json workspaces.

Removal: comment out the single import in CaseRepository._migrate_legacy,
then delete this file. SQLite creation, validation, and backup stay in storage.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from investigator.storage import CaseRepository


def migrate_legacy(repository: CaseRepository) -> None:
    """Activate a verified SQLite database without modifying the source JSON."""
    from investigator.storage import WorkspaceError

    try:
        payload = json.loads(repository.legacy_database_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            f"조사 데이터 파일을 읽을 수 없습니다: {repository.legacy_database_path}"
        ) from exc
    decoded = repository._decode_payload(payload)
    repository._create_database(*decoded)
    repository._adopt_metadata(*decoded)
