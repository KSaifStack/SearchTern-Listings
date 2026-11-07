"""Phase-2 enrichment: fetch descriptions only for rows that need one.

Discovery is cheap because board payloads carry titles, not prose. Paying for a
description on all ~29k rows is the expensive half of the run, and most of those
rows already have one or will never be shown. So phase 2 is a plan, not a sweep:
pick the new rows that qualify, fetch a bounded number, and record what the
phase actually cost.

Nothing here runs unless the caller opts in (``SEARCHTERN_ENRICH=1`` in the
pipeline), so turning the switch on is a deliberate, measurable change.
"""

import html
import re

import readme_utils

# `<meta property="og:description" content="...">` and the plain fallback.
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](og:description|description)[\"'][^>]*>",
    re.IGNORECASE,
)
_CONTENT_RE = re.compile(r"content=[\"'](.*?)[\"']", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

QUALIFYING_TYPES = ("internship", "new_grad", "newgrad", "entry_level", "full_time")


def _blank(value):
    return not str(value or "").strip()


def plan(frame, cap=200, types=QUALIFYING_TYPES, new_only=None):
    """Rows worth a phase-2 fetch, newest first, at most ``cap`` of them.

    ``new_only`` is an optional predicate over the row; the pipeline passes
    "index is new this run". Without it every description-less qualifying row
    is a candidate, which is what a first full backfill wants.
    """
    if frame is None or len(frame) == 0:
        return []
    has_desc = frame["description"] if "description" in frame.columns else None
    wanted = []
    for idx, row in frame.iterrows():
        if not str(row.get("link") or "").startswith(("http://", "https://")):
            continue
        if has_desc is not None and not _blank(has_desc.loc[idx]):
            continue
        job_type = str(row.get("job_type") or "").strip().lower()
        if types and job_type and job_type not in types:
            continue
        if new_only is not None and not new_only(row):
            continue
        wanted.append(idx)
    wanted.sort(key=lambda i: str(frame.at[i, "date"] or ""), reverse=True)
    return wanted[: max(0, int(cap))]


def extract_description(page):
    """Pull a description out of an HTML page, or "" when there is none.

    og:description first: it is the one field every ATS job page fills for
    social previews, and unlike the body it is already de-tagged prose.
    """
    text = str(page or "")
    if not text:
        return ""
    for match in _META_RE.finditer(text):
        content = _CONTENT_RE.search(match.group(0))
        if not content:
            continue
        value = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", content.group(1)))).strip()
        if len(value) >= 40:
            return value
    return ""


def apply_plan(frame, indices, fetch=None, workers=4):
    """Fetch and fill descriptions for ``indices``; returns what it cost.

    ``fetch`` takes a URL and returns page text (tests inject a stub). A failed
    fetch leaves the row alone -- a missing description is a gap, a wrong one is
    a lie, and the pipeline synthesizes text for the rest.
    """
    stats = {"planned": len(indices), "fetched": 0, "filled": 0, "failed": 0, "seconds": 0.0}
    if frame is None or len(frame) == 0 or not indices:
        return stats

    import concurrent.futures
    import time

    fetch = fetch or _fetch_page
    started = time.time()
    links = {idx: str(frame.at[idx, "link"]) for idx in indices}

    def _one(item):
        idx, link = item
        try:
            return idx, fetch(link)
        except Exception:
            return idx, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for idx, page in pool.map(_one, links.items()):
            if page is None:
                stats["failed"] += 1
                continue
            stats["fetched"] += 1
            desc = extract_description(page)
            if desc:
                frame.at[idx, "description"] = desc
                stats["filled"] += 1

    stats["seconds"] = round(time.time() - started, 2)
    return stats


def _fetch_page(url, timeout=20):
    response = readme_utils.http_get(url, timeout=timeout, retries=1)
    if response is None:
        return None
    return response.text
