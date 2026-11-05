"""Canonical listing schema — one dtype per column, enforced before any write.

PyArrow infers a Parquet column's type from the first non-null value it sees,
so a single mixed column is fatal: one adapter emitting ``"$85,000"`` beside
another's ``85000`` raises ``ArrowInvalid: Could not convert '$85,000' with
type str: tried to convert to double`` and the whole tier cache write fails
(heavy #219 / all #180). Dates have the same failure mode — ``datetime64``
from one adapter and a raw string from another land in one object column.

Every frame is therefore pushed through :func:`canonicalize` before it is
cached or published:

* ``salary_min`` / ``salary_max`` → float64, NaN for missing or malformed.
* ``date`` / ``posted_at`` → str, "" for missing (the pipeline's convention).
* identity/text columns → str (ints and None preserved as text/null), so an
  ``ats_id`` emitted as BIGINT by one source cannot clash with UUID strings.

Unknown columns pass through untouched, after the canonical ones.
"""

import math
import os
import re
import tempfile

import pandas as pd
import pyarrow.parquet as pq

SALARY_COLUMNS = ("salary_min", "salary_max")
DATE_COLUMNS = ("date", "posted_at")
TEXT_COLUMNS = (
    "company", "role", "location", "link", "is_remote",
    "salary_currency", "country_iso", "description",
    "ats_type", "ats_id", "job_type", "source", "observed_at",
    "employment_type", "seniority",
)
# Order used by every cache and feed; extras keep their relative order after.
CANONICAL_COLUMNS = (
    "company", "role", "location", "date", "link", "is_remote",
    "salary_min", "salary_max", "salary_currency", "country_iso",
    "description", "ats_type", "ats_id", "job_type", "source",
    "observed_at",
)

_EMPTY_TEXT = {"", "nan", "nat", "none", "null", "n/a", "na", "-"}
_NUMBER_RE = re.compile(r"-?\d+(?:[\d,]*)(?:\.\d+)?")
_K_SUFFIX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[kK]\b")


def coerce_salary(value):
    """Return a float salary or NaN.

    Accepts ints, floats, Decimals and the shapes adapters actually publish:
    ``"$85,000"``, ``"85,000.50"``, ``"USD 85000"``, ``"85k"``, ``"80000-90000"``
    (lower bound, matching the min-column convention). Booleans, containers,
    blanks and unparseable text become NaN.
    """
    if value is None or isinstance(value, bool):
        return float("nan")
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number > 0 else float("nan")
    try:  # Decimal and numpy scalars
        number = float(value)
    except (TypeError, ValueError):
        pass
    else:
        return number if math.isfinite(number) and number > 0 else float("nan")

    text = str(value).strip()
    if text.lower() in _EMPTY_TEXT:
        return float("nan")
    match = _K_SUFFIX_RE.search(text)
    if match:
        return float(match.group(1)) * 1000
    match = _NUMBER_RE.search(text)
    if not match:
        return float("nan")
    try:
        number = float(match.group(0).replace(",", ""))
    except ValueError:
        return float("nan")
    return number if math.isfinite(number) and number > 0 else float("nan")


def coerce_date(value):
    """Return an ISO-ish string date, "" when missing."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, (pd.Timestamp,)):
        return "" if pd.isna(value) else value.isoformat()
    text = str(value).strip()
    return "" if text.lower() in _EMPTY_TEXT else text


def coerce_text(value):
    """Return text, preserving None/NaN as null rather than a fake string."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if not isinstance(value, str):
        text = str(value).strip()
    else:
        text = value
    return None if text.lower() in ("nan", "nat") else text


def canonicalize(df):
    """Return ``df`` with canonical dtypes; never mutates the input."""
    if df is None:
        return pd.DataFrame(columns=list(CANONICAL_COLUMNS))
    out = df.copy()

    for column in SALARY_COLUMNS:
        values = out[column] if column in out.columns else [None] * len(out)
        out[column] = pd.Series(
            [coerce_salary(v) for v in values], index=out.index, dtype="float64"
        )
    for column in DATE_COLUMNS:
        if column in out.columns:
            out[column] = out[column].map(coerce_date).astype(str)
    for column in TEXT_COLUMNS:
        if column in out.columns:
            out[column] = out[column].map(coerce_text).astype(object)

    ordered = [c for c in CANONICAL_COLUMNS if c in out.columns]
    ordered += [c for c in out.columns if c not in CANONICAL_COLUMNS]
    return out[ordered]


def write_parquet_atomic(df, path, canonical=True):
    """Write ``df`` to ``path`` atomically, replacing only a verified file.

    The frame is written to a sibling temp file and read back (schema only)
    before ``os.replace``; an exception anywhere leaves the previous good file
    untouched, so a bad run can never corrupt or truncate a valid cache.
    Returns the row count written, or None when the write was abandoned.
    """
    frame = canonicalize(df) if canonical else df
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=directory, suffix=".parquet")
    os.close(handle)
    try:
        frame.to_parquet(temp_path, index=False)
        pq.read_schema(temp_path)  # throws on a partial/half-written file
        os.replace(temp_path, path)
    except Exception as exc:  # noqa: BLE001 - any failure must keep the old file
        print(f"  Cache write abandoned for {path}: {type(exc).__name__}: {exc}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return len(frame)
