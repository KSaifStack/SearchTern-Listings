"""Source Intelligence payload: per-source freshness and board health.

``source_stats.json`` describes one run. ``source_health.jsonl`` remembers every
run. Neither is shaped for a dashboard, so this builds the single artifact a
frontend can poll:

* every source with rows, new, new-per-second, share of feed and last-ok age,
* alerts (two non-ok runs in a row, quiet sources, boards that never yield),
* one ``generated_at`` so a UI can show "verified at" instead of guessing.

Never raises; a missing log yields an empty but valid payload.
"""
from __future__ import annotations

import json
import os
import time

import source_health


def _age_hours(stamp, now):
    try:
        return round(max(now - float(stamp), 0.0) / 3600.0, 1)
    except (TypeError, ValueError):
        return None


def payload(snapshot, run_meta=None, health=None, now=None):
    """Build the dashboard payload from an ingest snapshot + health report."""
    now = time.time() if now is None else now
    report = health if health is not None else source_health.report(now=now)
    run_meta = run_meta or {}
    rows = list((snapshot or {}).get("sources") or [])
    total = sum(int(row.get("new", 0)) for row in rows) or 1
    freshness = report.get("best") or []

    sources = []
    for row in rows:
        best = next((b for b in freshness if b.get("source") == row.get("source")), {})
        sources.append({
            "source": row.get("source"),
            "rows": int(row.get("valid", 0)),
            "new": int(row.get("new", 0)),
            "share_of_new_pct": round(100.0 * int(row.get("new", 0)) / total, 2),
            "new_per_second": row.get("new_per_second"),
            "seconds": row.get("seconds", 0.0),
            "status": row.get("status", "ok"),
            "new_7d": best.get("new_7d"),
        })
    sources.sort(key=lambda s: (s["new"], s["rows"]), reverse=True)

    alerts = []
    for failure in report.get("failures") or []:
        alerts.append({"kind": "failing", "source": failure.get("source"),
                       "detail": str(failure.get("status"))})
    for quiet in report.get("quiet") or []:
        alerts.append({"kind": "quiet", "source": quiet.get("source"),
                       "detail": f"{quiet.get('hours_since_last')}h since last data"})
    for row in rows:
        if row.get("status") not in (None, "ok"):
            alerts.append({"kind": "degraded", "source": row.get("source"),
                           "detail": str(row.get("status"))})

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "totals": (snapshot or {}).get("totals", {}),
        "run": {"tier": run_meta.get("tier"), "workers": run_meta.get("workers"),
                "published": run_meta.get("published", {})},
        "sources": sources,
        "alerts": alerts,
        "healthy": not any(a["kind"] == "failing" for a in alerts),
    }


def attach_row_times(frame, first_by_index, now_iso, fresh_buckets=(), source_of=None,
                     bucket_of=None):
    """Add per-row ``first_seen_at`` / ``last_seen_at`` / ``verified_at``.

    ``first_by_index`` maps a row's index label to the first run that saw it, so
    a republished row keeps its real first-seen date instead of today's.
    ``verified_at`` is only stamped for rows whose source was actually re-fetched
    this run: a cached copy is not evidence the posting is still open.
    """
    if frame is None or len(frame) == 0:
        return frame
    out = frame.copy()
    out["first_seen_at"] = out.index.map(lambda i: str(first_by_index.get(i, "") or ""))
    out["last_seen_at"] = now_iso
    if fresh_buckets and source_of is not None:
        bucket_of = bucket_of or source_of
        fresh = set(fresh_buckets)
        out["verified_at"] = [
            now_iso if bucket_of(row) in fresh else "" for _, row in out.iterrows()
        ]
    else:
        out["verified_at"] = now_iso
    return out


def write_json(path, snapshot, run_meta=None, health=None):
    """Write the dashboard payload. Returns it, or None on failure."""
    try:
        data = payload(snapshot, run_meta=run_meta, health=health)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        return data
    except Exception as exc:  # noqa: BLE001
        print(f"  health.json skipped: {type(exc).__name__}: {exc}")
        return None
