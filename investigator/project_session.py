"""Lightweight project tabs; only the active project owns player widgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from investigator.models import InvestigationCase
from investigator.storage import CaseRepository


@dataclass(slots=True)
class ProjectSession:
    repository: CaseRepository
    cases: list[InvestigationCase] = field(default_factory=list)
    current_uid: str | None = None
    initialized: bool = False
    load_failed: bool = False
    list_state: dict[str, Any] = field(default_factory=dict)
    form_tab_key: str | None = None
    form_scroll: int = 0
    splitter_sizes: list[int] = field(default_factory=list)
    video_path: str | None = None
    video_frame: int = 0
    pending_video_paths: set[str] = field(default_factory=set)
    statistics_state: dict[str, Any] = field(default_factory=dict)
    restored: bool = False
