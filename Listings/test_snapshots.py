"""Snapshot contract: success gate, TTL eligibility, precedence, and the
property that a run can never erase another source's listings by not fetching it.

Phase 2 of docs/reverse-engineering/snapshot-audit.md.
"""
import os
import time

import pandas as pd
import pytest
import seen_index
import snapshots

NOW = pd.Timestamp("2026-11-09T12:00:00Z")


def frame(rows, source="test"):
    return pd.DataFrame(
        [[company, role, location, link, source] for company, role, location, link in rows],
        columns=["company", "role", "location", "link", "source"],
    )


def side(name, hours_ago, rows=10, status="ok", rejections=0, now=NOW):
    return {
        "source": name,
        "class": snapshots.class_of(name),
        "ttl_hours": snapshots.ttl_hours(name),
        "fetched_at": (now - pd.Timedelta(hours=hours_ago)).isoformat(),
        "provenance": "fetch",
        "rows": rows,
        "status": status,
        "etag": "",
        "rejections": rejections,
    }


def test_empty_record_is_reported_as_empty_not_fresh(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_jobhive.parquet")
    os.makedirs(cache)
    snapshots.record("jobhive", 0, status="empty", path=path, now=NOW)

    report = snapshots.report(str(cache), NOW)
    assert report["empty"] == ["jobhive"]
    assert report["fresh"] == []
    assert report["expired"] == []


def test_policy_matches_the_decided_ttls():
    assert snapshots.ttl_hours("priority") == 24
    assert snapshots.ttl_hours("heavy") == 48
    assert snapshots.ttl_hours("ats_new") == 48
    assert snapshots.ttl_hours("jobhive") == 24
    assert snapshots.ttl_hours("light") == 48
    assert snapshots.ttl_hours("medium") == 72
    assert snapshots.ttl_hours("ecr") == 72
    assert snapshots.ttl_hours("echo") == 168
    assert snapshots.PRECEDENCE[0] == "direct_ats"
    assert snapshots.PRECEDENCE[-1] == "specialty"


def test_light_run_cannot_erase_full_only_listings():
    """The regression this phase exists for.

    A light run refreshes jobhive and markdown. Medium (freehire) was not
    fetched, but its snapshot is two hours old and its class TTL is 72h, so the
    publisher still merges its 50 rows. Before the snapshot contract, the next
    hourly light run committed a feed without them.
    """
    sidecars = {"jobhive": side("jobhive", 0.5, 2), "medium": side("medium", 2, 50)}
    frames = {
        "jobhive": frame([("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1"),
                          ("Beta", "Data Intern", "Remote", "https://beta.test/1")]),
        "medium": frame([(f"Co{i}", "SWE Intern", "Remote", f"https://co{i}.test/1")
                         for i in range(50)]),
    }
    merged = snapshots.merge(sidecars, lambda name: frames.get(name, pd.DataFrame()), NOW)
    assert len(merged) == 52
    assert (merged["source"] == "test").all()


def test_expired_snapshot_is_not_published_but_other_sources_are():
    sidecars = {"jobhive": side("jobhive", 0.5, 2), "medium": side("medium", 80, 50)}
    frames = {
        "jobhive": frame([("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1"),
                          ("Beta", "Data Intern", "Remote", "https://beta.test/1")]),
        "medium": frame([(f"Co{i}", "SWE Intern", "Remote", f"https://co{i}.test/1")
                         for i in range(50)]),
    }
    merged = snapshots.merge(sidecars, lambda name: frames.get(name, pd.DataFrame()), NOW)
    assert len(merged) == 2
    assert set(snapshots.eligible(sidecars, NOW)) == {"jobhive"}


def test_ttl_expiry_cannot_close_entries():
    """Expiry withdraws rows from a feed; it never means a posting was delisted."""
    key = "freehire|Acme|SWE Intern|Remote"
    keys = {key: {"first": "2026-01-01", "last": "2026-11-01"}}
    expired = {"medium": side("medium", 80, 50)}

    # Seen keys are "bucket|company|role|location"; commit votes per bucket.
    closed, out, _ = seen_index.commit(
        dict(keys), {}, {}, {"freehire": "medium" in snapshots.eligible(expired, NOW)},
        lambda bucket: bucket, "2026-11-09T12:00:00+00:00")
    assert closed == [], closed
    assert not out[key].get("closed")

    # Positive control: a source that *was* refreshed this run does close.
    closed_fresh, _, _ = seen_index.commit(
        dict(keys), {}, {}, {"freehire": True}, lambda bucket: bucket,
        "2026-11-09T12:00:00+00:00")
    assert len(closed_fresh) == 1


def test_save_rejects_empty_and_collapsed_parses(tmp_path):
    path = str(tmp_path / "tier_medium.parquet")
    good = frame([(f"Co{i}", "SWE Intern", "Remote", f"https://co{i}.test/1")
                  for i in range(100)])
    assert snapshots.save(good, "medium", path, now=NOW) == 100

    assert snapshots.save(frame([]), "medium", path, now=NOW) == 0
    assert len(pd.read_parquet(path)) == 100
    assert snapshots.read("medium", path)["status"] == "rejected:empty"
    assert snapshots.read("medium", path)["rejections"] == 1

    thin = frame([("Co1", "SWE Intern", "Remote", "https://co1.test/1")])
    assert snapshots.save(thin, "medium", path, now=NOW) == 0
    assert len(pd.read_parquet(path)) == 100
    assert snapshots.read("medium", path)["status"] == "rejected:shrunken"


def test_future_dated_sidecar_is_not_fresh(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_jobhive.parquet")
    os.makedirs(cache)
    snapshots.record("jobhive", 1, status="ok", path=path,
                     now=NOW + pd.Timedelta(hours=1))

    assert not snapshots.is_fresh(snapshots.read("jobhive", path), NOW)


def test_empty_first_save_is_rejected_and_visible(tmp_path):
    path = str(tmp_path / "tier_echo.parquet")

    assert snapshots.save(frame([]), "echo", path, now=NOW) == 0
    assert not os.path.exists(path)
    side = snapshots.read("echo", path)
    assert side["status"] == "rejected:empty"
    assert side["rows"] == 0
    assert snapshots.report(str(tmp_path), NOW)["rejected"] == ["echo"]


def test_a_genuine_shrink_is_accepted_after_repeated_rejections(tmp_path):
    path = str(tmp_path / "tier_medium.parquet")
    big = frame([(f"Co{i}", "SWE Intern", "Remote", f"https://co{i}.test/1")
                 for i in range(100)])
    assert snapshots.save(big, "medium", path, now=NOW) == 100
    small = frame([("Co1", "SWE Intern", "Remote", "https://co1.test/1")])
    assert snapshots.save(small, "medium", path, now=NOW) == 0
    assert snapshots.save(small, "medium", path, now=NOW) == 0
    assert snapshots.save(small, "medium", path, now=NOW) == 1
    assert len(pd.read_parquet(path)) == 1
    assert snapshots.read("medium", path)["status"] == "ok:recovered"


def test_precedence_decides_which_row_survives_a_duplicate():
    link = "https://acme.test/jobs/1"
    direct = frame([("Acme", "SWE Intern", "Austin, TX", link)], source="direct")
    aggregator = frame([("ACME", "SWE Intern", "Austin, TX", link)], source="agg")
    sidecars = {"heavy": side("heavy", 1, 1), "medium": side("medium", 1, 1)}
    frames = {"heavy": direct, "medium": aggregator}
    merged = snapshots.merge(sidecars, lambda name: frames[name], NOW)
    assert len(merged) == 1
    assert merged.iloc[0]["source"] == "direct"


def test_report_calls_out_expired_and_rejected_sources(tmp_path):
    cache = tmp_path / "cache"
    good = frame([("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1")])
    snapshots.save(good, "heavy", str(cache / "tier_heavy.parquet"), now=NOW)
    snapshots.save(good, "medium", str(cache / "tier_medium.parquet"), now=NOW)
    # Age medium past its TTL by rewriting its sidecar timestamp.
    stale = snapshots.read("medium", str(cache / "tier_medium.parquet"))
    stale["fetched_at"] = (NOW - pd.Timedelta(hours=100)).isoformat()
    snapshots._write("medium", str(cache / "tier_medium.parquet"), stale)

    payload = snapshots.report(str(cache), now=NOW)
    assert payload["fresh"] == ["heavy"]
    assert payload["expired"] == ["medium"]
    assert payload["rows_eligible"] == 1
    assert payload["policy"]["ttl_hours"]["aggregators"] == 72


def test_missing_expected_sources_are_named_not_zero(tmp_path):
    report = snapshots.report(str(tmp_path / "empty"), now=NOW)

    assert report["missing"] == sorted(snapshots.EXPECTED_SOURCES)
    assert report["count"] == 0
    assert report["rows_eligible"] == 0


def test_seed_uses_cache_mtime_and_cannot_freshen_an_old_cache(tmp_path):
    cache = tmp_path / "cache"
    path = cache / "tier_heavy.parquet"
    os.makedirs(cache)
    good = frame([("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1")])
    good.to_parquet(path, index=False)
    old = time.time() - 10 * 24 * 3600
    os.utime(path, (old, old))

    side = snapshots.seed("heavy", str(path), now=NOW)

    assert side["status"] == "seeded"
    assert side["provenance"] == "cache_mtime"
    assert side["rows"] == 1
    assert snapshots.age_hours(side, NOW) > 48
    assert snapshots.is_fresh(side, NOW) is False
    collected = snapshots.collect(str(cache), NOW)
    assert collected["heavy"]["fresh"] is False
    assert "heavy" in snapshots.report(str(cache), NOW)["invalid"]


def test_ttl_is_policy_not_sidecar_metadata():
    # 60h is inside a forged 999h TTL but outside the real 48h direct-ATS TTL.
    forged = side("heavy", 60)
    forged["ttl_hours"] = 999
    assert snapshots.is_fresh(forged, NOW) is False

    # A sidecar cannot shorten the policy either: 1h is fresh for direct ATS.
    shortened = side("heavy", 1)
    shortened["ttl_hours"] = 0
    assert snapshots.is_fresh(shortened, NOW) is True


def test_unknown_provenance_and_source_mismatch_are_not_publishable(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_heavy.parquet")
    os.makedirs(cache)
    snapshots.record("heavy", 3, path=path, now=NOW)
    sidecar = snapshots.read("heavy", path)
    sidecar.pop("provenance")
    snapshots._write("heavy", path, sidecar)
    report = snapshots.report(str(cache), NOW)
    assert report["fresh"] == []
    assert "heavy" in report["invalid"]

    snapshots.record("heavy", 3, path=path, now=NOW)
    sidecar = snapshots.read("heavy", path)
    sidecar["source"] = "medium"
    snapshots._write("heavy", path, sidecar)
    collected = snapshots.collect(str(cache), NOW)["heavy"]
    assert collected["source_match"] is False
    assert collected["fresh"] is False
    assert "heavy" in snapshots.report(str(cache), NOW)["invalid"]


def test_collect_overrides_stale_class_and_ttl_metadata(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_heavy.parquet")
    os.makedirs(cache)
    snapshots.record("heavy", 3, path=path, now=NOW)
    side = snapshots.read("heavy", path)
    side["class"] = "markdown"
    side["ttl_hours"] = 999
    snapshots._write("heavy", path, side)

    collected = snapshots.collect(str(cache), NOW)["heavy"]

    assert collected["class"] == "direct_ats"
    assert collected["ttl_hours"] == 48
    assert collected["class_match"] is False
    assert collected["ttl_match"] is False


def test_reconcile_rows_preserves_fetch_time(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_heavy.parquet")
    os.makedirs(cache)
    snapshots.record("heavy", 3, path=path, now=NOW - pd.Timedelta(hours=2))
    before = snapshots.read("heavy", path)

    after = snapshots.reconcile_rows("heavy", path, 9)

    assert after["rows"] == 9
    assert after["rows_reconciled_from"] == 3
    assert after["fetched_at"] == before["fetched_at"]


def test_rejected_parse_is_not_fresh_but_serves_previous_rows():
    rejected = side("medium", 1, rows=50, status="rejected:shrunken")

    assert snapshots.is_fresh(rejected, NOW) is False
    assert snapshots.is_servable(rejected, NOW) is True
    assert "medium" in snapshots.eligible({"medium": rejected}, NOW)


def test_rejected_empty_has_nothing_to_serve():
    empty = side("echo", 1, rows=0, status="rejected:empty")

    assert snapshots.is_fresh(empty, NOW) is False
    assert snapshots.is_servable(empty, NOW) is False
    assert snapshots.eligible({"echo": empty}, NOW) == {}


def test_rejected_source_is_not_reported_as_expired(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    snapshots._write("echo", str(cache / "tier_echo.parquet"),
                     side("echo", 1, rows=0, status="rejected:empty"))

    report = snapshots.report(str(cache), NOW)

    assert report["rejected"] == ["echo"]
    assert report["expired"] == []


def test_report_separates_fresh_from_serving_previous(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    rejected = side("medium", 1, rows=50, status="rejected:shrunken")
    snapshots._write("medium", str(cache / "tier_medium.parquet"), rejected)
    snapshots.record("heavy", 3, path=str(cache / "tier_heavy.parquet"), now=NOW)

    report = snapshots.report(str(cache), NOW)

    assert report["fresh"] == ["heavy"]
    assert report["serving_previous"] == ["medium"]
    assert report["rejected"] == ["medium"]
    assert report["rows_fresh"] == 3
    assert report["rows_eligible"] == 53


def test_every_expected_source_can_hold_a_valid_sidecar(tmp_path):
    cache = tmp_path / "cache"
    os.makedirs(cache)
    for name in snapshots.EXPECTED_SOURCES:
        assert name in snapshots.CLASS_OF, name
        snapshots.record(name, 1, path=str(cache / f"tier_{name}.parquet"), now=NOW)

    report = snapshots.report(str(cache), NOW)

    assert report["missing"] == []
    assert report["invalid"] == []
    assert report["expired"] == []
    assert set(report["fresh"]) == set(snapshots.EXPECTED_SOURCES)


def test_cache_mtime_cannot_refresh_an_older_sidecar(tmp_path):
    """Writing the parquet again does not make an old fetch look fresh."""
    cache = tmp_path / "cache"
    path = cache / "tier_heavy.parquet"
    os.makedirs(cache)
    frame([("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1")]).to_parquet(
        path, index=False)
    snapshots.record("heavy", 1, path=str(path), now=NOW - pd.Timedelta(hours=72))

    # A later writer touches the cache; provenance still says it is 72h old.
    os.utime(path, None)

    side = snapshots.read("heavy", str(path))
    assert snapshots.is_fresh(side, NOW) is False
    assert snapshots.collect(str(cache), NOW)["heavy"]["fresh"] is False


def test_unknown_status_is_not_publishable(tmp_path):
    cache = tmp_path / "cache"
    path = str(cache / "tier_heavy.parquet")
    os.makedirs(cache)
    snapshots.record("heavy", 3, status="pending", path=path, now=NOW)

    side = snapshots.read("heavy", path)

    assert snapshots.is_fresh(side, NOW) is False
    assert "heavy" in snapshots.report(str(cache), NOW)["expired"]


def test_every_precedence_class_survives_the_union(tmp_path):
    names = ("heavy", "priority", "jobhive", "light", "medium", "echo")
    sidecars = {name: side(name, 1, 1) for name in names}
    frames = {
        name: frame([(f"Co-{name}", "SWE Intern", "Remote",
                      f"https://{name}.test/1")], source=name)
        for name in names
    }

    merged = snapshots.merge(sidecars, lambda name: frames[name], NOW)

    assert len(merged) == len(names)
    assert set(merged["source"]) == set(names)
    assert list(merged["source"]) == list(names)  # precedence order


def test_write_report_is_atomic(tmp_path):
    cache = tmp_path / "cache"
    frame([("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1")])
    target = tmp_path / "pages" / "snapshots.json"
    payload = snapshots.write_report(str(target), str(cache), now=NOW)
    assert target.exists()
    assert payload["count"] == 0
    assert not list((tmp_path / "pages").glob("*.tmp"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
