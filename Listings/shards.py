"""Compressed feed shards + a manifest, so clients stop pulling one 65 MB file.

``pages/searchtern.json`` is 65 MB and is rewritten whole on every run: a client
cannot fetch "what changed", and a crawler parses the entire feed to read one
field. Published shards are newline-delimited gzip (streamable, cheap to diff)
with a ``manifest.json`` that carries counts, timestamps, per-field coverage and
the exact byte size of every part, so a consumer can pick what it needs.

Deterministic: same rows in, same shard bytes out (sorted keys, fixed order).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time

DEFAULT_SHARD_ROWS = 20000
COVERAGE_FIELDS = (
    "company", "role", "location", "date", "link", "description",
    "salary_min", "seniority", "employment_type", "ats_id", "country_iso",
)


def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    if hasattr(value, "item"):  # numpy scalar
        return _jsonable(value.item())
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def rows_from_frame(frame) -> list[dict]:
    """Frame -> list of JSON-safe row dicts."""
    columns = [str(c) for c in frame.columns]
    out = []
    for record in frame.itertuples(index=False, name=None):
        out.append({name: _jsonable(value) for name, value in zip(columns, record, strict=True)})
    return out


def coverage(rows: list[dict]) -> list[dict]:
    total = len(rows) or 1
    out = []
    for field in COVERAGE_FIELDS:
        filled = sum(1 for row in rows if str(row.get(field) or "").strip())
        out.append({"field": field, "filled": filled,
                    "pct": round(100.0 * filled / total, 2)})
    return out


def publish(frame_or_rows, pages_dir, name="listings",
            shard_rows=DEFAULT_SHARD_ROWS) -> dict | None:
    """Write ``{name}-NNNNN.json.gz`` parts and ``manifest.json``.

    Stale parts beyond the new count are removed, so a shrinking feed cannot
    leave orphan shards behind. Returns the manifest, or None on failure.
    """
    try:
        rows = (rows_from_frame(frame_or_rows)
                if hasattr(frame_or_rows, "columns") else list(frame_or_rows))
        shard_dir = os.path.join(pages_dir, "shards")
        os.makedirs(shard_dir, exist_ok=True)
        keep = []
        shards = []
        for index in range(0, len(rows), shard_rows):
            chunk = rows[index:index + shard_rows]
            relative = f"shards/{name}-{index // shard_rows:05d}.json.gz"
            path = os.path.join(pages_dir, relative)
            payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"))
                                for row in chunk) + "\n"
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as handle:
                handle.write(payload)
            os.replace(tmp, path)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
            shards.append({"path": relative, "rows": len(chunk),
                           "bytes": os.path.getsize(path), "sha256_16": digest})
            keep.append(os.path.basename(path))
        for existing in os.listdir(shard_dir):
            if existing.startswith(f"{name}-") and existing not in keep:
                os.unlink(os.path.join(shard_dir, existing))
        manifest = {
            "name": name,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rows": len(rows),
            "shard_rows": shard_rows,
            "shards": shards,
            "bytes": sum(s["bytes"] for s in shards),
            "field_coverage": coverage(rows),
        }
        with open(os.path.join(pages_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
        return manifest
    except Exception as exc:  # noqa: BLE001 - publication must not fake success
        print(f"  Shards skipped: {type(exc).__name__}: {exc}")
        return None


def read_shard(pages_dir, relative_path) -> list[dict]:
    """Read one shard back (used by tests and by consumers in the same repo)."""
    with gzip.open(os.path.join(pages_dir, relative_path), "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
