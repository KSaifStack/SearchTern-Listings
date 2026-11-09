"""Snapshot contract: success gate, TTL eligibility, precedence, and the
property that a run can never erase another source's listings by not fetching it.

Phase 2 of docs/reverse-engineering/snapshot-audit.md.
"""
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
        "rows": rows,
        "status": status,
        "etag": "",
        "rejections": rejections,
    }


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
