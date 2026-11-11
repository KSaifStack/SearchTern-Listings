"""A partial run cannot replace a larger valid publication with a smaller one.

The snapshot ledger is what lets a light run republish heavy/medium rows. On a
fresh checkout with no sidecars every cache is uneligible, so a run only knows
what it fetched live — the 2026-11-07..09 swing from ~30k to ~9k published rows.
`prepare_listings_json` now refuses that write and leaves the previous
generation byte-identical.
"""

import json

import pandas as pd
import pytest
import readme_generation


def _frame(rows, offset=0):
    now = pd.Timestamp.now(tz="UTC").isoformat()
    return pd.DataFrame([
        {
            "company": f"Employer {i + offset}",
            "role": "Finance Intern",
            "location": "New York, NY",
            "date": now,
            "link": f"https://example.com/job/{i + offset}",
            "is_remote": "false",
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "country_iso": "US",
            "job_type": "internship",
            "source": "jobhive",
            "verified_at": now,
        }
        for i in range(rows)
    ])


def test_smaller_publication_is_refused_and_previous_files_survive(tmp_path):
    readme_generation.write_listings_json(_frame(600), output_dir=str(tmp_path))
    feed = tmp_path / "pages" / "searchtern.json"
    manifest = tmp_path / "pages" / "feed-manifest.json"
    before_feed = feed.read_bytes()
    before_manifest = manifest.read_bytes()
    assert len(json.loads(before_feed)) == 600

    with pytest.raises(readme_generation.PublicationRejected) as excinfo:
        readme_generation.write_listings_json(_frame(100, offset=1000), output_dir=str(tmp_path))

    assert "retention floor" in str(excinfo.value)
    assert feed.read_bytes() == before_feed
    assert manifest.read_bytes() == before_manifest


def test_retention_gate_can_be_disabled(monkeypatch, tmp_path):
    readme_generation.write_listings_json(_frame(600), output_dir=str(tmp_path))
    monkeypatch.setenv("SEARCHTERN_MIN_RETAINED", "0")
    readme_generation.write_listings_json(_frame(100, offset=1000), output_dir=str(tmp_path))
    assert len(json.loads((tmp_path / "pages" / "searchtern.json").read_text())) == 100


def test_publication_carries_coverage_for_the_same_generation(tmp_path):
    readme_generation.write_listings_json(_frame(600), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    manifest = json.loads((pages / "feed-manifest.json").read_text())
    coverage = json.loads((pages / "coverage.json").read_text())

    assert coverage["run"]["generation"] == manifest["run_id"]
    assert coverage["generation"] == manifest["run_id"]
    assert coverage["run"]["published"] == {
        "listings": manifest["published"]["listings"]["rows"],
        "searchtern": manifest["published"]["searchtern"]["rows"],
    }
    assert coverage["totals"]["published"] == manifest["published"]["searchtern"]["rows"]
    assert any(row["name"] == "Finance, Accounting & Banking"
               for row in coverage["career_families"])
    assert coverage["verification"]["verified"] == coverage["totals"]["verified"]
