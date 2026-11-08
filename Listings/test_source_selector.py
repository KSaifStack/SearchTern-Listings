"""Source selection: measured failures and measured zero yield are skipped, nothing else."""

import source_selector


def _health(failures=(), quiet=(), best=()):
    return {
        "failures": [{"source": name, "status": "error"} for name in failures],
        "quiet": [{"source": name, "hours_since_last": 72.0} for name in quiet],
        "best": list(best),
    }


def test_a_source_with_no_history_is_polled():
    out = source_selector.select(["new"], _health())
    assert out["poll"] == ["new"]
    assert out["defer"] == []


def test_failing_and_empty_sources_are_deferred():
    health = _health(
        failures=["bad"],
        best=[{"source": "dead", "rows_7d": 0, "runs_7d": 6}],
    )
    out = source_selector.select(["bad", "dead"], health)
    reasons = {entry["source"]: entry["reason"] for entry in out["defer"]}
    assert reasons == {"bad": "failing", "dead": "empty"}
    assert out["poll"] == []


def test_a_valuable_quiet_source_is_still_polled():
    health = _health(
        quiet=["good"],
        best=[
            {"source": "good", "rows_7d": 400, "runs_7d": 6},
            {"source": "meh", "rows_7d": 20, "runs_7d": 6},
        ],
    )
    out = source_selector.select(["good", "meh"], health)
    assert out["poll"] == ["good", "meh"]
    assert out["expand"] == ["good"]


def test_a_quiet_source_without_value_is_deferred():
    health = _health(quiet=["silent"], best=[{"source": "silent", "rows_7d": 0, "runs_7d": 2}])
    out = source_selector.select(["silent"], health)
    assert out["defer"] == [{"source": "silent", "reason": "quiet"}]
