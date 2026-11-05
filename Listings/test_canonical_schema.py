"""Canonical schema regression fixtures.

Guards the heavy #219 / all #180 failure: pyarrow takes a column's type from
its first non-null value, so one adapter emitting "$85,000" beside another's
85000 raised ArrowInvalid and killed the whole tier cache write.

Run: python test_canonical_schema.py  (also collected by pytest)
"""

import glob
import math
import os
import tempfile

import pandas as pd

import canonical


def _frame(rows):
    return pd.DataFrame(rows)


def test_salary_numeric_shapes():
    assert canonical.coerce_salary(85000) == 85000.0
    assert canonical.coerce_salary(85000.5) == 85000.5
    assert canonical.coerce_salary("85000") == 85000.0
    assert canonical.coerce_salary("85,000.50") == 85000.5
    assert canonical.coerce_salary("$85,000") == 85000.0
    assert canonical.coerce_salary("  USD 120000 ") == 120000.0
    assert canonical.coerce_salary("85k") == 85000.0
    assert canonical.coerce_salary("80000-90000") == 80000.0  # lower bound


def test_salary_missing_and_malformed():
    for blank in (None, "", "  ", "n/a", "-", "NaN", float("nan")):
        assert math.isnan(canonical.coerce_salary(blank)), blank
    for junk in ("negotiable", "DOE", [], {}, True, False, object()):
        assert math.isnan(canonical.coerce_salary(junk)), junk
    # zero/negative never mean a real salary
    assert math.isnan(canonical.coerce_salary(0))
    assert math.isnan(canonical.coerce_salary(-5))


def test_mixed_source_salary_column_writes_parquet():
    """The exact production mix: ints, floats, strings, blanks, nulls."""
    frame = _frame([
        {"company": "A", "salary_min": 85000, "salary_max": 95000},
        {"company": "B", "salary_min": 85000.5, "salary_max": "$95,000"},
        {"company": "C", "salary_min": "85000", "salary_max": ""},
        {"company": "D", "salary_min": None, "salary_max": float("nan")},
        {"company": "E", "salary_min": "DOE", "salary_max": "85k"},
    ])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tier_heavy.parquet")
        # The unconverted frame must genuinely fail — otherwise this fixture
        # would pass even if canonicalize stopped doing anything.
        try:
            frame.to_parquet(path, index=False)
            raise AssertionError("mixed salary column unexpectedly serialized")
        except Exception as exc:
            assert "salary" in str(exc).lower() or "convert" in str(exc).lower()

        assert canonical.write_parquet_atomic(frame, path) == 5
        back = pd.read_parquet(path)

    assert str(back["salary_min"].dtype) == "float64"
    assert str(back["salary_max"].dtype) == "float64"
    assert back["salary_min"].tolist()[:3] == [85000.0, 85000.5, 85000.0]
    assert math.isnan(back["salary_min"].iloc[4])        # "DOE"
    assert back["salary_max"].iloc[4] == 85000.0         # "85k"
    assert math.isnan(back["salary_max"].iloc[2])        # "" stays missing


def test_date_column_mixed_types():
    frame = _frame([
        {"company": "A", "date": pd.Timestamp("2026-10-01T12:00:00")},
        {"company": "B", "date": "2026-10-02"},
        {"company": "C", "date": None},
        {"company": "D", "date": float("nan")},
        {"company": "E", "date": ""},
    ])
    out = canonical.canonicalize(frame)
    assert out["date"].tolist() == ["2026-10-01T12:00:00", "2026-10-02", "", "", ""]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.parquet")
        assert canonical.write_parquet_atomic(frame, path) == 5


def test_text_columns_never_mix_types():
    frame = _frame([{"company": "A", "ats_id": 12345},
                    {"company": "B", "ats_id": "0dfbe31e-89ef"}])
    out = canonical.canonicalize(frame)
    assert out["ats_id"].tolist() == ["12345", "0dfbe31e-89ef"]


def test_missing_salary_columns_are_added():
    out = canonical.canonicalize(_frame([{"company": "A"}]))
    assert str(out["salary_min"].dtype) == "float64"
    assert math.isnan(out["salary_min"].iloc[0])


def test_column_order_and_idempotence():
    frame = _frame([{"extra": 1, "salary_min": "$100,000", "company": "A",
                     "date": "2026-10-01"}])
    once = canonical.canonicalize(frame)
    twice = canonical.canonicalize(once)
    # canonical columns first, in schema order; unknown columns appended last
    assert list(once.columns) == [
        "company", "date", "salary_min", "salary_max", "extra"
    ]
    assert once.equals(twice)
    assert frame["salary_min"].iloc[0] == "$100,000", "input must not mutate"


def test_failed_write_preserves_previous_cache():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tier_all.parquet")
        good = _frame([{"company": "Good", "salary_min": 1.0}])
        assert canonical.write_parquet_atomic(good, path) == 1
        before = pd.read_parquet(path)

        # canonical=False deliberately bypasses the schema guard and hands
        # pyarrow a column it cannot serialize.
        broken = _frame([{"company": "Bad", "salary_min": 1},
                         {"company": "Bad", "salary_min": "$1"}])
        assert canonical.write_parquet_atomic(broken, path, canonical=False) is None

        after = pd.read_parquet(path)
        assert before.equals(after), "previous good cache must survive"
        assert glob.glob(os.path.join(tmp, "*.parquet")) == [path], "temp left behind"


def test_empty_and_none_frames():
    assert canonical.canonicalize(None).empty
    out = canonical.canonicalize(pd.DataFrame())
    assert out.empty and "salary_min" in out.columns
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "empty.parquet")
        canonical.write_parquet_atomic(pd.DataFrame(), path)
        assert len(pd.read_parquet(path)) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  {name} ok")
    print("test_canonical_schema: all checks passed")
