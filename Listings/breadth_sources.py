import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import pandas as pd

from readme_utils import http_get

STD_COLS = [
    "company", "role", "location", "date", "link", "is_remote",
    "salary_min", "salary_max", "salary_currency", "country_iso",
]

_UA = {"User-Agent": "SearchTern-Listings/1.0 (+https://github.com/KSaifStack/SearchTern-Listings)"}

JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs?count=50"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs?limit=100"
REMOTEOK_URL = "https://remoteok.com/api"
HIMALAYAS_URL = "https://himalayas.app/jobs/api?limit=100"
WORKINGNOMADS_URL = "https://www.workingnomads.com/api/exposed_jobs/"
WWR_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
HN_THREAD_URL = ("https://hn.algolia.com/api/v1/search_by_date"
                 "?query=Ask%20HN%3A%20Who%20is%20hiring&tags=story&hitsPerPage=5")
HN_ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
MUSE_URL = "https://www.themuse.com/api/public/jobs"
MUSE_PAGES = 50
_HN_ATS_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:greenhouse\.io|lever\.co|ashbyhq\.com|"
    r"myworkdayjobs\.com|smartrecruiters\.com|breezy\.hr|recruitee\.com|"
    r"pinpointhq\.com|teamtailor\.com|workable\.com|rippling\.com)",
    re.I,
)


def _get_json(url):
    resp = http_get(url, timeout=25, retries=2, headers=_UA)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return int(n) if n > 0 else None


def _from_unix(value):
    try:
        return str(pd.Timestamp(float(value), unit="s", tz="UTC"))
    except (TypeError, ValueError):
        return ""


def _from_date(value):
    if not value:
        return ""
    try:
        return str(pd.Timestamp(value))
    except (TypeError, ValueError):
        return ""


def _first_loc(value):
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    text = str(value or "").strip().strip("[]")
    return text.split(",")[0].strip().strip("'\"") if text else ""


def _row(company, role, location, date, link, is_remote,
         salary_min=None, salary_max=None, salary_currency=None):
    return {
        "company": (company or "").strip(),
        "role": (role or "").strip(),
        "location": (location or "").strip(),
        "date": date or "",
        "link": (link or "").strip(),
        "is_remote": "true" if is_remote else "false",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "country_iso": "",
    }


def _fetch_jobicy():
    data = _get_json(JOBICY_URL) or {}
    return [
        _row(j.get("companyName"), j.get("jobTitle"), j.get("jobGeo"),
             j.get("pubDate"), j.get("url"), True)
        for j in data.get("jobs", [])
    ]


def _fetch_arbeitnow():
    data = _get_json(ARBEITNOW_URL) or {}
    return [
        _row(j.get("company_name"), j.get("title"), j.get("location"),
             _from_unix(j.get("created_at")), j.get("url"),
             str(j.get("remote", "")).strip().lower() == "true")
        for j in data.get("data", [])
    ]


def _fetch_remotive():
    data = _get_json(REMOTIVE_URL) or {}
    return [
        _row(j.get("company_name"), j.get("title"),
             j.get("candidate_required_location"), j.get("publication_date"),
             j.get("url"), True)
        for j in data.get("jobs", [])
    ]


def _fetch_remoteok():
    data = _get_json(REMOTEOK_URL)
    if not isinstance(data, list):
        return []
    rows = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:
            continue
        rows.append(_row(
            j.get("company"), j.get("position"), j.get("location") or "Remote",
            j.get("date"), j.get("url") or j.get("apply_url"), True,
            _num(j.get("salary_min")), _num(j.get("salary_max")),
        ))
    return rows


def _fetch_himalayas():
    data = _get_json(HIMALAYAS_URL) or {}
    return [
        _row(j.get("companyName"), j.get("title"),
             _first_loc(j.get("locationRestrictions")), _from_unix(j.get("pubDate")),
             j.get("applicationLink") or j.get("guid"), True,
             _num(j.get("minSalary")), _num(j.get("maxSalary")), j.get("currency") or None)
        for j in data.get("jobs", [])
    ]


def _fetch_workingnomads():
    data = _get_json(WORKINGNOMADS_URL)
    if not isinstance(data, list):
        return []
    return [
        _row(j.get("company_name"), j.get("title"), j.get("location"),
             j.get("pub_date"), j.get("url"), True)
        for j in data if isinstance(j, dict)
    ]


def _fetch_wwr():
    resp = http_get(WWR_URL, timeout=25, retries=2, headers=_UA)
    if resp is None or resp.status_code != 200:
        return []
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return []
    rows = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        company, role = title.split(": ", 1) if ": " in title else ("", title)
        rows.append(_row(
            company, role, item.findtext("region") or "Remote",
            _from_date(item.findtext("pubDate")), item.findtext("link"), True,
        ))
    return rows


def _fetch_hn_whoishiring():
    data = _get_json(HN_THREAD_URL) or {}
    hits = data.get("hits") or []
    if not hits:
        return []
    thread = next((h for h in hits if str(h.get("title", "")).startswith("Ask HN: Who is hiring?")), hits[0])
    item = _get_json(HN_ITEM_URL.format(id=thread["objectID"])) or {}
    thread_date = str(thread.get("created_at", ""))[:10]
    rows = []
    for comment in item.get("children") or []:
        text = html.unescape(re.sub(r"<[^>]+>", " ", comment.get("text") or "")).strip()
        match = _HN_ATS_RE.search(text)
        if not match:
            continue
        parts = [p.strip() for p in re.split(r"\s*[|]\s*|\s+[—]\s+", text) if p.strip()]
        parts = [p for p in parts if not re.fullmatch(r"https?://\S+", p)]
        if len(parts) < 2:
            continue
        rows.append(_row(
            parts[0][:120], parts[1][:200], parts[2][:120] if len(parts) > 2 else "Remote",
            thread_date, match.group(0), "remote" in text.lower(),
        ))
    return rows


def _fetch_muse():
    rows = []
    for page in range(1, MUSE_PAGES + 1):
        url = f"{MUSE_URL}?{urlencode({'page': page, 'location': 'United States'})}"
        data = _get_json(url) or {}
        results = data.get("results") or []
        if not results:
            break
        for j in results:
            company = j.get("company")
            if isinstance(company, dict):
                company = company.get("name") or ""
            locations = j.get("locations") or []
            loc = ""
            if locations and isinstance(locations[0], dict):
                loc = locations[0].get("name") or ""
            refs = j.get("refs") if isinstance(j.get("refs"), dict) else {}
            rows.append(_row(
                company, j.get("name"), loc or "United States",
                j.get("publication_date"), refs.get("landing_page"),
                "remote" in loc.lower(),
            ))
    return rows


_FEEDS = [
    ("Jobicy", _fetch_jobicy),
    ("Arbeitnow", _fetch_arbeitnow),
    ("Remotive", _fetch_remotive),
    ("RemoteOK", _fetch_remoteok),
    ("Himalayas", _fetch_himalayas),
    ("Working Nomads", _fetch_workingnomads),
    ("WeWorkRemotely", _fetch_wwr),
    ("HN who-is-hiring", _fetch_hn_whoishiring),
    ("TheMuse", _fetch_muse),
]


def fetch_and_parse(infer_country=None):
    rows = []
    stats = []
    for name, fetch in _FEEDS:
        try:
            got = fetch()
        except Exception as e:
            print(f"  x {name}: {e}")
            got = []
        print(f"  {name}: {len(got):,} rows")
        stats.append((name, len(got)))
        rows.extend(got)

    df = pd.DataFrame(rows, columns=STD_COLS)
    if not df.empty and infer_country is not None:
        df["country_iso"] = df["location"].map(lambda v: infer_country(str(v or "")))
    return df, stats


if __name__ == "__main__":
    df, stats = fetch_and_parse()
    print(stats)
    assert not df.empty, "no jobs parsed — feed regression"
    assert set(STD_COLS) <= set(df.columns), "missing standard columns"
    assert df["link"].str.startswith("http").all(), "malformed links"
    print(f"self-check ok: {len(df):,} rows from {sum(1 for _n, c in stats if c)} feeds")
