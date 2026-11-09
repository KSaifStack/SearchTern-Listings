"""Shadow publisher: the union feed beside production, with nothing changed.

Phase 3 of docs/reverse-engineering/snapshot-audit.md.
"""
import hashlib
import json
import os

import pandas as pd
import pytest
import shadow_publish
import snapshots

NOW = pd.Timestamp("2026-11-09T12:00:00Z")
# The write-time filter drops inactive postings, so fixtures must be recent in
# wall-clock terms, which is what _build_json_records compares against.
DATE = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).date().isoformat()
COLUMNS = ["company", "role", "location", "link", "country_iso", "source",
           "ats_type", "ats_id", "date"]


def rows(specs, source="test"):
    return pd.DataFrame(
        [[c, r, loc, link, iso, source, ats, req, DATE]
         for c, r, loc, link, iso, ats, req in specs],
        columns=COLUMNS,
    )


def series(name, count, source=None, iso="US", ats="greenhouse"):
    return rows(
        [(f"Co{i}-{name}", "SWE Intern", "Austin, TX", f"https://{name}.test/{i}",
          iso, ats, f"{name}-{i}") for i in range(count)],
        source=source or name,
    )


def write_source(cache_dir, name, frame, age_hours=1):
    path = os.path.join(cache_dir, f"tier_{name}.parquet")
    os.makedirs(cache_dir, exist_ok=True)
    frame.to_parquet(path, index=False)
    snapshots.record(name, len(frame), path=path,
                     now=NOW - pd.Timedelta(hours=age_hours))


def production(rows_spec):
    return [
        {"company": c, "role": r, "location": loc, "link": link,
         "country_iso": iso, "source": "jobhive", "ats_type": ats, "ats_id": req,
         "date": DATE}
        for c, r, loc, link, iso, ats, req in rows_spec
    ]


def test_shadow_is_a_superset_of_production(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", series("medium", 3))
    payload = shadow_publish.build(
        pages, cache, now=NOW,
        production=production([("Acme", "SWE Intern", "Austin, TX",
                                "https://acme.test/1", "US", "greenhouse", "acme-1")]))
    assert payload["summary"]["production_rows"] == 1
    assert payload["summary"]["shadow_rows"] == 4
    assert payload["summary"]["added"] == 3
    assert payload["summary"]["removed"] == 0
    assert payload["summary"]["unexplained_removed"] == 0


def test_additions_name_the_source_that_supplied_them(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", series("medium", 2), age_hours=2.5)
    write_source(cache, "heavy", series("heavy", 1), age_hours=9)
    payload = shadow_publish.build(pages, cache, now=NOW, production=[])
    by_source = {a["source"]: a["added"] for a in payload["additions"]}
    assert by_source == {"heavy": 1, "medium": 2}
    heavy = next(c for c in payload["source_contributions"] if c["source"] == "heavy")
    assert heavy["age_hours"] == 9.0
    assert heavy["class"] == "direct_ats"


def test_expired_snapshot_is_reported_and_withheld(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", series("medium", 4), age_hours=80)  # TTL 72h
    write_source(cache, "heavy", series("heavy", 2), age_hours=1)
    payload = shadow_publish.build(pages, cache, now=NOW, production=[])
    assert payload["summary"]["shadow_rows"] == 2
    assert [e["source"] for e in payload["expired"]] == ["medium"]
    assert payload["expired"][0]["ttl_hours"] == 72


def test_a_duplicate_costs_nothing_and_is_counted(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    same = [("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1", "US",
             "greenhouse", "acme-1")]
    write_source(cache, "medium", rows(same, source="medium"))
    payload = shadow_publish.build(pages, cache, now=NOW,
                                   production=production(same))
    assert payload["summary"]["shadow_rows"] == 1
    assert payload["summary"]["added"] == 0
    assert payload["summary"]["duplicates"] == 1
    assert payload["summary"]["dropped_at_write"] == 0


def test_conflicting_ats_identity_is_reported(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    # One requisition id reachable through two ATS types: we would publish it
    # twice, because the identity key is scoped to the ATS.
    write_source(cache, "medium", rows(
        [("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1", "US",
          "greenhouse", "req-7")], source="medium"))
    write_source(cache, "heavy", rows(
        [("Acme", "SWE Intern", "Austin, TX", "https://acme.test/2", "US",
          "lever", "req-7")], source="heavy"))
    payload = shadow_publish.build(pages, cache, now=NOW, production=[])
    assert payload["summary"]["ats_conflicts"] == 1
    conflict = payload["ats_conflicts"][0]
    assert conflict["kind"] == "ats_id_under_multiple_ats_types"
    assert conflict["ats_id"] == "req-7"
    assert conflict["ats_types"] == ["greenhouse", "lever"]
    assert payload["summary"]["shadow_rows"] == 2


def test_production_rows_are_never_re_filtered(tmp_path):
    """A feed row that aged past the inactivity threshold stays in shadow.

    Re-running the write filter over the production base dropped 232 rows that
    had merely crossed the threshold since the run wrote them, which made the
    superset invariant fail for reasons that had nothing to do with snapshots.
    """
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    old = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=400)).date().isoformat()
    payload = shadow_publish.build(
        pages, cache, now=NOW,
        production=[{"company": "Acme", "role": "SWE Intern", "location": "Austin, TX",
                     "link": "https://acme.test/1", "country_iso": "US",
                     "source": "jobhive", "ats_type": "gh", "ats_id": "acme-1",
                     "date": old}])
    assert payload["summary"]["production_rows"] == 1
    assert payload["summary"]["shadow_rows"] == 1
    assert payload["summary"]["removed"] == 0
    assert payload["summary"]["unexplained_removed"] == 0


def test_scope_filter_is_applied_to_snapshot_rows(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", rows([
        ("Acme", "SWE Intern", "Austin, TX", "https://acme.test/1", "US", "gh", "1"),
        ("Beta", "SWE Intern", "Somewhere", "https://beta.test/1", "", "gh", "2"),
        ("Gamma", "SWE Intern", "Remote", "https://gamma.test/1", "", "gh", "3"),
    ]))
    payload = shadow_publish.build(pages, cache, now=NOW, production=[])
    contribution = payload["source_contributions"][0]
    assert contribution["rows"] == 3
    assert contribution["rows_after_scope"] == 2
    assert payload["summary"]["shadow_rows"] == 2


def test_repeated_builds_are_idempotent(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", series("medium", 3))
    first = shadow_publish.build(pages, cache, now=NOW, production=[])
    second = shadow_publish.build(pages, cache, now=NOW, production=[])
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_shadow_writes_nothing_outside_its_directory(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", series("medium", 2))
    prod_path = os.path.join(pages, "searchtern.json")
    with open(prod_path, "w", encoding="utf-8") as handle:
        json.dump(production([("Acme", "SWE Intern", "Austin, TX",
                               "https://acme.test/1", "US", "gh", "acme-1")]), handle)
    seen_path = os.path.join(cache, "seen.json")
    with open(seen_path, "w", encoding="utf-8") as handle:
        json.dump({"k": {"first": "2026-01-01", "last": "2026-11-01"}}, handle)
    cache_file = os.path.join(cache, "tier_medium.parquet")
    sidecar = os.path.join(cache, "snapshots", "medium.json")

    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.md5(handle.read()).hexdigest()

    before = {p: digest(p) for p in (prod_path, seen_path, cache_file, sidecar)}
    mtimes = {p: os.stat(p).st_mtime_ns for p in before}
    shadow_publish.run(pages, cache, now=NOW)
    after = {p: digest(p) for p in before}
    assert before == after
    assert mtimes == {p: os.stat(p).st_mtime_ns for p in before}
    assert set(os.listdir(cache)) == {"snapshots", "seen.json", "tier_medium.parquet"}
    assert os.listdir(pages) == ["shadow", "searchtern.json"]


def test_only_the_newest_runs_are_kept(tmp_path):
    cache = str(tmp_path / "cache")
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    write_source(cache, "medium", series("medium", 1))
    for hour in range(6):
        shadow_publish.run(pages, cache, now=NOW + pd.Timedelta(hours=hour),
                           production=[])
    kept = sorted(os.listdir(os.path.join(pages, "shadow")))
    assert len(kept) == shadow_publish.KEEP
    assert kept[-1].startswith("2026-11-09T17")


def test_an_empty_cache_is_not_an_error(tmp_path):
    pages = str(tmp_path / "pages")
    os.makedirs(pages)
    payload = shadow_publish.build(pages, str(tmp_path / "missing"), now=NOW, production=[])
    assert payload["summary"]["shadow_rows"] == 0
    assert payload["additions"] == []
    assert payload["expired"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
