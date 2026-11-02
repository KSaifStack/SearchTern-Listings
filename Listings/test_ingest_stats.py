"""Self-check for the ingestion observability collector. Stdlib only.

Run: python test_ingest_stats.py
"""
import json
import os
import tempfile

import ingest_stats


def test_counts_accumulate():
    ingest_stats.reset()
    ingest_stats.record("x", discovered=10, valid=4)
    ingest_stats.record("x", discovered=5, valid=3)
    row = ingest_stats.snapshot()["sources"][0]
    assert row["discovered"] == 15 and row["valid"] == 7
    assert row["calls"] == 2


def test_status_and_notes():
    ingest_stats.reset()
    ingest_stats.record("y", discovered=1, valid=1, status="ok")
    ingest_stats.record("y", status="rate_limited", notes="429 after 40 pages")
    row = ingest_stats.snapshot()["sources"][0]
    assert row["status"] == "rate_limited", row["status"]
    assert row["notes"] == ["429 after 40 pages"]


def test_new_per_second_only_with_time():
    ingest_stats.reset()
    ingest_stats.record("z", new=10)
    assert ingest_stats.snapshot()["sources"][0]["new_per_second"] is None
    ingest_stats.reset()
    ingest_stats.record("z", new=10, seconds=2.0)
    assert ingest_stats.snapshot()["sources"][0]["new_per_second"] == 5.0


def test_merge_digest_attributes_new_to_source():
    ingest_stats.reset()
    ingest_stats.merge_digest({"jobhive": 7, "priority": 3}, closed=2, dead_links=1)
    snap = ingest_stats.snapshot()
    by_name = {r["source"]: r for r in snap["sources"]}
    assert by_name["jobhive"]["new"] == 7 and by_name["priority"]["new"] == 3
    assert snap["totals"]["closed"] == 2 and snap["totals"]["dead_links"] == 1


def test_write_json_artifact():
    ingest_stats.reset()
    ingest_stats.record("a", discovered=4, valid=2, seconds=1.0, status="empty")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "source_stats.json")
        totals = ingest_stats.write_json(path, run_meta={"tier": "priority"})
        assert totals["sources"] == 1 and totals["failed"] == 1, totals
        with open(path) as f:
            payload = json.load(f)
        assert payload["run"]["tier"] == "priority"
        assert "generated_at" in payload and "elapsed_seconds" in payload
        assert payload["sources"][0]["source"] == "a"


def test_recording_never_raises():
    ingest_stats.reset()
    # A non-numeric count must be absorbed, not crash a four-hour run: the
    # source still appears (it did run), it just contributes no count.
    ingest_stats.record("b", discovered="not a number")
    sources = ingest_stats.snapshot()["sources"]
    assert len(sources) == 1 and sources[0]["discovered"] == 0, sources


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
    ingest_stats.reset()
    print("\n  test_ingest_stats: all checks passed")
