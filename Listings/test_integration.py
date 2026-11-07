"""Identity propagation and publication, end to end.

These tests exercise the chain the pipeline actually runs -- dedup, closure
voting, publication -- instead of one module at a time, because the bug they
guard against (ats_id silently dropped on the way out) lived in the seam
between two modules that were each individually fine.
"""

import json
from datetime import UTC, datetime, timedelta

import closure
import dedup
import freshness
import identity
import pandas as pd
import readme_generation
import shards

NOW = datetime.now(UTC)


def _row(company, role, link, ats_type="", ats_id="", days_old=1, **extra):
    row = {
        "company": company,
        "role": role,
        "location": "New York, NY",
        "date": (NOW - timedelta(days=days_old)).isoformat(),
        "link": link,
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": "US",
        "description": "",
        "ats_type": ats_type,
        "ats_id": ats_id,
        "job_type": "internship",
        "source": "jobhive",
        "observed_at": NOW.isoformat(),
    }
    row.update(extra)
    return row


def test_identity_fills_from_url_and_collapses_cross_source():
    frame = pd.DataFrame([
        # jobhive: id present, link on a different host
        _row("Acme", "SWE Intern", "https://jobs.acme.example/req/401",
             "greenhouse", "401"),
        # probe: same requisition, no id, canonical ATS URL
        _row("Acme", "SWE Intern", "https://boards.greenhouse.io/acme/jobs/401",
             source="ats"),
        # probe: distinct requisition, same title, must survive
        _row("Acme", "SWE Intern", "https://boards.greenhouse.io/acme/jobs/402",
             source="ats"),
    ])
    out = dedup.dedupe(frame)
    assert len(out) == 2, out[["link", "ats_id"]]
    # Dedup decides which row survives; identity.fill is what stamps the id on
    # the survivor (the probe copy never carried one).
    out = identity.fill(out)
    assert sorted(out["ats_id"]) == ["401", "402"]
    assert identity.coverage(out)["pct"] == 100.0


def test_closed_row_dropped_and_unvoted_board_survives():
    closure.reset()
    closure.record_snapshot("Greenhouse", "Acme", ["401"])
    frame = identity.fill(pd.DataFrame([
        _row("Acme", "Open role", "https://boards.greenhouse.io/acme/jobs/401"),
        _row("Acme", "Closed role", "https://boards.greenhouse.io/acme/jobs/999"),
        # Different employer: its board never voted, so it cannot be closed.
        _row("Beta", "Unprobed role", "https://boards.greenhouse.io/beta/jobs/7"),
    ]))
    kept, dropped, unverified = closure.filter_closed(frame)
    assert dropped == 1
    # Beta's board never voted, so its row is kept and is not counted as
    # unverifiable (it carries a greenhouse id; only the vote is missing).
    assert sorted(kept["role"]) == ["Open role", "Unprobed role"]
    assert unverified == 0


def test_published_record_keeps_identity_and_timestamps():
    closure.reset()
    frame = identity.fill(pd.DataFrame([
        _row("Acme", "SWE Intern", "https://boards.greenhouse.io/acme/jobs/401"),
    ]))
    frame = closure.drop_internal_columns(frame)
    frame = freshness.attach_row_times(
        frame, {frame.index[0]: "2026-11-01T00:00:00+00:00"}, NOW.isoformat(),
        {"jobhive", "ats"})
    records = readme_generation._build_json_records(frame)
    assert len(records) == 1
    record = records[0]
    assert record["ats_type"] == "greenhouse"
    assert record["ats_id"] == "401"
    assert record["first_seen_at"] == "2026-11-01T00:00:00+00:00"
    assert record["last_seen_at"] == NOW.isoformat()
    assert record["verified_at"] == NOW.isoformat()


def test_unfetched_source_never_claims_verification():
    frame = pd.DataFrame([_row("Acme", "SWE Intern",
                               "https://boards.greenhouse.io/acme/jobs/401",
                               source="ecr")])
    out = freshness.attach_row_times(
        frame, {frame.index[0]: "2026-11-01T00:00:00+00:00"}, NOW.isoformat(),
        {"jobhive"}, lambda row: row.get("source"))
    assert out.at[frame.index[0], "verified_at"] == ""
    assert out.at[frame.index[0], "last_seen_at"] == NOW.isoformat()


def test_stale_listing_is_not_published():
    frame = pd.DataFrame([
        _row("Acme", "Old role", "https://boards.greenhouse.io/acme/jobs/1", days_old=400),
        _row("Acme", "Fresh role", "https://boards.greenhouse.io/acme/jobs/2", days_old=1),
    ])
    records = readme_generation._build_json_records(frame)
    assert [r["role"] for r in records] == ["Fresh role"]


def test_publication_is_consistent_between_feeds_and_shards(tmp_path):
    frame = identity.fill(pd.DataFrame([
        _row("Acme", "SWE Intern", "https://boards.greenhouse.io/acme/jobs/401"),
        _row("Beta", "Data Intern", "https://jobs.lever.co/beta/abc-123", days_old=2),
    ]))
    frame = freshness.attach_row_times(frame, {}, NOW.isoformat(), {"jobhive"})
    readme_generation.write_listings_json(frame, output_dir=str(tmp_path),
                                          mirror_dataframe=frame)

    with open(tmp_path / "pages" / "listings.json", encoding="utf-8") as handle:
        listings = json.load(handle)
    with open(tmp_path / "pages" / "searchtern.json", encoding="utf-8") as handle:
        searchtern = json.load(handle)
    assert len(listings) == len(searchtern) == 2
    assert all(row["ats_id"] for row in listings)
    assert all(row["ats_type"] in ("greenhouse", "lever") for row in searchtern)

    manifest = shards.publish(frame, str(tmp_path / "pages"), name="listings")
    assert manifest["rows"] == 2
    assert sum(part["rows"] for part in manifest["shards"]) == 2


def test_zero_yield_boards_park_and_return_after_probation():
    import time

    import board_scheduler

    now = time.time()
    state = {"Greenhouse|dead": {"at": now - 60, "rows": 0, "zero": 3},
             "Greenhouse|alive": {"at": now - 60, "rows": 4, "zero": 0}}
    parked = board_scheduler.skip_boards(state, now=now)
    assert parked == {"Greenhouse|dead"}
    boards = [("Dead", "Greenhouse", "dead"), ("Alive", "Greenhouse", "alive")]
    batch, deferred = board_scheduler.plan(boards, state, cap=10, now=now, skip=parked)
    assert [b[2] for b in batch] == ["alive"] and deferred == []

    stale = {"Greenhouse|dead": {"at": now - 3600 * 24 * 8, "rows": 0, "zero": 9}}
    assert board_scheduler.skip_boards(stale, now=now) == set()


def _main():
    import tempfile
    from pathlib import Path

    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if "tmp_path" in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
        else:
            fn()
        print(f"  PASS  {name}")
    closure.reset()


if __name__ == "__main__":
    _main()
