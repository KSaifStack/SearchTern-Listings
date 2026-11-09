"""Shadow publisher: build the union feed beside production, change nothing.

Phase 3 of docs/reverse-engineering/snapshot-audit.md. Every run also computes
what the feed would contain if it were published from snapshots instead of from
the tiers that happened to run, and writes that comparison under
`pages/shadow/`. Production feeds, the seen log, closure state and the source
snapshots are read-only inputs here; the only thing this module writes is its own
directory.

The base of the shadow feed is the production feed the run just wrote, so the
invariant is strict: shadow is a superset of production. Anything production had
and shadow lost is a bug, reported as `unexplained` rather than excused.

Comparison is per canonical identity (`dedup.dedup_keys`: ats id, else canonical
URL, else company+role+location), never per row count.
"""
import json
import os

import dedup
import feed_scope
import pandas as pd
import snapshots

KEEP = int(os.environ.get("SEARCHTERN_SHADOW_KEEP", "4") or 4)
SAMPLES = int(os.environ.get("SEARCHTERN_SHADOW_SAMPLES", "100") or 0)


def _stamp(now):
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, (list, dict)) else []
    except (OSError, ValueError):
        return []


def _source_frame(cache_dir, name):
    path = os.path.join(cache_dir, f"tier_{name}.parquet")
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - a broken cache is a finding, not a crash
        print(f"  Shadow: could not read {name} cache ({type(exc).__name__}: {exc})")
        return pd.DataFrame()


def _ats_pairs(frame):
    """(ats_type, ats_id) for rows that carry one, in lowercase."""
    if frame.empty or "ats_type" not in frame or "ats_id" not in frame:
        return []
    ats = frame["ats_type"].astype(str).str.strip().str.lower()
    req = frame["ats_id"].astype(str).str.strip().str.lower()
    keep = ats.ne("") & req.ne("") & req.ne("nan") & req.ne("none")
    # Plain str, not numpy scalars: these end up in the JSON artifact.
    return [(str(a), str(r)) for a, r in zip(ats[keep], req[keep], strict=False)]


def build(pages_dir, cache_dir, now=None, production=None):
    """Compute the shadow comparison. Pure with respect to everything but `pages/shadow`."""
    stamp = _stamp(now)
    if production is None:
        production = _read_json(os.path.join(pages_dir, "searchtern.json"))
    base = pd.DataFrame(production) if production else pd.DataFrame()

    base_dupes = 0
    if not base.empty:
        deduped = dedup.dedupe(base)
        base_dupes = len(base) - len(deduped)
        base = deduped
    base_keys = list(dedup.dedup_keys(base)) if not base.empty else []
    key_source = dict.fromkeys(base_keys, "production")

    conflicts = {}
    id_types = {}
    for pair, key in zip(_ats_pairs(base), base_keys, strict=False):
        conflicts.setdefault(pair, set()).add(key)
        id_types.setdefault(pair[1], set()).add(pair[0])

    sidecars = snapshots.collect(cache_dir, stamp)
    fresh = snapshots.eligible(sidecars, stamp)
    expired = [
        {
            "source": name,
            "age_hours": side.get("age_hours"),
            "ttl_hours": side.get("ttl_hours"),
            "rows": int(side.get("rows") or 0),
            "status": side.get("status", ""),
        }
        for name, side in sorted(sidecars.items())
        if name not in fresh
    ]

    order = sorted(fresh, key=lambda n: (snapshots.PRECEDENCE.index(snapshots.class_of(n)), n))
    contributions = []
    additions = {}
    duplicates = {}
    for name in order:
        if name == "jobhive":
            # The run's own live fetch is already the base; merging its snapshot
            # too would double-count the rows it contributed.
            continue
        frame = _source_frame(cache_dir, name)
        rows_in = len(frame)
        entry = {"source": name, "class": snapshots.class_of(name), "rows": rows_in,
                 "rows_after_scope": 0, "added": 0, "duplicates": 0,
                 "age_hours": sidecars[name].get("age_hours"),
                 "stale": bool(sidecars[name].get("rejections"))}
        if rows_in == 0:
            contributions.append(entry)
            continue

        frame = feed_scope.scope(frame)
        entry["rows_after_scope"] = len(frame)
        frame = dedup.dedupe(frame)
        keys = list(dedup.dedup_keys(frame))
        entry["duplicates"] += len(frame) - len(set(keys))

        for pair, key in zip(_ats_pairs(frame), keys, strict=False):
            conflicts.setdefault(pair, set()).add(key)
            id_types.setdefault(pair[1], set()).add(pair[0])

        new = []
        for key in keys:
            if key in key_source:
                entry["duplicates"] += 1
                continue
            key_source[key] = name
            new.append(key)
        entry["added"] = len(new)
        if new:
            additions[name] = sorted(new)
        duplicates[name] = entry["duplicates"]
        contributions.append(entry)

    shadow_keys = set(key_source)
    removed = sorted(set(base_keys) - shadow_keys)

    added_by_source = [
        {
            "source": name,
            "added": len(keys),
            "samples": keys[:SAMPLES],
            "samples_truncated": len(keys) > SAMPLES,
        }
        for name, keys in sorted(additions.items(), key=lambda kv: -len(kv[1]))
    ]
    # Two shapes of disagreement: one identity key carrying several requisitions
    # (we would publish one and drop the rest), and one requisition id appearing
    # under several ATS types (we would publish it twice). Both are reported with
    # the rows that disagree so a human can decide which is right.
    conflict_rows = [
        {"kind": "ats_identity_split", "ats_type": pair[0], "ats_id": pair[1],
         "keys": sorted(keys), "count": len(keys)}
        for pair, keys in sorted(conflicts.items())
        if len(keys) > 1
    ] + [
        {"kind": "ats_id_under_multiple_ats_types", "ats_id": req,
         "ats_types": sorted(types), "count": len(types)}
        for req, types in sorted(id_types.items())
        if len(types) > 1
    ]

    payload = {
        "generated_at": stamp.isoformat(),
        "base": {"path": "pages/searchtern.json", "rows": len(base),
                 "duplicates_in_base": base_dupes, "identities": len(base_keys)},
        "policy": {"ttl_hours": dict(snapshots.TTL_HOURS),
                   "precedence": list(snapshots.PRECEDENCE),
                   "keep_runs": KEEP},
        "summary": {
            "production_rows": len(base),
            "shadow_rows": len(shadow_keys),
            "added": len(shadow_keys) - len(base_keys),
            "removed": len(removed),
            "duplicate_rows": sum(duplicates.values()) + base_dupes,
            "expired_snapshots": len(expired),
            "ats_conflicts": len(conflict_rows),
            "unexplained_removed": len(removed),
            "samples_capped_at": SAMPLES,
        },
        "additions": added_by_source,
        "removals": {
            "count": len(removed),
            "reason": "production base is merged first, so a loss here is a bug",
            "samples": removed[:SAMPLES],
        },
        "duplicates": {"count": sum(duplicates.values()) + base_dupes,
                       "by_source": dict(sorted(duplicates.items()))},
        "expired": expired,
        "ats_conflicts": conflict_rows[:SAMPLES],
        "ats_conflicts_truncated": len(conflict_rows) > SAMPLES,
        "source_contributions": contributions,
    }
    return payload


def write(pages_dir, payload):
    """Persist one timestamped artifact and keep the newest KEEP of them."""
    directory = os.path.join(pages_dir, "shadow")
    os.makedirs(directory, exist_ok=True)
    stamp = str(payload["generated_at"]).replace(":", "").replace("+0000", "Z")
    stamp = stamp.replace("+00:00", "Z")
    target = os.path.join(directory, f"{stamp}.json")
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(temp, target)

    history = sorted(n for n in os.listdir(directory)
                     if n.endswith(".json") and n != "last-error.json")
    for name in history[:-max(KEEP, 1)]:
        os.unlink(os.path.join(directory, name))
    return target


def run(pages_dir, cache_dir, now=None, production=None):
    payload = build(pages_dir, cache_dir, now=now, production=production)
    payload["artifact"] = os.path.basename(write(pages_dir, payload))
    return payload


def write_error(pages_dir, exc):
    """Record a shadow failure inside its own directory; never raises."""
    try:
        directory = os.path.join(pages_dir, "shadow")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "last-error.json"), "w", encoding="utf-8") as handle:
            json.dump({"at": pd.Timestamp.now(tz="UTC").isoformat(),
                       "error": f"{type(exc).__name__}: {exc}"}, handle, indent=1)
    except Exception:  # noqa: BLE001 - a shadow failure must not fail the run
        pass
