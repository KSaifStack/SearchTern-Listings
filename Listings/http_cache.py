"""HTTP conditional-GET cache: ETag / Last-Modified with body replay.

Why: the ATS probe re-downloads every board payload even when nothing changed.
All three major board APIs send an ``ETag`` (measured 2026-11-07), so
``If-None-Match`` turns an unchanged board into a 304 with no body. The old code
could not use that: a 304 arrived as "no 200" and the row was thrown away, which
is why ``sitemap_sources`` hand-rolled its own store — and why this module stores
the body too, so a 304 replays the last good payload instead of losing a board.

Storage: one gzipped JSON record per URL under ``Listings/cache/http``, keyed by
a hash of the URL. Bodies are capped, so a large artifact cannot fill the disk.
Nothing here raises: a broken cache must never fail a fetch.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import threading

import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "http")
MAX_BODY_BYTES = 8 * 1024 * 1024
_LOCK = threading.Lock()


def _path(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return os.path.join(CACHE_DIR, f"{digest}.json.gz")


def load(url: str) -> dict | None:
    """Return the stored record for ``url`` or None."""
    try:
        with gzip.open(_path(url), "rt", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(record, dict) or record.get("url") != url:
        return None
    return record


def validators(url: str) -> dict:
    """Request headers that make this URL conditional, when we can."""
    record = load(url)
    if not record:
        return {}
    headers = {}
    if record.get("etag"):
        headers["If-None-Match"] = record["etag"]
    if record.get("last_modified"):
        headers["If-Modified-Since"] = record["last_modified"]
    return headers


def store(url: str, response) -> bool:
    """Persist a 200 response's body and validators. Returns stored-any."""
    try:
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if not (etag or last_modified):
            return False
        body = response.content
        if not body or len(body) > MAX_BODY_BYTES:
            return False
        record = {
            "url": url,
            "etag": etag or "",
            "last_modified": last_modified or "",
            "content_type": response.headers.get("Content-Type", ""),
            "body": base64.b64encode(body).decode("ascii"),
        }
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _path(url)
        tmp = f"{path}.{os.getpid()}.tmp"
        with _LOCK:
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 - caching must never fail a fetch
        return False


def replay(url: str):
    """Rebuild a 200 response from the cached body after a 304, or None."""
    record = load(url)
    if not record or not record.get("body"):
        return None
    try:
        body = base64.b64decode(record["body"])
    except (ValueError, TypeError):
        return None
    response = requests.Response()
    response.status_code = 200
    response._content = body  # noqa: SLF001 - the documented way to build one
    response.url = url
    response.encoding = "utf-8"
    response.headers["Content-Type"] = record.get("content_type") or "application/json"
    return response


def stats() -> dict:
    """(files, bytes) of the cache dir, for the run log."""
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return {"files": 0, "bytes": 0}
    total = 0
    for name in names:
        try:
            total += os.path.getsize(os.path.join(CACHE_DIR, name))
        except OSError:
            continue
    return {"files": len(names), "bytes": total}
