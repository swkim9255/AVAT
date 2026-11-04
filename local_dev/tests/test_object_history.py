"""Pure domain checks for bounded object edit undo/redo."""

from __future__ import annotations

import pytest

from investigator.object_detection import (
    ROLE_COLLISION,
    ROLE_TRIGGER,
    DetectedObject,
    DetectionRun,
    LabelRef,
    ObjectBox,
    VideoObjects,
    delete_box,
)
from investigator.object_history import ObjectEditHistory

KEY = ("C:/project", "case-1", "C:/video.mp4")


def _object(uid: str, frames: tuple[int, ...] = (3,)) -> DetectedObject:
    return DetectedObject(
        uid=uid,
        object_id=uid,
        role=ROLE_TRIGGER,
        label=None,
        boxes=[ObjectBox(frame, (10.5, 20.0, 30.0, 40.0), 0.7) for frame in frames],
    )


def _record(history, before, after, key=KEY, frame=3):  # type: ignore[no-untyped-def]
    return history.record(
        key,
        before,
        after,
        before_selection=[item.uid for item in before.objects],
        after_selection=[item.uid for item in after.objects],
        frame=frame,
    )


def test_add_undo_redo_restores_uid_selection_frame_and_does_not_mutate():
    history = ObjectEditHistory()
    before = VideoObjects()
    after = VideoObjects([_object("new")])
    original = after.to_dict()
    assert _record(history, before, after, frame=17)
    assert history.can_undo(KEY) and not history.can_redo(KEY)
    undone = history.undo(KEY, after)
    assert undone is not None and undone.objects.objects == []
    assert undone.selection == [] and undone.frame == 17
    assert after.to_dict() == original
    redone = history.redo(KEY, undone.objects)
    assert redone is not None and redone.objects.to_dict() == original
    assert redone.selection == ["new"] and redone.frame == 17
    assert redone.objects.objects[0] is not after.objects[0]
    redone.objects.objects[0].object_id = "caller mutation"
    assert after.to_dict() == original


def test_noop_does_not_consume_entry_or_clear_redo():
    history = ObjectEditHistory()
    before = VideoObjects()
    after = VideoObjects([_object("new")])
    _record(history, before, after)
    undone = history.undo(KEY, after)
    assert undone is not None
    assert not _record(history, undone.objects, undone.objects.copy())
    assert history.can_redo(KEY) and history._entry_count == 1
    assert history.redo(KEY, undone.objects) is not None


def test_branch_after_undo_invalidates_previous_redo():
    history = ObjectEditHistory()
    empty = VideoObjects()
    one = VideoObjects([_object("one")])
    two = VideoObjects([_object("one"), _object("two")])
    _record(history, empty, one)
    _record(history, one, two)
    undone = history.undo(KEY, two)
    assert undone is not None
    alternate = VideoObjects([_object("one"), _object("alternate")])
    _record(history, undone.objects, alternate)
    assert not history.can_redo(KEY)
    assert history.redo(KEY, alternate) is None
    assert history._entry_count == 2


def test_deletion_restores_original_order_and_last_frame_object():
    history = ObjectEditHistory()
    before = VideoObjects([_object("a"), _object("b"), _object("c")])
    after = delete_box(before, "b", 3)
    assert [item.uid for item in after.objects] == ["a", "c"]
    _record(history, before, after)
    undone = history.undo(KEY, after)
    assert undone is not None
    assert undone.objects.to_dict() == before.to_dict()
    assert [item.uid for item in undone.objects.objects] == ["a", "b", "c"]
    redone = history.redo(KEY, undone.objects)
    assert redone is not None and redone.objects.to_dict() == after.to_dict()


def test_only_one_box_deleted_preserves_other_frames_and_precise_coordinates():
    history = ObjectEditHistory()
    before = VideoObjects([_object("a", (3, 5, 9))])
    after = delete_box(before, "a", 5)
    _record(history, before, after)
    undone = history.undo(KEY, after)
    assert undone is not None and undone.objects.to_dict() == before.to_dict()
    assert undone.objects.objects[0].boxes[0].bbox[0] == 10.5


def test_metadata_only_does_not_retain_existing_boxes_or_unchanged_objects():
    history = ObjectEditHistory()
    before = VideoObjects([_object("large", tuple(range(3000))), _object("unchanged")])
    after = before.copy()
    changed = after.objects[0]
    changed.role = ROLE_COLLISION
    changed.label = LabelRef("collision_targets", "vehicle.passenger")
    changed.reviewed = True
    _record(history, before, after)
    entry = history._timelines[KEY].entries[0]
    assert [delta.uid for delta in entry.changes] == ["large"]
    assert entry.changes[0].before_boxes is None and entry.changes[0].after_boxes is None
    assert entry.size < 2000
    undone = history.undo(KEY, after)
    assert undone is not None and undone.objects.to_dict() == before.to_dict()
    redone = history.redo(KEY, undone.objects)
    assert redone is not None and redone.objects.to_dict() == after.to_dict()


def test_runs_are_preserved_and_copied_from_live_document():
    history = ObjectEditHistory()
    before = VideoObjects(
        [_object("a")],
        [DetectionRun("r", ROLE_TRIGGER, 1, 9, 2, ["passenger"], created_at="fixed")],
    )
    after = before.copy()
    after.objects[0].reviewed = True
    _record(history, before, after)
    undone = history.undo(KEY, after)
    assert undone is not None
    assert undone.objects.runs == after.runs
    assert undone.objects.runs[0] is not after.runs[0]
    assert undone.objects.runs[0].classes is not after.runs[0].classes


@pytest.mark.parametrize(
    "other_key",
    [
        ("C:/other", "case-1", "C:/video.mp4"),
        ("C:/project", "case-2", "C:/video.mp4"),
        ("C:/project", "case-1", "C:/other.mp4"),
    ],
)
def test_contexts_do_not_share_history(other_key):
    history = ObjectEditHistory()
    before, after = VideoObjects(), VideoObjects([_object("a")])
    _record(history, before, after)
    assert history.undo(other_key, after) is None
    _record(history, before, after, key=other_key)
    history.clear(KEY)
    assert not history.can_undo(KEY) and history.can_undo(other_key)


@pytest.mark.parametrize("mutation", ["object", "run", "order"])
def test_external_changes_invalidate_undo(mutation):
    history = ObjectEditHistory()
    before = VideoObjects([_object("a"), _object("b")])
    after = before.copy()
    after.objects[0].reviewed = True
    _record(history, before, after)
    external = after.copy()
    if mutation == "object":
        external.objects[1].object_id = "external change to unchanged object"
    elif mutation == "run":
        external.runs.append(DetectionRun("r", ROLE_TRIGGER, 1, 9, 1, [], created_at="fixed"))
    else:
        external.objects.reverse()
    assert history.undo(KEY, external) is None
    assert not history.can_undo(KEY) and not history.can_redo(KEY)


def test_external_changes_invalidate_redo():
    history = ObjectEditHistory()
    before, after = VideoObjects(), VideoObjects([_object("a")])
    _record(history, before, after)
    undone = history.undo(KEY, after)
    assert undone is not None
    external = VideoObjects([_object("outside")])
    assert history.redo(KEY, external) is None
    assert not history.can_redo(KEY)


def test_new_edit_after_external_change_starts_fresh_baseline():
    history = ObjectEditHistory()
    empty, one = VideoObjects(), VideoObjects([_object("a")])
    _record(history, empty, one)
    external = VideoObjects([_object("external")])
    after = external.copy()
    after.objects[0].reviewed = True
    _record(history, external, after)
    assert history._entry_count == 1
    undone = history.undo(KEY, after)
    assert undone is not None and undone.objects.to_dict() == external.to_dict()
    assert history.undo(KEY, undone.objects) is None


def test_record_is_detached_from_later_caller_mutation():
    history = ObjectEditHistory()
    before, after = VideoObjects(), VideoObjects([_object("a")])
    original = after.copy()
    before_selection, after_selection = [], ["a"]
    history.record(
        KEY,
        before,
        after,
        before_selection=before_selection,
        after_selection=after_selection,
        frame=3,
    )
    after.objects[0].object_id = "mutated"
    after_selection.clear()
    before_selection.append("unwanted")
    undone = history.undo(KEY, original)
    assert undone is not None and undone.selection == []
    redone = history.redo(KEY, undone.objects)
    assert redone is not None and redone.selection == ["a"]
    assert redone.objects.to_dict() == original.to_dict()


def test_reordering_without_object_changes_is_reversible():
    history = ObjectEditHistory()
    before = VideoObjects([_object("a"), _object("b")])
    after = before.copy()
    after.objects.reverse()
    assert _record(history, before, after)
    assert history._timelines[KEY].entries[0].changes == []
    undone = history.undo(KEY, after)
    assert undone is not None and undone.objects.to_dict() == before.to_dict()


def test_entry_limit_is_global_and_retained_undo_chain_remains_valid():
    history = ObjectEditHistory(max_entries=2)
    empty = VideoObjects()
    one = VideoObjects([_object("a")])
    two = VideoObjects([_object("a"), _object("b")])
    three = VideoObjects([_object("a"), _object("b"), _object("c")])
    _record(history, empty, one)
    _record(history, one, two)
    _record(history, two, three)
    assert history._entry_count == 2
    first = history.undo(KEY, three)
    assert first is not None and first.objects.to_dict() == two.to_dict()
    second = history.undo(KEY, first.objects)
    assert second is not None and second.objects.to_dict() == one.to_dict()
    assert history.undo(KEY, second.objects) is None
    redo1 = history.redo(KEY, second.objects)
    assert redo1 is not None and redo1.objects.to_dict() == two.to_dict()
    redo2 = history.redo(KEY, redo1.objects)
    assert redo2 is not None and redo2.objects.to_dict() == three.to_dict()


def test_eviction_of_next_redo_discards_dependent_redo_chain():
    history = ObjectEditHistory(max_entries=2)
    empty, one = VideoObjects(), VideoObjects([_object("a")])
    two = VideoObjects([_object("a"), _object("b")])
    _record(history, empty, one)
    _record(history, one, two)
    back1 = history.undo(KEY, two)
    assert back1 is not None
    back2 = history.undo(KEY, back1.objects)
    assert back2 is not None
    other = ("other workspace", "case", "video")
    _record(history, empty, one, key=other)
    assert not history.can_redo(KEY) and history.can_undo(other)
    assert history._entry_count == 1


def test_byte_limit_is_global_and_large_entry_clears_its_key_but_is_a_change():
    probe = ObjectEditHistory()
    empty, one = VideoObjects(), VideoObjects([_object("a")])
    _record(probe, empty, one)
    size = probe._byte_count
    history = ObjectEditHistory(max_bytes=size + 10)
    other = ("other", "case", "video")
    assert _record(history, empty, one)
    assert _record(history, empty, one, key=other)
    assert not history.can_undo(KEY) and history.can_undo(other)
    assert history._byte_count <= size + 10
    large = VideoObjects([_object("large", tuple(range(200)))])
    assert _record(history, one, large, key=other)
    assert not history.can_undo(other) and not history.can_redo(other)
    assert history._entry_count == 0 and history._byte_count == 0


@pytest.mark.parametrize("options", [{"max_entries": 0}, {"max_bytes": 0}])
def test_disabled_history_still_reports_actual_change(options):
    history = ObjectEditHistory(**options)
    assert _record(history, VideoObjects(), VideoObjects([_object("a")]))
    assert history._entry_count == 0
    assert not history.can_undo(KEY)


def test_run_change_clears_history_but_does_not_suppress_edit():
    history = ObjectEditHistory()
    empty, one = VideoObjects(), VideoObjects([_object("a")])
    _record(history, empty, one)
    after = one.copy()
    after.runs.append(DetectionRun("r", ROLE_TRIGGER, 1, 9, 1, [], created_at="fixed"))
    assert _record(history, one, after)
    assert not history.can_undo(KEY)


def test_mixed_sparse_metadata_box_add_delete_and_order_chain():
    history = ObjectEditHistory(max_entries=500)
    current = VideoObjects()
    states = [current.to_dict()]
    for number in range(30):
        after = current.copy()
        after.objects.append(_object(f"a{number}", (number, number + 40)))
        _record(history, current, after, frame=number)
        states.append(after.to_dict())
        current = after
        after = current.copy()
        after.objects[number // 2].reviewed = True
        after.objects[number // 2].label = LabelRef("obstacle_vehicle", "승용")
        if _record(history, current, after, frame=number):
            states.append(after.to_dict())
        current = after
        after = delete_box(current, f"a{number}", number)
        _record(history, current, after, frame=number)
        states.append(after.to_dict())
        current = after
        after = current.copy()
        after.objects.reverse()
        if _record(history, current, after, frame=number):
            states.append(after.to_dict())
        current = after
    for expected in reversed(states[:-1]):
        result = history.undo(KEY, current)
        assert result is not None and result.objects.to_dict() == expected
        current = result.objects
    assert not history.can_undo(KEY)
    for expected in states[1:]:
        result = history.redo(KEY, current)
        assert result is not None and result.objects.to_dict() == expected
        current = result.objects
    assert not history.can_redo(KEY)


def test_negative_limits_are_rejected():
    with pytest.raises(ValueError):
        ObjectEditHistory(max_entries=-1)
    with pytest.raises(ValueError):
        ObjectEditHistory(max_bytes=-1)
