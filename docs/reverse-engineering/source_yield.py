#!/usr/bin/env python3
"""Measure per-source value from an already-published SearchTern feed.

Read-only: it never writes pages/, cache/, or any production artifact. It answers
the question the roadmap depends on — which sources contribute listings that no
other source in the same feed contributes — using stable, normalized links.

Caveats are printed with the result: a source can look "unique" only because the
strategy that would duplicate it is not in this feed (e.g. a competitor's
protected search), and a missing link makes a row uncountable.

Usage:
    python docs/reverse-engineering/source_yield.py pages/searchtern.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import urlsplit

_TRACKING_PARAM = re.compile(r"^(utm_.*|gh_src|lever-source|source|ref|referrer)$", re.I)


def normalize_link(link) -> str:
    """Collapse the ways the same posting is written across sources.

    Only host, path and non-tracking query survive. A query that carries the
    requisition id (Workday, some Lever boards) is kept, so distinct jobs do not
    merge, while marketing parameters that differ per source do not split them.
    """
    text = str(link or "").strip()
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else f"https://{text}")
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path)
    kept = []
    for pair in parts.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0]
        if _TRACKING_PARAM.match(key):
            continue
        kept.append(pair)
    return f"{host}{path}" + (f"?{'&'.join(kept)}" if kept else "")


def age_days(record) -> float | None:
    raw = str(record.get("date") or "").replace("Z", "+00:00")
    if not raw:
        return None
    try:
        posted = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return (datetime.now(UTC) - posted).total_seconds() / 86400


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[idx]


def main(path):
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    total = len(rows)

    by_source = defaultdict(list)
    owners = defaultdict(set)  # normalized link -> source labels
    missing_link = 0
    for record in rows:
        label = str(record.get("source") or "(none)")
        by_source[label].append(record)
        key = normalize_link(record.get("link"))
        if not key:
            missing_link += 1
            continue
        owners[key].add(label)

    print(f"feed={path} rows={total:,} unique_links={len(owners):,} "
          f"rows_without_link={missing_link:,}")
    dup_rows = sum(count - 1 for count in (len(v) for v in owners.values()))
    if owners:
        print(f"cross_source_duplicate_links={sum(1 for v in owners.values() if len(v) > 1):,} "
              f"(extra rows={dup_rows:,})")

    print(f"\n{'source':44} {'rows':>7} {'uniq':>7} {'sole':>7} {'median_age':>10} "
          f"{'p90_age':>8}")
    stats = []
    for label, records in by_source.items():
        links = [normalize_link(r.get("link")) for r in records]
        distinct = {link for link in links if link}
        sole = {link for link in distinct if len(owners[link]) == 1}
        ages = [a for a in (age_days(r) for r in records) if a is not None and a >= 0]
        stats.append({
            "source": label,
            "rows": len(records),
            "distinct": len(distinct),
            "sole": len(sole),
            "median_age": percentile(ages, 50),
            "p90_age": percentile(ages, 90),
        })

    for row in sorted(stats, key=lambda r: -r["sole"]):
        med = "-" if row["median_age"] is None else f"{row['median_age']:.1f}d"
        p90 = "-" if row["p90_age"] is None else f"{row['p90_age']:.1f}d"
        print(f"{row['source'][:44]:44} {row['rows']:>7,} {row['distinct']:>7,} "
              f"{row['sole']:>7,} {med:>10} {p90:>8}")

    print("\nRead this as: 'sole' = postings only this source carries in this feed.")
    print("It is an upper bound on unique value, not a freshness or fetch-cost measure.")
    print("Sources absent from this feed can still be valuable; absence is not evidence.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pages/searchtern.json")
