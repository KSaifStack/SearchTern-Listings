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
    "rss": "specialty",
}

# Sources a complete run is expected to leave a sidecar for. Optional sources
# are recorded when enabled, but their absence is not a degraded publication.
EXPECTED_SOURCES = (
    "jobhive",
    "heavy",
    "ats_new",
    "priority",
    "light",
    "medium",
    "breadth",
    "ecr",
    "echo",
    "skill",
    "jobspy",
)
OPTIONAL_SOURCES = ("sitemap", "runway", "rss")

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
        "provenance": "fetch",
        "rows": int(rows),
        "status": str(status),
        "etag": str(etag or ""),
        "rejections": int(rejected),
    }
    return _write(name, path, payload)


def seed(name, path, now=None):
    """Create a sidecar for an existing cache that predates the ledger.

    The cache's mtime is the only honest timestamp available, so it is used as
    `fetched_at`; seeding must not make an old parquet look fresh. The status is
    `seeded` so a consumer can distinguish a backfilled record from a fetch.
    """
    existing = read(name, path)
    if existing:
        return existing
    if not os.path.exists(path):
        return {}
    try:
        rows = len(pd.read_parquet(path))
        stamp = pd.Timestamp.fromtimestamp(os.path.getmtime(path), tz="UTC")
    except Exception as exc:  # noqa: BLE001 - an unreadable cache stays missing
        print(f"  Snapshot seed {name} failed ({type(exc).__name__}: {exc})")
        return {}
    payload = record(name, rows, status="seeded", path=path, now=stamp)
    payload["provenance"] = "cache_mtime"
    payload["provenance_ok"] = False
    _write(name, path, payload)
    return read(name, path)


def reconcile_rows(name, path, rows):
    """Correct a sidecar's row count when it disagrees with the parquet.

    The fetch timestamp and status are preserved: this repairs metadata, it does
    not pretend the cache was refreshed.
    """
    side = read(name, path)
    if not side:
        return side
    actual = int(rows)
    recorded = int(side.get("rows") or 0)
    if recorded == actual:
        return side
    side["rows"] = actual
    side["rows_reconciled_from"] = recorded
    return _write(name, path, side)


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
    if rows == 0:
        # An empty fetch is never a successful snapshot, even on a first run.
        # It must be visible as rejected rather than a fresh zero-row source.
        reason = "empty"
    elif (previous_rows > 0 and rejections < MAX_REJECTIONS
          and rows < previous_rows * SHRINK_BAND):
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


def _provenance_ok(sidecar):
    if str(sidecar.get("provenance") or "") != "fetch":
        return False
    return sidecar.get("provenance_ok") is not False


def _within_ttl(sidecar, now=None, name=None):
    age = age_hours(sidecar, now)
    if age is None or age < 0:
        return False
    source = str(name or sidecar.get("source") or "")
    if not source:
        return False
    return age <= float(ttl_hours(source))


def is_fresh(sidecar, now=None, name=None):
    """True only for a successful, recent fetch.

    A rejected parse is never fresh, even when its previous rows are still
    servable: "fresh" answers "was this source refreshed", not "can we publish
    something older". Seeded records, unknown statuses and cache mtimes are
    always false. TTL comes from policy, never from the sidecar, so old
    metadata cannot extend a cache's life.

    An expired snapshot is not eligible to be published. It is not a closure
    signal: expiry says "we have not refreshed this lately", nothing about
    whether the postings still exist.
    """
    sidecar = sidecar or {}
    if not _provenance_ok(sidecar):
        return False
    if str(sidecar.get("status") or "") not in ("ok", "ok:recovered"):
        return False
    return _within_ttl(sidecar, now, name)


def is_servable(sidecar, now=None, name=None):
    """True when a snapshot may contribute rows to a publication.

    A rejected parse with previous rows inside TTL is served as the previous
    snapshot and reported as rejected; an empty rejected parse has nothing to
    serve. TTL expiry excludes it either way.
    """
    if is_fresh(sidecar, now, name):
        return True
    sidecar = sidecar or {}
    if not _provenance_ok(sidecar):
        return False
    if not str(sidecar.get("status") or "").startswith("rejected:"):
        return False
    if int(sidecar.get("rows") or 0) <= 0:
        return False
    return _within_ttl(sidecar, now, name)


def eligible(sidecars, now=None):
    """The subset of snapshots a publisher may read, keyed by source name."""
    return {name: side for name, side in (sidecars or {}).items()
            if is_servable(side, now)}


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
        # The filename is the identity. A sidecar whose `source` disagrees with
        # its filename has unknown provenance and must never be published under
        # either name.
        file_name = entry[:-5]
        name = str(side.get("source") or file_name)
        side["file_source"] = file_name
        side["source_match"] = name == file_name
        side["known"] = file_name in CLASS_OF
        side["provenance_ok"] = bool(
            side.get("provenance_ok", True)
            and side["source_match"]
            and side["known"]
            and side.get("provenance") == "fetch"
        )
        # Policy is code, not metadata copied into an old sidecar. Override the
        # class/TTL so a policy change cannot be defeated by a stale file.
        side["class_match"] = side.get("class") in (None, "", class_of(file_name))
        side["ttl_match"] = side.get("ttl_hours") in (None, "", ttl_hours(file_name))
        side["class"] = class_of(file_name)
        side["ttl_hours"] = ttl_hours(file_name)
        side["seeded"] = str(side.get("status", "")) == "seeded"
        side["age_hours"] = age_hours(side, now)
        side["fresh"] = side["provenance_ok"] and is_fresh(side, now, file_name)
        side["servable"] = side["provenance_ok"] and is_servable(side, now, file_name)
        sources[file_name] = side
    return sources


def report(cache_dir, now=None):
    """Payload for pages/snapshots.json: what each source has, and how old it is."""
    stamp = _stamp(now)
    sources = collect(cache_dir, stamp)
    expired = sorted(
        n for n, s in sources.items()
        if not s["fresh"]
        and str(s.get("status", "")) not in ("empty",)
        and not str(s.get("status", "")).startswith("rejected:")
    )
    empty = sorted(n for n, s in sources.items()
                   if str(s.get("status", "")) == "empty")
    rejected = sorted(n for n, s in sources.items()
                      if str(s.get("status", "")).startswith("rejected"))
    invalid = sorted(n for n, s in sources.items() if not s.get("provenance_ok"))
    expected = list(EXPECTED_SOURCES)
    return {
        "generated_at": stamp.isoformat(),
        "policy": {"ttl_hours": dict(TTL_HOURS), "precedence": list(PRECEDENCE),
                   "shrink_band": SHRINK_BAND},
        "count": len(sources),
        "expected": expected,
        "optional": list(OPTIONAL_SOURCES),
        "missing": sorted(set(expected) - set(sources)),
        "unexpected": sorted(set(sources) - set(expected) - set(OPTIONAL_SOURCES)),
        "fresh": sorted(n for n, s in sources.items() if s["fresh"]),
        "expired": expired,
        "empty": empty,
        "rejected": rejected,
        "invalid": invalid,
        "seeded": sorted(n for n, s in sources.items() if s.get("seeded")),
        "serving_previous": sorted(n for n, s in sources.items()
                                  if s.get("servable") and not s["fresh"]),
        "rows_fresh": sum(int(s.get("rows") or 0) for s in sources.values() if s["fresh"]),
        "rows_eligible": sum(int(s.get("rows") or 0) for s in sources.values()
                             if s.get("servable")),
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
