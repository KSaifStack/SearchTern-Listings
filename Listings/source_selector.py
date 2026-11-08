"""Spend a run's fetch budget where the measured value is.

The pipeline knows two things about a source by now: whether it works
(``source_health`` failure/quiet streaks) and how many genuinely new postings
it produced in the trailing week. This turns that into one decision per
configured source:

* **defer** -- two non-ok runs in a row, or no rows returned across at least
  ``MIN_RUNS_FOR_VALUE`` runs. Re-polling a dead source on every run costs the
  run its wall clock and yields nothing (six ``ats-probe`` runs in a row with
  0 new, measured 2026-11-06).
* **poll** -- everything else.
* **expand** -- healthy sources at or above the median trailing-week value.
  They are polled even when quiet, because a valuable source that went silent
  is exactly the one worth re-checking.

Value is what the adapter returned (``rows_7d``, or ``discovered_7d`` where the
adapter only records discoveries), never ``new_7d``: new postings are attributed
to the adapter *bucket*, so every namespaced feed (``breadth:Jobicy``) reports 0
new and would be retired on day one.

Conservative by construction: an unknown source (no history yet) is polled, not
deferred. A new source has no value data, and deferring it would mean it never
gets any. Only measured failures and measured zero yield are skipped.
"""
from __future__ import annotations

MIN_RUNS_FOR_VALUE = 4


def select(configured, health=None):
    """Split ``configured`` source names into poll / defer / expand.

    ``health`` is ``source_health.report()`` output; it is read when omitted.
    Returns ``{"poll": [...], "defer": [{"source", "reason"}], "expand": [...],
    "expand_cut": n}``.
    """
    if health is None:
        import source_health

        health = source_health.report()
    names = sorted({str(name) for name in configured or () if str(name or "").strip()})

    failing = {f.get("source") for f in health.get("failures") or []}
    quiet = {q.get("source") for q in health.get("quiet") or []}
    stats = {b.get("source"): b for b in health.get("best") or []}
    value = {
        name: max(int((stats.get(name) or {}).get("rows_7d", 0) or 0),
                  int((stats.get(name) or {}).get("discovered_7d", 0) or 0))
        for name in names
    }

    positive = sorted(v for v in value.values() if v > 0)
    expand_cut = positive[len(positive) // 2] if positive else 0

    poll, defer, expand = [], [], []
    for name in names:
        entry = stats.get(name) or {}
        if name in failing:
            defer.append({"source": name, "reason": "failing"})
            continue
        runs = int(entry.get("runs_7d", 0) or 0)
        if runs >= MIN_RUNS_FOR_VALUE and value.get(name, 0) == 0:
            defer.append({"source": name, "reason": "empty"})
            continue
        valuable = expand_cut > 0 and value.get(name, 0) >= expand_cut
        if name in quiet and not valuable:
            defer.append({"source": name, "reason": "quiet"})
            continue
        poll.append(name)
        if valuable:
            expand.append(name)
    return {"poll": poll, "defer": defer, "expand": expand, "expand_cut": expand_cut}
