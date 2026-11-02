"""Staging validation for verified closure. Read-only: never touches production feeds.

Crawls a small set of real public boards (same URL shapes as
generate_listings._fetch_company_jobs), records snapshots through closure.py,
then checks the safety rules against a synthetic jobhive-shaped frame:

* every dropped row belongs to a board crawled this run (voted),
* rows from never-crawled boards and unverifiable ATS kinds survive,
* incomplete/failed crawls record no vote and delete nothing.

Run:  python Listings/validate_closure.py [--offline] [--out PATH]
  --offline  skip live fetches, run synthetic checks only (for CI).
  --out      report path. Default writes nothing, prints to stdout.

Exit code 0 = all checks passed, 1 = a rule was violated.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import closure  # noqa: E402

try:
    from readme_utils import http_get  # noqa: E402
except Exception:  # pragma: no cover
    http_get = None

_UA = {"User-Agent": "SearchTern-staging-probe/1.0"}

# Small, public, verified-live boards. Mirrors EXTRA_BOARDS + priority list.
STAGING_BOARDS = [
    ("Ally", "Workday", "ally.wd1.myworkdayjobs.com/wday/cxs/ally/Ally"),
    ("Scale AI", "Greenhouse", "scaleai"),
    ("Palantir", "Lever", "palantir"),
    ("OpenAI", "Ashby", "openai"),
]

_URLS = {
    "Greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
    "Lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "Ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def _fetch_board(name, ats, slug, timeout=20):
    """Read-only fetch of one board. Returns (raw_json_or_None, error_or_None)."""
    if http_get is None:
        return None, "no-http"
    try:
        if ats == "Workday":
            # NOTE: slug already holds the CXS base path; POST the jobs
            # endpoint like generate_listings._fetch_company_jobs does.
            import requests

            resp = requests.post(
                f"https://{slug}/jobs",
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
                headers={**_UA, "Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                return None, str(resp.status_code)
            return resp.json(), None
        url = _URLS[ats].format(slug=slug)
        resp = http_get(url, timeout=timeout, retries=1, headers=_UA)
        if resp is None or resp.status_code != 200:
            return None, str(resp.status_code if resp is not None else "unreachable")
        return resp.json(), None
    except Exception as e:  # noqa: BLE001 - staging must report, not raise
        return None, f"{type(e).__name__}: {e}"


def _live_ids(ats, data):
    if data is None:
        return set()
    if ats == "Workday":
        return closure.req_ids_from_workday_postings((data or {}).get("jobPostings"))
    if ats == "Greenhouse":
        return {str(j.get("id")) for j in (data.get("jobs") or []) if j.get("id")}
    if ats == "Lever":
        return {str(j.get("id")) for j in (data if isinstance(data, list) else []) if j.get("id")}
    if ats == "Ashby":
        return {str(j.get("id")) for j in (data.get("jobs") or []) if j.get("id")}
    return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    closure.reset()
    report = {"boards": [], "checks": [], "ok": True}
    failures = []

    def check(name, cond, detail=""):
        report["checks"].append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            failures.append(name)

    if not args.offline:
        for name, ats, slug in STAGING_BOARDS:
            data, err = _fetch_board(name, ats, slug)
            ids = _live_ids(ats, data) if err is None else set()
            voted = closure.record_snapshot(ats, name, ids)
            report["boards"].append({
                "company": name, "ats": ats, "error": err,
                "live_ids": len(ids), "voted": voted,
            })
    else:
        # Synthetic votes so the rules are checkable with no network.
        closure.record_snapshot("Greenhouse", "Acme", ["101", "102"])
        closure.record_snapshot("Workday", "Ally", ["R2600885"])
        report["boards"].append({"company": "Acme (synthetic)", "ats": "Greenhouse",
                                 "live_ids": 2, "voted": 2})
        report["boards"].append({"company": "Ally (synthetic)", "ats": "Workday",
                                 "live_ids": 1, "voted": 1})

    cov = closure.covered()
    report["coverage"] = cov
    check("at-least-one-vote-or-offline-note",
          cov["boards"] > 0 or args.offline,
          f"{cov['boards']} boards voted")

    # Synthetic jobhive-shaped frame: live + fake-closed ids per voted board,
    # plus rows that must never drop.
    try:
        import pandas as pd
    except ImportError:
        print("pandas required for validation")
        return 1

    rows = []
    for (ats, key), live in list(closure._COMPLETE.items()):
        live = sorted(live)
        for lid in live[:3]:
            rows.append({"company": key, "ats_type": ats, "ats_id": lid,
                         "role": f"live-{lid}"})
        rows.append({"company": key, "ats_type": ats, "ats_id": "__closed_probe__",
                     "role": "closed-probe"})
    rows += [
        {"company": "NeverCrawled", "ats_type": "greenhouse",
         "ats_id": "__closed_probe__", "role": "uncrawled-must-survive"},
        {"company": "Other", "ats_type": "successfactors",
         "ats_id": "__closed_probe__", "role": "unverifiable-must-survive"},
        {"company": "Acme", "ats_type": "", "ats_id": "",
         "role": "no-id-must-survive"},
    ]
    df = pd.DataFrame(rows)
    kept, dropped, unverified = closure.filter_closed(df)
    report["filter"] = {"rows": len(df), "dropped": dropped,
                        "unverified": unverified, "kept": len(kept)}

    dropped_roles = set(kept["role"]) if len(kept) else set()
    check("only-closed-probes-dropped",
          all(r == "closed-probe" or r.startswith("live-") is False
              for r in df[~df.index.isin(kept.index)]["role"]),
          f"dropped={dropped}")
    check("uncrawled-board-survives",
          "uncrawled-must-survive" in set(kept["role"]), "")
    check("unverifiable-ats-survives",
          "unverifiable-must-survive" in set(kept["role"]), "")
    check("no-id-row-survives", "no-id-must-survive" in set(kept["role"]), "")

    # Failed-board rule: an empty snapshot must not delete.
    closure.record_snapshot("Greenhouse", "EmptyBoard", [])
    df2 = pd.DataFrame([{"company": "EmptyBoard", "ats_type": "greenhouse",
                         "ats_id": "999", "role": "x"}])
    kept2, dropped2, _ = closure.filter_closed(df2)
    check("failed-board-never-deletes", dropped2 == 0 and len(kept2) == 1, "")

    report["ok"] = not failures
    print(json.dumps(report, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"wrote {args.out}")

    closure.reset()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
