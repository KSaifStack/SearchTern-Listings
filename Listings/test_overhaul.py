"""Tests for the freshness overhaul modules: scheduler, conditional GET,
shards, cross-source identity dedup, health payload, USAJobs parsing."""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import board_scheduler
import dedup
import freshness
import http_cache
import pandas as pd
import pytest
import readme_utils
import shards
import usajobs_source


# ── scheduling ────────────────────────────────────────────────────────────────
def test_high_yield_board_beats_quiet_board_at_equal_staleness():
    now = time.time()
    state = {
        "Greenhouse|rich": {"at": now - 3600 * 5, "rows": 120},
        "Greenhouse|quiet": {"at": now - 3600 * 5, "rows": 0},
    }
    boards = [("Q", "Greenhouse", "quiet"), ("R", "Greenhouse", "rich")]
    batch, _ = board_scheduler.plan(boards, state, cap=1, now=now)
    assert batch[0][2] == "rich"


def test_starved_board_jumps_the_queue():
    now = time.time()
    state = {
        "Greenhouse|starved": {"at": now - 3600 * (board_scheduler.STARVATION_HOURS + 1), "rows": 0},
        "Greenhouse|fresh": {"at": now - 3600 * 2, "rows": 500},
    }
    boards = [("F", "Greenhouse", "fresh"), ("S", "Greenhouse", "starved")]
    batch, _ = board_scheduler.plan(boards, state, cap=1, now=now)
    assert batch[0][2] == "starved"


def test_never_probed_board_comes_first_and_cap_holds():
    boards = [("X", "Lever", f"b{i}") for i in range(6)]
    batch, deferred = board_scheduler.plan(boards, {"Lever|b0": time.time()}, cap=2)
    assert len(batch) == 2
    assert len(deferred) == 4


def test_legacy_float_state_still_reads():
    now = time.time()
    assert board_scheduler.last_probe(now) == now
    assert board_scheduler.last_probe({"at": 12.0, "rows": 3}) == 12.0
    assert board_scheduler.last_probe(None) == 0.0
    assert board_scheduler.last_rows({"rows": 7}) == 7


def test_priority_lane_outranks_bulk_lane():
    now = time.time()
    state = {"Workday|p": {"at": now - 3600, "rows": 5},
             "Workday|b": {"at": now - 3600, "rows": 5}}
    boards = [("B", "Workday", "b"), ("P", "Workday", "p")]
    lanes = {"Workday|p": "priority", "Workday|b": "bulk"}
    batch, _ = board_scheduler.plan(boards, state, cap=1, now=now, lane_of=lanes)
    assert batch[0][2] == "p"


def test_board_rows_reads_every_payload_shape():
    assert board_scheduler.board_rows({"jobs": [1, 2, 3]}) == 3
    assert board_scheduler.board_rows({"jobPostings": [1]}) == 1
    assert board_scheduler.board_rows([1, 2]) == 2
    assert board_scheduler.board_rows({"nothing": []}) == 0
    assert board_scheduler.board_rows(None) == 0


def test_record_round_trips_into_reordering():
    state = {}
    board_scheduler.record(state, "Ashby", "acme", 40, now=1000.0)
    assert state["Ashby|acme"] == {"at": 1000.0, "rows": 40, "zero": 0}


# ── conditional GET ───────────────────────────────────────────────────────────
@pytest.fixture()
def origin(tmp_path, monkeypatch):
    hits = {"n": 0, "bodies": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits["n"] += 1
            if self.path == "/changed":
                body = b'{"v": %d}' % hits["n"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", f'"v{hits["n"]}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.send_header("ETag", '"v1"')
                self.end_headers()
                return
            body = b'{"jobs": [1, 2, 3]}'
            hits["bodies"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(http_cache, "CACHE_DIR", str(tmp_path / "http"))
    yield hits, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_304_replays_the_cached_body(origin):
    hits, base = origin
    first = readme_utils.http_get(f"{base}/board")
    second = readme_utils.http_get(f"{base}/board")
    assert first is not None and second is not None
    assert second.status_code == 200
    assert second.json() == {"jobs": [1, 2, 3]}
    assert hits["n"] == 2 and hits["bodies"] == 1  # second hit was a 304


def test_changed_etag_fetches_new_body(origin):
    hits, base = origin
    first = readme_utils.http_get(f"{base}/changed")
    second = readme_utils.http_get(f"{base}/changed")
    assert first.json() != second.json()
    assert hits["n"] == 2


def test_cache_disabled_skips_conditional_headers(origin):
    hits, base = origin
    readme_utils.http_get(f"{base}/board")
    readme_utils.http_get(f"{base}/board", cache=False)
    assert hits["bodies"] == 2


def test_server_without_validators_is_not_cached(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(http_cache, "CACHE_DIR", str(tmp_path / "http2"))
    try:
        url = f"http://127.0.0.1:{server.server_port}/x"
        readme_utils.http_get(url)
        assert http_cache.load(url) is None
    finally:
        server.shutdown()


# ── shards ────────────────────────────────────────────────────────────────────
def test_shards_round_trip_with_manifest(tmp_path):
    frame = pd.DataFrame({
        "company": [f"C{i}" for i in range(7)],
        "role": ["Intern"] * 7,
        "link": ["https://x/1"] * 7,
        "salary_min": [float("nan")] * 7,
    })
    manifest = shards.publish(frame, str(tmp_path), shard_rows=3)
    assert manifest["rows"] == 7
    assert len(manifest["shards"]) == 3
    assert (tmp_path / "manifest.json").exists()
    rows = []
    for part in manifest["shards"]:
        rows.extend(shards.read_shard(str(tmp_path), part["path"]))
    assert [r["company"] for r in rows] == [f"C{i}" for i in range(7)]
    assert all(r["salary_min"] is None for r in rows)  # NaN -> null, not "nan"
    coverage = {c["field"]: c["pct"] for c in manifest["field_coverage"]}
    assert coverage["company"] == 100.0
    assert coverage["salary_min"] == 0.0


def test_shards_are_streamable_gzip_and_stale_parts_removed(tmp_path):
    shards.publish([{"company": "A"}] * 4, str(tmp_path), shard_rows=2)
    first = sorted(os.listdir(tmp_path / "shards"))
    shards.publish([{"company": "A"}] * 2, str(tmp_path), shard_rows=2)
    second = sorted(os.listdir(tmp_path / "shards"))
    assert len(first) == 2 and len(second) == 1
    with gzip.open(tmp_path / "shards" / second[0], "rt", encoding="utf-8") as handle:
        assert json.loads(handle.readline())["company"] == "A"


# ── identity dedup ───────────────────────────────────────────────────────────
def test_same_requisition_via_tracker_and_ats_collapses():
    frame = pd.DataFrame({
        "company": ["Acme", "Acme"],
        "role": ["SWE Intern", "SWE Intern"],
        "location": ["NYC", "NYC"],
        "link": ["https://go.vansh.nyc/x", "https://boards.greenhouse.io/acme/jobs/1"],
        "ats_type": ["greenhouse", "greenhouse"],
        "ats_id": ["1", "1"],
    })
    assert len(dedup.dedupe(frame)) == 1


def test_distinct_requisitions_with_same_title_survive():
    frame = pd.DataFrame({
        "company": ["Acme", "Acme"],
        "role": ["SWE Intern", "SWE Intern"],
        "location": ["NYC", "NYC"],
        "link": ["https://a/1", "https://a/2"],
        "ats_type": ["greenhouse", "greenhouse"],
        "ats_id": ["1", "2"],
    })
    assert len(dedup.dedupe(frame)) == 2


def test_missing_ids_fall_back_to_url():
    frame = pd.DataFrame({
        "company": ["Acme", "Acme"],
        "role": ["SWE Intern", "SWE Intern"],
        "location": ["NYC", "NYC"],
        "link": ["https://a/1", "https://a/1"],
        "ats_type": ["", ""],
        "ats_id": ["", ""],
    })
    assert len(dedup.dedupe(frame)) == 1


# ── health payload ──────────────────────────────────────────────────────────
def test_health_payload_ranks_sources_and_surfaces_alerts():
    snapshot = {"sources": [
        {"source": "ats-probe", "valid": 900, "new": 3, "new_per_second": 0.004,
         "seconds": 700.0, "status": "ok"},
        {"source": "jobhive", "valid": 14000, "new": 20, "new_per_second": 0.02,
         "seconds": 900.0, "status": "ok"},
        {"source": "echojobs", "valid": 0, "new": 0, "seconds": 5.0, "status": "429"},
    ], "totals": {"new": 23, "sources": 3}}
    health = {"failures": [{"source": "echojobs", "status": "429"}],
              "quiet": [{"source": "runway", "hours_since_last": 61}],
              "best": [{"source": "jobhive", "new_7d": 140}]}
    data = freshness.payload(snapshot, run_meta={"tier": "heavy"}, health=health)
    assert data["sources"][0]["source"] == "jobhive"
    assert data["sources"][0]["share_of_new_pct"] > data["sources"][1]["share_of_new_pct"]
    kinds = {a["kind"] for a in data["alerts"]}
    assert {"failing", "quiet", "degraded"} <= kinds
    assert data["healthy"] is False
    assert data["generated_at"].endswith("Z")


def test_health_payload_survives_empty_input():
    data = freshness.payload({}, health={"failures": [], "quiet": [], "best": []})
    assert data["sources"] == [] and data["healthy"] is True


# ── USAJobs ─────────────────────────────────────────────────────────────────
USAJOBS_FIXTURE = {"SearchResult": {"SearchResultItems": [{
    "MatchedObjectId": "123",
    "MatchedObjectDescriptor": {
        "PositionID": "ABC-123",
        "PositionTitle": "IT Student Trainee",
        "OrganizationName": "Department of Testing",
        "PositionURI": "https://www.usajobs.gov/job/123",
        "PublicationStartDate": "2026-11-01",
        "PositionRemuneration": [{"MinimumRange": "35000", "MaximumRange": "45000"}],
        "QualificationSummary": "Do computer things.",
        "Locations": [{"LocationName": "Anywhere in the U.S."}],
        "UserArea": {"Details": {"JobSummary": "Summary text"}},
    },
}]}}


def test_usajobs_parse_maps_to_pipeline_rows():
    rows = usajobs_source.parse(USAJOBS_FIXTURE)
    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "Department of Testing"
    assert row["role"] == "IT Student Trainee"
    assert row["ats_type"] == "usajobs"
    assert row["ats_id"] == "ABC-123"
    assert row["location"] == "Anywhere in the U.S."
    assert row["salary_min"] == "35000"


def test_usajobs_skips_rows_without_id_or_title():
    payload = {"SearchResult": {"SearchResultItems": [
        {"MatchedObjectDescriptor": {"PositionID": "", "PositionTitle": "x"}},
        {"MatchedObjectDescriptor": {"PositionID": "1", "PositionTitle": ""}},
    ]}}
    assert usajobs_source.parse(payload) == []


def test_usajobs_fetch_noops_without_credentials(monkeypatch):
    monkeypatch.delenv(usajobs_source.KEY_ENV, raising=False)
    monkeypatch.delenv(usajobs_source.EMAIL_ENV, raising=False)
    assert usajobs_source.configured() is False
    assert usajobs_source.fetch() == []
