"""Per-run probe budgeting: oldest-first rotation under a hard cap.

The ATS probe used to submit every board whose cooldown had expired in a single
pass, so a run's wall time was whatever the registry happened to cost that hour
(measured 2026-11-07: 719 s, 73,477 rows discovered, 0 net-new). Ordering by
last-probe time and capping the pass bounds the run *without* starving a board:
a deferred board keeps its old timestamp, so it sorts first next run.

Pure functions, no network, no clock — the caller passes the state it already
loads from ``ats_probe_state.json``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any


def _timestamp(value: Any) -> float:
    """Last-probe time from either a float (board entry) or a dict (Workday)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dict):
        at = value.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            return float(at)
    return 0.0


def _default_key(board: Sequence[Any]) -> str:
    return "|".join(str(part) for part in board[1:3])


def oldest_first(
    boards: Iterable[Sequence[Any]],
    last_probe: dict[str, Any],
    cap: int | None = None,
    key: Callable[[Sequence[Any]], str] | None = None,
) -> tuple[list[Sequence[Any]], list[Sequence[Any]]]:
    """Split ``boards`` into (probe now, defer to the next run).

    Never-probed boards have timestamp 0 and therefore sort first. A cap of
    ``None``, ``0`` or a negative number means no limit.
    """
    key = key or _default_key
    ordered = sorted(boards, key=lambda board: _timestamp(last_probe.get(key(board))))
    if cap is None or cap <= 0 or len(ordered) <= cap:
        return ordered, []
    return ordered[:cap], ordered[cap:]
