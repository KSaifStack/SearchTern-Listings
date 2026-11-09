"""Per-source snapshot records: success gate, TTL eligibility, precedence.

Phase 2 of the snapshot/unified-publication refactor
(docs/reverse-engineering/snapshot-audit.md). A cache parquet is the data; the
sidecar beside it records what the run that wrote it knew: when, how many rows,
whether the parse looked trustworthy, and which class the source belongs to.

Nothing here chooses what a run publishes yet. Publication still merges exactly
the tiers that ran; `eligible`/`merge` are the rules the publisher will use in a
later phase, and they are exercised by tests today so that phase has something
verified to build on.

Policy (decided 2026-11-09):
  TTL             priority ATS 24h, other direct ATS 48h, jobhive 24h,
                  markdown 48h, aggregators 72h, specialty 7 days
  precedence      direct ATS -> priority -> jobhive -> markdown -> aggregators
                  -> specialty
  storage         the existing CI caches (`Listings/cache`)
  TTL expiry      removes rows from a feed and nothing else: it never stamps an
                  entry closed and never implies a posting was delisted
"""
import json
import os
import tempfile

import canonical
import dedup
import pandas as pd

# Every cache this pipeline writes, mapped to the policy class that governs its
# TTL. Names match `_cache_path(name)` in generate_listings.py.
CLASS_OF = {
    "heavy": "direct_ats",
    "ats_new": "direct_ats",
    "sitemap": "direct_ats",
    "priority": "priority_ats",
    "jobhive": "jobhive",
    "light": "markdown",
    "medium": "aggregators",
    "breadth": "aggregators",
    "jobspy": "aggregators",
    "ecr": "aggregators",
    "echo": "specialty",
    "hn": "specialty",
    "remoteok": "specialty",
    "skill": "specialty",
    "startupjobs": "specialty",
    "runway": "specialty",
}

TTL_HOURS = {
    "priority_ats": 24,
    "direct_ats": 48,
    "jobhive": 24,
    "markdown": 48,
    "aggregators": 72,
    "specialty": 24 * 7,
}

# Higher class wins a duplicate. This is the order a publisher concatenates in.
PRECEDENCE = (
    "direct_ats",
    "priority_ats",
    "jobhive",
    "markdown",
    "aggregators",
    "specialty",
)

# A snapshot below this share of the previous one is treated as a failed parse.
SHRINK_BAND = 0.3
# After this many consecutive rejections the next result is accepted, so a
# source that genuinely shrinks cannot be pinned to a stale snapshot forever.
MAX_REJECTIONS = 2


def class_of(name):
    return CLASS_OF.get(str(name), "specialty")


def ttl_hours(name):
    return TTL_HOURS[class_of(name)]


def _dir(path):
    return os.path.join(os.path.dirname(os.path.abspath(path)), "snapshots")


def _sidecar_path(name, path):
    return os.path.join(_dir(path), f"{name}.json")


def read(name, path):
    """The sidecar recorded for this cache, or an empty dict."""
    try:
        with open(_sidecar_path(name, path), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name, path, payload):
    target = _sidecar_path(name, path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return payload


def _stamp(now):
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp


def record(name, rows, status="ok", etag="", path=None, now=None, rejected=0):
    """Write a sidecar without touching the parquet (used for live-fetched sources)."""
    path = path or os.path.join("cache", f"tier_{name}.parquet")
    payload = {
        "source": name,
        "class": class_of(name),
        "ttl_hours": ttl_hours(name),
        "fetched_at": _stamp(now).isoformat(),
        "rows": int(rows),
        "status": str(status),
        "etag": str(etag or ""),
        "rejections": int(rejected),
    }
    return _write(name, path, payload)


def save(frame, name, path, etag="", now=None):
    """Gate and persist one source's snapshot. Returns rows written, or 0.

    Rejects, and leaves the previous parquet untouched:
      * an empty result when the previous snapshot had rows;
      * a result below SHRINK_BAND of the previous row count;
      * a failed write.
    A rejected run is recorded in the sidecar instead, so the next run can see
    that a source is flapping rather than silently serving a stale cache.
    """
    previous = read(name, path)
    previous_rows = int(previous.get("rows") or 0)
    rejections = int(previous.get("rejections") or 0)
    rows = 0 if frame is None else len(frame)

    reason = ""
    if previous_rows > 0 and rejections < MAX_REJECTIONS:
        if rows == 0:
            reason = "empty"
        elif rows < previous_rows * SHRINK_BAND:
            reason = "shrunken"

    if reason:
        record(name, previous_rows, status=f"rejected:{reason}", etag=previous.get("etag", ""),
               path=path, now=now, rejected=rejections + 1)
        print(f"  Snapshot {name} rejected ({reason}): kept {previous_rows:,} rows, "
              f"got {rows:,}")
        return 0

    written = canonical.write_parquet_atomic(frame, path)
    if written is None:
        record(name, previous_rows, status="rejected:write_failed",
               etag=previous.get("etag", ""), path=path, now=now, rejected=rejections + 1)
        return 0
    status = "ok" if not reason and rejections == 0 else "ok:recovered"
    record(name, written, status=status, etag=etag, path=path, now=now, rejected=0)
    return written


def age_hours(sidecar, now=None):
    stamp = str((sidecar or {}).get("fetched_at") or "")
    if not stamp:
        return None
    try:
        fetched = pd.Timestamp(stamp)
    except (ValueError, TypeError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.tz_localize("UTC")
    delta = _stamp(now) - fetched
    return round(delta.total_seconds() / 3600.0, 2)


def is_fresh(sidecar, now=None):
    """True when the snapshot is inside its class TTL.

    An expired snapshot is simply not eligible to be published. It is not a
    closure signal: expiry says "we have not refreshed this lately", nothing
    about whether the postings still exist.
    """
    age = age_hours(sidecar, now)
    if age is None:
        return False
    name = str((sidecar or {}).get("source") or "")
    ttl = (sidecar or {}).get("ttl_hours") or ttl_hours(name)
    return age <= float(ttl)


def eligible(sidecars, now=None):
    """The subset of snapshots a publisher may read, keyed by source name."""
    return {name: side for name, side in (sidecars or {}).items() if is_fresh(side, now)}


def merge(sidecars, load, now=None):
    """Deterministic union of every eligible snapshot, in precedence order.

    `sidecars` maps source name -> sidecar; `load(name)` returns that source's
    frame from disk. The point of taking a loader instead of a tier list is the
    property this exists for: a light run refreshes jobhive and markdown, and
    every other source is still merged from its own snapshot because it is
    within TTL — a run cannot erase another source's listings by not fetching it.
    """
    live = eligible(sidecars, now)
    order = sorted(live, key=lambda n: (PRECEDENCE.index(class_of(n)), n))
    out = pd.DataFrame()
    seen = set()
    for name in order:
        frame = load(name)
        if frame is None or len(frame) == 0:
            continue
        frame = dedup.dedupe(frame)
        if out.empty:
            out = frame.copy()
            seen = set(dedup.dedup_keys(out))
            continue
        keys = dedup.dedup_keys(frame)
        keep = ~keys.isin(seen)
        if not keep.any():
            continue
        out = pd.concat([out, frame[keep]], ignore_index=True)
        seen |= set(keys[keep])
    return out


def collect(cache_dir, now=None):
    """Every sidecar in the cache directory, with its age and eligibility."""
    sources = {}
    directory = os.path.join(cache_dir, "snapshots")
    if not os.path.isdir(directory):
        return sources
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, entry), encoding="utf-8") as fh:
                side = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(side, dict):
            continue
        side["age_hours"] = age_hours(side, now)
        side["fresh"] = is_fresh(side, now)
        sources[str(side.get("source") or entry[:-5])] = side
    return sources


def report(cache_dir, now=None):
    """Payload for pages/snapshots.json: what each source has, and how old it is."""
    stamp = _stamp(now)
    sources = collect(cache_dir, stamp)
    expired = sorted(n for n, s in sources.items() if not s["fresh"])
    rejected = sorted(n for n, s in sources.items()
                      if str(s.get("status", "")).startswith("rejected"))
    return {
        "generated_at": stamp.isoformat(),
        "policy": {"ttl_hours": dict(TTL_HOURS), "precedence": list(PRECEDENCE),
                   "shrink_band": SHRINK_BAND},
        "count": len(sources),
        "fresh": sorted(n for n, s in sources.items() if s["fresh"]),
        "expired": expired,
        "rejected": rejected,
        "rows_eligible": sum(int(s.get("rows") or 0) for s in sources.values() if s["fresh"]),
        "sources": sources,
    }


def write_report(path, cache_dir, now=None):
    payload = report(cache_dir, now)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    os.replace(temp, path)
    return payload
