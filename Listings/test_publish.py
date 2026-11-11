"""Publication union: a run cannot erase a source it did not fetch."""
import os

import pandas as pd
import publish
import pytest
import snapshots

NOW = pd.Timestamp("2026-11-09T12:00:00Z")
COLUMNS = ["company", "role", "location", "link", "source", "ats_type", "ats_id"]


def frame(specs, source):
    return pd.DataFrame(
        [[company, role, location, link, source, ats, req]
         for company, role, location, link, ats, req in specs],
        columns=COLUMNS,
    )


def write_snapshot(cache_dir, name, rows, age_hours=1):
    path = os.path.join(str(cache_dir), f"tier_{name}.parquet")
    os.makedirs(str(cache_dir), exist_ok=True)
    rows.to_parquet(path, index=False)
    snapshots.record(name, len(rows), path=path,
                     now=NOW - pd.Timedelta(hours=age_hours))
    return path


def jobhive_rows():
    return frame([
        ("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1", "greenhouse", "1"),
        ("Beta", "Data Intern", "Remote", "https://beta.test/1", "lever", "2"),
    ], source="jobhive")


def heavy_rows():
    return frame([
        ("Gamma", "SWE Intern", "Seattle, WA", "https://gamma.test/1", "ashby", "3"),
        ("Delta", "SWE Intern", "Remote", "https://delta.test/1", "greenhouse", "4"),
        ("Epsilon", "SWE Intern", "New York, NY", "https://epsilon.test/1", "lever", "5"),
    ], source="ats")


def test_markdown_feed_labels_use_markdown_precedence():
    assert publish._source_class("SimplifyJobs Summer 2027") == "markdown"
    assert publish._source_class("md") == "markdown"


def test_unfetched_heavy_snapshot_survives_a_light_live_frame(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "heavy", heavy_rows())

    merged = publish.build_publish_frame(jobhive_rows(), str(cache), now=NOW)

    assert len(merged) == 5
    assert set(merged["source"]) == {"jobhive", "ats"}


def test_direct_snapshot_wins_a_duplicate_with_live_jobhive(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "heavy", frame([
        ("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1", "greenhouse", "1"),
    ], source="ats"))

    merged = publish.build_publish_frame(jobhive_rows(), str(cache), now=NOW)

    assert len(merged) == 2
    row = merged[merged["link"] == "https://acme.test/1"].iloc[0]
    assert row["source"] == "ats"


def test_live_jobhive_beats_cached_markdown(tmp_path):
    link = "https://acme.test/1"
    cache = tmp_path / "cache"
    write_snapshot(cache, "light", frame([
        ("Acme", "SWE Intern", "Austin, TX", link, "greenhouse", "1"),
    ], source="md"))
    live = pd.concat([
        jobhive_rows(),
        frame([("Acme", "SWE Intern", "Austin, TX", link, "greenhouse", "1")],
              source="md"),
    ], ignore_index=True)

    merged = publish.build_publish_frame(live, str(cache), now=NOW)

    row = merged[merged["link"] == link].iloc[0]
    assert row["source"] == "jobhive"


def test_expired_snapshot_is_not_added(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "heavy", heavy_rows(), age_hours=80)  # TTL 48h

    merged = publish.build_publish_frame(jobhive_rows(), str(cache), now=NOW)

    assert len(merged) == 2
    assert set(merged["source"]) == {"jobhive"}


def test_missing_jobhive_blocks_publication(tmp_path):
    assessment = publish.assess(str(tmp_path / "missing"), now=NOW,
                                frame=jobhive_rows())

    assert assessment["status"] == "failed"
    assert assessment["blockers"][0] == {"source": "jobhive", "reason": "missing"}


def test_expired_jobhive_blocks_publication(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "jobhive", jobhive_rows(), age_hours=80)  # TTL 24h

    assessment = publish.assess(str(cache), now=NOW, frame=jobhive_rows())

    assert assessment["status"] == "failed"
    assert assessment["blockers"][0] == {"source": "jobhive", "reason": "expired"}


def test_rejected_jobhive_blocks_publication(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_jobhive.parquet")
    os.makedirs(cache, exist_ok=True)
    snapshots.record("jobhive", 2, status="rejected:shrunken", path=path, now=NOW)

    assessment = publish.assess(str(cache), now=NOW, frame=jobhive_rows())

    assert assessment["status"] == "failed"
    assert assessment["blockers"][0]["reason"] == "rejected:shrunken"


def test_missing_noncritical_source_degrades_but_publishes(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "jobhive", jobhive_rows())

    assessment = publish.assess(str(cache), now=NOW, frame=jobhive_rows())

    assert assessment["status"] == "degraded"
    assert "heavy" in assessment["report"]["missing"]
    assert assessment["blockers"] == []


def test_empty_union_is_a_failed_publication(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "jobhive", jobhive_rows())

    assessment = publish.assess(str(cache), now=NOW, frame=jobhive_rows().iloc[0:0])

    assert assessment["status"] == "failed"
    assert assessment["blockers"] == [{"source": "feed", "reason": "empty"}]


def test_no_snapshot_keeps_the_live_frame_unchanged(tmp_path):
    merged = publish.build_publish_frame(jobhive_rows(), str(tmp_path / "missing"), now=NOW)

    assert len(merged) == 2
    assert set(merged["source"]) == {"jobhive"}


def test_partial_run_publishes_known_rows_and_reports_degraded(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "jobhive", jobhive_rows())
    write_snapshot(cache, "medium", frame([
        ("Free Co", "SWE Intern", "Remote", "https://free.test/1", "greenhouse", "9"),
    ], source="freehire"))
    write_snapshot(cache, "heavy", heavy_rows(), age_hours=80)  # direct ATS TTL 48h
    snapshots.record("echo", 0, status="rejected:empty",
                     path=str(cache / "tier_echo.parquet"), now=NOW)

    merged = publish.build_publish_frame(jobhive_rows(), str(cache), now=NOW)
    assessment = publish.assess(str(cache), now=NOW, frame=merged)

    assert len(merged) == 3  # live jobhive (2) + fresh medium (1)
    assert set(merged["source"]) == {"jobhive", "freehire"}
    assert assessment["status"] == "degraded"
    assert assessment["blockers"] == []
    assert "heavy" in assessment["report"]["expired"]
    assert "echo" in assessment["report"]["rejected"]


def test_same_posting_in_live_and_snapshot_is_published_once(tmp_path):
    cache = tmp_path / "cache"
    write_snapshot(cache, "heavy", frame([
        ("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1", "greenhouse", "1"),
    ], source="ats"))

    merged = publish.build_publish_frame(jobhive_rows(), str(cache), now=NOW)

    assert len(merged) == 2
    assert merged["link"].is_unique


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
