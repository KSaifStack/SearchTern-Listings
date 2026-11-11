"""Production publication boundary: union eligible snapshots, then the live run.

`snapshots.merge` already knows how to union every source that is inside its
TTL in precedence order. This module is the small production adapter around it:

* cached sources are loaded from their own parquet snapshots;
* the frame the current run built is split by provenance and re-inserted at
  its snapshot precedence, so a live jobhive row cannot beat a direct-ATS row
  and a cached markdown row cannot beat a live jobhive row;
* the result is deduplicated in precedence order.

The important property is that publication no longer depends on which tier
triggered the run. A light run can fetch jobhive and markdown while the heavy
and medium snapshots are still inside TTL; those rows remain in the feed.
"""
import json
import os
import tempfile
from datetime import UTC, datetime

import dedup
import pandas as pd
import snapshots
from markdown_sources import MARKDOWN_SOURCES

# The one source whose absence means the feed is not safe to replace. Direct ATS
# and cached aggregators can be degraded; jobhive is the base feed.
CRITICAL_SOURCES = ("jobhive",)

# The live frame uses provenance labels, not snapshot names. Map each label to
# the same policy class `snapshots.CLASS_OF` uses for its cache.
SOURCE_CLASS = {
    "jobhive": "jobhive",
    "ats": "direct_ats",
    "sitemap": "direct_ats",
    "priority": "priority_ats",
    "md": "markdown",
    "freehire": "aggregators",
    "breadth": "aggregators",
    "jobspy": "aggregators",
    "ecr": "aggregators",
    "echo": "specialty",
    "echojobs": "specialty",
    "hn": "specialty",
    "remoteok": "specialty",
    "skillexchange": "specialty",
    "skill": "specialty",
    "startupjobs": "specialty",
    "runway": "specialty",
    "rss": "specialty",
}


def _load_parquet(cache_dir):
    def load(name):
        path = os.path.join(cache_dir, f"tier_{name}.parquet")
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            return pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - a broken cache is a finding, not a crash
            print(f"  Publication: could not read {name} cache "
                  f"({type(exc).__name__}: {exc})")
            return pd.DataFrame()

    return load


MARKDOWN_NAMES = {
    str(source.get("name")).strip().lower()
    for source in MARKDOWN_SOURCES if source.get("name")
}


def _source_class(source):
    key = str(source or "").strip().lower()
    if key in MARKDOWN_NAMES:
        return "markdown"
    return SOURCE_CLASS.get(key, "specialty")


def _frames_by_class(union, live):
    frames = {name: [] for name in snapshots.PRECEDENCE}
    if union is not None and not union.empty and "source" in union.columns:
        for source, group in union.groupby("source", dropna=False):
            frames[_source_class(source)].append(group)
    if live is not None and not live.empty:
        if "source" not in live.columns:
            frames["specialty"].insert(0, live)
        else:
            # Live rows are fresher than the cache of the same class, so they
            # go first inside their class. Cross-class order still comes from
            # `snapshots.PRECEDENCE`.
            for source, group in live.groupby("source", dropna=False):
                frames[_source_class(source)].insert(0, group)
    return frames


def _dedupe_ordered(frames):
    out = pd.DataFrame()
    seen = set()
    for frame in frames:
        if frame is None or frame.empty:
            continue
        frame = dedup.dedupe(frame)
        if frame.empty:
            continue
        keys = dedup.dedup_keys(frame)
        keep = ~keys.isin(seen)
        if not keep.any():
            continue
        frame = frame[keep]
        out = frame.copy() if out.empty else pd.concat([out, frame], ignore_index=True)
        seen |= set(keys[keep])
    return out


def assess(cache_dir, now=None, frame=None):
    """Decide whether the current publication is safe to replace the feed.

    Expiry is a publication state, not a closure signal. A missing, expired, or
    rejected critical snapshot blocks the write; a missing non-critical source
    marks the result degraded but still publishes the rows that are known.
    """
    report = snapshots.report(cache_dir, now)
    sidecars = report.get("sources", {})
    blockers = []
    for name in CRITICAL_SOURCES:
        side = sidecars.get(name)
        if not side:
            blockers.append({"source": name, "reason": "missing"})
            continue
        status = str(side.get("status") or "")
        if not side.get("provenance_ok"):
            blockers.append({"source": name, "reason": "invalid_provenance"})
        elif status.startswith("rejected") or status in ("empty", "failed"):
            blockers.append({"source": name, "reason": status})
        elif not side.get("fresh"):
            blockers.append({"source": name, "reason": "expired"})
        elif int(side.get("rows") or 0) <= 0:
            blockers.append({"source": name, "reason": "empty"})

    rows = None if frame is None else int(len(frame))
    if rows == 0 and not blockers:
        blockers.append({"source": "feed", "reason": "empty"})

    degraded = bool(report.get("missing") or report.get("expired")
                    or report.get("empty") or report.get("rejected")
                    or report.get("invalid"))
    status = "failed" if blockers else ("degraded" if degraded else "ok")
    return {
        "status": status,
        "blockers": blockers,
        "rows": rows,
        "report": report,
    }


def write_status(pages_dir, assessment):
    """Record a blocked publish without touching the previous feed or manifest."""
    os.makedirs(pages_dir, exist_ok=True)
    report = assessment.get("report") or {}
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": assessment.get("status", "failed"),
        "blockers": assessment.get("blockers", []),
        "rows": assessment.get("rows"),
        "snapshots": {
            "count": report.get("count", 0),
            "fresh": report.get("fresh", []),
            "expired": report.get("expired", []),
            "missing": report.get("missing", []),
            "rejected": report.get("rejected", []),
            "invalid": report.get("invalid", []),
        },
        "last_known_good": "pages/last-known-good",
        "last_known_good_manifest": "pages/last-known-good/current.json",
    }
    target = os.path.join(pages_dir, "publish-status.json")
    handle, temp = tempfile.mkstemp(dir=pages_dir, prefix=".publish-status.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return payload


def build_publish_frame(live_frame, cache_dir, now=None, load=None):
    """Return the union of eligible snapshots plus this run's live rows.

    Duplicate identity keys keep the higher-precedence source. Within one
    class, the live row wins over its cached copy because the live fetch is
    newer.
    """
    live = live_frame if live_frame is not None else pd.DataFrame()
    sidecars = snapshots.collect(cache_dir, now)
    if not sidecars:
        return live.copy()

    union = snapshots.merge(sidecars, load or _load_parquet(cache_dir), now=now)
    if union.empty:
        return live.copy()
    if live.empty:
        return union

    grouped = _frames_by_class(union, live)
    frames = []
    for name in snapshots.PRECEDENCE:
        frames.extend(grouped[name])
    return _dedupe_ordered(frames)
