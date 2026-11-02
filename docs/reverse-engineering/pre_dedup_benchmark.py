#!/usr/bin/env python3
"""Read-only pre-dedup benchmark for candidate and existing sources.

Measures, per source, what the pipeline currently cannot see because dedup runs
before publication:

    discovered -> valid -> duplicate in batch -> already in feed -> new -> updated

plus wall time and milliseconds per 1,000 discovered records. Nothing is
written to `pages/`, `Listings/cache/`, or any production artifact; the only
optional write is a JSON evidence file under docs/reverse-engineering/.

Keyed sources are skipped unless their environment variable is set. This is
deliberate: a source must not be integrated before its incremental value is
demonstrated, and buying a key first would invert that order.

Usage:
    python docs/reverse-engineering/pre_dedup_benchmark.py
    python docs/reverse-engineering/pre_dedup_benchmark.py --limit 500 --json out.json
    HEADCOUNT_LABS_API_KEY=... JOBDATAAPI_KEY=... python .../pre_dedup_benchmark.py

Read the footer. A matched feed row means "already published", not "wasted":
the feed only carries rows that survived filtering, and a source can still be
worth its cost in fresher or more complete records.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Listings"))

try:  # optional: reuse the project's own relevance rules
    import classify

    _INTERN = re.compile(classify.LISTINGS_INTERN_RE, re.I)
    _NEWGRAD = re.compile(classify.LISTINGS_NEWGRAD_RE, re.I)
    _EXCLUDE = re.compile(classify.TITLE_EXCLUDE_RE, re.I)
except Exception:  # pragma: no cover - benchmark must run standalone
    classify = None

UA = "SearchTern-Listings/1.0 (+https://github.com/KSaifStack/SearchTern-Listings)"
FEED = ROOT / "pages" / "searchtern.json"
_TRACKING = re.compile(r"^(utm_.*|gh_src|lever-source|source|ref|referrer|trk)$", re.I)


def norm_link(link) -> str:
    text = str(link or "").strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else f"https://{text}")
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path)
    query = "&".join(p for p in parts.query.split("&") if p and not _TRACKING.match(p.split("=", 1)[0]))
    return f"{host}{path}" + (f"?{query}" if query else "")


def key(record) -> str:
    return "|".join(
        re.sub(r"\s+", " ", str(record.get(f) or "").strip().lower())
        for f in ("company", "role", "location")
    )


def _http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _rows_from(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for field in ("results", "jobs", "data", "records"):
        value = payload.get(field)
        if isinstance(value, list):
            return value
    return []


# --- source adapters -------------------------------------------------------
# Each returns a list of {company, role, location, link, date, source_id}.

def src_jobhive(limit, ats="adp"):
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    url = f"https://storage.stapply.ai/jobhive/v1/{ats}/jobs.parquet"
    rows = con.execute(
        f"""SELECT company, title, location, url, posted_at, ats_id, requisition_id
            FROM read_parquet('{url}') LIMIT {int(limit)}"""
    ).fetchall()
    return [
        {"company": r[0], "role": r[1], "location": r[2], "link": r[3],
         "date": r[4], "source_id": r[5] or r[6]}
        for r in rows
    ]


def src_headcount_sample(limit, _ats=None):
    payload = _http_json("https://headcountlabs.com/sample-records.json")
    out = []
    for rec in _rows_from(payload)[:limit]:
        out.append({
            "company": rec.get("company"),
            "role": rec.get("title"),
            "location": rec.get("location"),
            "link": rec.get("sourceURL"),
            "date": rec.get("sourcePostedAt") or rec.get("postedAt"),
            "source_id": rec.get("sourceATSJobId"),
        })
    return out


def src_headcount_api(limit, _ats=None):
    token = os.environ.get("HEADCOUNT_LABS_API_KEY")
    if not token:
        raise LookupError("HEADCOUNT_LABS_API_KEY not set")
    req = urllib.request.Request(
        f"https://api.headcountlabs.com/v1/jobs?limit={int(limit)}&currentness=current",
        headers={"User-Agent": UA, "Accept": "application/json",
                 "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    out = []
    for rec in _rows_from(payload):
        postings = rec.get("postings") or [{}]
        first = postings[0] if isinstance(postings, list) and postings else {}
        out.append({
            "company": (rec.get("company") or {}).get("name") if isinstance(rec.get("company"), dict) else rec.get("company"),
            "role": rec.get("title"),
            "location": rec.get("location"),
            "link": first.get("source_url") or first.get("url"),
            "date": first.get("source_posted_at"),
            "source_id": first.get("source_id"),
        })
    return out


def src_jobdataapi(limit, _ats=None):
    key_ = os.environ.get("JOBDATAAPI_KEY")
    if not key_:
        raise LookupError("JOBDATAAPI_KEY not set (anonymous access returned 403 on 2026-11-02)")
    req = urllib.request.Request(
        f"https://jobdataapi.com/api/jobs/?max_age=7&page_size={int(limit)}",
        headers={"User-Agent": UA, "Accept": "application/json",
                 "Authorization": f"Api-Key {key_}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    return [
        {"company": (r.get("company") or {}).get("name"), "role": r.get("title"),
         "location": r.get("location"), "link": r.get("application_url"),
         "date": r.get("published"), "source_id": r.get("ext_id")}
        for r in _rows_from(payload)[:limit]
    ]


def src_freehire(limit, _ats=None):
    payload = _http_json(
        "https://freehire.me/api/v1/jobs/search?employment_type=internship&is_tech=tech"
    )
    return [
        {"company": r.get("company"), "role": r.get("title"), "location": r.get("location"),
         "link": r.get("url") or r.get("apply_url"), "date": r.get("posted_at"),
         "source_id": r.get("id")}
        for r in _rows_from(payload)[:limit]
    ]


def src_arbeitnow(limit, _ats=None):
    payload = _http_json("https://www.arbeitnow.com/api/job-board-api")
    return [
        {"company": r.get("company_name"), "role": r.get("title"),
         "location": r.get("location"), "link": r.get("url"),
         "date": r.get("created_at"), "source_id": r.get("slug")}
        for r in _rows_from(payload)[:limit]
    ]


SOURCES = {
    "jobhive:adp": src_jobhive,
    "jobhive:apple": src_jobhive,
    "headcountlabs:sample": src_headcount_sample,
    "headcountlabs:api": src_headcount_api,
    "jobdataapi:api": src_jobdataapi,
    "freehire": src_freehire,
    "arbeitnow": src_arbeitnow,
}


def relevant(record) -> bool:
    """Valid + in-scope for SearchTern's intern / new-grad feeds."""
    if not record.get("company") or not record.get("role") or not record.get("link"):
        return False
    if classify is None:
        return True
    title = str(record["role"])
    if _EXCLUDE.search(title):
        return False
    return bool(_INTERN.search(title) or _NEWGRAD.search(title))


def load_feed_index():
    if not FEED.exists():
        return set(), set()
    with FEED.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    links = {norm_link(r.get("link")) for r in rows}
    keys = {key({"company": r.get("company"), "role": r.get("role"),
                 "location": r.get("location")}) for r in rows}
    return {link for link in links if link}, keys


def measure(name, fetch, limit, feed_links, feed_keys):
    started = time.monotonic()
    try:
        records = fetch(limit)
    except LookupError as exc:
        return {"source": name, "status": "skipped", "reason": str(exc)}
    except urllib.error.HTTPError as exc:
        return {"source": name, "status": f"HTTP {exc.code}"}
    except Exception as exc:  # network, parse, or optional dependency
        return {"source": name, "status": f"error: {type(exc).__name__}"}
    elapsed_ms = (time.monotonic() - started) * 1000

    seen_links, seen_keys = set(), set()
    valid = dup = matched = new = updated = 0
    for record in records:
        if not relevant(record):
            continue
        valid += 1
        link, k = norm_link(record.get("link")), key(record)
        if (link and link in seen_links) or (k and k in seen_keys):
            dup += 1
            continue
        seen_links.add(link)
        seen_keys.add(k)
        if link and link in feed_links:
            matched += 1
        elif k and k in feed_keys:
            updated += 1
        else:
            new += 1

    return {
        "source": name, "status": "ok", "discovered": len(records), "valid": valid,
        "duplicate_in_batch": dup, "already_in_feed": matched, "new": new,
        "updated": updated, "elapsed_ms": round(elapsed_ms),
        "ms_per_1000": round(elapsed_ms / max(len(records), 1) * 1000),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--json", help="write evidence JSON here")
    args = parser.parse_args()

    feed_links, feed_keys = load_feed_index()
    print(f"feed_index links={len(feed_links):,} keys={len(feed_keys):,} "
          f"(pages/searchtern.json, read-only)")

    results = [
        measure(name, fetch, args.limit, feed_links, feed_keys)
        for name, fetch in SOURCES.items()
    ]

    header = f"{'source':24}{'status':10}{'found':>7}{'valid':>7}{'dup':>6}{'in_feed':>9}{'new':>6}{'upd':>6}{'ms':>8}{'ms/1k':>8}"
    print("\n" + header)
    for row in results:
        if row["status"] != "ok":
            print(f"{row['source']:24}{row['status'][:9]:10}  {row.get('reason', '')}")
            continue
        print(f"{row['source']:24}{'ok':10}{row['discovered']:>7,}{row['valid']:>7,}"
              f"{row['duplicate_in_batch']:>6,}{row['already_in_feed']:>9,}"
              f"{row['new']:>6,}{row['updated']:>6,}{row['elapsed_ms']:>8,}"
              f"{row['ms_per_1000']:>8,}")

    print("\nnew = identity absent from the published feed; updated = same")
    print("company/role/location with a different URL. Skipped sources need a key")
    print("on purpose: value is proven before a subscription is bought.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "limit": args.limit, "results": results}, handle, indent=1)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
