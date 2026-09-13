import json
import re
import time

import pandas as pd

from readme_utils import http_get

# EchoJobs: software-engineering board scraped from companies' own career pages.
# Newest-first block in sitemap-jobs/1.xml (10k URLs, <lastmod> as date). Jobs are
# client-rendered, so we filter candidate slugs from the URL itself and fetch the
# matching server-rendered detail pages, which carry schema.org JobPosting JSON-LD.
SITEMAP_URL = "https://echojobs.io/sitemap-jobs/1.xml"
# ponytail: newest block only + cap detail fetches. A full sweep is 25 files x 10k
# URLs; raise if a wider window is ever wanted.
MAX_DETAIL_FETCHES = 100
DETAIL_DELAY_SECS = 1.0

# EchoJobs 429s default requests UA; a browser UA works (verified by probe).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
}

INTERN_RE = re.compile(
    r"\bintern(?:ship)?\b|co-op|coop|undergraduate|undergrad|student|reu|apprentice"
    r"|trainee|fellowship|praktikum|werkstudent|new[\s-]grad(?:uate)?\b"
    r"|entry[\s-]level|early\s+career|university[\s-]grad(?:uate)?\b|junior\b"
    r"|rotational|campus\b",
    re.I,
)

_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)
_LOC_RE = re.compile(r"<strong>Location:</strong>\s*(?:</?p>)?\s*([^<]+?)\s*(?:<|$)", re.I)
_REMOTE_RE = re.compile(r"<strong>Remote Type:</strong>\s*(?:</?p>)?\s*([^<]+?)\s*(?:<|$)", re.I)


def _strip_html(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def _parse_jobposting(html):
    for m in _JSONLD_RE.findall(html):
        try:
            data = json.loads(m)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "JobPosting":
            return data
    return None


def _parse_detail(html):
    data = _parse_jobposting(html)
    if not data:
        return None
    org = data.get("hiringOrganization") or {}
    desc = data.get("description") or ""
    loc_m = _LOC_RE.search(desc)
    remote_m = _REMOTE_RE.search(desc)
    remote = bool(remote_m and "remote" in remote_m.group(1).lower())
    return {
        "company": _strip_html(org.get("name") or ""),
        "role": _strip_html(data.get("title") or ""),
        "date": data.get("datePosted", ""),
        "description": desc,
        "location_raw": (loc_m.group(1).strip() if loc_m else ""),
        "is_remote": str(remote).lower(),
    }


def _extract_sitemap(xml_text):
    """Return list of (job_url, lastmod) for /job/ URLs, strict newest-first."""
    urls = re.findall(r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", xml_text)
    return [(u, t.strip()) for u, t in urls if "/job/" in u]


def _slug_candidates(urls):
    seen = set()
    out = []
    for url, lastmod in urls:
        slug = url.split("/job/")[-1]
        if slug in seen:
            continue
        seen.add(slug)
        slug_words = re.sub(r"-[a-z0-9]{5}$", "", slug).replace("-", " ")
        if INTERN_RE.search(slug_words):
            out.append((url, lastmod))
    return out


def _normalize(details, infer_country):
    rows = []
    for item in details:
        if not item:
            continue
        role, company = item["role"], item["company"]
        if not role or not company:
            continue
        loc = item["location_raw"] or "Remote"
        rows.append({
            "company": company,
            "role": role,
            "location": loc,
            "date": item["date"],
            "link": item["link"],
            "is_remote": item["is_remote"],
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "country_iso": infer_country(loc) if infer_country else "",
        })
    return rows


def fetch_and_parse(infer_country=None):
    resp = http_get(SITEMAP_URL, timeout=30, retries=3, headers=_HEADERS)
    if resp is None or resp.status_code != 200:
        print(
            f"  x EchoJobs sitemap: "
            f"{resp.status_code if resp is not None else 'unreachable'}"
        )
        return pd.DataFrame(), []
    sitemap = _extract_sitemap(resp.text)
    candidates = _slug_candidates(sitemap)
    print(f"  EchoJobs sitemap: {len(sitemap):,} urls, "
          f"{len(candidates):,} intern/new-grad candidates")

    candidates = candidates[:MAX_DETAIL_FETCHES]
    details = []
    for i, (url, _lastmod) in enumerate(candidates):
        d = http_get(url, timeout=20, retries=2, headers=_HEADERS)
        if d is not None and d.status_code == 200:
            parsed = _parse_detail(d.text)
            if parsed:
                parsed["link"] = url
                details.append(parsed)
        if i < len(candidates) - 1:
            time.sleep(DETAIL_DELAY_SECS)

    df = pd.DataFrame(_normalize(details, infer_country))
    if not df.empty:
        df = df.drop_duplicates(subset=["company", "role", "location"], keep="first")
    print(f"  EchoJobs detail pages parsed: {len(details):,} -> {len(df):,} rows")
    return df, [("EchoJobs (career pages)", len(details))]


if __name__ == "__main__":
    df, stats = fetch_and_parse()
    print(stats)
    assert not df.empty, "no jobs parsed — parser regression"
    cols = {"company", "role", "location", "date", "link",
            "is_remote", "salary_min", "salary_max", "salary_currency", "country_iso"}
    assert cols <= set(df.columns), f"missing columns: {cols - set(df.columns)}"
    assert df["link"].str.startswith("https://echojobs.io/job/").all()
    print(f"self-check ok: {len(df):,} rows")