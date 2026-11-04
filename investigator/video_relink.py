"""Conservative, non-mutating recovery of video paths and their annotations.

Only the file's location changes.  Accident identity, workflow state, frame
indices and all detector data are deliberately left untouched.  The caller is
responsible for showing a plan, backing up and atomically persisting the copies.
"""

from __future__ import annotations

import copy
import ntpath
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from investigator.models import InvestigationCase
from investigator.video_import import IGNORED_VIDEO_DIRECTORIES, VIDEO_EXTENSIONS, scan_video_tree


@dataclass(frozen=True, slots=True)
class RelinkMatch:
    old_path: str
    candidates: tuple[str, ...]
    proposed_path: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class AutoRelinkPlan:
    """Automatic repairs plus candidates that must not be newly imported."""

    mapping: dict[str, str]
    unresolved: tuple[RelinkMatch, ...]
    reserved_paths: frozenset[str]


def _path_key(path: str | Path, workspace_root: str | Path | None = None) -> str:
    """Windows file identity, also usable for old Windows paths in portable tests."""

    raw = str(path)
    if workspace_root is not None and not ntpath.isabs(raw):
        raw = str(Path(workspace_root) / raw)
    return ntpath.normpath(raw.replace("/", "\\")).casefold()


def _components(path: str | Path, workspace_root: str | Path | None = None) -> tuple[str, ...]:
    return tuple(part for part in _path_key(path, workspace_root).split("\\") if part)


def _file_path(path: str | Path, workspace_root: str | Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and workspace_root is not None:
        value = Path(workspace_root) / value
    return value


def scan_relink_videos(folder: str | Path) -> tuple[str, ...]:
    """Recursively discover all video names, without accident-number parsing.

    Use the same application/backup/symlink pruning as ordinary import and
    watching. Permission errors are surfaced, not silently treated as absence.
    """

    root = Path(folder).expanduser().resolve()
    found: dict[str, str] = {}
    videos, _ = scan_video_tree(root)
    for candidate in videos:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root):
            found.setdefault(_path_key(resolved), str(resolved))
    return tuple(sorted(found.values(), key=_path_key))


def _suffix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    length = 0
    for old, new in zip(reversed(left), reversed(right), strict=False):
        if old != new:
            break
        length += 1
    return length


def plan_relink(
    cases: Iterable[InvestigationCase],
    new_root: str | Path,
    *,
    old_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    excluded_paths: Iterable[str | Path] = (),
) -> tuple[RelinkMatch, ...]:
    """Plan missing links; existing valid paths are never proposed for change.

    An explicit old root permits exact relative-path matching. Otherwise the
    longest component suffix wins only if it is unique and contains at least a
    parent directory plus filename. Filename-only matches require confirmation
    even when there is only one: ``front.mp4`` is not an identity. Ambiguous
    candidates remain available for an explicit per-video selection in the UI.
    """

    case_list = tuple(cases)
    excluded = {_path_key(path, workspace_root) for path in excluded_paths}
    candidates = tuple(
        path for path in scan_relink_videos(new_root) if _path_key(path) not in excluded
    )
    by_key = {_path_key(path): path for path in candidates}
    by_name: dict[str, list[str]] = {}
    for path in candidates:
        by_name.setdefault(_components(path)[-1], []).append(path)

    known: dict[str, str] = {}
    occupied: set[str] = set()
    for case in case_list:
        for path in case.videos:
            key = _path_key(path, workspace_root)
            if _file_path(path, workspace_root).is_file():
                occupied.add(key)
            elif key not in excluded:
                known.setdefault(key, path)

    root_parts = _components(old_root, workspace_root) if old_root is not None else ()
    target_root = Path(new_root).expanduser().resolve()
    matches: list[RelinkMatch] = []
    for old_path in known.values():
        old_parts = _components(old_path, workspace_root)
        choices = tuple(by_name.get(old_parts[-1], ()))
        if root_parts and old_parts[: len(root_parts)] == root_parts:
            relative_parts = old_parts[len(root_parts) :]
            # Look up a normalized key instead of constructing a lowercased path
            # on case-sensitive filesystems.
            exact_key = _path_key(target_root.joinpath(*relative_parts))
            exact = by_key.get(exact_key)
            if exact is not None:
                matches.append(RelinkMatch(old_path, (exact,), exact, "relative_root"))
                continue
            # The user explicitly paired two roots. A missing corresponding
            # relative file must not be replaced with a different year's same
            # case/camera name merely because two tail components still match.
            matches.append(RelinkMatch(old_path, choices, None, "relative_missing"))
            continue
        if not choices:
            matches.append(RelinkMatch(old_path, (), None, "not_found"))
            continue
        ranked = sorted(
            ((_suffix_length(old_parts, _components(path)), path) for path in choices),
            key=lambda pair: (-pair[0], _path_key(pair[1])),
        )
        best_length = ranked[0][0]
        best = tuple(path for length, path in ranked if length == best_length)
        ordered = tuple(path for _, path in ranked)
        if best_length < 2:
            matches.append(RelinkMatch(old_path, ordered, None, "filename_only"))
        elif len(best) != 1:
            matches.append(RelinkMatch(old_path, ordered, None, "ambiguous"))
        else:
            matches.append(RelinkMatch(old_path, ordered, best[0], "suffix"))

    proposed_counts = Counter(
        _path_key(match.proposed_path)
        for match in matches
        if match.proposed_path is not None
    )
    return tuple(
        replace(match, proposed_path=None, reason="target_conflict")
        if match.proposed_path is not None
        and (
            _path_key(match.proposed_path) in occupied
            or proposed_counts[_path_key(match.proposed_path)] > 1
        )
        else match
        for match in matches
    )


def build_auto_relink_plan(
    cases: Iterable[InvestigationCase],
    workspace: str | Path,
    *,
    excluded_paths: Iterable[str | Path] = (),
    discovered_paths: Iterable[str | Path] | None = None,
    missing_paths: Iterable[str | Path] | None = None,
    trusted_scan: bool = False,
) -> AutoRelinkPlan:
    """Recover missing links at startup without guessing ambiguous identities.

    Unique multi-component suffixes are preferred. A filename alone is enough
    only when its *original*, globally filtered group contains one distinct old
    source and one available new file. Do not infer a leftover match by removing
    other repairs from an initially ambiguous group. A shared old source in two
    accidents still counts once, but two different old roots count separately.

    Only workspace-contained video files are considered; already-owned valid
    paths, deleted/import-excluded paths and application/backup trees are never
    stolen as candidates. ``discovered_paths`` can reuse a caller's recursive
    scan to avoid another directory walk. Unresolved candidates are reserved so
    subsequent auto-import cannot create duplicate attachments for them.

    ``missing_paths``는 호출자가 같은 작업에서 이미 확인한 누락 영상 목록이다.
    ``trusted_scan``은 ``discovered_paths``가 같은 작업의 scan_video_tree 결과라는
    뜻으로, 파일마다 존재·링크·resolve 확인을 반복하지 않는다(수만 건 대응).
    """

    root = Path(workspace).expanduser().resolve()
    cases = tuple(cases)
    excluded = {_path_key(path, root) for path in excluded_paths}
    known_missing = (
        None if missing_paths is None else {_path_key(path, root) for path in missing_paths}
    )
    occupied: set[str] = set()
    missing: dict[str, str] = {}
    for case in cases:
        for path in case.videos:
            key = _path_key(path, root)
            exists = (
                key not in known_missing if known_missing is not None
                else _file_path(path, root).is_file()
            )
            if exists:
                occupied.add(key)
            elif key not in excluded:
                missing.setdefault(key, path)
    for key in occupied:
        missing.pop(key, None)
    if not missing:
        return AutoRelinkPlan({}, (), frozenset())

    if discovered_paths is None:
        discovered_paths, _ = scan_video_tree(root)
        trusted_scan = True
    by_key: dict[str, str] = {}
    for raw_path in discovered_paths:
        candidate = _file_path(raw_path, root)
        if candidate.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        if trusted_scan:
            # scan_video_tree가 존재 여부·링크·관리 폴더를 이미 걸렀고 경로도 정규형이다.
            resolved = candidate
            if not resolved.is_relative_to(root):
                continue
        else:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            # Keep reused scan input subject to the same application-directory
            # exclusions. Symlink/reparse pruning is performed by scan_video_tree;
            # direct file symlinks are also rejected for caller-supplied scans.
            parts = candidate.absolute().relative_to(root).parts
            if candidate.is_symlink() or any(
                part.startswith(".") or part.casefold() in IGNORED_VIDEO_DIRECTORIES
                for part in parts[:-1]
            ):
                continue
        key = _path_key(resolved)
        if key not in occupied and key not in excluded:
            by_key.setdefault(key, str(resolved))
    available = tuple(sorted(by_key.values(), key=_path_key))
    grouped_new: dict[str, list[str]] = {}
    # A suffix index gives the strongest match without comparing every missing
    # front.mp4 against every new front.mp4. Store only cardinality and a sole
    # candidate, not a separate candidate list for every shared path suffix.
    suffix_index: dict[tuple[str, ...], tuple[int, str | None]] = {}
    for path in available:
        parts = _components(path)
        grouped_new.setdefault(parts[-1], []).append(path)
        for length in range(2, len(parts) + 1):
            suffix = parts[-length:]
            count, _ = suffix_index.get(suffix, (0, None))
            suffix_index[suffix] = (count + 1, path if count == 0 else None)
    # All unresolved records for a basename share this immutable candidate tuple.
    # Lexical order is deterministic regardless of filesystem scan order.
    new_by_name = {name: tuple(paths) for name, paths in grouped_new.items()}
    old_by_name: dict[str, list[str]] = {}
    old_components: dict[str, tuple[str, ...]] = {}
    for key, path in missing.items():
        parts = _components(path, root)
        old_components[key] = parts
        old_by_name.setdefault(parts[-1], []).append(key)

    matches: dict[str, RelinkMatch] = {}
    proposals: dict[str, str] = {}
    for key, old_path in missing.items():
        old_parts = old_components[key]
        name = old_parts[-1]
        choices = new_by_name.get(name, ())
        if not choices:
            matches[key] = RelinkMatch(old_path, (), None, "not_found")
            continue
        best: str | None = None
        for length in range(len(old_parts), 1, -1):
            found = suffix_index.get(old_parts[-length:])
            if found is not None:
                count, sole_path = found
                best = sole_path if count == 1 else None
                break
        if best is not None:
            proposals[key] = best
            matches[key] = RelinkMatch(old_path, choices, best, "suffix")
        elif len(old_by_name[name]) == 1 and len(choices) == 1:
            proposals[key] = choices[0]
            matches[key] = RelinkMatch(old_path, choices, choices[0], "unique_filename")
        else:
            matches[key] = RelinkMatch(old_path, choices, None, "ambiguous")

    counts = Counter(_path_key(path) for path in proposals.values())
    mapping: dict[str, str] = {}
    unresolved: list[RelinkMatch] = []
    unresolved_names: set[str] = set()
    for key, match in matches.items():
        target = proposals.get(key)
        if target is not None and counts[_path_key(target)] == 1:
            mapping[match.old_path] = target
        else:
            unresolved_names.add(old_components[key][-1])
            unresolved.append(
                replace(match, proposed_path=None, reason="target_conflict")
                if target is not None else match
            )
    confirmed = {_path_key(path) for path in mapping.values()}
    reserved = frozenset(
        path for name in unresolved_names for path in new_by_name.get(name, ())
        if _path_key(path) not in confirmed
    )
    return AutoRelinkPlan(mapping, tuple(unresolved), reserved)


def _normalized_mapping(
    mapping: Mapping[str | Path, str | Path], workspace_root: str | Path | None
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for source, target in mapping.items():
        key = _path_key(source, workspace_root)
        value = str(_file_path(target, workspace_root).resolve())
        if key in normalized and _path_key(normalized[key]) != _path_key(value):
            raise ValueError("같은 영상에 서로 다른 재연결 경로가 지정되었습니다.")
        normalized[key] = value
    return normalized


def remap_video_path_list(
    paths: Iterable[str],
    mapping: Mapping[str | Path, str | Path],
    *,
    workspace_root: str | Path | None = None,
) -> list[str]:
    """Copy a path list (e.g. import exclusions), preserving unrelated entries."""

    normalized = _normalized_mapping(mapping, workspace_root)
    return [normalized.get(_path_key(path, workspace_root), path) for path in paths]


def apply_video_path_mapping(
    case: InvestigationCase,
    mapping: Mapping[str | Path, str | Path],
    *,
    workspace_root: str | Path | None = None,
) -> InvestigationCase:
    """Copy a case and atomically rekey its videos and all per-video payloads.

    Mapping entries for other cases are accepted so a project-wide mapping can
    be passed to every case. Every affected target must exist and be a supported
    video. Duplicate targets and annotation overwrites are rejected before any
    copy is returned; the source case is never modified or marked as edited.
    """

    normalized = _normalized_mapping(mapping, workspace_root)
    relevant = {
        _path_key(path, workspace_root): normalized[_path_key(path, workspace_root)]
        for path in case.videos
        if _path_key(path, workspace_root) in normalized
    }
    for source in case.videos:
        key = _path_key(source, workspace_root)
        if key not in relevant:
            continue
        target = Path(relevant[key])
        if target.suffix.casefold() not in VIDEO_EXTENSIONS or not target.is_file():
            raise ValueError(f"재연결할 영상 파일이 없습니다: {target}")
        if _file_path(source, workspace_root).is_file() and key != _path_key(target):
            raise ValueError(f"이미 정상 연결된 영상은 재연결할 수 없습니다: {source}")

    videos = [relevant.get(_path_key(path, workspace_root), path) for path in case.videos]
    if relevant and len({_path_key(path, workspace_root) for path in videos}) != len(videos):
        raise ValueError("여러 영상이 같은 파일에 연결됩니다. 개별 영상을 다시 선택해 주세요.")

    def rekey(payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        used: set[str] = set()
        for source, value in payload.items():
            target = (
                relevant.get(_path_key(source, workspace_root), source)
                if source != "__case__"
                else source
            )
            key = _path_key(target, workspace_root)
            if key in used:
                raise ValueError("재연결 경로에 기존 영상 주석이 있어 덮어쓸 수 없습니다.")
            used.add(key)
            result[target] = copy.deepcopy(value)
        return result

    # copy.copy intentionally avoids __post_init__: do not migrate/normalize
    # unrelated investigation values or detector payloads during path recovery.
    result = copy.copy(case)
    result.values = copy.deepcopy(case.values)
    result.videos = videos
    result.markers_by_video = rekey(case.markers_by_video)
    result.rotation_by_video = rekey(case.rotation_by_video)
    result.objects_by_video = rekey(case.objects_by_video)
    return result


def apply_workspace_video_path_mapping(
    cases: Iterable[InvestigationCase],
    mapping: Mapping[str | Path, str | Path],
    *,
    workspace_root: str | Path | None = None,
) -> list[InvestigationCase]:
    """Validate choices and copy only affected cases, preserving other identities.

    Persistence uses object identity to find changed rows. Copying an unrelated
    accident would unnecessarily deserialize/rewrite potentially large detector
    payloads; returning it untouched is safe because this operation is pure.
    """

    case_list = tuple(cases)
    normalized = _normalized_mapping(mapping, workspace_root)
    known = {
        _path_key(path, workspace_root)
        for case in case_list
        for path in case.videos
    }
    unknown = normalized.keys() - known
    if unknown:
        raise ValueError("사고 목록에 없는 영상 경로가 재연결 목록에 있습니다.")
    targets: dict[str, str] = {}
    for source in known:
        target = _path_key(normalized.get(source, source), workspace_root)
        if target in targets and targets[target] != source:
            raise ValueError("서로 다른 기존 영상은 같은 새 영상에 연결할 수 없습니다.")
        targets[target] = source
    result: list[InvestigationCase] = []
    for case in case_list:
        local_mapping = {
            path: normalized[_path_key(path, workspace_root)]
            for path in case.videos
            if _path_key(path, workspace_root) in normalized
        }
        result.append(
            apply_video_path_mapping(case, local_mapping, workspace_root=workspace_root)
            if local_mapping else case
        )
    return result


__all__ = [
    "AutoRelinkPlan",
    "RelinkMatch",
    "apply_video_path_mapping",
    "apply_workspace_video_path_mapping",
    "build_auto_relink_plan",
    "plan_relink",
    "remap_video_path_list",
    "scan_relink_videos",
]
