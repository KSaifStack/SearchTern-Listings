"""Per-source ingestion observability.

Why this exists: the published feed cannot rank sources. Dedup runs before
publication, so a surviving row is source-exclusive *by construction* and every
source looks equally valuable (measured: 57 duplicate links across 20,844 rows,
every source near 100% "sole source"). Adding a source was therefore
indistinguishable from adding duplicates.

Each adapter now reports what it actually did -- how many rows it was handed,
how many survived validation, how long it took, whether it was truncated or
rate-limited. Combined with the digest's per-source new/closed counts this
answers the only question that matters for a new source: how many genuinely new
postings per second of fetching does it buy.

Design constraints kept deliberately small: stdlib only, no pipeline imports
(so tests can import it without running the scraper), and recording never
raises -- a broken counter must not fail a 4-hour ingredient run.
"""
import json
import os
import time

# source -> accumulated fields. Insertion-ordered so the JSON reads in run order.
_SOURCES = {}
_RUN = {"started": time.monotonic(), "notes": []}


def reset():
    """Forget everything recorded so far. Used by tests."""
    _SOURCES.clear()
    _RUN.clear()
    _RUN.update({"started": time.monotonic(), "notes": []})


def record(source, *, discovered=0, valid=0, status="ok", seconds=None,
           notes=None, **extra):
    """Accumulate one source's contribution.

    `discovered` is what the source handed us; `valid` is what survived that
    source's own validation (parsing, classification, link checks). The gap is
    the adapter's rejection rate, which is the first thing to look at when a
    source's yield drops.

    Counts add across calls so a source that fetches in phases can report each
    one; `status` and `notes` keep the last value (a later failure is the one
    worth surfacing). Numeric extras add, other extras overwrite.
    """
    try:
        entry = _SOURCES.setdefault(source, {
            "source": source,
            "discovered": 0,
            "valid": 0,
            "seconds": 0.0,
            "calls": 0,
            "status": "ok",
            "notes": [],
        })
        entry["discovered"] += int(discovered or 0)
        entry["valid"] += int(valid or 0)
        entry["calls"] += 1
        if seconds is not None:
            entry["seconds"] = round(entry["seconds"] + float(seconds), 3)
        if status and status != "ok":
            entry["status"] = str(status)
        if notes:
            entry["notes"].append(str(notes))
        for key, value in extra.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[key] = entry.get(key, 0) + value
            else:
                entry[key] = value
    except Exception:
        # Observability must never break ingestion.
        pass


def note(message):
    """Attach a run-level note (a skipped source, a degraded fallback)."""
    _RUN.setdefault("notes", []).append(str(message))


def merge_digest(new_by_source=None, closed=0, dead_links=0):
    """Fold the digest's post-dedup counts back in.

    These are the numbers that actually decide a source's worth: `new` counts
    survived dedup and merged into the published feed, so it is the only figure
    that is not inflated by overlap with another source.
    """
    for source, count in (new_by_source or {}).items():
        record(source, new=count)
    _RUN["closed"] = int(closed)
    _RUN["dead_links"] = int(dead_links)


def snapshot():
    """Return {sources: [...], totals: {...}} without writing anything."""
    rows = []
    for entry in _SOURCES.values():
        row = dict(entry)
        seconds = row.get("seconds") or 0.0
        row["new"] = row.get("new", 0)
        # The one comparative figure this module exists to produce: genuinely
        # new postings per second of fetch time.
        row["new_per_second"] = round(row["new"] / seconds, 4) if seconds else None
        if not row["notes"]:
            row.pop("notes")
        rows.append(row)
    rows.sort(key=lambda r: (r.get("new", 0), r["valid"], r["discovered"]),
              reverse=True)
    totals = {
        "sources": len(rows),
        "discovered": sum(r["discovered"] for r in rows),
        "valid": sum(r["valid"] for r in rows),
        "new": sum(r.get("new", 0) for r in rows),
        "seconds": round(sum(r.get("seconds", 0.0) for r in rows), 3),
        "failed": sum(1 for r in rows if r["status"] != "ok"),
        "closed": _RUN.get("closed", 0),
        "dead_links": _RUN.get("dead_links", 0),
    }
    return {"sources": rows, "totals": totals, "notes": _RUN.get("notes", [])}


def write_json(path, run_meta=None):
    """Write the snapshot. Returns the totals dict, or None on failure."""
    try:
        payload = snapshot()
        payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload["elapsed_seconds"] = round(time.monotonic() - _RUN["started"], 1)
        if run_meta:
            payload["run"] = dict(run_meta)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return payload["totals"]
    except Exception as e:
        print(f"  source_stats skipped: {e}")
        return None


def summary_line():
    """One-line console summary, so every run prints its own yield table."""
    snap = snapshot()
    totals = snap["totals"]
    if not snap["sources"]:
        return "  source_stats: nothing recorded"
    return (f"  source_stats: {totals['sources']} sources, "
            f"{totals['discovered']:,} discovered, {totals['valid']:,} valid, "
            f"{totals['new']:,} new, {totals['failed']} degraded")
