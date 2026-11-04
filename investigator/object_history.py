"""Bounded, per-video undo history for object edits, without Qt dependencies.

Entries retain only changed objects (and only changed boxes for existing
objects). Unchanged detections and run provenance come from the live document
when an edit is restored, rather than being duplicated into every entry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from investigator.object_detection import DetectedObject, VideoObjects

ObjectHistoryKey = tuple[str, str, str]


@dataclass(slots=True)
class ObjectEditResult:
    objects: VideoObjects
    selection: list[str]
    frame: int


@dataclass(slots=True)
class _ObjectDelta:
    uid: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    before_boxes: list[dict[str, Any]] | None
    after_boxes: list[dict[str, Any]] | None


@dataclass(slots=True)
class _Entry:
    sequence: int
    changes: list[_ObjectDelta]
    before_order: tuple[str, ...]
    after_order: tuple[str, ...]
    before_digest: bytes
    after_digest: bytes
    before_selection: list[str]
    after_selection: list[str]
    frame: int
    size: int


@dataclass(slots=True)
class _Timeline:
    entries: list[_Entry] = field(default_factory=list)
    cursor: int = 0


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _fingerprint(objects: VideoObjects) -> bytes:
    # Stream one object at a time, so hashing does not retain a second document.
    digest = hashlib.sha256()
    digest.update(b'{"objects":[')
    for item in objects.objects:
        digest.update(_encode(item.to_dict()))
        digest.update(b",")
    digest.update(b'],"runs":[')
    for run in objects.runs:
        digest.update(_encode(run.to_dict()))
        digest.update(b",")
    digest.update(b"]}")
    return digest.digest()


def _metadata(item: DetectedObject | None) -> dict[str, Any] | None:
    if item is None:
        return None
    # Keep every annotation/version field without maintaining a second schema.
    # Boxes remain delta-encoded separately, avoiding a per-edit frame copy.
    return item.metadata_dict()


class ObjectEditHistory:
    """Undo/redo timelines sharing a global entry and serialized-byte budget.

    Keys must distinguish workspace, case UID, and video path. Callers should
    clear a key after accepting an automatic detection result or another edit
    outside this history. Fingerprints also refuse stale undo/redo operations.
    """

    def __init__(self, max_entries: int = 100, max_bytes: int = 16 * 1024 * 1024) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("History limits must be non-negative.")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._timelines: dict[ObjectHistoryKey, _Timeline] = {}
        self._sequence = 0
        self._entry_count = 0
        self._byte_count = 0

    def clear(self, key: ObjectHistoryKey) -> None:
        timeline = self._timelines.pop(key, None)
        if timeline is not None:
            self._entry_count -= len(timeline.entries)
            self._byte_count -= sum(entry.size for entry in timeline.entries)

    def can_undo(self, key: ObjectHistoryKey) -> bool:
        timeline = self._timelines.get(key)
        return timeline is not None and timeline.cursor > 0

    def can_redo(self, key: ObjectHistoryKey) -> bool:
        timeline = self._timelines.get(key)
        return timeline is not None and timeline.cursor < len(timeline.entries)

    def record(
        self,
        key: ObjectHistoryKey,
        before: VideoObjects,
        after: VideoObjects,
        *,
        before_selection: list[str],
        after_selection: list[str],
        frame: int,
    ) -> bool:
        before_digest = _fingerprint(before)
        after_digest = _fingerprint(after)
        if before_digest == after_digest:
            return False  # A no-op must not invalidate a pending redo.
        # Runs are detector provenance, not manually editable data. Do not
        # promise a partial undo if a caller accidentally records a new run.
        if before.runs != after.runs:
            self.clear(key)
            return True
        before_by_uid = {item.uid: item for item in before.objects}
        after_by_uid = {item.uid: item for item in after.objects}
        if len(before_by_uid) != len(before.objects) or len(after_by_uid) != len(after.objects):
            self.clear(key)
            return True
        changes = []
        for uid in dict.fromkeys([*before_by_uid, *after_by_uid]):
            old, new = before_by_uid.get(uid), after_by_uid.get(uid)
            if old == new:
                continue
            boxes_changed = old is None or new is None or old.boxes != new.boxes
            changes.append(
                _ObjectDelta(
                    uid,
                    _metadata(old),
                    _metadata(new),
                    [box.to_dict() for box in old.boxes] if boxes_changed and old else None,
                    [box.to_dict() for box in new.boxes] if boxes_changed and new else None,
                )
            )
        before_order = tuple(item.uid for item in before.objects)
        after_order = tuple(item.uid for item in after.objects)
        selected_before, selected_after = list(before_selection), list(after_selection)
        # Serialized payload accounting is deterministic and includes UID order,
        # selection and a fixed allowance for digests/entry bookkeeping.
        size = len(
            _encode(
                {
                    "changes": [
                        [d.uid, d.before, d.after, d.before_boxes, d.after_boxes] for d in changes
                    ],
                    "before_order": before_order,
                    "after_order": after_order,
                    "before_selection": selected_before,
                    "after_selection": selected_after,
                    "frame": frame,
                }
            )
        ) + 256
        if not self.max_entries or size > self.max_bytes:
            self.clear(key)
            return True
        timeline = self._timelines.get(key)
        if timeline and timeline.entries:
            expected = (
                timeline.entries[timeline.cursor - 1].after_digest
                if timeline.cursor
                else timeline.entries[0].before_digest
            )
            if before_digest != expected:
                self.clear(key)
                timeline = None
        if timeline is None:
            timeline = _Timeline()
            self._timelines[key] = timeline
        dropped = timeline.entries[timeline.cursor :]
        self._entry_count -= len(dropped)
        self._byte_count -= sum(entry.size for entry in dropped)
        del timeline.entries[timeline.cursor :]
        self._sequence += 1
        timeline.entries.append(
            _Entry(
                self._sequence,
                changes,
                before_order,
                after_order,
                before_digest,
                after_digest,
                selected_before,
                selected_after,
                frame,
                size,
            )
        )
        timeline.cursor += 1
        self._entry_count += 1
        self._byte_count += size
        self._trim()
        return True

    def _trim(self) -> None:
        while self._entry_count > self.max_entries or self._byte_count > self.max_bytes:
            key, timeline = min(
                self._timelines.items(), key=lambda pair: pair[1].entries[0].sequence
            )
            if timeline.cursor == 0:
                # Evicting the next redo also invalidates every dependent redo.
                self.clear(key)
                continue
            entry = timeline.entries.pop(0)
            timeline.cursor -= 1
            self._entry_count -= 1
            self._byte_count -= entry.size
            if not timeline.entries:
                self._timelines.pop(key)

    def undo(self, key: ObjectHistoryKey, current: VideoObjects) -> ObjectEditResult | None:
        return self._restore(key, current, undo=True)

    def redo(self, key: ObjectHistoryKey, current: VideoObjects) -> ObjectEditResult | None:
        return self._restore(key, current, undo=False)

    def _restore(
        self, key: ObjectHistoryKey, current: VideoObjects, *, undo: bool
    ) -> ObjectEditResult | None:
        timeline = self._timelines.get(key)
        if timeline is None or (not self.can_undo(key) if undo else not self.can_redo(key)):
            return None
        entry = timeline.entries[timeline.cursor - 1 if undo else timeline.cursor]
        expected = entry.after_digest if undo else entry.before_digest
        if _fingerprint(current) != expected:
            self.clear(key)
            return None
        result = current.copy()
        objects = {item.uid: item for item in result.objects}
        for change in entry.changes:
            metadata = change.before if undo else change.after
            boxes = change.before_boxes if undo else change.after_boxes
            if metadata is None:
                objects.pop(change.uid, None)
                continue
            payload = dict(metadata)
            payload["boxes"] = (
                boxes if boxes is not None else [box.to_dict() for box in objects[change.uid].boxes]
            )
            objects[change.uid] = DetectedObject.from_dict(payload)
        order = entry.before_order if undo else entry.after_order
        result.objects = [objects[uid] for uid in order]
        if _fingerprint(result) != (entry.before_digest if undo else entry.after_digest):
            self.clear(key)
            return None
        timeline.cursor += -1 if undo else 1
        return ObjectEditResult(
            result,
            list(entry.before_selection if undo else entry.after_selection),
            entry.frame,
        )


__all__ = ["ObjectEditHistory", "ObjectEditResult", "ObjectHistoryKey"]
