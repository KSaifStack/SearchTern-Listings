"""Published-feed contract: identity, closure, honest status and field coverage.

Each case here covers an audit finding from the 2026-11-11 feed: no stable job
key, closed_at always empty, a bare `degraded` status, and a coverage report
that called snapshot freshness "verified" while every industry showed 0.
"""
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import readme_generation


def _frame(**overrides):
    row = {
        "company": "Acme",
        "role": "SWE Intern",
        "location": "Austin, TX",
        "date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "link": "https://boards.greenhouse.io/acme/jobs/4012345",
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": "US",
        "job_type": "internship",
        "source": "jobhive",
        "description": "Build software with the team.",
        "first_seen_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _write(tmp_path, frame=None, **kwargs):
    readme_generation.write_listings_json(
        frame if frame is not None else _frame(), output_dir=str(tmp_path),
        **kwargs)
    pages = tmp_path / "pages"
    return (
        json.loads((pages / "searchtern.json").read_text()),
        json.loads((pages / "feed-manifest.json").read_text()),
        json.loads((pages / "coverage.json").read_text()),
    )


def test_feed_owns_a_stable_id_and_employer_identity(tmp_path):
    rows, _manifest, _coverage = _write(tmp_path)
    row = rows[0]

    assert row["id"] == "greenhouse:4012345"
    assert row["ats_type"] == "greenhouse"
    assert row["ats_id"] == "4012345"
    assert row["company_key"] == "acme"
    assert "company_domain" in row


def test_a_row_without_an_ats_id_gets_a_url_stable_id(tmp_path):
    rows, _manifest, _coverage = _write(
        tmp_path, frame=_frame(link="https://example.com/jobs/42"))

    assert rows[0]["id"].startswith("url:")
    assert len(rows[0]["id"]) == len("url:") + 16


def test_closed_at_is_carried_not_overwritten(tmp_path):
    stamp = "2026-11-01T00:00:00+00:00"
    rows, _manifest, _coverage = _write(tmp_path, frame=_frame(closed_at=stamp))

    assert rows[0]["closed_at"] == stamp


def test_closed_at_defaults_to_empty_for_an_open_row(tmp_path):
    rows, _manifest, _coverage = _write(tmp_path)

    assert rows[0]["closed_at"] == ""


def test_degraded_status_says_which_kind_of_gap(tmp_path):
    report = {
        "generated_at": "2026-11-13T00:00:00+00:00",
        "count": 3,
        "expected": ["jobhive", "heavy", "echo"],
        "fresh": ["jobhive"],
        "expired": ["echo"],
        "empty": [],
        "missing": ["heavy"],
        "rejected": [],
        "invalid": ["echo"],
        "seeded": [],
    }
    _rows, manifest, _coverage = _write(tmp_path, snapshots_report=report)

    assert manifest["status"] == "degraded"
    assert "missing:heavy" in manifest["status_reasons"]
    assert "expired:echo" in manifest["status_reasons"]
    assert manifest["degradation"]["snapshot_missing"] == ["heavy"]
    assert manifest["degradation"]["snapshot_expired"] == ["echo"]


def test_a_seeded_tier_is_reported_as_never_fetched(tmp_path):
    report = {
        "generated_at": "2026-11-13T00:00:00+00:00",
        "count": 2,
        "expected": ["jobhive", "light"],
        "fresh": ["jobhive"],
        "expired": ["light"],
        "empty": [],
        "missing": [],
        "rejected": [],
        "invalid": ["light"],
        "seeded": ["light"],
    }
    _rows, manifest, _coverage = _write(tmp_path, snapshots_report=report)

    assert manifest["degradation"]["never_fetched"] == ["light"]
    assert "never_fetched:light" in manifest["status_reasons"]
    assert manifest["degradation"]["snapshot_expired"] == []


def test_coverage_reports_snapshot_freshness_separately_from_liveness(tmp_path):
    report = {
        "generated_at": "2026-11-13T00:00:00+00:00",
        "count": 1,
        "expected": ["jobhive"],
        "fresh": ["jobhive"],
        "expired": [],
        "empty": [],
        "missing": [],
        "rejected": [],
        "invalid": [],
        "seeded": [],
    }
    _rows, _manifest, coverage = _write(
        tmp_path, snapshots_report=report, liveness={"checked": 4, "dead": 1})
    verification = coverage["verification"]

    assert "verified" not in verification
    assert verification["snapshot_fresh"] == coverage["totals"]["snapshot_fresh"]
    assert verification["liveness"]["checked"] == 4
    assert verification["liveness"]["dead"] == 1
    # The fallback family is a family, so the advertised count includes it.
    assert coverage["totals"]["career_families"] == len(coverage["career_families"])
    assert sum(row["snapshot_fresh"] for row in coverage["industries"]) == (
        coverage["totals"]["snapshot_fresh"])


def test_field_coverage_is_reported_per_field(tmp_path):
    _rows, _manifest, coverage = _write(tmp_path)
    fields = {row["field"]: row for row in coverage["fields"]}

    assert fields["description"]["filled"] == 1
    assert fields["salary_min"]["filled"] == 0
    assert fields["first_seen_at"]["filled"] == 1
    assert fields["closed_at"]["filled"] == 0
