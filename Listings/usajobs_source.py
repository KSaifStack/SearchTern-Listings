"""USAJobs adapter.

Search API v2: ``GET https://data.usajobs.gov/api/search`` with a
``Host: data.usajobs.gov``, an ``Authorization-Key`` (free, per-email) and a
``User-Agent`` set to the same email. No key configured means the source is
skipped, never a crash — the same convention the other keyed adapters use.

Only ``parse`` is used by tests; ``fetch`` needs the network and a key.
"""
from __future__ import annotations

import os

from readme_utils import http_get

API = "https://data.usajobs.gov/api/search"
KEY_ENV = "USAJOBS_API_KEY"
EMAIL_ENV = "USAJOBS_EMAIL"
DEFAULT_KEYWORDS = ("intern", "student trainee", "pathways")


def configured() -> bool:
    return bool(os.environ.get(KEY_ENV) and os.environ.get(EMAIL_ENV))


def _location(descriptor) -> str:
    if not isinstance(descriptor, dict):
        return "N/A"
    parts = []
    for item in descriptor.get("Locations") or []:
        name = item.get("LocationName")
        if name and name not in parts:
            parts.append(name)
    return ", ".join(parts) if parts else "N/A"


def parse(payload) -> list[dict]:
    """Turn a USAJobs search response into pipeline-shaped rows."""
    rows = []
    for item in ((payload or {}).get("SearchResult") or {}).get("SearchResultItems") or []:
        job = item.get("MatchedObjectDescriptor") or {}
        position_id = str(job.get("PositionID") or item.get("MatchedObjectId") or "").strip()
        url = str(job.get("PositionURI") or "").strip()
        title = str(job.get("PositionTitle") or "").strip()
        if not (position_id and title):
            continue
        org = str(job.get("OrganizationName") or job.get("DepartmentName") or "").strip()
        pay = job.get("PositionRemuneration") or [{}]
        first = pay[0] if isinstance(pay, list) and pay else {}
        rows.append({
            "company": org or "U.S. Government",
            "role": title,
            "location": _location(job),
            "link": url,
            "date": str(job.get("PublicationStartDate") or ""),
            "ats_type": "usajobs",
            "ats_id": position_id,
            "source": "usajobs",
            "description": str(job.get("UserArea", {}).get("Details", {}).get("JobSummary")
                               or job.get("QualificationSummary") or "")[:4000],
            "salary_min": first.get("MinimumRange"),
            "salary_max": first.get("MaximumRange"),
            "salary_currency": "USD",
        })
    return rows


def fetch(keyword="intern", results_per_page=500, page=1, timeout=25):
    """Fetch one page of results. Returns rows, or [] when unconfigured."""
    if not configured():
        return []
    params = (f"?Keyword={keyword.replace(' ', '%20')}"
              f"&ResultsPerPage={int(results_per_page)}&Page={int(page)}")
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": os.environ[EMAIL_ENV],
        "Authorization-Key": os.environ[KEY_ENV],
    }
    response = http_get(API + params, timeout=timeout, headers=headers)
    if response is None or response.status_code != 200:
        return []
    try:
        return parse(response.json())
    except ValueError:
        return []
