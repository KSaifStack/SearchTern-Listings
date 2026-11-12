"""Release-asset delivery: the manifest is the contract, not the checkout copy."""
import json
from datetime import UTC, datetime, timedelta

import feed_manifest
import pandas as pd
import pytest
import readme_generation
import readme_rendering


def _frame(rows=2):
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


def _release_env(monkeypatch):
    monkeypatch.setenv("SEARCHTERN_FEED_DELIVERY", "release")
    monkeypatch.setenv("SEARCHTERN_RELEASE_TAG", "feed-test")
    monkeypatch.setenv("SEARCHTERN_REPO", "owner/repo")


def test_release_manifest_names_assets_and_ignores_stale_checkout_copy(tmp_path, monkeypatch):
    _release_env(monkeypatch)
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    manifest = json.loads((pages / "feed-manifest.json").read_text())

    assert manifest["delivery"] == {
        "mode": "release",
        "repository": "owner/repo",
        "tag": "feed-test",
    }
    for name in ("searchtern", "listings"):
        assert manifest["published"][name]["url"] == (
            f"https://github.com/owner/repo/releases/download/feed-test/{name}.json"
        )

    # A fresh checkout has the previous compatibility files. They are not the
    # published artifact in release mode and must not block the next publish.
    (pages / "searchtern.json").write_text("stale compatibility copy", encoding="utf-8")
    (pages / "listings.json").write_text("stale compatibility copy", encoding="utf-8")
    feed_manifest.verify(str(pages), check_rows=True)


def test_release_manifest_requires_asset_metadata(tmp_path, monkeypatch):
    _release_env(monkeypatch)
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    manifest_path = tmp_path / "pages" / "feed-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["published"]["searchtern"].pop("url")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.verify(str(tmp_path / "pages"))


def test_release_readme_links_the_asset(monkeypatch):
    _release_env(monkeypatch)
    header = readme_rendering.build_header(1, 1, 2)
    assert "https://github.com/owner/repo/releases/download/feed-test/searchtern.json" in header
    assert "raw.githubusercontent.com/KSaifStack/SearchTern-Listings/main/pages/searchtern.json" not in header
