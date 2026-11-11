"""Atomic feed writes: a failed write must leave the previous feed intact."""
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import feed_manifest
import pandas as pd
import pytest
import readme_generation


def _frame():
    return pd.DataFrame([{
        "company": "Acme",
        "role": "SWE Intern",
        "location": "Austin, TX",
        "date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "link": "https://example.test/1",
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": "US",
        "job_type": "internship",
        "source": "jobhive",
        "ats_type": "greenhouse",
        "ats_id": "1",
    }])


def test_write_listings_json_writes_both_feeds(tmp_path):
    result = readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))

    assert result == {"listings": 1, "searchtern": 1}
    for name in ("listings.json", "searchtern.json"):
        data = json.loads((tmp_path / "pages" / name).read_text())
        assert len(data) == 1


def test_manifest_reconciles_with_the_published_files(tmp_path):
    readme_generation.write_listings_json(
        _frame(), output_dir=str(tmp_path),
        snapshots_report={"count": 2, "fresh": ["jobhive"], "expired": [], "rejected": []},
    )

    pages = tmp_path / "pages"
    manifest = json.loads((pages / "feed-manifest.json").read_text())
    for name in ("listings", "searchtern"):
        text = (pages / f"{name}.json").read_text()
        assert manifest["published"][name]["rows"] == len(json.loads(text))
        assert manifest["published"][name]["bytes"] == len(text.encode("utf-8"))
        assert manifest["published"][name]["sha256"] == hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
    assert manifest["source_counts"] == {"jobhive": 1}
    snapshots = manifest["inputs"]["snapshots"]
    assert snapshots["count"] == 2
    assert snapshots["fresh"] == ["jobhive"]
    assert snapshots["expired"] == [] and snapshots["missing"] == []
    assert manifest["status"] == "ok"


def test_failed_single_write_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "feed.json"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    failed = False

    def fail_once(src, dst):
        nonlocal failed
        if str(dst) == str(target) and not failed:
            failed = True
            raise OSError("simulated disk failure")
        return real_replace(src, dst)

    monkeypatch.setattr(readme_generation.os, "replace", fail_once)
    with pytest.raises(OSError):
        readme_generation._atomic_write_text(str(target), "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak.*"))


def test_missing_required_source_keeps_previous_feed(tmp_path):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    before = {name: (pages / name).read_bytes()
              for name in ("listings.json", "searchtern.json", "feed-manifest.json")}
    other = _frame().assign(source="ats")

    with pytest.raises(readme_generation.PublicationRejected):
        readme_generation.write_listings_json(
            other, output_dir=str(tmp_path), required_sources=("jobhive",))

    after = {name: (pages / name).read_bytes() for name in before}
    assert after == before
    assert not list(pages.glob("*.tmp"))
    assert not list(pages.glob("*.bak.*"))


def test_empty_feed_keeps_previous_feed(tmp_path):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    before = (pages / "searchtern.json").read_bytes()

    with pytest.raises(readme_generation.PublicationRejected):
        readme_generation.write_listings_json(
            _frame().iloc[0:0], output_dir=str(tmp_path), minimum_rows=1)

    assert (pages / "searchtern.json").read_bytes() == before


def test_manifest_carries_previous_artifact_as_last_known_good(tmp_path):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    first = json.loads((pages / "feed-manifest.json").read_text())

    changed = _frame().assign(role="Backend Intern")
    readme_generation.write_listings_json(changed, output_dir=str(tmp_path))
    second = json.loads((pages / "feed-manifest.json").read_text())

    assert second["last_known_good"]["generated_at"] == first["generated_at"]
    assert second["last_known_good"]["published"] == first["published"]


def test_physical_lkg_restores_the_previous_generation(tmp_path):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    first = json.loads((tmp_path / "pages" / "feed-manifest.json").read_text())

    changed = _frame().assign(role="Backend Intern")
    readme_generation.write_listings_json(changed, output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    assert feed_manifest.has_last_known_good(str(pages))

    # Simulate a crash after one feed file was replaced by a later run.
    (pages / "searchtern.json").write_text("torn", encoding="utf-8")
    recovered = feed_manifest.recover(str(pages))

    assert recovered["generated_at"] == first["generated_at"]
    assert json.loads((pages / "searchtern.json").read_text())[0]["role"] == "SWE Intern"


def test_prepare_heals_a_torn_current_generation(tmp_path):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    readme_generation.write_listings_json(
        _frame().assign(role="Backend Intern"), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    (pages / "searchtern.json").write_text("torn", encoding="utf-8")

    feed_manifest.prepare(str(pages))

    rows = json.loads((pages / "searchtern.json").read_text())
    assert len(rows) == 1
    assert rows[0]["role"] == "SWE Intern"
    feed_manifest.verify(str(pages))


def test_recover_without_lkg_fails_loudly(tmp_path):
    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.recover(str(tmp_path / "pages"))


def test_prepare_without_lkg_refuses_to_overwrite_unknown_state(tmp_path):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    (pages / "searchtern.json").write_text("torn", encoding="utf-8")

    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.prepare(str(pages))


def test_manifest_replace_failure_rolls_back_all_three(tmp_path, monkeypatch):
    readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    before = {name: (pages / name).read_bytes()
              for name in ("listings.json", "searchtern.json", "feed-manifest.json")}
    target = pages / "feed-manifest.json"
    real_replace = os.replace
    failed = False

    def fail_manifest_once(src, dst):
        nonlocal failed
        if str(dst) == str(target) and not failed:
            failed = True
            raise OSError("simulated manifest crash")
        return real_replace(src, dst)

    monkeypatch.setattr(readme_generation.os, "replace", fail_manifest_once)
    with pytest.raises(OSError):
        readme_generation.write_listings_json(
            _frame().assign(role="Backend Intern"), output_dir=str(tmp_path))

    after = {name: (pages / name).read_bytes() for name in before}
    assert after == before
    assert not list(pages.glob("*.tmp"))
    assert not list(pages.glob("*.bak.*"))


def test_listing_pair_rolls_back_both_files(tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    pages.mkdir()
    listings = pages / "listings.json"
    searchtern = pages / "searchtern.json"
    listings.write_text("old listings", encoding="utf-8")
    searchtern.write_text("old searchtern", encoding="utf-8")
    real_replace = os.replace
    failed = False

    def fail_mirror_once(src, dst):
        nonlocal failed
        if str(dst) == str(searchtern) and not failed:
            failed = True
            raise OSError("simulated mirror failure")
        return real_replace(src, dst)

    monkeypatch.setattr(readme_generation.os, "replace", fail_mirror_once)
    with pytest.raises(OSError):
        readme_generation.write_listings_json(_frame(), output_dir=str(tmp_path))

    assert listings.read_text(encoding="utf-8") == "old listings"
    assert searchtern.read_text(encoding="utf-8") == "old searchtern"
    assert not list(pages.glob("*.tmp"))
    assert not list(pages.glob("*.bak.*"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
