"""Manifest verification and count reconciliation with legacy artifacts."""
import json
from datetime import UTC, datetime, timedelta

import feed_manifest
import pandas as pd
import pytest
import readme_generation


def _frame(rows=1):
    now = datetime.now(UTC)
    return pd.DataFrame([{
        "company": f"Acme {i}",
        "role": "SWE Intern",
        "location": "Austin, TX",
        "date": (now - timedelta(days=1)).isoformat(),
        "link": f"https://example.test/{i}",
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": "US",
        "job_type": "internship",
        "source": "jobhive",
        "ats_type": "greenhouse",
        "ats_id": str(i),
    } for i in range(rows)])


def _legacy(pages, published):
    for name in ("run_meta.json", "health.json", "source_stats.json"):
        payload = {"run": {"published": published}}
        if name == "run_meta.json":
            payload = {"total": 999, "published": published}
        (pages / name).write_text(json.dumps(payload), encoding="utf-8")


def test_reconcile_patches_all_legacy_count_artifacts(tmp_path):
    readme_generation.write_listings_json(_frame(2), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    _legacy(pages, {"listings": 999, "searchtern": 999})

    manifest = feed_manifest.reconcile(str(pages), check_rows=True)

    expected = {"listings": 2, "searchtern": 2}
    assert manifest["published"]["searchtern"]["rows"] == 2
    assert manifest["recovery"]["path"] == "pages/last-known-good"
    assert manifest["recovery"]["manifest"] == "pages/last-known-good/current.json"
    run_meta = json.loads((pages / "run_meta.json").read_text())
    assert run_meta["published"] == expected
    assert run_meta["total"] == 999
    assert run_meta["total_stage"] == "pre_write_candidate_rows"
    assert run_meta["published_manifest"] == "pages/feed-manifest.json"
    for name in ("health.json", "source_stats.json"):
        payload = json.loads((pages / name).read_text())
        assert payload["run"]["published"] == expected
        assert payload["run"]["manifest"] == "pages/feed-manifest.json"


def test_wrong_manifest_row_count_fails_before_legacy_update(tmp_path):
    readme_generation.write_listings_json(_frame(1), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    _legacy(pages, {"listings": 999, "searchtern": 999})
    manifest_path = pages / "feed-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["published"]["searchtern"]["rows"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.reconcile(str(pages), check_rows=True)

    run_meta = json.loads((pages / "run_meta.json").read_text())
    assert run_meta["published"] == {"listings": 999, "searchtern": 999}


def test_tampered_feed_hash_fails_verification(tmp_path):
    readme_generation.write_listings_json(_frame(1), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    feed = pages / "searchtern.json"
    feed.write_text(feed.read_text() + "\n", encoding="utf-8")

    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.verify(str(pages), check_rows=True)
