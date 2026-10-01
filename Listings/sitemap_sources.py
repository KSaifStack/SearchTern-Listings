"""Sitemap-driven job discovery for ATS vendors that publish no public API.

Career sitemaps enumerate every live job URL. Vendors that gate their search
behind a JS-only SPA still advertise one, because crawlers need it for SEO.
This module walks the sitemap, pre-filters URLs by slug, then extracts
schema.org JobPosting JSON-LD from the pages robots.txt permits.

robots.txt is a hard gate. Hosts advertising ``Disallow: /`` (Eightfold, for
example) are skipped entirely rather than fetched at a slower rate.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.parse

import pandas as pd
import requests

from protego import Protego
from readme_utils import http_get
from requests_cache import CachedSession

UA_STRING = (
    "SearchTern-Listings/1.0 (+https://github.com/KSaifStack/SearchTern-Listings)"
)
UA = {"User-Agent": UA_STRING}
ROBOT_AGENT = UA_STRING.split("/")[0].lower()

PER_HOST_DELAY_SECS = 1.5
HOST_WORKERS = 4
MAX_SITEMAP_DEPTH = 2
MAX_SITEMAP_DOCS = 12
ROBOTS_TTL_SECS = 24 * 3600

SITEMAP_PATHS = (
    "/sitemap.xml",
    "/careers/sitemap.xml",
    "/sitemap_index.xml",
    "/careers/sitemap_index.xml",
    "/jobs/sitemap.xml",
)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "sitemap")

_INTERN_SLUG_RE = re.compile(
    r"(intern|internship|co-?op|new-?grad|newgrad|graduate|entry-?level|junior|"
    r"trainee|apprentice|early-?career|rotation)",
    re.I,
)
_JOB_PATH_RE = re.compile(
    r"/(job|jobs|career|careers|position|positions|opening|openings|vacancy|"
    r"vacancies|requisition|requisitions|jobsearch|job-search|viewjob)/",
    re.I,
)
_SKIP_PATH_RE = re.compile(
    r"/(?:apply|application|search|searchresults|login|signin|account|profile|"
    r"preapply|applybutton|talentcommunity|emailsubscribe)(?:/|$)"
    r"|\.(?:pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|svg|zip|css|js)(?:\?|$)",
    re.I,
)
_LOCSTRIP_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:job\s*details?|careers?|job\s*search|search\s*jobs?|positions?|apply|"
    r"vacanc(?:y|ies)|current\s+openings?)\s*$",
    re.I,
)
_TITLE_TAIL_RE = re.compile(
    r"\s*(?:\||\u2013|\u2014|-)\s*(?:job\s*details?|careers?|search|jobs|home)\s*$",
    re.I,
)


def _clean_fallback_role(raw_title: str, company: str = "") -> str:
    label = _strip_html(company or "")

    def drop_company(value: str) -> str:
        if not label:
            return value
        for sep in (" - ", " | ", " \u2013 ", " \u2014 ", ", ", " @ ", " at "):
            suffix = f"{sep}{label}"
            if value.lower().endswith(suffix.lower()):
                return value[: -len(suffix)]
        return value

    role = drop_company(raw_title.strip())
    for _ in range(3):
        if "|" in role:
            head, _, tail = role.rpartition("|")
            if head.strip() and 0 < len(tail.split()) <= 5:
                role = head
                continue
        role = _TITLE_TAIL_RE.sub("", role).strip(" -|\u2013\u2014")
        role = re.sub(
            r"\s+job\s*details?\s*$", "", role, flags=re.I
        ).strip(" -|\u2013\u2014")
        role = drop_company(role).strip(" -|\u2013\u2014")
    if not role or _PLACEHOLDER_TITLE_RE.match(role):
        return ""
    return role
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]*'
    r'content=["\']([^"\']*)["\']',
    re.I,
)

_robots_cache: dict[str, tuple[float, object | None, bool, bool]] = {}
_robots_lock = threading.Lock()
_host_locks: dict[str, threading.Lock] = {}
_host_lock_guard = threading.Lock()
_cache_lock = threading.Lock()
_HTTP = None

JOB_COLUMNS = [
    "company",
    "role",
    "location",
    "date",
    "link",
    "is_remote",
    "salary_min",
    "salary_max",
    "salary_currency",
    "country_iso",
    "employment_type",
    "seniority",
]


def _host_lock(host: str) -> threading.Lock:
    with _host_lock_guard:
        lock = _host_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _host_locks[host] = lock
        return lock


def _strip_html(value: str) -> str:
    if not value:
        return ""
    text = value.replace("<br>", "\n").replace("<br/>", "\n").replace("</p>", "\n")
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return _LOCSTRIP_RE.sub(" ", text).strip()


_BLANKET_DISALLOW_RE = re.compile(r"(?im)^\s*disallow\s*:\s*/\s*$")


def _applicable_group(text: str, agent_token: str) -> str:
    """Return the robots.txt body that governs us, honouring group scoping.

    Blanket 'Disallow: /' lines are common in per-bot stanzas (TrackIf,
    coccocbot, ...) sitting alongside a permissive 'User-agent: *' stanza. The
    directive applies only within its own group, so a file-wide regex would
    deny hosts that explicitly allow us. Consecutive User-agent lines share one
    group, and the first specific match wins over the wildcard.
    """
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if current and value:
                current = []
            current.append(value)
            groups.setdefault("\n".join(current), [])
            continue
        if field in ("disallow", "allow"):
            groups.setdefault("\n".join(current), []).append(f"{field}:{value}")
    token = agent_token.lower()
    for name, body in groups.items():
        agents = [a.strip().lower() for a in name.split("\n") if a.strip()]
        if any(a and a != "*" and a in token for a in agents):
            return "\n".join(body)
    for name, body in groups.items():
        if "*" in [a.strip().lower() for a in name.split("\n") if a.strip()]:
            return "\n".join(body)
    return ""


def _blanket_disallow(text: str, agent_token: str = ROBOT_AGENT) -> bool:
    """True when the group that governs us disallows the whole site.

    protego resolves 'Disallow: /' plus 'Allow: /careers' by longest match and
    therefore admits every /careers/job/<id> page, which is exactly the traffic
    those vendors (Eightfold and friends) do not want from an unattended
    crawler. We detect the blanket rule and stay out; protego still governs
    every host that publishes ordinary, path-scoped rules.
    """
    return bool(_BLANKET_DISALLOW_RE.search(_applicable_group(text, agent_token)))


def _robots_for(host: str) -> tuple["object | None", bool, bool]:
    """Return (protego parser, unavailable, blanket) for host.

    unavailable=True means we could not read a verdict, not that the host has
    no rules. Only a confirmed 404/410 (no robots.txt published) counts as
    unrestricted; 401/403/5xx/timeouts must deny, since a vendor gating
    robots.txt is signalling it does not want automated traffic.

    blanket=True means robots.txt was read but disallows the entire site for
    the wildcard agent. That is a denial, distinct from "no rules at all".
    """
    now = time.time()
    with _robots_lock:
        hit = _robots_cache.get(host)
        if hit and now - hit[0] < ROBOTS_TTL_SECS:
            return hit[1], hit[2], hit[3]
    parser = None
    unavailable = True
    blanket = False
    try:
        resp = http_get(f"https://{host}/robots.txt", timeout=10, retries=1, headers=UA)
        if resp is not None:
            if resp.status_code in (404, 410):
                unavailable = False
            elif resp.status_code == 200:
                unavailable = False
                text = resp.text.strip()
                if text:
                    blanket = _blanket_disallow(text, ROBOT_AGENT)
                    if not blanket:
                        parser = Protego.parse(text)
    except Exception:
        parser = None
    with _robots_lock:
        _robots_cache[host] = (now, parser, unavailable, blanket)
    return parser, unavailable, blanket


def robots_allows(url: str) -> bool:
    """True when robots.txt permits this URL for us.

    Absent robots.txt means yes; an unreadable one, or a blanket 'Disallow: /',
    means no.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return False
    parser, unavailable, blanket = _robots_for(parts.netloc)
    if unavailable or blanket:
        return False
    if parser is None:
        return True
    try:
        return bool(parser.can_fetch(url, ROBOT_AGENT))
    except Exception:
        return False


def _session():
    """CachedSession that revalidates politely and replays stored bodies.

    requests-cache owns conditional GET for us: it stores the response, sends
    If-None-Match on the next run, and — critically — returns the cached body
    on 304 instead of an empty string. That last part is why this replaces the
    hand-rolled ETag/body cache, which silently dropped the row on every
    incremental run.
    """
    global _HTTP
    with _cache_lock:
        if _HTTP is None:
            os.makedirs(CACHE_DIR, exist_ok=True)
            _HTTP = CachedSession(
                cache_name=os.path.join(CACHE_DIR, "http"),
                backend="sqlite",
                expire_after=ROBOTS_TTL_SECS,
                allowable_codes=(200, 304),
                match_headers=["If-None-Match", "If-Modified-Since"],
                cache_control=False,
                stale_if_error=True,
                retries=0,
            )
        return _HTTP


def _polite_get(url: str, host: str, timeout: int = 20):
    """Serial, delayed, cached GET for a single host."""
    with _host_lock(host):
        time.sleep(PER_HOST_DELAY_SECS)
        try:
            resp = _session().get(url, headers=UA, timeout=timeout)
        except requests.RequestException:
            return None
        if resp.status_code in (429, 503):
            wait = float(resp.headers.get("Retry-After", "0") or 0)
            time.sleep(min(30.0, wait) if wait else 10.0)
            return None
        if resp.status_code != 200:
            return None
        return resp.text


def _sitemap_urls(text: str, depth: int = 0) -> list[str]:
    if not text or depth > MAX_SITEMAP_DEPTH:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
    if "<sitemapindex" in text.lower() and len(locs) > MAX_SITEMAP_DOCS:
        return locs[:MAX_SITEMAP_DOCS]
    out: list[str] = []
    for loc in locs:
        low = loc.lower()
        if low.endswith((".xml", ".xml.gz")) or "sitemap" in low:
            if depth < MAX_SITEMAP_DEPTH:
                out.extend(_sitemap_urls(_fetch_text(loc, depth + 1), depth + 1))
        else:
            out.append(loc)
    return out


def _fetch_text(url: str, depth: int = 0) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return ""
    if not robots_allows(url):
        return ""
    return _polite_get(url, parts.netloc) or ""


# Career hosts for vendors with no public API. Sourced from the state-of-ats
# registry, restricted to pools we cannot already reach by API. Every host here
# is still subject to the robots gate — eightfold-style "Disallow: /" vendors
# resolve to zero rows at runtime rather than being trusted from this list.
DEFAULT_HOSTS: tuple[tuple[str, str], ...] = (
    ("Walmart", "careers.walmart.com"),
    ("Target", "corporate.target.com"),
    ("Kroger", "jobs.kroger.com"),
    ("Nucor", "jobs.nucor.com"),
    ("Union Pacific", "jobs.up.com"),
    ("Sysco", "sysco.wd1.myworkdayjobs.com"),
    ("General Motors", "search-careers.gm.com"),
    ("Deloitte", "deloitte.wd1.myworkdayjobs.com"),
    ("Accenture", "careers.accenture.com"),
    ("Capgemini", "capgemini.wd3.myworkdayjobs.com"),
    ("Cognizant", "cognizantcareers.com"),
    ("DXC Technology", "jobs.dxc.com"),
    ("Publicis Sapient", "careers.publicissapient.com"),
    ("Verizon", "verizon.wd1.myworkdayjobs.com"),
    ("Lockheed Martin", "lockheedmartin.com"),
    ("Northrop Grumman", "northropgrumman.com"),
    ("Honeywell", "careers.honeywell.com"),
    ("Johnson Controls", "careers.johnsoncontrols.com"),
    ("GE HealthCare", "careers.gehealthcare.com"),
    ("ServiceNow", "careers.servicenow.com"),
    ("Workday", "workdayjobs.com"),
)


def default_hosts() -> list[tuple[str, str]]:
    """Bundled (company, host) pairs for --sitemap.

    Entries without a resolvable host are skipped by harvest(); the list is a
    starting point, not a guarantee of coverage.
    """
    return [(c, h.strip().replace(" ", "")) for c, h in DEFAULT_HOSTS]


def discover_sitemap(host: str) -> list[str]:
    """Job URLs a host advertises in its sitemap, minus anything robots denies."""
    urls: list[str] = []
    for path in SITEMAP_PATHS:
        url = f"https://{host}{path}"
        if not robots_allows(url):
            continue
        text = _polite_get(url, host)
        if not text or "<urlset" not in text.lower() and "<sitemapindex" not in text.lower():
            continue
        urls.extend(_sitemap_urls(text))
        if urls:
            break
    seen: set[str] = set()
    jobs = [
        u
        for u in urls
        if _JOB_PATH_RE.search(u) and not _SKIP_PATH_RE.search(u) and u not in seen
    ]
    seen.update(jobs)
    return jobs


def _iter_ld_nodes(blob):
    if isinstance(blob, list):
        for item in blob:
            yield from _iter_ld_nodes(item)
        return
    if not isinstance(blob, dict):
        return
    yield blob
    for key in ("@graph", "mainEntity", "itemListElement"):
        if key in blob:
            yield from _iter_ld_nodes(blob[key])


def _first(value, default=""):
    if isinstance(value, list):
        return value[0] if value else default
    return value if value is not None else default


def _address(job: dict) -> dict:
    loc = _first(job.get("jobLocation"), {})
    if not isinstance(loc, dict):
        return {}
    addr = _first(loc.get("address"), {})
    if not isinstance(addr, dict):
        return {"addressLocality": str(addr)}
    out = dict(addr)
    for key in ("addressLocality", "addressRegion", "addressCountry", "streetAddress"):
        val = out.get(key)
        if isinstance(val, dict):
            out[key] = _first(val.get("name", ""))
    return out


def _base_salary(job: dict):
    bs = job.get("baseSalary") or {}
    if not isinstance(bs, dict):
        return None, None, None
    val = bs.get("value") or {}
    if isinstance(val, list):
        val = _first(val)
    if not isinstance(val, dict):
        return None, None, None
    lo = val.get("minValue") or val.get("value")
    hi = val.get("maxValue")
    cur = bs.get("currency") or bs.get("currencyCode")
    if isinstance(cur, dict):
        cur = cur.get("name")
    if lo is None and hi is None:
        return None, None, None
    return lo, hi, (str(cur).upper() if cur else None)


_COUNTRY_NAMES = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u s a": "US",
    "america": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "germany": "DE",
    "india": "IN",
    "ireland": "IE",
    "australia": "AU",
    "new zealand": "NZ",
    "netherlands": "NL",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "brazil": "BR",
    "mexico": "MX",
    "japan": "JP",
    "singapore": "SG",
    "sweden": "SE",
    "switzerland": "CH",
    "poland": "PL",
    "israel": "IL",
    "south korea": "KR",
    "china": "CN",
    "taiwan": "TW",
    "belgium": "BE",
    "austria": "AT",
    "denmark": "DK",
    "finland": "FI",
    "norway": "NO",
    "portugal": "PT",
    "united arab emirates": "AE",
}


def _country_code(addr: dict) -> str:
    """ISO2 for a job location, or '' when the page does not say clearly.

    Returning '' is deliberate: generate_listings._infer_country() runs over the
    assembled location string afterwards, and an invented code ('UN' sliced out
    of 'United States') is worse than none.
    """
    raw = addr.get("addressCountry") or ""
    if isinstance(raw, dict):
        raw = raw.get("name", "")
    if not isinstance(raw, str):
        return ""
    token = raw.strip()
    if not token:
        return ""
    if len(token) == 2 and token.isalpha():
        return token.upper()
    low = token.lower()
    if low in _COUNTRY_NAMES:
        return _COUNTRY_NAMES[low]
    for part in token.replace(";", ",").split(","):
        part = part.strip().lower()
        if len(part) == 2 and part.isalpha():
            return part.upper()
        if part in _COUNTRY_NAMES:
            return _COUNTRY_NAMES[part]
    return ""


def _remote(job: dict, addr: dict) -> str:
    kind = _first(job.get("jobLocationType"), "")
    if isinstance(kind, dict):
        kind = kind.get("name", "")
    blob = f"{kind} {addr.get('addressLocality', '')}"
    if re.search(r"remote", blob, re.I):
        return "true"
    return "false"


def parse_job_page(company: str, url: str, html: str) -> dict | None:
    for raw in _LD_RE.findall(html or ""):
        try:
            blob = json.loads(raw.strip())
        except Exception:
            continue
        for node in _iter_ld_nodes(blob):
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not any(k == "JobPosting" for k in kinds):
                continue
            title = _strip_html(str(node.get("title") or ""))
            if not title:
                continue
            org = _first(node.get("hiringOrganization"), {})
            org_name = _strip_html(str(org.get("name") or "")) if isinstance(org, dict) else ""
            addr = _address(node)
            parts = [
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("addressCountry", ""),
            ]
            location = ", ".join([str(p) for p in parts if p])
            lo, hi, cur = _base_salary(node)
            employment = _first(node.get("employmentType"), "")
            if isinstance(employment, dict):
                employment = employment.get("name", "")
            return {
                "company": (org_name or company or "").strip(),
                "role": title,
                "location": location,
                "date": str(node.get("datePosted") or "")[:10],
                "link": url,
                "is_remote": _remote(node, addr),
                "salary_min": lo,
                "salary_max": hi,
                "salary_currency": cur,
                "country_iso": _country_code(addr),
                "employment_type": str(employment) if employment else None,
                "seniority": None,
            }
    title_m = _TITLE_RE.search(html or "")
    if title_m:
        role = _clean_fallback_role(_strip_html(title_m.group(1)), company)
        if not role:
            return None
        desc_m = _META_RE.search(html or "")
        return {
            "company": (company or "").strip(),
            "role": role[:160],
            "location": "",
            "date": "",
            "link": url,
            "is_remote": "false",
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "country_iso": "",
            "employment_type": None,
            "seniority": None,
            "description": _strip_html(desc_m.group(1))[:2000] if desc_m else "",
        }
    return None


def harvest_host(host: str, company: str, max_fetches: int = 40) -> list[dict]:
    jobs = discover_sitemap(host)
    if not jobs:
        return []
    shortlist = [u for u in jobs if _INTERN_SLUG_RE.search(u)]
    if not shortlist:
        return []
    rows: list[dict] = []
    for url in shortlist[:max_fetches]:
        if not robots_allows(url):
            continue
        html = _polite_get(url, host)
        if not html:
            continue
        row = parse_job_page(company, url, html)
        if row:
            rows.append(row)
    return rows


def harvest(hosts, max_fetches_per_host: int = 40, workers: int = HOST_WORKERS):
    """Harvest many hosts concurrently, one serial polite stream per host.

    hosts is an iterable of (company, host) pairs.
    """
    targets = []
    seen: set[str] = set()
    for company, host in hosts:
        host = str(host).strip().lower()
        if "." not in host or host in seen:
            continue
        seen.add(host)
        targets.append((company, host))
    rows: list[dict] = []
    failed: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(harvest_host, host, company, max_fetches_per_host): host
            for company, host in targets
        }
        for fut in concurrent.futures.as_completed(futs):
            host = futs[fut]
            try:
                rows.extend(fut.result())
            except Exception as exc:
                failed.append(f"{host}: {exc}")
    frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=JOB_COLUMNS)
    for col in JOB_COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    if failed:
        print(f"  sitemap hosts failed: {len(failed)}")
    return frame, failed