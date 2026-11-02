"""Persistent per-run source health log.

ingest_stats.py describes one run; this module remembers across runs. Each
pipeline run appends one JSON line per source (timestamp, discovered, valid,
new, seconds, status) to Listings/cache/source_health.jsonl, capped at the
most recent entries so the file cannot grow without bound.

report() reads the log back and returns:
  failures: sources whose last ALERT_AFTER runs in a row are non-ok
  (a source that fails twice running is paged, not one that blipped once),
  quiet: sources with no entry in the last QUIET_AFTER_HOURS hours,
  best: top sources by new postings in the trailing window (the ranking input
  for what to poll more often vs retire).

Stdlib only; recording never raises, like ingest_stats.
"""

import json
import os
import time

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_PATH = os.path.join(_CACHE_DIR, "source_health.jsonl")
MAX_LINES = 5000
ALERT_AFTER = 2
QUIET_AFTER_HOURS = 49
RANK_WINDOW_HOURS = 24 * 7


def _read_lines():
    try:
        with open(_PATH, encoding="utf-8") as f:
            return [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def append(stats_snapshot, new_by_source=None):
    """Append one line per source. stats_snapshot is ingest_stats.snapshot()."""
    try:
        now = time.time()
        lines = _read_lines()
        for entry in stats_snapshot.get("sources", []):
            rec = {
                "ts": now,
                "source": entry.get("source"),
                "discovered": entry.get("discovered", 0),
                "valid": entry.get("valid", 0),
                "new": (new_by_source or {}).get(entry.get("source"), 0),
                "seconds": entry.get("seconds", 0.0),
                "status": entry.get("status", "ok"),
            }
            lines.append(json.dumps(rec, ensure_ascii=False))
        lines = lines[-MAX_LINES:]
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, _PATH)
    except Exception:
        pass


def report(now=None):
    """Return {failures, quiet, best} from the log. Never raises."""
    now = now if now is not None else time.time()
    failures, quiet, best = [], [], []
    try:
        rows = []
        for ln in _read_lines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
        by_source = {}
        for r in rows:
            by_source.setdefault(r.get("source"), []).append(r)
        for source, recs in sorted(by_source.items()):
            recs.sort(key=lambda r: r.get("ts", 0))
            tail = recs[-ALERT_AFTER:]
            if len(tail) == ALERT_AFTER and all(
                    str(r.get("status", "ok")) != "ok" for r in tail):
                failures.append({
                    "source": source,
                    "status": tail[-1].get("status"),
                    "since": tail[0].get("ts"),
                })
            age_h = (now - recs[-1].get("ts", 0)) / 3600
            if age_h > QUIET_AFTER_HOURS:
                quiet.append({"source": source,
                              "hours_since_last": round(age_h, 1)})
            window = [r for r in recs
                      if now - r.get("ts", 0) <= RANK_WINDOW_HOURS * 3600]
            best.append({"source": source,
                         "new_7d": sum(r.get("new", 0) for r in window),
                         "runs_7d": len(window)})
        best.sort(key=lambda b: b["new_7d"], reverse=True)
    except Exception:
        pass
    return {"failures": failures, "quiet": quiet, "best": best}
