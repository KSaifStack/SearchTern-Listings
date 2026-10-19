"""Grow the ATS probe list from the employer links we already ingest.

The heavy probe used to rely only on a third-party CSV of companies. But the
links we pull from jobhive / markdown / sitemap point straight at employer
boards, so parsing slugs out of them yields a self-growing probe list. State is
committed in cache/discovered_boards.json, so it accumulates across CI runs.
"""

import json
import os
import re
import time

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_STATE_PATH = os.path.join(_CACHE_DIR, "discovered_boards.json")

# Must match what _fetch_company_jobs can probe: ATS_ENDPOINTS keys plus Workday.
PROBEABLE = {"Greenhouse", "Lever", "Ashby", "SmartRecruiters", "Workday"}

MAX_DISCOVERED = 2000

_GREENHOUSE_HOSTS = {
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "boards.eu.greenhouse.io",
}
_LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
_WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")
_WORKDAY_LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")
# Path segments that are never a board slug (embed widgets, apply paths, ...).
_NON_SLUG = {"jobs", "job", "embed", "j", "apply", "application", "cxs", "wday"}


def _segments(path):
    return [p for p in path.split("/") if p]


def parse_link(link):
    """Return (ats_type, slug) for an employer-ATS URL, else None.

    Workday's slug is the CXS path ``host/wday/cxs/tenant/site`` that
    _fetch_company_jobs expects, not just the board name.
    """
    if not isinstance(link, str):
        return None
    m = re.match(r"^https?://([^/]+)(/.*)?$", link.strip())
    if not m:
        return None
    host = m.group(1).lower()
    parts = _segments(m.group(2) or "")

    if host in _GREENHOUSE_HOSTS:
        if parts and parts[0] not in _NON_SLUG:
            return ("Greenhouse", parts[0].lower())
        return None
    if host in _LEVER_HOSTS:
        if parts:
            return ("Lever", parts[0].lower())
        return None
    if host == "jobs.ashbyhq.com":
        if parts:
            return ("Ashby", parts[0].lower())
        return None
    if host == "jobs.smartrecruiters.com" or host.endswith(".smartrecruiters.com"):
        if parts:
            return ("SmartRecruiters", parts[0].lower())
        return None

    wd = _WORKDAY_HOST_RE.match(host)
    if wd and parts:
        site = parts[0]
        if _WORKDAY_LOCALE_RE.match(site.lower()) and len(parts) > 1:
            site = parts[1]
        if site.lower() in _NON_SLUG:
            return None
        tenant = wd.group("tenant")
        return ("Workday", f"{host}/wday/cxs/{tenant}/{site}")
    return None


def _load():
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, _STATE_PATH)


def _key(ats, slug):
    return f"{ats}|{slug}"


def update_state(rows, now=None):
    """Fold a frame of company/link rows into the discovered-board state.

    Each board is counted once per call (not once per posting), so seen_count
    tracks how many runs it has shown up in. Returns (n_new, n_total).
    """
    if rows is None or len(rows) == 0:
        return (0, 0)
    now = time.time() if now is None else now

    found = {}
    for company, link in zip(rows.get("company", []), rows.get("link", [])):
        parsed = parse_link(link)
        if parsed and parsed[0] in PROBEABLE:
            name = str(company).strip() if company is not None else ""
            found[parsed] = name or parsed[1]

    state = _load()
    new = 0
    for (ats, slug), company in found.items():
        k = _key(ats, slug)
        rec = state.get(k)
        if rec is None:
            state[k] = {"company": company, "ats": ats, "slug": slug,
                        "first_seen": now, "last_seen": now, "seen_count": 1}
            new += 1
        else:
            rec["last_seen"] = now
            rec["seen_count"] = rec.get("seen_count", 0) + 1
            if company:
                rec["company"] = company

    if len(state) > MAX_DISCOVERED:
        ranked = sorted(
            state.items(),
            key=lambda kv: (kv[1].get("seen_count", 0), kv[1].get("last_seen", 0)),
            reverse=True,
        )
        state = dict(ranked[:MAX_DISCOVERED])

    _save(state)
    return (new, len(state))


def merge_into_probe(probe_list):
    """Append discovered boards the CSV list does not already cover.

    Deduped on (ats, slug) so the same board is never probed twice under two
    company names; the caller's own entries keep priority.
    """
    state = _load()
    if not state:
        return probe_list
    seen = {(ats, slug) for _name, ats, slug in probe_list}
    extra = []
    for rec in sorted(state.values(),
                      key=lambda r: (r.get("seen_count", 0), r.get("last_seen", 0)),
                      reverse=True):
        ats, slug = rec.get("ats"), rec.get("slug")
        if not ats or not slug or ats not in PROBEABLE or (ats, slug) in seen:
            continue
        seen.add((ats, slug))
        extra.append((rec.get("company") or slug, ats, slug))
    return probe_list + extra
