"""Self-check for source_health.py. Uses a temp cache dir, no network.

Run: python test_source_health.py
"""

import json
import os
import tempfile

import source_health as H


def _snap(*entries):
    return {"sources": [
        {"source": n, "discovered": d, "valid": v, "seconds": 1.0,
         "status": s}
        for n, d, v, s in entries]}


def test_failure_after_two_bad_runs():
    with tempfile.TemporaryDirectory() as tmp:
        H._PATH, orig = os.path.join(tmp, "h.jsonl"), H._PATH
        try:
            H.append(_snap(("s1", 10, 5, "ok")))
            H.append(_snap(("s1", 0, 0, "empty")))
            assert H.report()["failures"] == [], H.report()
            H.append(_snap(("s1", 0, 0, "rate_limited")))
            fails = H.report()["failures"]
            assert len(fails) == 1 and fails[0]["source"] == "s1", fails
        finally:
            H._PATH = orig


def test_recovery_clears_alert():
    with tempfile.TemporaryDirectory() as tmp:
        H._PATH, orig = os.path.join(tmp, "h.jsonl"), H._PATH
        try:
            H.append(_snap(("s1", 0, 0, "empty")))
            H.append(_snap(("s1", 0, 0, "empty")))
            assert len(H.report()["failures"]) == 1
            H.append(_snap(("s1", 10, 9, "ok")))
            assert H.report()["failures"] == [], H.report()
        finally:
            H._PATH = orig


def test_ranking_and_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        H._PATH, orig = os.path.join(tmp, "h.jsonl"), H._PATH
        try:
            H.append(_snap(("a", 10, 10, "ok"), ("b", 5, 5, "ok")),
                     {"a": 7, "b": 1})
            rep = H.report()
            assert rep["best"][0] == {"source": "a", "new_7d": 7, "rows_7d": 10,
                                      "discovered_7d": 10, "runs_7d": 1}, rep["best"]
            assert rep["quiet"] == [], rep
        finally:
            H._PATH = orig


def test_never_raises_without_log():
    with tempfile.TemporaryDirectory() as tmp:
        H._PATH, orig = os.path.join(tmp, "missing.jsonl"), H._PATH
        try:
            assert H.report() == {"failures": [], "quiet": [], "best": []}
        finally:
            H._PATH = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
    print("\n  test_source_health: all checks passed")
