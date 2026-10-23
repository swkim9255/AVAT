"""Pure-Python checks: these tests never start a decoder or touch user media."""

import json

import pytest

from tools.benchmark_seek import decode_worker_output, sample_frames, summarize


def test_sample_counts_and_bounds():
    for size in (1, 3, 1822):
        targets = sample_frames(size)
        assert {key: len(value) for key, value in targets.items()} == {
            "jump": 12, "drag": 40, "backward": 20}
        assert all(0 <= frame < size for values in targets.values() for frame in values)
        assert sample_frames(size) == targets
    with pytest.raises(ValueError):
        sample_frames(0)


def test_summary_nearest_rank_p95_and_empty():
    rows = [{"total_ms": value} for value in range(1, 21)]
    assert summarize(rows) == {"mean_ms": 10.5, "median_ms": 10.5,
                               "p95_ms": 19.0, "max_ms": 20.0, "calls": 20}
    assert summarize([])["calls"] == 0
    assert summarize([])["mean_ms"] is None


def test_partial_child_output_retains_samples_without_completion():
    stdout = "noise\n" + "\n".join(json.dumps(event) for event in [
        {"event": "metadata", "backend": "FFMPEG", "actual_threads": 16,
         "open_ms": 12.0, "video": {"name": "example.mp4"}},
        {"event": "sample", "phase": "drag", "row": {"frame": 5, "total_ms": 25}},
    ]) + '\n{"event": "sample"'
    run, metadata = decode_worker_output(stdout)
    assert run["backend"] == "FFMPEG"
    assert run["actual_threads"] == 16
    assert run["phases"]["drag"] == [{"frame": 5, "total_ms": 25}]
    assert not run["completed"]
    assert metadata["video"]["name"] == "example.mp4"


def test_completed_and_error_events():
    run, _ = decode_worker_output('{"event":"error","message":"read failed"}\n'
                                  '{"event":"done"}\n')
    assert run["errors"] == ["read failed"]
    assert run["completed"]
