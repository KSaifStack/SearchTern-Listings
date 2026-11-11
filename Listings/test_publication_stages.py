"""Publication stages: a crash at any point leaves one coherent generation.

The publication writes the feed, manifest, README, country pages, RSS and the
reconciled count artifacts as one transaction. These tests fail each stage in
turn and assert that a reader either sees the complete previous generation or
the complete new one — never a mix.
"""
import glob
import json
import os
from datetime import UTC, datetime, timedelta

import feed_manifest
import pandas as pd
import pytest
import readme_generation
import readme_rendering
import snapshots

PAIRS = ("pages/listings.json", "pages/searchtern.json", "pages/feed-manifest.json",
         "pages/run_meta.json", "pages/health.json", "pages/source_stats.json",
         "pages/snapshots.json", "pages/feed.xml",
         "README.md", "countries/US.md")


def _frame(role="SWE Intern", country="US", link="https://example.test/1", source="jobhive"):
    return pd.DataFrame([{
        "company": "Acme",
        "role": role,
        "location": "Austin, TX",
        "date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "link": link,
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": country,
        "job_type": "internship",
        "source": source,
        "ats_type": "greenhouse",
        "ats_id": "1",
        "description": "Build software with the team.",
    }])


def _many(count):
    now = datetime.now(UTC) - timedelta(days=1)
    return pd.DataFrame([{
        "company": f"Acme {i}",
        "role": "SWE Intern",
        "location": "Austin, TX",
        "date": now.isoformat(),
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
        "description": "Build software with the team. " * 8,
    } for i in range(count)])


def _snapshots_report(root):
    """The input ledger every manifest summary must match."""
    return snapshots.write_report(
        os.path.join(root, "pages", "snapshots.json"),
        os.path.join(root, "cache"),
        now=pd.Timestamp.now("UTC"),
    )


def _legacy_artifacts(root):
    pages = os.path.join(root, "pages")
    for name in ("run_meta.json", "health.json", "source_stats.json"):
        payload = {"total": 999} if name == "run_meta.json" else {}
        payload.setdefault("run", {})["published"] = {"listings": 999, "searchtern": 999}
        with open(os.path.join(pages, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)


def _setup(root):
    os.makedirs(os.path.join(root, "pages"), exist_ok=True)
    os.makedirs(os.path.join(root, "countries"), exist_ok=True)
    _legacy_artifacts(root)
    return _snapshots_report(root)


def _publish(root, report, role="SWE Intern", country="US",
             link="https://example.test/1", **kwargs):
    frame = _frame(role=role, country=country, link=link)
    return readme_generation.publish_generation(
        frame, mirror_frame=frame, readme_frame=frame, output_dir=str(root),
        snapshots_report=report, required_sources=("jobhive",), **kwargs,
    )


def _bytes(root):
    out = {}
    for rel in PAIRS:
        path = os.path.join(root, rel)
        if os.path.exists(path):
            with open(path, "rb") as handle:
                out[rel] = handle.read()
    return out


def test_publish_writes_one_coherent_generation(tmp_path):
    report = _setup(str(tmp_path))

    manifest = _publish(str(tmp_path), report)

    feed_manifest.audit(str(tmp_path), check_rows=True)
    pages = tmp_path / "pages"
    on_disk = json.loads((pages / "feed-manifest.json").read_text())
    assert on_disk["run_id"] == manifest["run_id"]
    readme = on_disk["published"]["readme"]
    assert readme["generation"] == manifest["run_id"]
    assert readme["rows"] == 1 and readme["pages"] == 1
    assert feed_manifest.marker(manifest["run_id"]) in (tmp_path / "README.md").read_text()
    assert feed_manifest.marker(manifest["run_id"]) in (
        tmp_path / "countries" / "US.md").read_text()
    assert feed_manifest.marker(manifest["run_id"]) in (pages / "feed.xml").read_text()
    for name, section in (("health.json", "run"), ("source_stats.json", "run"),
                          ("run_meta.json", None)):
        payload = json.loads((pages / name).read_text())
        scoped = payload if section is None else payload[section]
        assert scoped["generation"] == manifest["run_id"]
        assert scoped["published"] == {
            "listings": manifest["published"]["listings"]["rows"],
            "searchtern": manifest["published"]["searchtern"]["rows"],
        }
        assert scoped["publication_status"] == "published"


def test_multipage_readme_rows_and_pages_reconcile(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))
    monkeypatch.setattr(readme_generation, "GITHUB_FILE_SIZE_LIMIT", 12_000)
    monkeypatch.setattr(readme_generation, "SIZE_BUFFER", 500)
    frame = _many(40)

    manifest = readme_generation.publish_generation(
        frame, mirror_frame=frame, readme_frame=frame, output_dir=str(tmp_path),
        snapshots_report=report, required_sources=("jobhive",),
    )

    readme = manifest["published"]["readme"]
    assert readme["pages"] >= 2
    assert readme["rows"] == 40
    feed_manifest.audit(str(tmp_path), check_rows=True)
    pages = [os.path.join(str(tmp_path), "README.md")] + glob.glob(
        os.path.join(str(tmp_path), "pages", "README-*.md"))
    assert len(pages) == readme["pages"]
    rows = 0
    for path in pages:
        with open(path, encoding="utf-8") as handle:
            rows += handle.read().count("<tr>")
    assert rows - len(pages) == 40


def test_crash_at_every_transaction_stage_keeps_one_generation(tmp_path):
    """Fail the first backup, the last backup and every replace in between."""
    reference = tmp_path / "reference"
    _setup(str(reference))
    _publish(str(reference), _snapshots_report(str(reference)))
    file_count = len(_bytes(str(reference)))
    assert file_count >= 9

    real_replace = os.replace
    for stage in range(1, 2 * file_count + 1):
        case = tmp_path / f"stage-{stage}"
        _setup(str(case))
        report = _snapshots_report(str(case))
        _publish(str(case), report)
        before = _bytes(str(case))
        calls = {"n": 0}

        def fail_nth(src, dst, _stage=stage, _calls=calls):
            _calls["n"] += 1
            if _calls["n"] == _stage:
                raise OSError(f"simulated crash at stage {_stage}")
            return real_replace(src, dst)

        os.replace = fail_nth
        try:
            with pytest.raises(OSError):
                _publish(str(case), report, role="Backend Intern")
        finally:
            os.replace = real_replace

        assert _bytes(str(case)) == before, f"generation changed at stage {stage}"
        assert not list(case.rglob("*.tmp")), stage
        assert not list(case.rglob("*.bak.*")), stage
        feed_manifest.audit(str(case), check_rows=True)


def test_readme_render_failure_touches_nothing(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))
    _publish(str(tmp_path), report)
    before = _bytes(str(tmp_path))

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated README render failure")

    monkeypatch.setattr(readme_generation, "render_readme_pages", fail)
    with pytest.raises(RuntimeError):
        _publish(str(tmp_path), report, role="Backend Intern")

    assert _bytes(str(tmp_path)) == before
    feed_manifest.audit(str(tmp_path), check_rows=True)


def test_country_render_failure_touches_nothing(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))
    _publish(str(tmp_path), report)
    before = _bytes(str(tmp_path))

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated country render failure")

    monkeypatch.setattr(readme_rendering, "render_country_pages", fail)
    with pytest.raises(RuntimeError):
        _publish(str(tmp_path), report, role="Backend Intern")

    assert _bytes(str(tmp_path)) == before
    feed_manifest.audit(str(tmp_path), check_rows=True)


def test_legacy_build_failure_touches_nothing(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))
    _publish(str(tmp_path), report)
    before = _bytes(str(tmp_path))

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated reconciliation failure")

    monkeypatch.setattr(feed_manifest, "build_reconciled", fail)
    with pytest.raises(RuntimeError):
        _publish(str(tmp_path), report, role="Backend Intern")

    assert _bytes(str(tmp_path)) == before
    feed_manifest.audit(str(tmp_path), check_rows=True)


def test_preserve_failure_does_not_invalidate_publication(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))
    _publish(str(tmp_path), report)

    def fail(*_args, **_kwargs):
        raise OSError("simulated LKG copy failure")

    monkeypatch.setattr(feed_manifest, "_write_bytes", fail)
    with pytest.raises(OSError):
        feed_manifest.preserve(str(tmp_path / "pages"))

    # The main pipeline catches a preserve failure; the published generation is
    # still complete and self-consistent.
    feed_manifest.audit(str(tmp_path), check_rows=True)


def test_audit_failure_after_commit_restores_previous_generation(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))
    first = _publish(str(tmp_path), report)
    before = _bytes(str(tmp_path))

    def fail(*_args, **_kwargs):
        raise feed_manifest.ManifestMismatch("simulated audit failure")

    monkeypatch.setattr(feed_manifest, "audit", fail)
    with pytest.raises(feed_manifest.ManifestMismatch):
        _publish(str(tmp_path), report, role="Backend Intern")
    monkeypatch.undo()

    assert _bytes(str(tmp_path)) == before
    assert feed_manifest.audit(str(tmp_path), check_rows=True)["run_id"] == first["run_id"]


def test_first_publish_audit_failure_has_nothing_to_restore(tmp_path, monkeypatch):
    report = _setup(str(tmp_path))

    def fail(*_args, **_kwargs):
        raise feed_manifest.ManifestMismatch("simulated audit failure")

    monkeypatch.setattr(feed_manifest, "audit", fail)
    with pytest.raises(feed_manifest.ManifestMismatch):
        _publish(str(tmp_path), report)

    # No previous generation exists, so recovery is impossible; the run fails
    # loudly instead of claiming success.
    assert not feed_manifest.has_last_known_good(str(tmp_path / "pages"))


def test_recovery_restores_readme_countries_and_count_artifacts(tmp_path):
    report = _setup(str(tmp_path))
    first = _publish(str(tmp_path), report)
    _publish(str(tmp_path), report, role="Backend Intern")
    pages = tmp_path / "pages"
    (pages / "searchtern.json").write_text("torn", encoding="utf-8")
    (tmp_path / "README.md").write_text("torn", encoding="utf-8")
    (tmp_path / "countries" / "US.md").write_text("torn", encoding="utf-8")

    feed_manifest.recover(str(pages))

    assert feed_manifest.audit(str(tmp_path), check_rows=True)["run_id"] == first["run_id"]
    assert "SWE Intern" in (tmp_path / "README.md").read_text()
    assert feed_manifest.marker(first["run_id"]) in (tmp_path / "countries" / "US.md").read_text()
    run_meta = json.loads((pages / "run_meta.json").read_text())
    assert run_meta["generation"] == first["run_id"]


def test_recovery_removes_pages_from_the_newer_generation(tmp_path):
    report = _setup(str(tmp_path))
    _publish(str(tmp_path), report)
    _publish(str(tmp_path), report, country="CA", link="https://example.test/ca")

    assert (tmp_path / "countries" / "CA.md").exists()
    feed_manifest.recover(str(tmp_path / "pages"))

    assert not (tmp_path / "countries" / "CA.md").exists()
    feed_manifest.audit(str(tmp_path), check_rows=True)


def test_blocked_publish_repoints_legacy_counts_at_last_generation(tmp_path):
    report = _setup(str(tmp_path))
    first = _publish(str(tmp_path), report)
    before = _bytes(str(tmp_path))

    with pytest.raises(readme_generation.PublicationRejected):
        _publish(str(tmp_path), report, role="Backend Intern", minimum_rows=5)

    assert _bytes(str(tmp_path)) == before
    feed_manifest.mark_blocked(str(tmp_path / "pages"), "simulated refusal")
    run_meta = json.loads((tmp_path / "pages" / "run_meta.json").read_text())
    assert run_meta["generation"] == first["run_id"]
    assert run_meta["publication_status"] == "blocked"
    assert run_meta["published"]["searchtern"] == first["published"]["searchtern"]["rows"]
    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.audit(str(tmp_path))


def test_audit_rejects_a_readme_from_a_previous_generation(tmp_path):
    report = _setup(str(tmp_path))
    first = _publish(str(tmp_path), report)
    _publish(str(tmp_path), report, role="Backend Intern")
    # Simulate a crash that left the previous README beside the new feed.
    (tmp_path / "README.md").write_text(
        f"# stale\n{feed_manifest.marker(first['run_id'])}\n", encoding="utf-8")

    with pytest.raises(feed_manifest.ManifestMismatch):
        feed_manifest.audit(str(tmp_path))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
