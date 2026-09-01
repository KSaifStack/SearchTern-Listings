import argparse
import concurrent.futures
import csv
import io
import os
import re
import requests
import time as _time
import duckdb
import markdown_sources
import pandas as pd
import readme_generation

TIER_LIGHT = "light"
TIER_MEDIUM = "medium"
TIER_HEAVY = "heavy"
TIER_ALL = "all"
ALL_TIERS = [TIER_LIGHT, TIER_MEDIUM, TIER_HEAVY]

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")


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
]

# --- Title exclusions (shared base + per-pipeline extras) ---
BLACKLISTED_COMPANIES = [
    'focusgrouppanel', 'familiehulp',
]

COMMON_TITLE_EXCLUSIONS = [
    'pharmacist', 'pharmacy', 'dental', 'nurse', 'nursing', 'physician',
    'medical intern', 'clinical intern', 'internal medicine', 'internal audit',
    'internal only', 'internal security', 'sales associate',
    'sales representative', 'veterinary', 'pastor', 'teacher',
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
            OR title ILIKE '%coop%'
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

LISTINGS_INTERN_COND = """
            commitment ILIKE '%intern%' OR regexp_matches(title, '\\bintern(?:ship)?\\b', 'i') OR title ILIKE '%co-op%'
            OR title ILIKE '%coop%' OR title ILIKE '%undergraduate%' OR title ILIKE '%undergrad%'
            OR title ILIKE '%student%' OR title ILIKE '%REU%' OR title ILIKE '%apprentice%'
            OR title ILIKE '%trainee%' OR title ILIKE '%fellowship%' OR title ILIKE '%praktikum%'
            OR title ILIKE '%werkstudent%' OR title ILIKE '% stage %'
            OR commitment ILIKE '%student%'
"""

LISTINGS_NEWGRAD_COND = """
            title ILIKE '%new grad%' OR title ILIKE '%new graduate%' OR title ILIKE '%newgrad%'
            OR title ILIKE '%university grad%' OR title ILIKE '%university graduate%'
            OR title ILIKE '%entry level%' OR title ILIKE '%entry-level%'
            OR title ILIKE '%early career%' OR title ILIKE '%campus%'
            OR title ILIKE '%rotational%' OR title ILIKE '%junior%'
            OR title ILIKE '%fresh grad%'
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

_LISTINGS_INTERN_RE = (
    r'\bintern(?:ship)?\b|co-op|coop|undergraduate|undergrad|student'
    r'|reu|apprentice|trainee|fellowship|praktikum|werkstudent'
    r'|\bstage\b'
)
_LISTINGS_NEWGRAD_RE = (
    r'new[\s-]grad(?:uate)?\b|university[\s-]grad(?:uate)?\b'
    r'|entry[\s-]level|early\s+career|campus|rotational'
    r'|junior|fresh\s+grad'
)
_TITLE_EXCLUDE_RE = (
    r'pharmacist|pharmacy|dental|nurse|nursing|physician'
    r'|medical\s+intern|clinical\s+intern|internal medicine|internal audit'
    r'|internal\s+only|internal\s+security|sales\s+associate'
    r'|sales\s+representative|veterinary|pastor|teacher'
    r'|marketing\s+intern|hr\s+intern|human\s+resources'
    r'|senior\s+(?!.*(?:intern|co-op|apprentice|trainee))'
)

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
    resp = requests.get(f"{url}{sep}limit=1&offset=0", timeout=15)
    if resp.status_code != 200:
        print(f"  freehire API error {resp.status_code}")
        return []
    total = resp.json()["meta"]["total"]
    print(f"  freehire {total} total ...")

    def _page(offset):
        r = requests.get(f"{url}{sep}limit=100&offset={offset}", timeout=15)
        if r.status_code != 200:
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


manifest = requests.get("https://storage.stapply.ai/jobhive/v1/manifest.json").json()
parquet_urls = [
    manifest["by_ats"][ats]["parquet"]
    for ats in ALLOWED_ATS
    if ats in manifest["by_ats"]
]

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

# ── Direct ATS probing (big tech supplement) ──────────────────────────────
ATS_ENDPOINTS = {
    'Greenhouse': 'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&per_page=100',
    'Lever': 'https://api.lever.co/v0/postings/{slug}?mode=json',
    'Ashby': 'https://api.ashbyhq.com/posting-api/job-board/{slug}',
    'SmartRecruiters': 'https://api.smartrecruiters.com/v1/companies/{slug}/postings',
}

ATS_CSV_URL = 'https://raw.githubusercontent.com/Kayvan-Zahiri/state-of-ats-2026/main/data/companies.csv'


def _fetch_company_jobs(company, ats_type, slug):
    url = ATS_ENDPOINTS.get(ats_type)
    if not url:
        return None
    try:
        resp = requests.get(url.format(slug=slug), timeout=15)
        if resp.status_code != 200:
            return None
        return (company, ats_type, resp.json())
    except Exception:
        return None


def _normalize_ats_jobs(company, ats_type, data):
    rows = []
    if ats_type == 'Greenhouse':
        for j in data.get('jobs', []):
            loc = j.get('location') or {}
            loc_str = (loc.get('name') or '').strip()
            rows.append({
                'company': company, 'role': (j.get('title') or '').strip(),
                'location': loc_str,
                'date': j.get('updated_at', ''),
                'link': j.get('absolute_url', ''),
                'is_remote': 'false',
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
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
            rows.append({
                'company': company, 'role': (j.get('text') or '').strip(),
                'location': loc_str,
                'date': dt,
                'link': j.get('hostedUrl', ''),
                'is_remote': str(j.get('workplaceType') == 'remote').lower(),
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
            })
    elif ats_type == 'Ashby':
        for j in data.get('jobs', []):
            loc_str = (j.get('location') or '').strip()
            rows.append({
                'company': company, 'role': (j.get('title') or '').strip(),
                'location': loc_str,
                'date': j.get('publishedAt', ''),
                'link': j.get('applicationUrl', ''),
                'is_remote': str(bool(j.get('isRemote'))).lower(),
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
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
            })
    return rows


def _fetch_ats_probe():
    print("Fetching ATS-probe company list...")
    resp = requests.get(ATS_CSV_URL, timeout=15)
    if resp.status_code != 200:
        print(f"  ATS CSV download failed: {resp.status_code}")
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
    print(f"  {len(probe_list)} companies to probe")

    all_raw = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_fetch_company_jobs, name, ats, slug)
                for name, ats, slug in probe_list]
        for fut in concurrent.futures.as_completed(futs):
            result = fut.result()
            if result:
                all_raw.append(result)

    print(f"  Fetched {len(all_raw)}/{len(probe_list)} endpoints successfully")

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
        & (pd.to_datetime(fh_df["date"], errors='coerce') >= now - pd.Timedelta(days=60))
        & fh_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    fh_for_listings = fh_df[
        pd.to_datetime(fh_df["date"], errors='coerce') >= now - pd.Timedelta(days=90)
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
    ats_df = _fetch_ats_probe()
    if ats_df is None:
        ats_df = pd.DataFrame()
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
        & (pd.to_datetime(ats_df["date"], errors='coerce') >= now - pd.Timedelta(days=60))
        & ats_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    ats_for_listings = ats_df[
        pd.to_datetime(ats_df["date"], errors='coerce') >= now - pd.Timedelta(days=90)
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
        & (pd.to_datetime(md_df["date"], errors='coerce') >= now - pd.Timedelta(days=60))
        & md_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
    ].copy()
    md_for_listings = md_df[
        pd.to_datetime(md_df["date"], errors='coerce') >= now - pd.Timedelta(days=90)
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

_TECH_KEYWORDS = (
    r"\b(?:swe|sde|mts|it)\b", "software", "developer", "programmer", "coder", "engineer",
    "data", "machine learn", "deep learn", "artificial intellig", " ai ", "ai/", "ml",
    "nlp", "computer vision", "cloud", "devops", "sre", "site reliabil", "cybersecur",
    "security", "full stack", "fullstack", "full-stack", "backend", "back-end",
    "frontend", "front end", "front-end", "web", "mobile", "ios", "android", "qa",
    "quality assur", "test", "sdet", "systems", "network", "sysadmin", "infrastructure",
    "platform", "hardware", "firmware", "embedded", "fpga", "asic", "chip", "database",
    "dba", "ux", "ui", "design", "product", "quant", "computer science", "linux",
    "unix", "robotics", "compiler", "distributed", "game", "blockchain", "web3",
    "cryptograph", "hpc", "scientific comput", "technical", "technology",
    "research", "electrical", "electronics", "controls", "signal", "telecom",
    "architect", "devrel", "automation", "applied scien", "analyst",
    "business", "finance", "consulting", "accounting", "logistics",
    "operations",
)

_us_loc_re = re.compile(r'\b(?:US|USA|U\.S\.A\.|United States|California|Texas|New York|Washington|Seattle|San Francisco|SF|NYC|Austin|Chicago|Boston|Mountain View|Palo Alto|Sunnyvale|Los Angeles|Irvine|San Diego|Santa Clara|Cupertino|Menlo Park|Redmond|Kirkland|Bellevue|Arlington|McLean|Reston|Atlanta|Denver|Portland|Phoenix|Philadelphia|Pittsburgh|Minneapolis|Ann Arbor|Detroit|Miami|Orlando|Tampa|Dallas|Houston|Raleigh|Durham|Charlotte|Nashville|Salt Lake City|St Louis|Kansas City|Columbus|Indianapolis|Milwaukee|Baltimore|Portland)\b', re.IGNORECASE)

role_lower = listings_result["role"].str.lower()
tech_mask = role_lower.str.contains('|'.join(_TECH_KEYWORDS), regex=True, na=False)
listings_result = listings_result[tech_mask]

# Listings.json: USA only
listings_result = listings_result.fillna({"country_iso": ""})
listings_result = listings_result[
    (listings_result["country_iso"] == "US")
    | ((listings_result["country_iso"] == "") & listings_result["location"].str.contains(_us_loc_re, na=False))
]
listings_result = listings_result[listings_result["location"].notna() & (listings_result["location"] != "")]

# --- Output Pipelines ---
readme_role_lower = readme_result["role"].str.lower()
readme_tech_mask = readme_role_lower.str.contains('|'.join(_TECH_KEYWORDS), regex=True, na=False)
readme_result = readme_result[readme_tech_mask]
readme_generation.generate_readme(readme_result, output_dir="..")
readme_generation.write_listings_json(listings_result, output_dir="..")

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
    from readme_utils import clean_company_name, clean_location
    md_final = md_df.copy()

    md_final["company"] = md_final["company"].map(clean_company_name)
    md_final["location"] = md_final["location"].map(clean_location)
    md_final = md_final[md_final["company"].notna() & (md_final["company"] != "")]

    md_final = md_final[
        (md_final["country_iso"] == "US")
        | ((md_final["country_iso"] == "") & md_final["location"].str.contains(_us_loc_re, na=False))
    ]
    md_final = md_final[
        md_final["role"].str.lower().str.contains('|'.join(_TECH_KEYWORDS), regex=True, na=False)
    ]
    md_final = md_final[
        pd.to_datetime(md_final["date"], errors='coerce') >= now - pd.Timedelta(days=60)
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