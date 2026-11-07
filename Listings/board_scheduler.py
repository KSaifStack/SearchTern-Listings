"""Adaptive per-board probe scheduling.

Old behaviour: probe every board whose cooldown expired, in registry order.
That spends the run's whole wall clock on boards that returned nothing (measured:
719 s over 4,000 boards, 26 net-new rows; six ``ats-probe`` runs with 0 new).

This module orders a run's budget by *value of the next visit*:

    wait_hours * (1 + yield_weight * last_rows / YIELD_REFERENCE)

so a board that has waited longest goes first, and between equally stale boards
the one that actually returns postings wins. ``priority``/``standard``/``bulk``
lanes weight that further, so the 15-minute lane's curated boards stay ahead of
a bulk tenant without being re-listed in another file.

Starvation guard: anything not probed for ``STARVATION_HOURS`` jumps to the
front regardless of yield, so a quiet board can never be squeezed out for good.

State is the probe state file the caller already has: a float timestamp or a
``{"at": ..., "rows": ...}`` dict per board. Both shapes are read.
"""
from __future__ import annotations

import time

STARVATION_HOURS = 72.0
YIELD_REFERENCE = 50.0
# A board that returns nothing three visits running is not worth another
# round trip, but it is not dead either -- employers open boards in bursts.
ZERO_RUNS = 3
PROBATION_HOURS = 24.0 * 7
LANE_WEIGHT = {"priority": 3.0, "standard": 1.0, "bulk": 0.2}
DEFAULT_CAP = 1200


def last_probe(entry) -> float:
    """Timestamp of the last probe, 0.0 when never probed."""
    if isinstance(entry, dict):
        at = entry.get("at")
        return float(at) if isinstance(at, (int, float)) else 0.0
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return float(entry)
    return 0.0


def last_rows(entry) -> int:
    """Postings the board returned last time, 0 when unknown."""
    if isinstance(entry, dict):
        rows = entry.get("rows")
        return int(rows) if isinstance(rows, (int, float)) else 0
    return 0


def board_rows(data) -> int:
    """Count postings in a raw board payload, whatever its shape."""
    if isinstance(data, dict):
        if "_rows" in data and isinstance(data["_rows"], list):
            return len(data["_rows"])
        for key in ("jobPostings", "jobs", "results", "data", "postings"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return 0
    if isinstance(data, list):
        return len(data)
    return 0


def score(entry, now: float, lane: str = "standard", yield_weight: float = 1.0) -> float:
    """Rank for the next visit. Higher means probe sooner."""
    stamp = last_probe(entry)
    wait_hours = max((now - stamp) / 3600.0, 0.0) if stamp else STARVATION_HOURS * 2
    if wait_hours >= STARVATION_HOURS:
        return 1e9 + wait_hours
    yield_factor = 1.0 + yield_weight * min(last_rows(entry), 500) / YIELD_REFERENCE
    return wait_hours * yield_factor * LANE_WEIGHT.get(lane, 1.0)


def plan(boards, state, cap=None, now=None, lane_of=None, yield_weight=1.0, skip=()):
    """Split ``boards`` into (probe now, defer). Highest score first."""
    now = time.time() if now is None else now
    cap = DEFAULT_CAP if cap is None else cap
    lane_of = lane_of or {}
    skip = set(skip or ())
    if skip:
        boards = [b for b in boards if f"{b[1]}|{b[2]}" not in skip]

    def key(board):
        entry = state.get(f"{board[1]}|{board[2]}")
        return score(entry, now, lane_of.get(board[1] + "|" + board[2], "standard"),
                     yield_weight)

    ordered = sorted(boards, key=key, reverse=True)
    if cap <= 0 or len(ordered) <= cap:
        return ordered, []
    return ordered[:cap], ordered[cap:]


def record(state, ats, slug, rows, now=None):
    """Store this probe's timestamp, yield and zero-run streak."""
    key = f"{ats}|{slug}"
    prior = state.get(key)
    zero = prior.get("zero", 0) if isinstance(prior, dict) else 0
    rows = int(rows or 0)
    state[key] = {"at": now if now is not None else time.time(),
                  "rows": rows,
                  "zero": zero + 1 if rows == 0 else 0}


def skip_boards(state, runs=ZERO_RUNS, now=None, probation_hours=PROBATION_HOURS):
    """Boards to leave out of the rotation this run.

    Zero-yield for ``runs`` consecutive visits, but never for good: after
    ``probation_hours`` the board is retried once, so one that wakes up comes
    back on its own. Retirement is a saving, not a deletion.
    """
    now = time.time() if now is None else now
    out = set()
    for key, entry in state.items():
        if not isinstance(entry, dict) or entry.get("zero", 0) < runs:
            continue
        if (now - last_probe(entry)) / 3600.0 >= probation_hours:
            continue
        out.add(key)
    return out
