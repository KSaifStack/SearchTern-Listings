import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import random
import re
import sys
import requests
import time as _time
import duckdb

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import echojobs
import jobspy_source
import markdown_sources
import pandas as pd
import readme_generation
import skillexchange
from html import unescape
from html.parser import HTMLParser
from readme_utils import http_get

import classify
from readme_utils import canonical_url, clean_company_name, clean_location

TIER_LIGHT = "light"
TIER_MEDIUM = "medium"
TIER_HEAVY = "heavy"
TIER_ALL = "all"
ALL_TIERS = [TIER_LIGHT, TIER_MEDIUM, TIER_HEAVY]

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
HASH_FILE = os.path.join(CACHE_DIR, "last_hash.json")

_UA = {"User-Agent": "SearchTern-Listings/1.0 (+https://github.com/KSaifStack/SearchTern-Listings)"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._buf = []

    def handle_data(self, data):
        self._buf.append(data)


def _strip_html(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    value = unescape(str(value))
    if "<" not in value:
        value = re.sub(r"\s+", " ", value).strip()
        return value or None
    p = _TextExtractor()
    try:
        p.feed(value)
    except Exception:
        pass
    text = unescape("".join(p._buf))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _parse_tier(value):
    value = value.lower()
    if value in ("all", "everything", "full"):
        return TIER_ALL
    if value not in ALL_TIERS:
        raise argparse.ArgumentTypeError(
            f"tier must be one of {ALL_TIERS} or 'all'"
        )
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate SearchTern job listings")
    parser.add_argument(
        "--tier",
        type=_parse_tier,
        default=TIER_ALL,
        help="Which source tier to refresh: light (no API), medium (freehire), heavy (ATS probe), or all",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force regeneration even if source data hasn't changed",
    )
    return parser.parse_args(argv)


def _cache_path(tier):
    return os.path.join(CACHE_DIR, f"tier_{tier}.parquet")


def _load_cached(tier):
    path = _cache_path(tier)
    if os.path.exists(path):
        try:
            df = pd.read_parquet(path)
            print(f"  Loaded cached {tier} data: {len(df):,} rows")
            return df
        except Exception as e:
            print(f"  Could not load cached {tier} data ({e}); treating as empty")
    return pd.DataFrame()


def _save_cache(df, tier):
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_parquet(_cache_path(tier), index=False)
    print(f"  Cached {tier} data: {len(df):,} rows")
    return len(df)


def _compute_data_hash(manifest_sha, md_sources_hash):
    """Hash of manifest SHA + markdown source content hashes = fingerprint of input data."""
    raw = f"{manifest_sha}|{md_sources_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_last_hash():
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE) as f:
            return json.load(f).get("hash", "")
    return ""


def _save_last_hash(h):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(HASH_FILE, "w") as f:
        json.dump({"hash": h}, f)


ALLOWED_ATS = [
    'greenhouse', 'lever', 'ashby', 'workday', 'icims',
    'bamboohr', 'rippling', 'smartrecruiters', 'teamtailor',
    'recruitee', 'breezy', 'pinpoint', 'workable',
    'successfactors', 'phenom', 'avature', 'cornerstone',
    'eightfold', 'gem', 'recruiterbox', 'personio',
    'amazon', 'apple', 'tesla', 'google', 'tiktok', 'uber',
    'ycombinator',
    'welcometothejungle', 'jazzhr',
    'oracle', 'dayforce', 'ukg', 'jobvite', 'builtin',
    'weworkremotely', 'wellfound', 'remoteok',
    # Hidden-in-ATS buckets jobhive ships but we never pulled (scanned all 22
    # non-ALLOWED ATS, 2026-09-18, counted via the README query: 60d/ascii/
    # non-EU). Added those above taleo's 11-row bar:
    #   taleo 17 | darwinbox 78 | paycom 101 | paylocity 29 | keka 16 (IN
    #   internships: Toddle/Signzy) | moka 18 (CN/HK/TW: osl/trip/klook) |
    #   beisen 13 (single company CICC, HK/SG project interns).
    # Skipped: jobbankca 13 (keyword noise: 'campus maintenance manager',
    #   'rotational moulding operator'), eures 4 (HR medical-residency 'REU'
    #   matches), getonbrd 3 (ES LatAm), arbetsformedlingen/bundesagentur/
    #   jobsch/gupy/herp/hrmos/join_com/programathor/pageup/softgarden/wanted/
    #   meta/bytedance/mercor/manfred/beisen_legacy 0 (empty or no campus
    #   roles) — left off to skip dead parquet downloads on every run.
    'taleo', 'darwinbox', 'paycom', 'paylocity',
    'keka', 'moka', 'beisen',
]

# --- Title exclusions (shared base + per-pipeline extras) ---
BLACKLISTED_COMPANIES = [
    'focusgrouppanel', 'familiehulp',
]

COMMON_TITLE_EXCLUSIONS = list(classify.TITLE_EXCLUDE_TERMS) + [
    'medical intern', 'clinical intern', 'internal medicine', 'internal audit',
    'internal only', 'internal security', 'sales associate',
    'sales representative',
]
README_ONLY_EXCLUSIONS = ['data entry', 'front end entry', 'international only']
LISTINGS_ONLY_EXCLUSIONS = ['marketing intern', 'hr intern', 'human resources']

def _exclusion_clause(terms):
    return "".join(f"\n    AND title NOT ILIKE '%{t}%'" for t in terms)

README_TITLE_EXCLUSIONS = _exclusion_clause(COMMON_TITLE_EXCLUSIONS + README_ONLY_EXCLUSIONS)
LISTINGS_TITLE_EXCLUSIONS = _exclusion_clause(COMMON_TITLE_EXCLUSIONS + LISTINGS_ONLY_EXCLUSIONS)

# --- Intern / new-grad matching conditions (used once per query, in both
#     the CASE and the WHERE, instead of being retyped in each place) ---
README_INTERN_COND = """
            commitment ILIKE '%intern%'
            OR regexp_matches(title, '\\bintern\\b', 'i')
            OR title ILIKE '%co-op%'
            OR regexp_matches(title, '(^|[^a-z])coop([^a-z]|$)', 'i')
            OR title ILIKE '%undergraduate research%'
            OR title ILIKE '%undergrad research%'
            OR (
                title ILIKE '%research assistant%'
                AND title NOT ILIKE '%postdoc%'
                AND title NOT ILIKE '%post-doc%'
                AND title NOT ILIKE '%phd%'
            )
            OR title ILIKE '%student researcher%'
            OR title ILIKE '%student research%'
            OR title ILIKE '%REU%'
            OR title ILIKE '%summer research%'
            OR title ILIKE '%undergraduate assistant%'
"""

README_NEWGRAD_COND = """
            title ILIKE '%new grad%'
            OR title ILIKE '%new graduate%'
            OR title ILIKE '%entry level%'
            OR title ILIKE '%entry-level%'
            OR title ILIKE '%early career%'
            OR title ILIKE '%campus%'
            OR title ILIKE '%rotational%'
            OR title ILIKE '%graduate engineer%'
            OR title ILIKE '%graduate developer%'
            OR title ILIKE '%graduate analyst%'
            OR commitment ILIKE '%new grad%'
            OR commitment ILIKE '%entry%'
"""

LISTINGS_INTERN_COND = f"""
            commitment ILIKE '%intern%'
            OR {classify.intern_title_cond()}
            OR commitment ILIKE '%student%'
"""

LISTINGS_NEWGRAD_COND = f"""
            {classify.newgrad_title_cond()}
            OR commitment ILIKE '%new grad%'
            OR commitment ILIKE '%entry%'
"""

COUNTRY_EXCLUSIONS = """
        AND country_iso NOT IN (
            'DE', 'AT', 'CH', 'FR', 'PL', 'NO', 'SE', 'DK',
            'NL', 'IT', 'ES', 'PT', 'RO', 'HU', 'CZ', 'SK',
            'HR', 'BG', 'FI', 'LU', 'BE', 'MT', 'CY'
        )"""


def build_job_query(intern_cond, newgrad_cond, title_exclusions, lookback_days, extra_where=""):
    """Shared skeleton for both the README and listings.json pipelines.
    Only the match conditions, exclusions, lookback window, and any extra
    WHERE clause differ between callers."""
    return f"""
    SELECT
        company,
        title        as role,
        location,
        posted_at    as date,
        url          as link,
        is_remote,
        salary_min,
        salary_max,
        salary_currency,
        country_iso,
        description,
        CASE
            WHEN ({intern_cond}){title_exclusions}
            AND url IS NOT NULL
            AND TRIM(url) != ''
            AND location IS NOT NULL
            THEN 'internship'

            WHEN ({newgrad_cond}){title_exclusions}
            AND url IS NOT NULL
            AND TRIM(url) != ''
            AND location IS NOT NULL
            THEN 'new_grad'

            ELSE 'other'
        END as job_type

    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY company, title, location
                ORDER BY posted_at DESC
            ) as rn
        FROM read_parquet($1)
        WHERE ({intern_cond} OR {newgrad_cond})
        AND CAST(posted_at AS TIMESTAMP) >= CURRENT_DATE - INTERVAL '{lookback_days} days'
        AND url IS NOT NULL AND title IS NOT NULL AND company IS NOT NULL
        AND title NOT ILIKE '%(m/w/d)%'
        AND title NOT ILIKE '%(m/f/d)%'
        AND title NOT ILIKE '%(w/m/d)%'{extra_where}
    )
    WHERE rn = 1
    AND LOWER(company) NOT IN ('focusgrouppanel', 'familiehulp')
    ORDER BY posted_at DESC
"""


FREEHIRE_INTERN_API = "https://freehire.me/api/v1/jobs/search?employment_type=internship&is_tech=tech"
FREEHIRE_NEWGRAD_API = "https://freehire.me/api/v1/jobs/search?employment_type=full_time&is_tech=tech&seniority=junior&q=new+grad+OR+entry+level+OR+early+career+OR+campus+OR+rotational"
FREEHIRE_PAGE_DELAY_SECS = 0.25

# Shared classifiers (word-bounded) live in classify.py — same rules drive the
# SQL commit/where clauses above and the pandas filters below.
_LISTINGS_INTERN_RE = classify.LISTINGS_INTERN_RE
_LISTINGS_NEWGRAD_RE = classify.LISTINGS_NEWGRAD_RE
_TITLE_EXCLUDE_RE = classify.TITLE_EXCLUDE_RE

_US_STATES = {
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
    'HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
    'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
    'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
    'DC','AS','GU','MP','PR','VI',
}
_CA_PROVS = {'AB','BC','MB','NB','NL','NS','NT','NU','ON','PE','QC','SK','YT'}
_AU_STATES = {'NSW','QLD','SA','TAS','VIC','WA','ACT','NT'}
_LOC_TO_COUNTRY = {
    'united states':'US','usa':'US','u.s.a.':'US','us':'US',
    'canada':'CA','united kingdom':'GB','uk':'GB','england':'GB',
    'australia':'AU','new zealand':'NZ','ireland':'IE','singapore':'SG',
    'india':'IN','germany':'DE','netherlands':'NL','france':'FR',
    'japan':'JP','brazil':'BR','mexico':'MX','switzerland':'CH',
    'austria':'AT','belgium':'BE','sweden':'SE','spain':'ES',
    'luxembourg':'LU','italy':'IT','poland':'PL','norway':'NO',
    'denmark':'DK','finland':'FI','malta':'MT','portugal':'PT',
    'czech republic':'CZ','cyprus':'CY','romania':'RO',
    'china':'CN','hong kong':'HK','south korea':'KR','israel':'IL',
    'south africa':'ZA','russia':'RU','turkey':'TR',
    'saudi arabia':'SA','uae':'AE','colombia':'CO','chile':'CL',
    'peru':'PE','argentina':'AR','costa rica':'CR',
}

def _load_city_map():
    try:
        import json, os
        path = os.path.join(os.path.dirname(__file__), 'city_to_country.json')
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

_CITY_MAP = _load_city_map()

def _infer_country(location):
    if not location or not isinstance(location, str):
        return ''
    loc_lower = location.lower().strip()
    for name, code in sorted(_LOC_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
        if name in loc_lower:
            return code
    parts = [p.strip() for p in loc_lower.replace(',', ' ').split()]
    for part in parts:
        upper = part.upper()
        if upper in _US_STATES:
            return 'US'
        if upper in _CA_PROVS and upper not in _AU_STATES:
            return 'CA'
    code = _CITY_MAP.get(loc_lower.split(',')[0].strip())
    if code:
        return code
    return ''


def _fetch_freehire(url):
    sep = "&" if "?" in url else "?"
    resp = http_get(f"{url}{sep}limit=1&offset=0")
    if resp is None or resp.status_code != 200:
        print(
            f"  freehire API error "
            f"{resp.status_code if resp is not None else 'unreachable'}"
        )
        return []
    total = resp.json()["meta"]["total"]
    print(f"  freehire {total} total ...")

    def _page(offset):
        _time.sleep(FREEHIRE_PAGE_DELAY_SECS)
        r = http_get(f"{url}{sep}limit=100&offset={offset}")
        if r is None or r.status_code != 200:
            return []
        return r.json().get("data", [])

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        offsets = list(range(0, min(total, 3000), 100))
        futs = [ex.submit(_page, o) for o in offsets]
        jobs = []
        for fut in concurrent.futures.as_completed(futs):
            jobs.extend(fut.result())
    return jobs


def _normalize_freehire(jobs):
    rows = []
    for j in jobs:
        e = j.get("enrichment") or {}
        loc = (j.get("location", "") or "").strip()
        country = ((j.get("countries") or [None])[0] or "")
        rows.append({
            "company": j.get("company", "") or "",
            "role": (j.get("title", "") or "").strip(),
            "location": loc,
            "date": j.get("posted_at", ""),
            "link": j.get("url", ""),
            "is_remote": str(j.get("work_mode") == "remote").lower(),
            "salary_min": e.get("salary_min"),
            "salary_max": e.get("salary_max"),
            "salary_currency": e.get("salary_currency"),
            "country_iso": (country.upper() or _infer_country(loc)),
            "employment_type": e.get("employment_type"),
            "seniority": e.get("seniority"),
        })
    return pd.DataFrame(rows)


def _classify_freehire(df):
    role = df["role"].str.lower()
    intern = role.str.contains(_LISTINGS_INTERN_RE, regex=True, na=False)
    newgrad = role.str.contains(_LISTINGS_NEWGRAD_RE, regex=True, na=False)
    exclude = role.str.contains(_TITLE_EXCLUDE_RE, regex=True, na=False)
    has_link = df["link"].notna() & (df["link"].str.strip() != "")
    has_loc = df["location"].notna()

    df = df.copy()
    df["job_type"] = "other"

    # Use freehire's own enrichment as an additional signal
    if "employment_type" in df.columns:
        fh_intern = df["employment_type"].fillna("").str.lower() == "internship"
        fh_junior = df["seniority"].fillna("").str.lower().isin(["intern", "junior"])
        df.loc[fh_intern & fh_junior & ~exclude & has_link & has_loc, "job_type"] = "internship"
    elif "enrichment" in df.columns:
        def enrichment_type(j):
            e = j if isinstance(j, dict) else {}
            return str(e.get("employment_type", "")).lower()
        def enrichment_sen(j):
            e = j if isinstance(j, dict) else {}
            return str(e.get("seniority", "")).lower()
        fh_intern = df["enrichment"].apply(enrichment_type) == "internship"
        fh_junior = df["enrichment"].apply(enrichment_sen).isin(["intern", "junior"])
        df.loc[fh_intern & fh_junior & ~exclude & has_link & has_loc, "job_type"] = "internship"

    intern = role.str.contains(_LISTINGS_INTERN_RE, regex=True, na=False)
    newgrad = role.str.contains(_LISTINGS_NEWGRAD_RE, regex=True, na=False)
    df.loc[intern & ~exclude & has_link & has_loc, "job_type"] = "internship"
    df.loc[newgrad & ~exclude & has_link & has_loc, "job_type"] = "new_grad"
    return df[df["job_type"] != "other"].copy()


def _dedup_across(df_a, df_b):
    """Remove rows from df_b that duplicate (company, role, location) in df_a."""
    if df_a.empty or df_b.empty:
        return df_b
    keys_b = df_b[["company", "role", "location"]].astype(str).agg("|".join, axis=1)
    keys_a = df_a[["company", "role", "location"]].astype(str).agg("|".join, axis=1)
    return df_b[~keys_b.isin(keys_a)].copy()


def _stamp(df, source, now):
    """Attach provenance + observation time so listings.json rows carry them."""
    df = df.copy()
    df["source"] = source
    df["observed_at"] = now.isoformat()
    return df


def _seen_path():
    return os.path.join(CACHE_DIR, "seen.json")


def _load_seen():
    try:
        with open(_seen_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(seen):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_seen_path(), "w", encoding="utf-8") as f:
        json.dump(seen, f)


def _verify_link(u):
    """HEAD-check an apply link. Network blips keep the row rather than drop it."""
    try:
        r = requests.head(u, timeout=10, allow_redirects=True, headers=_UA)
        return r.status_code not in (404, 410)
    except Exception:
        return True

print("Fetching jobhive manifest...")
manifest_resp = http_get("https://storage.stapply.ai/jobhive/v1/manifest.json")
if manifest_resp is None or manifest_resp.status_code != 200:
    print("  FATAL: could not fetch jobhive manifest — aborting to avoid wiping listings")
    sys.exit(1)
manifest = manifest_resp.json()
parquet_urls = [
    manifest["by_ats"][ats]["parquet"]
    for ats in ALLOWED_ATS
    if ats in manifest["by_ats"]
]
MANIFEST_HASH = manifest.get("parquet_sha256", manifest.get("sha256", ""))[:16]
print(f"  Manifest hash: {MANIFEST_HASH} — {len(parquet_urls)} ATS sources")

now = pd.Timestamp.now('UTC')
ARGS = parse_args()
RUN_TIERS = ALL_TIERS if ARGS.tier == TIER_ALL else [ARGS.tier]
print(f"Running tier(s): {', '.join(RUN_TIERS)}")

# --- README pipeline (pasted code filters) ---
# Country exclusions, ASCII-only titles, 60-day lookback, narrow patterns
readme_query = build_job_query(
    README_INTERN_COND, README_NEWGRAD_COND, README_TITLE_EXCLUSIONS,
    lookback_days=60,
    extra_where=f"""
        AND title ~ '^[[:ascii:]]+$'{COUNTRY_EXCLUSIONS}""",
)
readme_result = duckdb.execute(readme_query, [parquet_urls]).df()
readme_result = readme_result[readme_result["job_type"] != "other"]

# --- Listings.json pipeline (current generate_listings.py filters) ---
# Broader patterns, 90-day lookback, tech keyword filter, no country exclusions
listings_query = build_job_query(
    LISTINGS_INTERN_COND, LISTINGS_NEWGRAD_COND, LISTINGS_TITLE_EXCLUSIONS,
    lookback_days=90,
)
listings_result = duckdb.execute(listings_query, [parquet_urls]).df()
listings_result = listings_result[listings_result["job_type"] != "other"]
if "description" in listings_result.columns:
    listings_result["description"] = listings_result["description"].map(
        lambda v: _strip_html(v) if v is not None and str(v).strip() else None
    )
listings_result = _stamp(listings_result, "jobhive", now)

# ── Direct ATS probing (big tech supplement) ──────────────────────────────
ATS_ENDPOINTS = {
    'Greenhouse': 'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&per_page=100',
    'Lever': 'https://api.lever.co/v0/postings/{slug}?mode=json',
    'Ashby': 'https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true',
    'SmartRecruiters': 'https://api.smartrecruiters.com/v1/companies/{slug}/postings',
}

ATS_CSV_URL = 'https://raw.githubusercontent.com/Kayvan-Zahiri/state-of-ats-2026/main/data/companies.csv'
ATS_PROBE_STATE_PATH = os.path.join(CACHE_DIR, "ats_probe_state.json")
ATS_PROBE_COOLDOWN_HOURS = 24

# Boards jobhive doesn't scrape; verified by hand.
# Workday slug is the CXS base path: {host}/wday/cxs/{tenant}/{site}
EXTRA_BOARDS = [
    ("Ally", "Workday", "ally.wd1.myworkdayjobs.com/wday/cxs/ally/Ally"),
]


# Workday publishes no site-name registry: {tenant}.wdN hosts hide the CXS site
# name in robots.txt Disallow paths. Strip the first segment per line, fall back
# to the tenant/subdomain and its capitalization.
_WD_HOST_RE = re.compile(r'^([^.]+)\.wd\d+\.myworkdayjobs\.com$')


def _discover_workday_sites(host, tenant):
    # Discovery is a hint, not a contract: fail fast, never back off. A DNS blip
    # on one host must not stretch a serial 50-host pass into minutes.
    candidates = []
    try:
        resp = requests.get(f"https://{host}/robots.txt", headers=_UA, timeout=8)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                m = re.match(r'\s*Disallow:\s*/([A-Za-z0-9_.-]+)/', line)
                if m and m.group(1) != 'refreshFacet':
                    candidates.append(m.group(1))
    except Exception:
        pass
    candidates += [tenant, tenant.capitalize()]
    working = []
    for site in dict.fromkeys(candidates):
        if not site:
            continue
        try:
            resp = requests.post(
                f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                json={"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": ""},
                headers={**_UA, "Content-Type": "application/json"},
                timeout=8,
            )
            if resp.status_code == 200:
                working.append(f"{host}/wday/cxs/{tenant}/{site}")
        except Exception:
            pass
    return working


def _abs_posted_on(label, now=None):
    """Workday reports relative ages ('Posted 3 days ago'). Convert to ISO."""
    now = now or pd.Timestamp.now('UTC')
    if not label:
        return str(now)
    try:
        pd.Timestamp(label)
        return label
    except Exception:
        pass
    label = label.lower()
    if 'today' in label:
        return str(now)
    if 'yesterday' in label:
        return str(now - pd.Timedelta(days=1))
    m = re.search(r'(\d+) (day|week)s? ago', label)
    if m:
        n = int(m.group(1))
        unit = pd.Timedelta(days=7) if m.group(2) == 'week' else pd.Timedelta(days=1)
        return str(now - n * unit)
    return str(now)


def _smartrecruiters_desc(job):
    sections = (job.get('jobAd') or {}).get('sections') or {}
    if isinstance(sections, list):
        for s in sections:
            if str(s.get('name') or '').lower() in ('jobdescription', 'description'):
                return s.get('text') or ''
    desc = sections.get('jobDescription') or sections.get('description') or {}
    return desc.get('text') if isinstance(desc, dict) else ''


def _fetch_company_jobs(company, ats_type, slug):
    url = ATS_ENDPOINTS.get(ats_type)
    if url is None and ats_type == 'Workday':
        url = 'https://{slug}/jobs'
    if not url:
        return None
    try:
        _time.sleep(random.uniform(0, 0.15))
        if ats_type == 'Workday':
            host = slug.split('/')[0]
            data = {'jobPostings': []}
            offset = 0
            while len(data['jobPostings']) < 100:
                # ponytail: limit=20 (some tenants 400 on larger pages); 100-job cap = max 5 pages
                resp = requests.post(
                    url.format(slug=slug),
                    json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                    headers={**_UA, "Content-Type": "application/json"},
                    timeout=15,
                )
                if resp is None or resp.status_code != 200:
                    break
                page = resp.json().get('jobPostings') or []
                data['jobPostings'].extend(page)
                if len(page) < 20:
                    break
                offset += 20
            for jp in data['jobPostings']:
                p = jp.get('externalPath') or ''
                jp['externalPath'] = f"https://{host}{p}" if p else ''
        else:
            resp = http_get(url.format(slug=slug), timeout=15, retries=2)
            if resp is None or resp.status_code != 200:
                return None
            data = resp.json()
        return (company, ats_type, data)
    except Exception:
        return None


def _normalize_ats_jobs(company, ats_type, data):
    rows = []
    if ats_type == 'Greenhouse':
        for j in data.get('jobs', []):
            loc_name = ((j.get('location') or {}).get('name') or '').strip()
            loc_str = loc_name
            is_remote = 'false'
            if 'remote' in loc_name.lower():
                is_remote = 'true'
            rows.append({
                'company': company, 'role': (j.get('title') or '').strip(),
                'location': loc_str,
                'date': j.get('updated_at', ''),
                'link': j.get('absolute_url', ''),
                'is_remote': is_remote,
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
                'description': _strip_html(j.get('content') or ''),
            })
    elif ats_type == 'Lever':
        for j in data if isinstance(data, list) else []:
            cats = j.get('categories') or {}
            ts = j.get('createdAt', 0)
            if isinstance(ts, (int, float)) and ts > 0:
                dt = str(pd.Timestamp(ts, unit='ms', tz='UTC'))
            else:
                dt = ''
            loc_str = ((cats.get('location') or '')).strip()
            wtype = j.get('workplaceType')
            if wtype == 'remote':
                is_remote = 'true'
            elif isinstance(wtype, str):
                is_remote = 'false'
            else:
                # ponytail: unknown (missing field) kept distinct from not-remote
                is_remote = 'unknown'
            rows.append({
                'company': company, 'role': (j.get('text') or '').strip(),
                'location': loc_str,
                'date': dt,
                'link': j.get('hostedUrl', ''),
                'is_remote': is_remote,
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
                'description': _strip_html(j.get('descriptionPlain') or j.get('description') or ''),
            })
    elif ats_type == 'Ashby':
        for j in data.get('jobs', []):
            loc_str = (j.get('location') or '').strip()
            comp = j.get('compensation') or {}
            salary_min = comp.get('minValue') or comp.get('baseSalary', {}).get('minValue')
            salary_max = comp.get('maxValue') or comp.get('baseSalary', {}).get('maxValue')
            rows.append({
                'company': company, 'role': (j.get('title') or '').strip(),
                'location': loc_str,
                'date': j.get('publishedAt', ''),
                'link': j.get('applicationUrl', ''),
                'is_remote': str(bool(j.get('isRemote'))).lower(),
                'salary_min': salary_min, 'salary_max': salary_max,
                'salary_currency': comp.get('currency'),
                'country_iso': _infer_country(loc_str),
                'description': _strip_html(j.get('descriptionPlain') or j.get('descriptionHtml') or ''),
            })
    elif ats_type == 'SmartRecruiters':
        for j in data.get('content', []):
            loc = j.get('location') or {}
            loc_str = (loc.get('fullLocation') or '') or ', '.join(filter(None, [
                loc.get('city', ''), loc.get('region', ''), loc.get('country', '')
            ]))
            rows.append({
                'company': company, 'role': (j.get('name') or '').strip(),
                'location': loc_str,
                'date': j.get('publishedDate', ''),
                'link': j.get('applyUrl', j.get('id', '')),
                'is_remote': str(bool(loc.get('remote'))).lower(),
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
                'description': _strip_html(_smartrecruiters_desc(j)),
            })
    elif ats_type == 'Workday':
        for j in data.get('jobPostings', []):
            loc_str = (j.get('locationsText') or '').strip()
            rows.append({
                'company': company, 'role': (j.get('title') or '').strip(),
                'location': loc_str,
                'date': _abs_posted_on(j.get('postedOn'), now),
                'link': j.get('externalPath', ''),
                'is_remote': str('remote' in loc_str.lower()),
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
            })
    return rows


def _fetch_ats_probe():
    print("Fetching ATS-probe company list...")
    resp = http_get(ATS_CSV_URL, retries=2)
    if resp is None or resp.status_code != 200:
        print(
            f"  ATS CSV download failed: "
            f"{resp.status_code if resp is not None else 'unreachable'}"
        )
        return pd.DataFrame()

    lines = resp.text.strip().split('\n')
    data_lines = [l for l in lines if not l.startswith('#')]
    reader = csv.DictReader(io.StringIO('\n'.join(data_lines)))
    companies = list(reader)

    probe_list = [
        (c['name'], c['ats_system'], c['slug'])
        for c in companies
        if c['ats_system'] in ATS_ENDPOINTS
        and c.get('verified', '').lower() == 'true'
    ]
    probe_list += EXTRA_BOARDS

    # Cooldown + discovery caches live in one state file, so load before building
    # the probe list (Workday discovery reads/writes it below).
    probe_state = {}
    try:
        with open(ATS_PROBE_STATE_PATH) as f:
            probe_state = json.load(f)
    except Exception:
        pass

    # Workday CXS discovery: verified Workday tenants on the {tenant}.wdN shape
    # whose site name isn't in the CSV get their sites probed from robots.txt
    # (+ tenant fallback) and cached in probe_state, so discovery runs once per
    # cooldown window instead of every fetch. Each working site becomes its own
    # probe entry (tenants like Verizon publish several distinct career sites).
    wd_cutoff = _time.time() - ATS_PROBE_COOLDOWN_HOURS * 3600
    for c in [x for x in companies if x['ats_system'] == 'Workday'
              and x.get('verified', '').lower() == 'true']:
        host = (c.get('apply_host') or '').strip().lower()
        m = _WD_HOST_RE.match(host)
        if not m:
            # ponytail: non-{tenant}.wdN hosts (careers.walmart.com, ...site.com)
            # aren't addressable without per-tenant data — add a manual EXTRA_BOARD
            continue
        tenant = m.group(1)
        key = f"WorkdaySlugs|{c['name']}"
        cached = probe_state.get(key)
        if not (isinstance(cached, dict) and cached.get('slugs')
                and cached.get('at', 0) > wd_cutoff):
            probe_state[key] = {'at': _time.time(), 'slugs': _discover_workday_sites(host, tenant)}
        for slug in probe_state.get(key, {}).get('slugs', []):
            probe_list.append((c['name'], 'Workday', slug))

    # Cooldown: skip endpoints probed successfully within the last 24h.
    cutoff = _time.time() - ATS_PROBE_COOLDOWN_HOURS * 3600
    warm = []
    for name, ats, slug in probe_list:
        ts = probe_state.get(f"{ats}|{name}")
        if ts is None or ts <= cutoff:
            warm.append((name, ats, slug))
        else:
            probe_state.setdefault("_skipped_this_run", 0)
            probe_state["_skipped_this_run"] += 1
    if warm:
        print(f"  {len(warm)} to probe now, {len(probe_list) - len(warm)} skipped by cooldown")

    all_raw = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_fetch_company_jobs, name, ats, slug)
                for name, ats, slug in warm]
        for fut in concurrent.futures.as_completed(futs):
            result = fut.result()
            if result:
                all_raw.append(result)

    print(f"  Fetched {len(all_raw)}/{len(warm)} endpoints successfully")

    # Record successes for cooldown.
    for company, ats_type, _ in all_raw:
        probe_state[f"{ats_type}|{company}"] = _time.time()
    if "_skipped_this_run" in probe_state:
        del probe_state["_skipped_this_run"]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ATS_PROBE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(probe_state, f)

    all_rows = []
    for company, ats_type, data in all_raw:
        rows = _normalize_ats_jobs(company, ats_type, data)
        all_rows.extend(rows)

    print(f"  Total raw jobs: {len(all_rows):,}")
    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = _classify_freehire(df)
    print(f"  After classification: {len(df):,}")
    return df


# ── Freehire (medium tier) ────────────────────────────────────────────────────
# Fetch tech internships + entry-level roles from freehire.me (covers 75+ ATS)
fh_df = pd.DataFrame()
if TIER_MEDIUM in RUN_TIERS:
    print("Fetching freehire data...")
    fh_raw = _fetch_freehire(FREEHIRE_INTERN_API)
    fh_raw += _fetch_freehire(FREEHIRE_NEWGRAD_API)
    fh_df = _normalize_freehire(fh_raw)
    fh_df = _classify_freehire(fh_df)
    fh_df = _stamp(fh_df, "freehire", now)
    print(f"  Freehire raw fetched: {len(fh_raw):,}, classified: {len(fh_df):,}")
    _save_cache(fh_df, TIER_MEDIUM)
else:
    fh_df = _load_cached(TIER_MEDIUM)
print(f"  Freehire working set: {len(fh_df):,} rows")

source_contrib = {TIER_LIGHT: [0, 0], TIER_MEDIUM: [0, 0], TIER_HEAVY: [0, 0]}

# Remove jobs already covered by jobhive
if not fh_df.empty:
    fh_df = _dedup_across(readme_result, fh_df)
    print(f"  After dedup vs jobhive: {len(fh_df):,}")

    # Split freehire data — README (with country/ASCII filters), listings (broader)
    fh_for_readme = fh_df[
        ~fh_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                     'NL','IT','ES','PT','RO','HU','CZ','SK',
                                     'HR','BG','FI','LU','BE','MT','CY'])
        & (pd.to_datetime(fh_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60))
        & fh_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    fh_for_listings = fh_df[
        pd.to_datetime(fh_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=90)
    ].copy()
    source_contrib[TIER_MEDIUM] = [len(fh_for_readme), len(fh_for_listings)]

    readme_result = pd.concat([readme_result, fh_for_readme], ignore_index=True)
    readme_result = readme_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    listings_result = pd.concat([listings_result, fh_for_listings], ignore_index=True)
    listings_result = listings_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    print(f"  Freehire added: {source_contrib[TIER_MEDIUM][0]} to README, {source_contrib[TIER_MEDIUM][1]} to listings")

# ── Direct ATS probing (heavy tier) ──────────────────────────────────────────
ats_df = pd.DataFrame()
if TIER_HEAVY in RUN_TIERS:
    print("Fetching ATS probe data...")
    probe_df = _fetch_ats_probe()
    if probe_df is None:
        probe_df = pd.DataFrame()
    if not probe_df.empty:
        probe_df = _stamp(probe_df, "ats", now)
    if probe_df.empty:
        # Cooldown skipped every endpoint this run — fall back to last good
        # snapshot so the feed is never starved by a cooldown-only cycle.
        previous = _load_cached(TIER_HEAVY)
        if not previous.empty:
            print(f"  Cooldown cycle: reusing {len(previous):,} cached ATS rows")
            ats_df = previous
        else:
            ats_df = probe_df
    else:
        ats_df = probe_df
    _save_cache(ats_df, TIER_HEAVY)
else:
    ats_df = _load_cached(TIER_HEAVY)
print(f"  ATS probe working set: {len(ats_df):,} rows")

if not ats_df.empty:
    ats_df = _dedup_across(readme_result, ats_df)
    ats_for_readme = ats_df[
        ~ats_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                     'NL','IT','ES','PT','RO','HU','CZ','SK',
                                     'HR','BG','FI','LU','BE','MT','CY'])
        & (pd.to_datetime(ats_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60))
        & ats_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    ats_for_listings = ats_df[
        pd.to_datetime(ats_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=90)
    ].copy()
    source_contrib[TIER_HEAVY] = [len(ats_for_readme), len(ats_for_listings)]

    readme_result = pd.concat([readme_result, ats_for_readme], ignore_index=True)
    readme_result = readme_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    listings_result = pd.concat([listings_result, ats_for_listings], ignore_index=True)
    listings_result = listings_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    print(f"  ATS probe added: {source_contrib[TIER_HEAVY][0]} to README, {source_contrib[TIER_HEAVY][1]} to listings")

# ── Markdown GitHub sources (light tier, no API calls) ──────────────────────
md_df = pd.DataFrame()
md_source_stats = []
if TIER_LIGHT in RUN_TIERS:
    md_df, md_source_stats = markdown_sources.fetch_and_parse(infer_country=_infer_country)
    if md_df is None:
        md_df = pd.DataFrame()
    if not md_df.empty:
        md_df["observed_at"] = now.isoformat()
        print(f"  Markdown unique rows: {len(md_df):,}")
    _save_cache(md_df, TIER_LIGHT)
else:
    md_df = _load_cached(TIER_LIGHT)
print(f"  Markdown working set: {len(md_df):,} rows")

pre_md_listings_keys = set(
    listings_result[["company", "role", "location"]].astype(str).agg("|".join, axis=1)
)
if not md_df.empty:
    md_df = _dedup_across(readme_result, md_df)
    print(f"  After dedup vs existing sources: {len(md_df):,}")
    md_for_readme = md_df[
        ~md_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                    'NL','IT','ES','PT','RO','HU','CZ','SK',
                                    'HR','BG','FI','LU','BE','MT','CY'])
        & (pd.to_datetime(md_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60))
        & md_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    md_for_listings = md_df[
        pd.to_datetime(md_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=90)
    ].copy()
    source_contrib[TIER_LIGHT] = [len(md_for_readme), len(md_for_listings)]

    readme_result = pd.concat([readme_result, md_for_readme], ignore_index=True)
    readme_result = readme_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    listings_result = pd.concat([listings_result, md_for_listings], ignore_index=True)
    listings_result = listings_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    print(f"  Markdown added: {source_contrib[TIER_LIGHT][0]} to README, {source_contrib[TIER_LIGHT][1]} to listings")

TECH_KEYWORDS_RE = classify.TECH_KEYWORDS_RE
# ── SkillExchange board (light tier supplement) ────────────────────────────
sx_df = pd.DataFrame()
if TIER_LIGHT in RUN_TIERS:
    sx_df, _ = skillexchange.fetch_and_parse(infer_country=_infer_country)
    if sx_df is None:
        sx_df = pd.DataFrame()
    if not sx_df.empty:
        sx_df = _classify_freehire(sx_df)
        sx_df = _stamp(sx_df, "skillexchange", now)
        print(f"  SkillExchange classified: {len(sx_df):,} rows")
        _save_cache(sx_df, "skill")
else:
    sx_df = _load_cached("skill")
print(f"  SkillExchange working set: {len(sx_df):,} rows")

if not sx_df.empty:
    sx_df = _dedup_across(readme_result, sx_df)
    print(f"  SkillExchange after dedup vs existing sources: {len(sx_df):,}")
    sx_for_readme = sx_df[
        ~sx_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                    'NL','IT','ES','PT','RO','HU','CZ','SK',
                                    'HR','BG','FI','LU','BE','MT','CY'])
        & (pd.to_datetime(sx_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60))
        & sx_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    sx_for_listings = sx_df[
        pd.to_datetime(sx_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=90)
    ].copy()

    readme_result = pd.concat([readme_result, sx_for_readme], ignore_index=True)
    readme_result = readme_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    listings_result = pd.concat([listings_result, sx_for_listings], ignore_index=True)
    listings_result = listings_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    print(f"  SkillExchange added: {len(sx_for_readme)} to README, {len(sx_for_listings)} to listings")

# ── EchoJobs board (light tier supplement) ───────────────────────────────
ej_df = pd.DataFrame()
if TIER_LIGHT in RUN_TIERS:
    ej_df, _ = echojobs.fetch_and_parse(infer_country=_infer_country)
    if ej_df is None:
        ej_df = pd.DataFrame()
    if not ej_df.empty:
        ej_df = _classify_freehire(ej_df)
        ej_df = _stamp(ej_df, "echojobs", now)
        print(f"  EchoJobs classified: {len(ej_df):,} rows")
        _save_cache(ej_df, "echo")
else:
    ej_df = _load_cached("echo")
print(f"  EchoJobs working set: {len(ej_df):,} rows")

if not ej_df.empty:
    ej_df = _dedup_across(readme_result, ej_df)
    print(f"  EchoJobs after dedup vs existing sources: {len(ej_df):,}")
    ej_for_readme = ej_df[
        ~ej_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                    'NL','IT','ES','PT','RO','HU','CZ','SK',
                                    'HR','BG','FI','LU','BE','MT','CY'])
        & (pd.to_datetime(ej_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60))
        & ej_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    ej_for_listings = ej_df[
        pd.to_datetime(ej_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=90)
    ].copy()

    readme_result = pd.concat([readme_result, ej_for_readme], ignore_index=True)
    readme_result = readme_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    listings_result = pd.concat([listings_result, ej_for_listings], ignore_index=True)
    listings_result = listings_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    print(f"  EchoJobs added: {len(ej_for_readme)} to README, {len(ej_for_listings)} to listings")

# ── JobSpy boards (indeed/ziprecruiter/google) ────────────────────────────
js_df = pd.DataFrame()
if TIER_LIGHT in RUN_TIERS:
    js_df, _ = jobspy_source.fetch_and_parse(infer_country=_infer_country)
    if js_df is None:
        js_df = pd.DataFrame()
    if not js_df.empty:
        js_df = _classify_freehire(js_df)
        js_df = _stamp(js_df, "jobspy", now)
        print(f"  JobSpy classified: {len(js_df):,} rows")
        _save_cache(js_df, "jobspy")
else:
    js_df = _load_cached("jobspy")
print(f"  JobSpy working set: {len(js_df):,} rows")

if not js_df.empty:
    js_df = _dedup_across(readme_result, js_df)
    print(f"  JobSpy after dedup vs existing sources: {len(js_df):,}")
    js_for_readme = js_df[
        ~js_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                    'NL','IT','ES','PT','RO','HU','CZ','SK',
                                    'HR','BG','FI','LU','BE','MT','CY'])
        & (pd.to_datetime(js_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60))
        & js_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    js_for_listings = js_df[
        pd.to_datetime(js_df["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=90)
    ].copy()

    readme_result = pd.concat([readme_result, js_for_readme], ignore_index=True)
    readme_result = readme_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    listings_result = pd.concat([listings_result, js_for_listings], ignore_index=True)
    listings_result = listings_result.drop_duplicates(
        subset=["company", "role", "location"], keep="first"
    )
    print(f"  JobSpy added: {len(js_for_readme)} to README, {len(js_for_listings)} to listings")

_us_loc_re = re.compile(r'\b(?:US|USA|U\.S\.A\.|United States|California|Texas|New York|Washington|Seattle|San Francisco|SF|NYC|Austin|Chicago|Boston|Mountain View|Palo Alto|Sunnyvale|Los Angeles|Irvine|San Diego|Santa Clara|Cupertino|Menlo Park|Redmond|Kirkland|Bellevue|Arlington|McLean|Reston|Atlanta|Denver|Portland|Phoenix|Philadelphia|Pittsburgh|Minneapolis|Ann Arbor|Detroit|Miami|Orlando|Tampa|Dallas|Houston|Raleigh|Durham|Charlotte|Nashville|Salt Lake City|St Louis|Kansas City|Columbus|Indianapolis|Milwaukee|Baltimore|Portland)\b', re.IGNORECASE)

role_lower = listings_result["role"].str.lower()
tech_mask = role_lower.str.contains(TECH_KEYWORDS_RE, regex=True, na=False)
listings_result = listings_result[tech_mask]

# Listings.json: USA only
listings_result = listings_result.fillna({"country_iso": ""})
listings_result = listings_result[
    (listings_result["country_iso"] == "US")
    | ((listings_result["country_iso"] == "") & listings_result["location"].str.contains(_us_loc_re, na=False))
]
listings_result = listings_result[listings_result["location"].notna() & (listings_result["location"] != "")]

# Cross-source URL-canonical dedup (same posting from two sources collapses).
if not listings_result.empty and "link" in listings_result.columns:
    _canon = listings_result["link"].map(canonical_url)
    _no_canon = _canon == ""
    _with_canon = listings_result[~_no_canon].copy()
    _with_canon["_canon"] = _canon[~_no_canon]
    _with_canon = _with_canon.drop_duplicates(subset="_canon", keep="first").drop(columns="_canon")
    listings_result = pd.concat(
        [listings_result[_no_canon], _with_canon], ignore_index=True
    )

# ── Feed digest + closure tracking (B) ─────────────────────────────────────
# Diff this run's rows against cache/seen.json. Sources re-fetched this run
# vote on closures; cached-only sources are left alone (their rows may just
# be outside the run's lookback window).
def _row_source(row):
    src = str(row.get("source") or "").strip()
    return src if src in ("jobhive", "freehire", "ats") else "md"


def _seen_bucket(src):
    return src if src in ("jobhive", "freehire", "ats") else "md"


buckets_fresh = {
    "jobhive": True,
    "freehire": TIER_MEDIUM in RUN_TIERS,
    "ats": TIER_HEAVY in RUN_TIERS,
    "md": TIER_LIGHT in RUN_TIERS,
}

final = listings_result.copy()
current_by_key = {}
for idx, row in final.iterrows():
    key = "|".join([
        _seen_bucket(_row_source(row)),
        clean_company_name(str(row["company"])),
        str(row["role"]).strip(),
        str(row.get("location", "")).strip(),
    ])
    current_by_key[key] = row

seen = _load_seen()
cutoff = (now - pd.Timedelta(days=120)).isoformat()
seen = {k: v for k, v in seen.items() if v.get("last", "") >= cutoff}

current_keys = set(current_by_key)
is_new = current_keys - set(seen)
print(f"  Feed: {len(current_keys):,} rows, {len(is_new):,} new, {len(seen):,} known")

# Dead-link check on new postings (capped per run; blips keep the row).
new_links = []
for k in is_new:
    row = current_by_key[k]
    link = str(row.get("link") or "")
    if link.startswith(("http://", "https://")):
        new_links.append((k, link))

dead_keys = set()
checked = new_links[:200]
if checked:
    print(f"  Verifying {len(checked)} new posting links...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_verify_link, link): k for k, link in checked}
        for fut in concurrent.futures.as_completed(futs):
            if not fut.result():
                dead_keys.add(futs[fut])

closed = []
for k, meta in seen.items():
    bucket = _seen_bucket(k.split("|", 1)[0])
    if buckets_fresh.get(bucket, False) and k not in current_keys:
        closed.append({
            "key": k,
            "first_seen": meta.get("first", ""),
            "last_seen": meta.get("last", ""),
        })

# Drop dead new postings from the feed, then persist seen state.
if dead_keys:
    drop_idx = [current_by_key[k].name for k in dead_keys]
    listings_result = listings_result.drop(drop_idx)
    for k in dead_keys:
        current_by_key.pop(k, None)
    is_new = is_new - dead_keys

obs = now.isoformat()
for k, row in current_by_key.items():
    prior = seen.get(k, {})
    seen[k] = {"first": prior.get("first", obs), "last": obs}
_save_seen(seen)

root_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
digest_dir = os.path.join(root_dir, "digests")
os.makedirs(digest_dir, exist_ok=True)
today = now.strftime("%Y-%m-%d")

new_rows = [
    {
        "company": current_by_key[k]["company"],
        "role": current_by_key[k]["role"],
        "location": current_by_key[k].get("location", ""),
        "link": current_by_key[k].get("link", ""),
        "source": current_by_key[k].get("source", ""),
        "observed_at": obs,
    }
    for k in sorted(is_new)
]
with open(os.path.join(digest_dir, f"new-{today}.json"), "w", encoding="utf-8") as f:
    json.dump(new_rows, f, indent=2, ensure_ascii=False)
with open(os.path.join(digest_dir, f"closed-{today}.json"), "w", encoding="utf-8") as f:
    json.dump(closed, f, indent=2, ensure_ascii=False)

per_source = (
    listings_result["source"].value_counts().to_dict()
    if not listings_result.empty and "source" in listings_result.columns
    else {}
)
run_meta = {
    "generated_at": obs,
    "total": len(listings_result),
    "per_source": {str(k): int(v) for k, v in per_source.items()},
    "new": len(new_rows),
    "closed": len(closed),
    "dead_links": len(dead_keys),
    "sources_this_run": buckets_fresh,
    "markdown_source_stats": [{"name": n, "rows": c} for n, c in md_source_stats],
}
os.makedirs(os.path.join(root_dir, "pages"), exist_ok=True)
with open(os.path.join(root_dir, "pages", "run_meta.json"), "w", encoding="utf-8") as f:
    json.dump(run_meta, f, indent=2, ensure_ascii=False)
print(f"  Digest: {len(new_rows)} new, {len(closed)} closed, {len(dead_keys)} dead links")

# --- Output Pipelines ---
if not ats_df.empty and "description" in ats_df.columns and not listings_result.empty:
    desc_by_role = {}
    for _, r in ats_df.iterrows():
        d = _strip_html(r.get("description"))
        if d and str(d).strip():
            key = (str(r["company"]).strip().lower(), str(r["role"]).strip().lower())
            desc_by_role.setdefault(key, d)

    def _fill_desc(row):
        cur = row.get("description")
        cur = _strip_html(cur)
        if cur and str(cur).strip():
            return cur
        return desc_by_role.get(
            (str(row["company"]).strip().lower(), str(row["role"]).strip().lower())
        )

    listings_result["description"] = listings_result.apply(_fill_desc, axis=1)
    n_with_desc = listings_result["description"].notna().sum()
    print(f"  Description coverage: {n_with_desc:,}/{len(listings_result):,} rows")

readme_role_lower = readme_result["role"].str.lower()
readme_tech_mask = readme_role_lower.str.contains(TECH_KEYWORDS_RE, regex=True, na=False)
readme_result = readme_result[readme_tech_mask]
readme_generation.generate_readme(readme_result, output_dir="..")
readme_generation.write_listings_json(listings_result, output_dir="..")

# --- Output change detection ---
_listings_path = os.path.join("..", "pages", "listings.json")
if os.path.exists(_listings_path):
    with open(_listings_path, "rb") as f:
        _out_hash = hashlib.md5(f.read()).hexdigest()
    _prev_hash = _load_last_hash()
    if _out_hash == _prev_hash and not ARGS.force:
        print(f"\n--- Output unchanged (hash {_out_hash}) — skipping commit ---")
        print("NO_CHANGES")
    else:
        _save_last_hash(_out_hash)
        print(f"\n--- Output changed: {_prev_hash} -> {_out_hash} ---")

# --- Metrics ---
r_md, r_fh, r_ats = (source_contrib[TIER_LIGHT][0], source_contrib[TIER_MEDIUM][0], source_contrib[TIER_HEAVY][0])
l_md, l_fh, l_ats = (source_contrib[TIER_LIGHT][1], source_contrib[TIER_MEDIUM][1], source_contrib[TIER_HEAVY][1])
print("\n--- Markdown source pull counts ---")
for name, count in md_source_stats:
    print(f"  {name:<28} {count:>5,} rows")
print("\n--- README stats ---")
r_total = len(readme_result)
r_internships = (readme_result["job_type"] == "internship").sum()
r_new_grads = (readme_result["job_type"] == "new_grad").sum()
print(f"Total listings  : {r_total:,}  (+{r_fh} freehire+indeed, +{r_ats} ats-probe, +{r_md} markdown)")
print(f"  Internships   : {int(r_internships):,}")
print(f"  New grad      : {int(r_new_grads):,}")

print("\n--- Listings.json stats ---")
l_total = len(listings_result)
l_internships = (listings_result["job_type"] == "internship").sum()
l_new_grads = (listings_result["job_type"] == "new_grad").sum()
l_remote = (listings_result["is_remote"].astype(str).str.lower() == "true").sum()
numeric_salary = pd.to_numeric(listings_result["salary_min"], errors='coerce')
l_paid = (numeric_salary > 0).sum()
print(f"Total listings  : {l_total:,}  (+{l_fh} freehire+indeed, +{l_ats} ats-probe, +{l_md} markdown)")
print(f"  Internships   : {int(l_internships):,}")
print(f"  New grad      : {int(l_new_grads):,}")
print(f"Remote roles    : {int(l_remote):,}")
print(f"Paid roles      : {int(l_paid):,}")

print("\n--- Markdown contribution to listings.json ---")
if not md_df.empty:
    md_final = md_df.copy()

    md_final["company"] = md_final["company"].map(clean_company_name)
    md_final["location"] = md_final["location"].map(clean_location)
    md_final = md_final[md_final["company"].notna() & (md_final["company"] != "")]

    md_final = md_final[
        (md_final["country_iso"] == "US")
        | ((md_final["country_iso"] == "") & md_final["location"].str.contains(_us_loc_re, na=False))
    ]
    md_final = md_final[
        md_final["role"].str.lower().str.contains(TECH_KEYWORDS_RE, regex=True, na=False)
    ]
    md_final = md_final[
        pd.to_datetime(md_final["date"], errors='coerce', utc=True) >= now - pd.Timedelta(days=60)
    ]
    md_final = md_final[
        md_final.apply(
            lambda r: r["role"].isascii() and r["company"].isascii(), axis=1
        )
    ]
    md_final = md_final[
        ~md_final["company"].str.lower().str.strip().isin(
            readme_generation.NORMALIZED_BLOCKED_COMPANIES
        )
    ]

    md_keys = set(md_final[["company", "role", "location"]].astype(str).agg("|".join, axis=1))
    md_new = md_keys - pre_md_listings_keys
    print(f"  Markdown rows surviving full json filter : {len(md_final):,}")
    print(f"  Net-new markdown rows in listings.json   : {len(md_new):,}")