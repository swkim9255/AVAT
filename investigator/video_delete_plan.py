"""Plan record deletion using same-filename ownership, without moving files."""

from __future__ import annotations

import ntpath
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from investigator.models import InvestigationCase
from investigator.video_import import scan_video_tree


def _name(path: str | Path) -> str:
    return ntpath.basename(str(path).replace("/", "\\")).casefold()


def _key(path: str | Path, workspace: Path) -> str:
    value = str(path)
    if not ntpath.isabs(value):
        value = str(workspace / value)
    return ntpath.normpath(value.replace("/", "\\")).casefold()


@dataclass(frozen=True, slots=True)
class VideoDeletionPlan:
    movable: tuple[str, ...]
    shared: tuple[str, ...]
    excluded_paths: frozenset[str]


def plan_video_deletion(
    surviving_cases: Iterable[InvestigationCase], removed_paths: Iterable[str], workspace: str | Path,
) -> VideoDeletionPlan:
    """Keep files while any same-filename record survives, including stale links.

    On the last deletion, also find that filename inside the workspace: the
    final record can still point to an old, missing location. Recoverable moves
    remain the quarantine module's responsibility. Never scan external trees,
    follow links or include management/deleted-video directories.
    """
    root = Path(workspace).expanduser().resolve()
    removed = tuple(removed_paths)
    remaining_names = {_name(path) for case in surviving_cases for path in case.videos}
    shared = tuple(path for path in removed if _name(path) in remaining_names)
    final_names = {_name(path) for path in removed} - remaining_names
    movable: dict[str, str] = {
        _key(path, root): path for path in removed if _name(path) in final_names
    }
    if final_names:
        videos, _ = scan_video_tree(root)
        for path in videos:
            if _name(path) in final_names:
                movable.setdefault(_key(path, root), str(path))
    # Do not tombstone a retained file: a remaining stale link must still be
    # able to recover to it on refresh/restart.
    return VideoDeletionPlan(tuple(movable.values()), shared, frozenset(movable))
