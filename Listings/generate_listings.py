import argparse
import concurrent.futures
import contextlib
import csv
import hashlib
import io
import json
import os
import random
import re
import sys
import time as _time
import xml.etree.ElementTree as ET

import duckdb
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from html import unescape
from html.parser import HTMLParser

import ats_new_sources
import breadth_sources
import canonical
import classify
import closure
import discover_boards
import echojobs
import ecr_source
import h1b_enrich
import ingest_stats
import jobspy_source
import markdown_sources
import pandas as pd
import priority_boards
import readme_generation
import runway_source
import sitemap_sources
import skillexchange
import source_health
from dedup import dedup_across as _dedup_across
from dedup import dedup_keys as _dedup_keys
from dedup import dedupe as _dedupe
from readme_utils import (
    SESSION,
    canonical_url,
    clean_company_name,
    clean_location,
    http_get,
)

TIER_LIGHT = "light"
TIER_MEDIUM = "medium"
TIER_HEAVY = "heavy"
TIER_PRIORITY = "priority"
TIER_ALL = "all"
ALL_TIERS = [TIER_LIGHT, TIER_MEDIUM, TIER_HEAVY, TIER_PRIORITY]

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
HASH_FILE = os.path.join(CACHE_DIR, "last_hash.json")

_UA = {"User-Agent": "SearchTern-Listings/1.0 (+https://github.com/KSaifStack/SearchTern-Listings)"}

FREEHIRE_MAX_JOBS = int(os.environ.get("SEARCHTERN_FREEHIRE_MAX_JOBS", "6000"))
# Workday pages 20 at a time and answers newest-first, so a 200-job cap only
# kept the last few days of a busy board: sampling 40 real tenants, 16 (40%)
# reported totals above 200, missing ~11.8k postings between them — many inside
# the 60/90-day freshness window the feeds keep. 1000 (50 pages) covers every
# sampled board below the 90th percentile; the tail is env-tunable for the
# handful of mega-boards (micron: 2.8k postings).
WORKDAY_MAX_JOBS = int(os.environ.get("SEARCHTERN_WORKDAY_MAX_JOBS", "1000"))
SMARTRECRUITERS_MAX_JOBS = int(os.environ.get("SEARCHTERN_SMARTRECRUITERS_MAX_JOBS", "1000"))
GREENHOUSE_MAX_JOBS = int(os.environ.get("SEARCHTERN_GREENHOUSE_MAX_JOBS", "500"))
TEAMTAILOR_MAX_PAGES = int(os.environ.get("SEARCHTERN_TEAMTAILOR_MAX_PAGES", "5"))


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
        text = re.sub(r"\s+", " ", value).strip()
    else:
        p = _TextExtractor()
        with contextlib.suppress(Exception):
            p.feed(value)
        text = re.sub(r"\s+", " ", unescape("".join(p._buf))).strip()
    if not text or text.lower() in ("nan", "n/a", "na", "none", "null", "<>"):
        return None
    return text


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
        help="Which source tier to refresh: light (no API), medium (freehire), heavy (ATS probe), priority (hot-company fast lane), or all",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force regeneration even if source data hasn't changed",
    )
    parser.add_argument(
        "--sitemap",
        action="store_true",
        help="Also crawl career sitemaps for ATS vendors with no public API (light tier, opt-in)",
    )
    parser.add_argument(
        "--ecr",
        action="store_true",
        help="Include Early Career Radar postings in the light tier (the medium tier always includes them)",
    )
    parser.add_argument(
        "--ecr-limit",
        type=int,
        default=0,
        help="Cap Early Career Radar page fetches; 0 (default) means the whole sitemap",
    )
    parser.add_argument(
        "--runway",
        action="store_true",
        help="Also include Runway Explore postings (light tier, opt-in, shard-limited)",
    )
    parser.add_argument(
        "--runway-shards",
        type=str,
        default=runway_source.DEFAULT_SHARDS,
        help=f"Runway sitemap shards 0-10 to crawl; default {runway_source.DEFAULT_SHARDS} (~100k URLs)",
    )
    parser.add_argument(
        "--runway-limit",
        type=int,
        default=0,
        help="Cap Runway page fetches; 0 (default) means every URL in the selected shards",
    )
    parser.add_argument(
        "--sitemap-hosts",
        type=str,
        default="",
        help="Comma-separated company=host pairs for --sitemap; defaults to the bundled registry",
    )
    parser.add_argument(
        "--sitemap-max-per-host",
        type=int,
        default=15,
        help="Max job pages fetched per host under --sitemap (default 15)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Concurrent fetch workers (default: SEARCHTERN_WORKERS or 12)",
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
    """Cache a tier through the canonical schema + verified atomic write.

    A write that fails (mixed dtypes, half-written file, disk full) leaves the
    previous cache in place instead of truncating it, and returns 0 so callers
    can tell the cache did not advance.
    """
    written = canonical.write_parquet_atomic(df, _cache_path(tier))
    if written is None:
        return 0
    print(f"  Cached {tier} data: {written:,} rows")
    return written


def _to_dt(series):
    """Parse a date column element-by-element.

    Sources emit mixed formats (ISO date, ISO datetime, "Posted Aug 5",
    Unix epoch). pandas>=2 infers one format from the first value and
    coerces every other shape to NaT, which silently drops whole sources
    from the recency filters — so ask for per-element inference.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    try:
        return pd.to_datetime(series, format="mixed", errors="coerce", utc=True)
    except (ValueError, TypeError):  # pandas < 2.0 has no "mixed"
        return pd.to_datetime(series, errors="coerce", utc=True)


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
    tmp = HASH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"hash": h}, f)
    os.replace(tmp, HASH_FILE)


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
        -- Kept only to verify liveness against the boards this run crawled;
        -- dropped again before any feed is written (see closure.py).
        ats_type,
        ats_id,
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
        FROM read_parquet($1, union_by_name=>true)
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
FREEHIRE_PAGE_DELAY_SECS = float(os.environ.get("SEARCHTERN_FREEHIRE_PAGE_DELAY", "0.05"))

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
        import json
        import os
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


def _fetch_freehire(url, workers=None):
    _t0 = _time.monotonic()
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

    # Page delay only paces the initial burst; http_get already backs off on
    # 429s, so a long fixed sleep just serializes the pool. Kept small and
    # env-overridable (was a fixed 0.25s, ~15s of pure sleep on a full pull).
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers or WORKERS, 16)
    ) as ex:
        offsets = list(range(0, min(total, FREEHIRE_MAX_JOBS), 100))
        futs = [ex.submit(_page, o) for o in offsets]
        jobs = []
        failed = 0
        for fut in concurrent.futures.as_completed(futs):
            page = fut.result()
            if page:
                jobs.extend(page)
            else:
                failed += 1
    if failed:
        print(f"  freehire {failed}/{len(offsets)} pages failed (kept partial)")
    ingest_stats.record("freehire-pages", discovered=len(offsets) * 100,
                        valid=len(jobs), seconds=_time.monotonic() - _t0,
                        status="degraded" if failed else "ok",
                        truncated=int(total > FREEHIRE_MAX_JOBS),
                        note=url.split("?", 1)[0].rsplit("/", 1)[-1])
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
    tmp = _seen_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(tmp, _seen_path())


def _verify_link(u):
    """Confirm an apply link still resolves. Network blips keep the row.

    HEAD is tried first because it is cheap, but several ATS hosts (GM among
    them) answer 404 to HEAD on URLs that serve 200 on GET, so a 404/410/405
    falls back to a ranged GET before the row is called dead.
    """
    def _dead(code):
        return code in (404, 410)

    try:
        r = requests.head(u, timeout=10, allow_redirects=True, headers=_UA)
        if not _dead(r.status_code):
            return True
    except Exception:
        return True

    try:
        r = requests.get(
            u,
            timeout=15,
            allow_redirects=True,
            headers={**_UA, "Range": "bytes=0-2047"},
            stream=True,
        )
        return not _dead(r.status_code)
    except Exception:
        return True


def main(argv=None):
    """Pipeline entry point. Importing this module must not run it;
    use ``python generate_listings.py`` (or ``main([...])``) instead."""
    global API_SOURCES, ARGS, ATS_CSV_URL, ATS_ENDPOINTS, ATS_PROBE_COOLDOWN_HOURS, ATS_PROBE_STATE_PATH, EXTRA_BOARDS, MANIFEST_HASH, RUN_TIERS, TECH_KEYWORDS_RE
    global WORKERS, _NEW_ATS, _TYPE_LABEL, _WD_HOST_RE, _WD_MOVED_COMPANIES, _before, _breadth_stats, _cov, _f, _h1b_approvals
    global _h1b_roles, _health, _k, _listings_path, _new_by_source, _out_hash, _prev_hash, _pri_window, _q, _role_targets
    global _src, _top, _us_loc_re, ats_df, ats_for_listings, ats_for_readme, ats_new_df, b_for_listings, b_for_readme, breadth_df
    global bucket, buckets_fresh, checked, chunk, closed, current_by_key, current_keys, cutoff, d, dead_keys
    global desc_by_role, digest_dir, drop_idx, ecr_df, ecr_raw, ej_df, ej_for_listings, ej_for_readme, empty, fh_df
    global fh_for_listings, fh_for_readme, fh_raw, final, fut, futs, host_source, is_new, js_df, js_for_listings
    global js_for_readme, k, key, l_internships, l_new_grads, l_paid, l_remote, l_total, link, listings_query
    global listings_result, manifest, manifest_resp, md_df, md_final, md_for_listings, md_for_readme, md_keys, md_new, md_source_stats
    global n_for_listings, n_for_readme, n_with_desc, new_links, new_rows, now, numeric_salary, obs, pairs, parquet_urls
    global per_source, pre_md_listings_keys, previous, pri_for_listings, pri_for_readme, pri_probe_df, prior, priority_df, probe_df, r_internships
    global r_new_grads, r_total, readme_query, readme_result, readme_role_lower, readme_tech_mask, role_lower, root_dir, row, run_meta
    global runway_df, runway_raw, runway_shards, searchtern_result, seen, sitemap_df, sitemap_hosts_used, source_contrib, sources_manifest, sources_out
    global sx_df, sx_for_listings, sx_for_readme, tech_mask, today

    # Parse argv first so --help/--bad-flag never pays for a manifest fetch.
    ARGS = parse_args(argv)
    WORKERS = ARGS.workers or int(os.environ.get("SEARCHTERN_WORKERS", "12"))
    RUN_TIERS = ALL_TIERS if ARGS.tier == TIER_ALL else [ARGS.tier]
    print(f"Running tier(s): {', '.join(RUN_TIERS)}")

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
    ingest_stats.record("jobhive", valid=len(readme_result),
                        note="60d README window / 90d listings window, age is its only liveness signal")

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

    # ── Sitemap discovery (opt-in) ────────────────────────────────────────────
    # Vendors that gate search behind a JS-only SPA still publish a career
    # sitemap for crawlers, and those pages carry JSON-LD JobPosting. This is the
    # only extraction path for the unsupported pools (SuccessFactors, iCIMS,
    # Taleo) where no public API exists.
    sitemap_df = pd.DataFrame()
    sitemap_hosts_used = []
    if ARGS.sitemap and TIER_LIGHT in RUN_TIERS:
        if ARGS.sitemap_hosts:
            pairs = []
            for chunk in ARGS.sitemap_hosts.split(","):
                if "=" in chunk:
                    comp, _, host = chunk.partition("=")
                    pairs.append((comp.strip(), host.strip()))
            host_source = "--sitemap-hosts"
        else:
            pairs = sitemap_sources.default_hosts()
            host_source = "bundled registry"
        print(f"\nFetching career sitemaps for {len(pairs):,} hosts ({host_source})...")
        sitemap_df, sm_failed = sitemap_sources.harvest(
            pairs, max_fetches_per_host=ARGS.sitemap_max_per_host
        )
        if not sitemap_df.empty:
            sitemap_df["company"] = sitemap_df["company"].map(clean_company_name)
            sitemap_df["role"] = sitemap_df["role"].astype(str).str.strip()
            sitemap_df = sitemap_df[
                (sitemap_df["company"].notna())
                & (sitemap_df["company"] != "")
                & (sitemap_df["role"] != "")
            ]
            sitemap_df = sitemap_df[
                sitemap_df.apply(
                    lambda r: r["role"].isascii() and r["company"].isascii(), axis=1
                )
            ]
            sitemap_df = sitemap_df[
                sitemap_df["role"].str.lower().str.contains(
                    classify.TECH_KEYWORDS_RE, regex=True, na=False
                )
            ]
            sitemap_df = sitemap_df[
                ~sitemap_df["company"].str.lower().str.strip().isin(
                    readme_generation.NORMALIZED_BLOCKED_COMPANIES
                )
            ]
            if "country_iso" in sitemap_df.columns:
                sitemap_df["country_iso"] = sitemap_df["country_iso"].fillna("")
                sitemap_df["country_iso"] = sitemap_df.apply(
                    lambda r: r["country_iso"]
                    or _infer_country(str(r.get("location") or "")),
                    axis=1,
                )
            sitemap_df["location"] = sitemap_df["location"].map(
                lambda v: clean_location(str(v or ""))
            )
            # Fallback rows carry no date/location; they would otherwise pass every
            # freshness check and crowd out dated JSON-LD rows on the same job.
            sitemap_df = sitemap_df[
                sitemap_df["date"].astype(str).str.strip() != ""
            ]
            sitemap_df = _stamp(sitemap_df, "sitemap", now)
            sitemap_hosts_used = sorted({u.split("/")[2] for u in sitemap_df["link"]})
            print(
                f"  Sitemap rows: {len(sitemap_df):,} from {len(sitemap_hosts_used):,} hosts"
            )
            if sm_failed:
                print(f"  Sitemap hosts that errored: {len(sm_failed)}")
        if sitemap_df.empty and sm_failed:
            sitemap_df = _load_cached("sitemap")
        else:
            _save_cache(sitemap_df, "sitemap")
    else:
        sitemap_df = _load_cached("sitemap")
        if ARGS.sitemap:
            print("  --sitemap needs --tier light (or all); skipped")

    # ── Early Career Radar (aggregator, one request) ──────────────────────────
    # Their listing page embeds every record in the RSC flight payload, so one
    # fetch replaces a crawl over ~7k job pages, and each row can link to the
    # employer's own posting instead of the aggregator's copy. Rows arrive already
    # filtered for geography by ecr_source._country_backstop, which matters here:
    # ~43% of their pages omit addressCountry, so the usual ISO check would wave
    # them through.
    if ARGS.ecr or TIER_MEDIUM in RUN_TIERS:
        print("Fetching Early Career Radar...")
        ecr_raw = ecr_source.harvest_listing(limit=ARGS.ecr_limit or None)
        if ecr_raw.empty:
            print("  listing page yielded nothing; falling back to the sitemap crawl")
            ecr_raw = ecr_source.harvest(
                ecr_source.fetch_sitemap(),
                limit=ARGS.ecr_limit or None,
            )
        ecr_df = ecr_raw.copy()
        if not ecr_df.empty:
            ecr_df["company"] = ecr_df["company"].map(clean_company_name)
            ecr_df = ecr_df[
                (ecr_df["company"].notna())
                & (ecr_df["company"] != "")
                & (ecr_df["role"].astype(str).str.strip() != "")
            ]
            ecr_df = ecr_df[
                ecr_df.apply(
                    lambda r: r["role"].isascii() and str(r["company"]).isascii(), axis=1
                )
            ]
            ecr_df = ecr_df[
                ecr_df["role"]
                .str.lower()
                .str.contains(classify.TECH_KEYWORDS_RE, regex=True, na=False)
            ]
            ecr_df = ecr_df[
                ~ecr_df["company"]
                .str.lower()
                .str.strip()
                .isin(readme_generation.NORMALIZED_BLOCKED_COMPANIES)
            ]
            ecr_df["location"] = ecr_df["location"].map(
                lambda v: clean_location(str(v or ""))
            )
            ecr_df = ecr_df[ecr_df["date"].astype(str).str.strip() != ""]
            ecr_df = _stamp(ecr_df, "ecr", now)
            print(f"  ECR rows: {len(ecr_df):,}")
            ingest_stats.record("ecr", discovered=len(ecr_raw), valid=len(ecr_df))
        if ecr_df.empty:
            ecr_df = _load_cached("ecr")
        else:
            _save_cache(ecr_df, "ecr")
    else:
        ecr_df = _load_cached("ecr")
        if ARGS.ecr:
            print("  --ecr needs --tier light, medium or all; skipped")

    # ── Runway Explore (aggregator, shard-limited, opt-in) ───────────────────
    # Largest early-career-adjacent source found: 11 sitemap shards, ~510k URLs.
    # Content mix is broad (hourly roles included), so the tech/date/freshness
    # filters below are load-bearing here rather than cosmetic. Shard-limited by
    # default because a full crawl is ~35h at 0.25s/page.
    if ARGS.runway and ARGS.tier in (TIER_LIGHT, TIER_ALL):
        print("Fetching Runway Explore...")
        runway_shards = runway_source._parse_shards(ARGS.runway_shards)
        runway_raw = runway_source.harvest(
            runway_source.fetch_shard_urls(runway_shards),
            limit=ARGS.runway_limit or None,
        )
        runway_df = runway_raw.copy()
        if not runway_df.empty:
            runway_df["company"] = runway_df["company"].map(clean_company_name)
            runway_df = runway_df[
                (runway_df["company"].notna())
                & (runway_df["company"] != "")
                & (runway_df["role"].astype(str).str.strip() != "")
            ]
            runway_df = runway_df[
                runway_df.apply(
                    lambda r: str(r["role"]).isascii() and str(r["company"]).isascii(),
                    axis=1,
                )
            ]
            runway_df = runway_df[
                runway_df["role"]
                .str.lower()
                .str.contains(classify.TECH_KEYWORDS_RE, regex=True, na=False)
            ]
            # Then the stricter Runway-local gate. classify's shared regex includes
            # bare tokens (qa/it/ai/ml/data) that are fine on an engineering ATS
            # feed but leak "General Production" / "Grind" on a general job board.
            runway_df = runway_df[
                runway_df["role"].str.contains(runway_source.TECH_STRICT_RE, na=False)
            ]
            runway_df = runway_df[
                ~runway_df["company"]
                .str.lower()
                .str.strip()
                .isin(readme_generation.NORMALIZED_BLOCKED_COMPANIES)
            ]
            if "country_iso" in runway_df.columns:
                runway_df["country_iso"] = runway_df["country_iso"].fillna("")
                runway_df["country_iso"] = runway_df.apply(
                    lambda r: r["country_iso"]
                    or _infer_country(str(r.get("location") or "")),
                    axis=1,
                )
            runway_df["location"] = runway_df["location"].map(
                lambda v: clean_location(str(v or ""))
            )
            runway_df = runway_df[runway_df["date"].astype(str).str.strip() != ""]
            runway_df = _stamp(runway_df, "runway", now)
            print(f"  Runway rows: {len(runway_df):,}")
        if runway_df.empty:
            runway_df = _load_cached("runway")
        else:
            _save_cache(runway_df, "runway")
    else:
        runway_df = _load_cached("runway")
        if ARGS.runway:
            print("  --runway needs --tier light (or all); skipped")

    # ── Direct ATS probing (big tech supplement) ──────────────────────────────
    ATS_ENDPOINTS = {
        # pay_transparency=true adds pay_input_ranges (cents) to each posting.
        'Greenhouse': 'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&per_page=100&pay_transparency=true',
        'Lever': 'https://api.lever.co/v0/postings/{slug}?mode=json',
        'Ashby': 'https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true',
        'SmartRecruiters': 'https://api.smartrecruiters.com/v1/companies/{slug}/postings',
        'Workable': 'https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true',
        'Rippling': 'https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs',
        'Pinpoint': 'https://{slug}.pinpointhq.com/postings.json',
        'Recruitee': 'https://{slug}.recruitee.com/api/offers/',
        'Teamtailor': 'https://{slug}.teamtailor.com/jobs.json',
        'Breezy': 'https://{slug}.breezy.hr/json',
        'Personio': 'https://{slug}.jobs.personio.com/xml',
    }

    ATS_CSV_URL = 'https://raw.githubusercontent.com/Kayvan-Zahiri/state-of-ats-2026/main/data/companies.csv'
    ATS_PROBE_STATE_PATH = os.path.join(CACHE_DIR, "ats_probe_state.json")
    ATS_PROBE_COOLDOWN_HOURS = float(os.environ.get("SEARCHTERN_ATS_COOLDOWN_HOURS", "12"))

    # Boards jobhive doesn't scrape; verified by hand.
    # Workday slug is the CXS base path: {host}/wday/cxs/{tenant}/{site}
    EXTRA_BOARDS = [
        ("Ally", "Workday", "ally.wd1.myworkdayjobs.com/wday/cxs/ally/Ally"),
        ("Open (Personio)", "Personio", "open"),
        ("Personio", "Personio", "personio"),
    ]


    # Workday publishes no site-name registry: {tenant}.wdN hosts hide the CXS site
    # name in robots.txt Disallow paths. Strip the first segment per line, fall back
    # to the tenant/subdomain and its capitalization.
    _WD_HOST_RE = re.compile(r'^([^.]+)\.wd\d+\.myworkdayjobs\.com$')


    def _discover_workday_sites(host, tenant):


        candidates = []
        try:
            resp = SESSION.get(f"https://{host}/robots.txt", headers=_UA, timeout=8)
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
                resp = SESSION.post(
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


    def _greenhouse_pay(ranges):
        """Collapse pay_input_ranges (cents) to (min_dollars, max_dollars, currency).

        A posting can carry several ranges (one per location); the min of the
        minimums and max of the maximums bracket the posting. Returns Nones when
        the board publishes no ranges (most boards: 110 of Scale AI's 167 did).
        """
        mins, maxs, cur = [], [], None
        for r in ranges or []:
            try:
                lo = int(r.get('min_cents')) // 100 if r.get('min_cents') else None
                hi = int(r.get('max_cents')) // 100 if r.get('max_cents') else None
            except (TypeError, ValueError):
                continue
            if lo:
                mins.append(lo)
            if hi:
                maxs.append(hi)
            cur = cur or r.get('currency_type')
        if not mins and not maxs:
            return None, None, None
        return (min(mins) if mins else None, max(maxs) if maxs else None, cur)


    def _smartrecruiters_desc(job):
        sections = (job.get('jobAd') or {}).get('sections') or {}
        if isinstance(sections, list):
            for s in sections:
                if str(s.get('name') or '').lower() in ('jobdescription', 'description'):
                    return s.get('text') or ''
        desc = sections.get('jobDescription') or sections.get('description') or {}
        return desc.get('text') if isinstance(desc, dict) else ''


    # Employers whose Workday tenant answered HTTP 422 (tenant moved data centres)
    # this run. _fetch_ats_probe drops their cached WorkdaySlugs entries so the
    # next run rediscovers instead of failing until the cooldown expires.
    _WD_MOVED_COMPANIES = set()


    # New adapters live in ats_new_sources.py; discovered boards on them are
    # probed through the same _fetch_company_jobs entry point.
    _NEW_ATS = ("Eightfold", "iCIMS", "Oracle", "ADP", "BambooHR")


    def _fetch_new_ats_company(company, ats_type, slug):
        """Fetch one discovered board on a new ATS. Returns (company, ats, data).

        Rows come back already normalized (STD_COLS), carried as data["_rows"]
        so the probe loop can merge them without a second normalize pass.
        Slug shapes come from discover_boards.parse_link.
        """
        try:
            if ats_type == "Eightfold":
                rows, trunc, _ = ats_new_sources._fetch_eightfold_board(
                    company, f"{slug}.eightfold.ai", f"{slug}.com")
            elif ats_type == "iCIMS":
                rows, trunc, _ = ats_new_sources._fetch_icims_portal(company, slug)
            elif ats_type == "Oracle":
                host, _, site = slug.partition("|")
                if not host or not site:
                    return None
                rows, trunc, _ = ats_new_sources._fetch_oracle_board(
                    company, host, site)
            elif ats_type == "ADP":
                rows, trunc, _ = ats_new_sources._fetch_adp_domain(company, slug)
            elif ats_type == "BambooHR":
                rows, trunc, _ = ats_new_sources._fetch_bamboo_tenant(company, slug)
            else:
                return None
        except Exception:
            return None
        if not rows:
            return None
        return (company, ats_type, {"_rows": rows, "_truncated": int(bool(trunc))})


    def _fetch_company_jobs(company, ats_type, slug):
        if (ats_type or "") in _NEW_ATS:
            return _fetch_new_ats_company(company, ats_type, slug)
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
                while len(data['jobPostings']) < WORKDAY_MAX_JOBS:

                    resp = SESSION.post(
                        url.format(slug=slug),
                        json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
                        headers={**_UA, "Content-Type": "application/json"},
                        timeout=15,
                    )
                    if resp is None or resp.status_code != 200:
                        if resp is not None and resp.status_code == 422:
                            # Tenant moved data centres (wd1 -> wd108, ...); the
                            # cached site path is dead, not the board.
                            _WD_MOVED_COMPANIES.add(str(company or '').lower())
                        break
                    payload = resp.json()
                    if offset == 0 and isinstance(payload.get('total'), int):
                        data['totalReported'] = payload['total']
                    page = payload.get('jobPostings') or []
                    data['jobPostings'].extend(page)
                    if len(page) < 20:
                        break
                    offset += 20
                data['_truncated'] = int(
                    isinstance(data.get('totalReported'), int)
                    and data['totalReported'] > len(data['jobPostings'])
                )
                data['_missed'] = max(
                    0, (data.get('totalReported') or 0) - len(data['jobPostings'])
                )
                for jp in data['jobPostings']:
                    p = jp.get('externalPath') or ''
                    jp['externalPath'] = f"https://{host}{p}" if p else ''
            else:
                resp = http_get(url.format(slug=slug), timeout=15, retries=2)
                if resp is None or resp.status_code != 200:
                    return None
                # Teamtailor serves a JSON Feed (richer than its RSS: dates,
                # descriptions, schema.org locations per item); Personio serves XML.
                if ats_type == 'Teamtailor':
                    try:
                        first = resp.json()
                    except ValueError:
                        return None
                    items = list(first.get('items') or [])
                    nxt = first.get('next_url')
                    pages = 1
                    while nxt and pages < TEAMTAILOR_MAX_PAGES:
                        pr = http_get(nxt, timeout=15, retries=2)
                        if pr is None or pr.status_code != 200:
                            break
                        try:
                            payload = pr.json()
                        except ValueError:
                            break
                        page = payload.get('items') or []
                        if not page:
                            break
                        items.extend(page)
                        nxt = payload.get('next_url')
                        pages += 1
                    data = {**first, 'items': items,
                            '_truncated': int(bool(nxt))}
                elif ats_type == 'Personio':
                    data = {'xml': resp.text, 'slug': slug}
                elif ats_type == 'SmartRecruiters':
                    data = resp.json()
                    content = list(data.get('content') or [])
                    total = data.get('totalFound') or len(content)
                    offset = len(content)
                    while content and offset < min(total, SMARTRECRUITERS_MAX_JOBS):
                        page_resp = http_get(
                            f"{url.format(slug=slug)}?limit=100&offset={offset}",
                            timeout=15, retries=2,
                        )
                        if page_resp is None or page_resp.status_code != 200:
                            break
                        page = page_resp.json().get('content') or []
                        if not page:
                            break
                        content.extend(page)
                        offset += len(page)
                    data = {**data, 'content': content,
                            '_truncated': int(total > len(content))}
                elif ats_type == 'Greenhouse':
                    # Large boards may page (?page=N); meta carries the board
                    # total. Stop on duplicate ids: some boards ignore the page
                    # param and re-send page 1 (observed live 2026-11-02).
                    first = resp.json()
                    jobs = list(first.get('jobs') or [])
                    total = (first.get('meta') or {}).get('total') or len(jobs)
                    seen = {j.get('id') for j in jobs if j.get('id') is not None}
                    base = url.format(slug=slug)
                    page_no = 2
                    while jobs and len(jobs) < min(total, GREENHOUSE_MAX_JOBS):
                        page_resp = http_get(f"{base}&page={page_no}", timeout=15, retries=2)
                        if page_resp is None or page_resp.status_code != 200:
                            break
                        page = page_resp.json().get('jobs') or []
                        fresh = [j for j in page
                                 if j.get('id') is None or j.get('id') not in seen]
                        if not fresh:
                            break
                        jobs.extend(fresh)
                        seen.update(j.get('id') for j in fresh if j.get('id') is not None)
                        page_no += 1
                        if len(page) < 100:
                            break
                    data = {**first, 'jobs': jobs,
                            '_truncated': int(total > len(jobs))}
                else:
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
                sal_min, sal_max, sal_cur = _greenhouse_pay(j.get('pay_input_ranges'))
                rows.append({
                    'company': company, 'role': (j.get('title') or '').strip(),
                    'location': loc_str,
                    'date': j.get('updated_at', ''),
                    'link': j.get('absolute_url', ''),
                    'is_remote': is_remote,
                    'salary_min': sal_min, 'salary_max': sal_max,
                    'salary_currency': sal_cur,
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
                loc_str = (cats.get('location') or '').strip()
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
        elif ats_type == 'Workable':
            for j in data.get('jobs', []):
                locs = j.get('locations') or []
                first = locs[0] if locs else {}
                loc_str = ', '.join(filter(None, [
                    j.get('city') or first.get('city'),
                    j.get('state') or first.get('region'),
                    j.get('country') or first.get('country'),
                ])).strip()
                rows.append({
                    'company': company, 'role': (j.get('title') or '').strip(),
                    'location': loc_str,
                    'date': j.get('published_on', ''),
                    'link': j.get('url', ''),
                    'is_remote': str(bool(j.get('telecommuting'))).lower(),
                    'salary_min': None, 'salary_max': None, 'salary_currency': None,
                    'country_iso': _infer_country(loc_str),
                })
        elif ats_type == 'Rippling':
            for j in data if isinstance(data, list) else []:
                loc_str = ((j.get('workLocation') or {}).get('label') or '').strip()
                rows.append({
                    'company': company, 'role': (j.get('name') or '').strip(),
                    'location': loc_str,
                    'date': str(now),
                    'link': j.get('url', ''),
                    'is_remote': str('remote' in loc_str.lower()),
                    'salary_min': None, 'salary_max': None, 'salary_currency': None,
                    'country_iso': _infer_country(loc_str),
                })
        elif ats_type == 'Pinpoint':
            for j in data.get('data', []):
                loc_str = ((j.get('location') or {}).get('name') or '').strip()
                visible = bool(j.get('compensation_visible'))
                rows.append({
                    'company': company, 'role': (j.get('title') or '').strip(),
                    'location': loc_str,
                    'date': str(now),
                    'link': j.get('url', ''),
                    'is_remote': str((j.get('workplace_type') or '').lower() == 'remote'),
                    'salary_min': j.get('compensation_minimum') if visible else None,
                    'salary_max': j.get('compensation_maximum') if visible else None,
                    'salary_currency': j.get('compensation_currency') if visible else None,
                    'country_iso': _infer_country(loc_str),
                })
        elif ats_type == 'Recruitee':
            for j in data.get('offers', []):
                loc_str = (j.get('location') or '').strip()
                sal = j.get('salary') or {}
                rows.append({
                    'company': company, 'role': (j.get('title') or '').strip(),
                    'location': loc_str,
                    'date': j.get('published_at') or j.get('created_at') or '',
                    'link': j.get('careers_url') or j.get('careers_apply_url', ''),
                    'is_remote': str(bool(j.get('remote'))).lower(),
                    'salary_min': sal.get('min'), 'salary_max': sal.get('max'),
                    'salary_currency': sal.get('currency'),
                    'country_iso': _infer_country(loc_str),
                })
        elif ats_type == 'Teamtailor':
            rows.extend(_normalize_teamtailor(company, data))
        elif ats_type == 'Personio':
            rows.extend(_normalize_personio(company, data.get('slug', ''), data.get('xml', '')))
        elif ats_type == 'Breezy':
            for j in data if isinstance(data, list) else []:
                loc = j.get('location') or {}
                loc_str = (loc.get('name') or '').strip() or \
                    ((loc.get('country') or {}).get('name') or '').strip()
                rows.append({
                    'company': company, 'role': (j.get('name') or '').strip(),
                    'location': loc_str,
                    'date': j.get('published_date', ''),
                    'link': j.get('url', ''),
                    'is_remote': str(bool(loc.get('is_remote'))).lower(),
                    'salary_min': None, 'salary_max': None, 'salary_currency': None,
                    'country_iso': _infer_country(loc_str),
                })
        return rows


    def _normalize_teamtailor(company, data):
        """Normalize Teamtailor's JSON Feed (items carry schema.org JobPosting)."""
        rows = []
        items = (data.get('items') or []) if isinstance(data, dict) else []
        for item in items:
            jp = item.get('_jobposting') or {}
            loc_str = ''
            job_loc = jp.get('jobLocation')
            addr = None
            if isinstance(job_loc, list) and job_loc:
                addr = job_loc[0].get('address')
            elif isinstance(job_loc, dict):
                addr = job_loc.get('address')
            if isinstance(addr, dict):
                loc_str = ', '.join(filter(None, [
                    addr.get('addressLocality'), addr.get('addressRegion'),
                    addr.get('addressCountry')])).strip()
            pub = (item.get('date_published') or jp.get('datePosted') or '').strip()
            try:
                date = str(pd.Timestamp(pub)) if pub else ''
            except Exception:
                date = pub
            rows.append({
                'company': company,
                'role': ((item.get('title') or jp.get('title')) or '').strip(),
                'location': loc_str,
                'date': date,
                'link': (item.get('url') or '').strip(),
                'is_remote': str('remote' in loc_str.lower()),
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(loc_str),
                'description': _strip_html(
                    item.get('content_html') or jp.get('description') or ''),
            })
        return rows


    def _normalize_personio(company, slug, xml_text):
        rows = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return rows
        for pos in root.findall('position'):
            office = (pos.findtext('office') or '').strip()
            created = (pos.findtext('createdAt') or '').strip()
            try:
                date = str(pd.Timestamp(created)) if created else str(now)
            except Exception:
                date = str(now)
            job_id = (pos.findtext('id') or '').strip()
            link = (pos.findtext('careersUrl') or '').strip() or (
                f"https://{slug}.jobs.personio.com/job/{job_id}" if job_id else ''
            )
            rows.append({
                'company': company, 'role': (pos.findtext('name') or '').strip(),
                'location': office,
                'date': date,
                'link': link,
                'is_remote': str('remote' in office.lower()),
                'salary_min': None, 'salary_max': None, 'salary_currency': None,
                'country_iso': _infer_country(office),
            })
        return rows


    def _fetch_ats_probe(workers=None):
        print("Fetching ATS-probe company list...")
        _t0 = _time.monotonic()
        resp = http_get(ATS_CSV_URL, retries=2)
        if resp is None or resp.status_code != 200:
            print(
                f"  ATS CSV download failed: "
                f"{resp.status_code if resp is not None else 'unreachable'}"
            )
            return pd.DataFrame()

        lines = resp.text.strip().split('\n')
        data_lines = [line for line in lines if not line.startswith('#')]
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

        merged = discover_boards.merge_into_probe(probe_list)
        if len(merged) > len(probe_list):
            print(f"  +{len(merged) - len(probe_list)} boards discovered from past feeds")
        probe_list = merged

        cutoff = _time.time() - ATS_PROBE_COOLDOWN_HOURS * 3600
        warm = []
        for name, ats, slug in probe_list:
            ts = probe_state.get(f"{ats}|{slug}")
            if ts is None or ts <= cutoff:
                warm.append((name, ats, slug))
            else:
                probe_state.setdefault("_skipped_this_run", 0)
                probe_state["_skipped_this_run"] += 1
        if TIER_MEDIUM in RUN_TIERS:
            # --tier all refreshes the new adapters in the medium block above;
            # re-probing those same ~716 discovered boards here fetched every
            # one of them twice per run and threw the duplicate away at dedup.
            _already = [w for w in warm if w[1] in _NEW_ATS]
            if _already:
                warm = [w for w in warm if w[1] not in _NEW_ATS]
                print(f"  {len(_already)} new-ATS boards already refreshed by this "
                      f"run's medium tier — skipped")

        if warm:
            print(f"  {len(warm)} to probe now, {len(probe_list) - len(warm)} skipped by cooldown")

        all_raw = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers or WORKERS) as ex:
            futs = {ex.submit(_fetch_company_jobs, name, ats, slug): (ats, slug)
                    for name, ats, slug in warm}
            for fut in concurrent.futures.as_completed(futs):
                result = fut.result()
                if result:
                    all_raw.append(result)
                    ats, slug = futs[fut]
                    probe_state[f"{ats}|{slug}"] = _time.time()

        print(f"  Fetched {len(all_raw)}/{len(warm)} endpoints successfully")

        # Tenants that moved data centres: drop their discovery cache so the next
        # run re-probes robots.txt instead of replaying a dead site path.
        if _WD_MOVED_COMPANIES:
            for k in [k for k in probe_state if k.startswith("WorkdaySlugs|")]:
                if k.split("|", 1)[1].lower() in _WD_MOVED_COMPANIES:
                    del probe_state[k]
            print(f"  {len(_WD_MOVED_COMPANIES)} moved Workday tenants rediscovered next run")
            _WD_MOVED_COMPANIES.clear()

        # Record successes for cooldown.
        if "_skipped_this_run" in probe_state:
            del probe_state["_skipped_this_run"]
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(ATS_PROBE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(probe_state, f)

        all_rows = []
        for company, ats_type, data in all_raw:
            if isinstance(data, dict) and "_rows" in data:
                # New-ATS boards arrive already normalized; their id shapes are
                # not yet verified against jobhive, so they cast no closure vote.
                all_rows.extend(data["_rows"])
                continue
            rows = _normalize_ats_jobs(company, ats_type, data)
            all_rows.extend(rows)
            # A board that returned rows is a complete snapshot for that employer,
            # so its ids can verify jobhive liveness (closure.py). Workday has
            # no URL-tail id: use the CXS requisition ids from the raw postings.
            if (ats_type or "").strip().lower() == "workday":
                closure.record_snapshot(
                    ats_type, company,
                    closure.req_ids_from_workday_postings((data or {}).get("jobPostings")),
                )
            else:
                closure.record_snapshot(
                    ats_type, company, closure.ids_from_links(r.get("link") for r in rows)
                )

        print(f"  Total raw jobs: {len(all_rows):,}")
        if not all_rows:
            ingest_stats.record("ats-probe", status="empty", seconds=_time.monotonic() - _t0,
                                note="every endpoint failed or returned nothing")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df = _classify_freehire(df)
        print(f"  After classification: {len(df):,}")
        _trunc = sum(1 for _, _, _d in all_raw
                     if isinstance(_d, dict) and _d.get("_truncated"))
        _missed = sum(int((_d or {}).get("_missed") or 0)
                      for _, _, _d in all_raw if isinstance(_d, dict))
        if _trunc:
            print(f"  {_trunc} boards hit a pagination cap "
                  f"({_missed:,} postings beyond the caps, see truncated_boards)")
        ingest_stats.record(
            "ats-probe", discovered=len(all_rows), valid=len(df),
            seconds=_time.monotonic() - _t0, boards_ok=len(all_raw), boards_tried=len(warm),
            boards_skipped=len(probe_list) - len(warm), truncated_boards=_trunc,
            note=f"{len(probe_list)} boards in registry; {_missed} postings beyond caps",
        )
        return df


    # ── Direct ATS probing, priority fast lane ───────────────────────────────────
    # A curated set of hot companies (see priority_boards.py) is polled on every
    # run, bypassing the heavy tier's 24h cooldown so their newest postings hit the
    # feed within minutes. The list is intentionally small and verified by hand.
    def _fetch_priority_probe(workers=None):
        boards = priority_boards.boards()
        _t0 = _time.monotonic()
        print(f"Fetching {len(boards)} priority ATS boards (cooldown-exempt)...")
        all_raw = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers or WORKERS) as ex:
            futs = [ex.submit(_fetch_company_jobs, name, ats, slug)
                    for name, ats, slug in boards]
            for fut in concurrent.futures.as_completed(futs):
                result = fut.result()
                if result:
                    all_raw.append(result)

        print(f"  Fetched {len(all_raw)}/{len(boards)} priority endpoints successfully")

        all_rows = []
        for company, ats_type, data in all_raw:
            rows = _normalize_ats_jobs(company, ats_type, data)
            all_rows.extend(rows)
            if (ats_type or "").strip().lower() == "workday":
                closure.record_snapshot(
                    ats_type, company,
                    closure.req_ids_from_workday_postings((data or {}).get("jobPostings")),
                )
            else:
                closure.record_snapshot(
                    ats_type, company, closure.ids_from_links(r.get("link") for r in rows)
                )

        print(f"  Total raw priority jobs: {len(all_rows):,}")
        if not all_rows:
            ingest_stats.record("priority-probe", status="empty", seconds=_time.monotonic() - _t0,
                                note="every priority board failed")
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        df = _classify_freehire(df)
        print(f"  After classification: {len(df):,}")
        _trunc = sum(1 for _, _, _d in all_raw
                     if isinstance(_d, dict) and _d.get("_truncated"))
        ingest_stats.record(
            "priority-probe", discovered=len(all_rows), valid=len(df),
            seconds=_time.monotonic() - _t0, boards_ok=len(all_raw), boards_tried=len(boards),
            truncated_boards=_trunc,
        )
        return df


    # ── Freehire (medium tier) ────────────────────────────────────────────────────
    # Fetch tech internships + entry-level roles from freehire.me (covers 75+ ATS)
    fh_df = pd.DataFrame()
    if TIER_MEDIUM in RUN_TIERS:
        print("Fetching freehire data...")
        fh_raw = _fetch_freehire(FREEHIRE_INTERN_API, workers=WORKERS)
        fh_raw += _fetch_freehire(FREEHIRE_NEWGRAD_API, workers=WORKERS)
        fh_df = _normalize_freehire(fh_raw)
        fh_df = _classify_freehire(fh_df)
        fh_df = _stamp(fh_df, "freehire", now)
        print(f"  Freehire raw fetched: {len(fh_raw):,}, classified: {len(fh_df):,}")
        ingest_stats.record("freehire", discovered=len(fh_raw), valid=len(fh_df))
        if fh_df.empty:
            fh_df = _load_cached(TIER_MEDIUM)
        else:
            _save_cache(fh_df, TIER_MEDIUM)
    else:
        fh_df = _load_cached(TIER_MEDIUM)
    print(f"  Freehire working set: {len(fh_df):,} rows")

    source_contrib = {TIER_LIGHT: [0, 0], TIER_MEDIUM: [0, 0], TIER_HEAVY: [0, 0], TIER_PRIORITY: [0, 0]}

    # Remove jobs already covered by jobhive
    if not fh_df.empty:
        fh_df = _dedup_across(readme_result, fh_df)
        print(f"  After dedup vs jobhive: {len(fh_df):,}")

        # Split freehire data — README (with country/ASCII filters), listings (broader)
        fh_for_readme = fh_df[
            ~fh_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                         'NL','IT','ES','PT','RO','HU','CZ','SK',
                                         'HR','BG','FI','LU','BE','MT','CY'])
            & (_to_dt(fh_df["date"]) >= now - pd.Timedelta(days=60))
            & fh_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        fh_for_listings = fh_df[
            _to_dt(fh_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()
        source_contrib[TIER_MEDIUM] = [len(fh_for_readme), len(fh_for_listings)]

        readme_result = pd.concat([readme_result, fh_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, fh_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  Freehire added: {source_contrib[TIER_MEDIUM][0]} to README, {source_contrib[TIER_MEDIUM][1]} to listings")

    # ── Breadth feeds (medium tier) ──────────────────────────────────────────────
    # Remote-first aggregators broaden coverage beyond freehire's company set.
    breadth_df = pd.DataFrame()
    if TIER_MEDIUM in RUN_TIERS:
        print("Fetching breadth feeds...")
        _breadth_stats = []
        breadth_df, _breadth_stats = breadth_sources.fetch_and_parse(
            infer_country=_infer_country, workers=WORKERS
        )
        for _name, _rows in _breadth_stats:
            ingest_stats.record(f"breadth:{_name}", discovered=_rows)
        breadth_df = _classify_freehire(breadth_df)
        breadth_df = _stamp(breadth_df, "breadth", now)
        print(f"  Breadth classified: {len(breadth_df):,}")
        if breadth_df.empty:
            breadth_df = _load_cached("breadth")
        else:
            _save_cache(breadth_df, "breadth")
    else:
        breadth_df = _load_cached("breadth")
    print(f"  Breadth working set: {len(breadth_df):,} rows")

    if not breadth_df.empty:
        breadth_df = _dedup_across(readme_result, breadth_df)
        b_for_readme = breadth_df[
            ~breadth_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                             'NL','IT','ES','PT','RO','HU','CZ','SK',
                                             'HR','BG','FI','LU','BE','MT','CY'])
            & (_to_dt(breadth_df["date"]) >= now - pd.Timedelta(days=60))
            & breadth_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        b_for_listings = breadth_df[
            _to_dt(breadth_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()
        source_contrib[TIER_MEDIUM][0] += len(b_for_readme)
        source_contrib[TIER_MEDIUM][1] += len(b_for_listings)

        readme_result = pd.concat([readme_result, b_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, b_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  Breadth added: {len(b_for_readme)} to README, {len(b_for_listings)} to listings")

    # ── New ATS adapters (medium tier) ───────────────────────────────────────────
    # Eightfold, Oracle Recruiting Cloud, iCIMS, ADP, BambooHR. All public
    # endpoints, verified live 2026-11-02 (docs/reverse-engineering/ats-expansion.md).
    # Per-board caps, delays, truncation and rate-limit reporting live in
    # ats_new_sources.py; ingest_stats recording happens there (with seconds), so
    # this block only classifies, stamps and merges like the other tiers.
    ats_new_df = pd.DataFrame()
    if TIER_MEDIUM in RUN_TIERS:
        print("Fetching new ATS boards (Eightfold/Oracle/iCIMS/ADP/BambooHR)...")
        ats_new_df, _ats_new_stats = ats_new_sources.fetch_and_parse(
            infer_country=_infer_country, workers=WORKERS
        )
        for _name, _rows in _ats_new_stats:
            print(f"  {_name}: {_rows:,} rows")
        ats_new_df = _classify_freehire(ats_new_df)
        ats_new_df = _stamp(ats_new_df, "ats", now)
        print(f"  New ATS classified: {len(ats_new_df):,}")
        if ats_new_df.empty:
            ats_new_df = _load_cached("ats_new")
        else:
            _save_cache(ats_new_df, "ats_new")
    else:
        ats_new_df = _load_cached("ats_new")
    print(f"  New ATS working set: {len(ats_new_df):,} rows")

    if not ats_new_df.empty:
        ats_new_df = _dedup_across(readme_result, ats_new_df)
        n_for_readme = ats_new_df[
            ~ats_new_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                         'NL','IT','ES','PT','RO','HU','CZ','SK',
                                         'HR','BG','FI','LU','BE','MT','CY'])
            & (_to_dt(ats_new_df["date"]) >= now - pd.Timedelta(days=60))
            & ats_new_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        n_for_listings = ats_new_df[
            _to_dt(ats_new_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()
        source_contrib[TIER_MEDIUM][0] += len(n_for_readme)
        source_contrib[TIER_MEDIUM][1] += len(n_for_listings)

        readme_result = pd.concat([readme_result, n_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, n_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  New ATS added: {len(n_for_readme)} to README, {len(n_for_listings)} to listings")

    # ── Direct ATS probing (heavy tier) ──────────────────────────────────────────
    ats_df = pd.DataFrame()
    if TIER_HEAVY in RUN_TIERS:
        print("Fetching ATS probe data...")
        probe_df = _fetch_ats_probe(workers=WORKERS)
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
            & (_to_dt(ats_df["date"]) >= now - pd.Timedelta(days=60))
            & ats_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        ats_for_listings = ats_df[
            _to_dt(ats_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()
        source_contrib[TIER_HEAVY] = [len(ats_for_readme), len(ats_for_listings)]

        readme_result = pd.concat([readme_result, ats_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, ats_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  ATS probe added: {source_contrib[TIER_HEAVY][0]} to README, {source_contrib[TIER_HEAVY][1]} to listings")

    # ── Direct ATS probing (priority fast lane) ──────────────────────────────────
    # Runs on every priority cycle (or as part of --tier all) with no cooldown, so
    # the refreshed rows are always the newest; the parquet cache lets a
    # priority-only run reuse the (unchanged) heavy/medium rows.
    priority_df = pd.DataFrame()
    if TIER_PRIORITY in RUN_TIERS:
        print("Fetching priority ATS data...")
        pri_probe_df = _fetch_priority_probe(workers=WORKERS)
        if pri_probe_df is None:
            pri_probe_df = pd.DataFrame()
        if not pri_probe_df.empty:
            pri_probe_df = _stamp(pri_probe_df, "priority", now)
        if pri_probe_df.empty:
            previous = _load_cached(TIER_PRIORITY)
            if not previous.empty:
                print(f"  Priority fetch empty: reusing {len(previous):,} cached priority rows")
                priority_df = previous
            else:
                priority_df = pri_probe_df
        else:
            priority_df = pri_probe_df
        _save_cache(priority_df, TIER_PRIORITY)
    else:
        priority_df = _load_cached(TIER_PRIORITY)
    print(f"  Priority working set: {len(priority_df):,} rows")

    if not priority_df.empty:
        pri_for_readme = priority_df[
            ~priority_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                              'NL','IT','ES','PT','RO','HU','CZ','SK',
                                              'HR','BG','FI','LU','BE','MT','CY'])
            & (_to_dt(priority_df["date"]) >= now - pd.Timedelta(days=60))
            & priority_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        pri_for_listings = priority_df[
            _to_dt(priority_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()
        source_contrib[TIER_PRIORITY] = [len(pri_for_readme), len(pri_for_listings)]

        # Priority rows are the freshest copies (polled with no cooldown), so they
        # must win over a staler cached ATS/probe copy of the same posting. Drop the
        # base row a priority row supersedes before appending, rather than dropping
        # the priority row (which is what the other tiers do — they run after and
        # only add genuinely new companies).
        readme_result = _dedup_across(pri_for_readme, readme_result)
        readme_result = pd.concat([readme_result, pri_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = _dedup_across(pri_for_listings, listings_result)
        listings_result = pd.concat([listings_result, pri_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  Priority added: {source_contrib[TIER_PRIORITY][0]} to README, {source_contrib[TIER_PRIORITY][1]} to listings")

    # ── Verified closure (jobhive rows a completed board no longer lists) ───────
    # jobhive never removes a closed posting and carries no status field, so age is
    # its only liveness signal (docs/reverse-engineering/jobhive-staleness.md). The
    # boards crawled above know their live ids; a jobhive row whose employer board
    # was re-crawled this run and whose id is gone is closed. Boards that failed or
    # returned nothing recorded no vote, so they can never delete their own rows.
    _cov = closure.covered()
    if _cov["boards"]:
        _before = len(readme_result) + len(listings_result)
        readme_result, _rd_dropped, _rd_unver = closure.filter_closed(readme_result)
        listings_result, _ld_dropped, _ld_unver = closure.filter_closed(listings_result)
        print(f"  Closure: dropped {_rd_dropped:,} README + {_ld_dropped:,} listings rows "
              f"against {_cov['boards']} boards / {_cov['live_ids']:,} live ids "
              f"(unverifiable: {_rd_unver:,} + {_ld_unver:,})")
        ingest_stats.record("jobhive-closure", discovered=_before,
                            valid=_before - _rd_dropped - _ld_dropped,
                            dropped=_rd_dropped + _ld_dropped,
                            boards_voted=_cov["boards"], live_ids=_cov["live_ids"])
    # The join columns exist only to answer that question — never publish them.
    readme_result = closure.drop_vote_columns(readme_result)
    listings_result = closure.drop_vote_columns(listings_result)

    # ── Markdown GitHub sources (light tier, no API calls) ──────────────────────
    md_df = pd.DataFrame()
    md_source_stats = []
    if TIER_LIGHT in RUN_TIERS:
        md_df, md_source_stats = markdown_sources.fetch_and_parse(
            infer_country=_infer_country, workers=WORKERS
        )
        if md_df is None:
            md_df = pd.DataFrame()
        if not md_df.empty:
            md_df["observed_at"] = now.isoformat()
            print(f"  Markdown unique rows: {len(md_df):,}")
            for _name, _rows in md_source_stats:
                ingest_stats.record(f"md:{_name}", discovered=_rows)
        if md_df.empty:
            md_df = _load_cached(TIER_LIGHT)
        else:
            _save_cache(md_df, TIER_LIGHT)
    else:
        md_df = _load_cached(TIER_LIGHT)
    print(f"  Markdown working set: {len(md_df):,} rows")

    pre_md_listings_keys = set(_dedup_keys(listings_result))
    if not md_df.empty:
        md_df = _dedup_across(readme_result, md_df)
        print(f"  After dedup vs existing sources: {len(md_df):,}")
        md_for_readme = md_df[
            ~md_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                        'NL','IT','ES','PT','RO','HU','CZ','SK',
                                        'HR','BG','FI','LU','BE','MT','CY'])
            & (_to_dt(md_df["date"]) >= now - pd.Timedelta(days=60))
            & md_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        md_for_listings = md_df[
            _to_dt(md_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()
        source_contrib[TIER_LIGHT] = [len(md_for_readme), len(md_for_listings)]

        readme_result = pd.concat([readme_result, md_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, md_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  Markdown added: {source_contrib[TIER_LIGHT][0]} to README, {source_contrib[TIER_LIGHT][1]} to listings")

    TECH_KEYWORDS_RE = classify.TECH_KEYWORDS_RE


    def _sitemap_tech_mask(frame):
        """Tech-keyword filter, resolved lazily.

        Kept as a named helper so the sitemap stage reads clearly; classify owns
        the regex, and it is loaded by the time this runs.
        """
        return frame["role"].str.lower().str.contains(
            classify.TECH_KEYWORDS_RE, regex=True, na=False
        )
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
            & (_to_dt(sx_df["date"]) >= now - pd.Timedelta(days=60))
            & sx_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        sx_for_listings = sx_df[
            _to_dt(sx_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()

        readme_result = pd.concat([readme_result, sx_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, sx_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
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

    # ── Merge opt-in supplementary rows into listings.json ────────────────────
    def _merge_supplementary(label, frame):
        """Dedupe a supplementary frame against listings and append what survives.

        Shared by the sitemap and Early Career Radar stages: both are opt-in feeds
        that run after the main markdown tier, so they merge last and get the same
        freshness, geography, and cross-source dedupe treatment.
        """
        global listings_result
        if frame is None or frame.empty:
            return
        frame = _dedup_across(listings_result, frame)
        print(f"  {label} after dedup vs listings: {len(frame):,}")
        keep = frame.copy()
        if "date" in keep.columns:
            keep = keep[_to_dt(keep["date"]) >= now - pd.Timedelta(days=60)]
        if "description" not in keep.columns:
            keep["description"] = None
        existing = set(_dedup_keys(listings_result))
        net_new = set(_dedup_keys(keep)) - existing
        listings_result = pd.concat([listings_result, keep], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  {label} added: {len(keep):,} rows ({len(net_new):,} net-new) to listings")


    _merge_supplementary("Sitemap", sitemap_df)
    _merge_supplementary("ECR", ecr_df)
    _merge_supplementary("Runway", runway_df)

    if not ej_df.empty:
        ej_df = _dedup_across(readme_result, ej_df)
        print(f"  EchoJobs after dedup vs existing sources: {len(ej_df):,}")
        ej_for_readme = ej_df[
            ~ej_df["country_iso"].isin(['DE','AT','CH','FR','PL','NO','SE','DK',
                                        'NL','IT','ES','PT','RO','HU','CZ','SK',
                                        'HR','BG','FI','LU','BE','MT','CY'])
            & (_to_dt(ej_df["date"]) >= now - pd.Timedelta(days=60))
            & ej_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        ej_for_listings = ej_df[
            _to_dt(ej_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()

        readme_result = pd.concat([readme_result, ej_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, ej_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  EchoJobs added: {len(ej_for_readme)} to README, {len(ej_for_listings)} to listings")

    # ── JobSpy boards (indeed/ziprecruiter/google) ────────────────────────────
    js_df = pd.DataFrame()
    if TIER_LIGHT in RUN_TIERS:
        _js_df, _js_stats = jobspy_source.fetch_and_parse(infer_country=_infer_country)
        js_df = _js_df
        for _site, _rows in _js_stats:
            ingest_stats.record(_site, discovered=_rows)
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
            & (_to_dt(js_df["date"]) >= now - pd.Timedelta(days=60))
            & js_df["role"].str.match(r'^[^\x80-\xFF]+$', na=False)
        ].copy()
        js_for_listings = js_df[
            _to_dt(js_df["date"]) >= now - pd.Timedelta(days=90)
        ].copy()

        readme_result = pd.concat([readme_result, js_for_readme], ignore_index=True)
        readme_result = _dedupe(readme_result)
        listings_result = pd.concat([listings_result, js_for_listings], ignore_index=True)
        listings_result = _dedupe(listings_result)
        print(f"  JobSpy added: {len(js_for_readme)} to README, {len(js_for_listings)} to listings")

    _us_loc_re = re.compile(r'\b(?:US|USA|U\.S\.A\.|United States|California|Texas|New York|Washington|Seattle|San Francisco|SF|NYC|Austin|Chicago|Boston|Mountain View|Palo Alto|Sunnyvale|Los Angeles|Irvine|San Diego|Santa Clara|Cupertino|Menlo Park|Redmond|Kirkland|Bellevue|Arlington|McLean|Reston|Atlanta|Denver|Portland|Phoenix|Philadelphia|Pittsburgh|Minneapolis|Ann Arbor|Detroit|Miami|Orlando|Tampa|Dallas|Houston|Raleigh|Durham|Charlotte|Nashville|Salt Lake City|St Louis|Kansas City|Columbus|Indianapolis|Milwaukee|Baltimore|Portland)\b', re.IGNORECASE)

    def _feed_scope_filter(df):
        """Keep located US, international, and remote roles; deduplicate by URL."""
        df = df.fillna({"country_iso": ""})
        remote = df["location"].str.contains(r"\bremote\b", case=False, na=False)
        if "is_remote" in df.columns:
            remote = remote | df["is_remote"].astype(str).str.lower().isin(["true", "1"])
        df = df[
            (df["country_iso"] != "")
            | df["location"].str.contains(_us_loc_re, na=False)
            | remote
        ]
        df = df[df["location"].notna() & (df["location"] != "")]
        # Cross-source URL-canonical dedup (same posting from two sources collapses).
        if not df.empty and "link" in df.columns:
            _canon = df["link"].map(canonical_url)
            _no_canon = _canon == ""
            _with_canon = df[~_no_canon].copy()
            _with_canon["_canon"] = _canon[~_no_canon]
            _with_canon = _with_canon.drop_duplicates(subset="_canon", keep="first").drop(columns="_canon")
            df = pd.concat([df[_no_canon], _with_canon], ignore_index=True)
        return df


    role_lower = listings_result["role"].str.lower()
    tech_mask = role_lower.str.contains(TECH_KEYWORDS_RE, regex=True, na=False)

    # searchtern.json keeps every role type; listings.json is the CS/tech subset.
    # Both retain located international and remote roles as well as US postings.
    searchtern_result = _feed_scope_filter(listings_result)
    listings_result = _feed_scope_filter(listings_result[tech_mask])

    # ── H-1B sponsor enrichment (Tier C) ────────────────────────────────────────
    # visa_sponsor column sourced from USCIS employer data; "" means no signal
    # rather than "does not sponsor". Runs regardless of --tier (one cached
    # map is shared by every tier) and degrades to the last good cache if USCIS is
    # unreachable, so a missing column never blocks a run.
    try:
        _h1b_approvals = h1b_enrich.load_approvals()
    except Exception as _e:
        print(f"  H-1B enrichment skipped: {_e}")
        _h1b_approvals = {}

    _h1b_roles = {}
    try:
        _role_targets = (readme_result["company"].astype(str).value_counts()
                         .head(h1b_enrich.ROLE_MAX_COMPANIES).index.tolist())
        _h1b_roles = h1b_enrich.load_roles(_role_targets)
    except Exception as _e:
        print(f"  H-1B role data skipped: {_e}")

    if _h1b_approvals:
        readme_result = h1b_enrich.add_visa_column(readme_result, _h1b_approvals)
        searchtern_result = h1b_enrich.add_visa_column(searchtern_result, _h1b_approvals)
        listings_result = h1b_enrich.add_visa_column(listings_result, _h1b_approvals)
        if _h1b_roles:
            readme_result = h1b_enrich.add_role_columns(readme_result, _h1b_roles)
            searchtern_result = h1b_enrich.add_role_columns(searchtern_result, _h1b_roles)
            listings_result = h1b_enrich.add_role_columns(listings_result, _h1b_roles)

    try:
        h1b_enrich.write_h1b_json(_h1b_approvals, _h1b_roles, output_dir="..")
    except Exception as _e:
        print(f"  h1b.json skipped: {_e}")

    # ── Feed digest + closure tracking (B) ─────────────────────────────────────
    # Diff this run's rows against cache/seen.json. Sources re-fetched this run
    # vote on closures; cached-only sources are left alone (their rows may just
    # be outside the run's lookback window).
    def _row_source(row):
        src = str(row.get("source") or "").strip()
        return src if src in ("jobhive", "freehire", "breadth", "ecr", "ats", "priority") else "md"


    def _seen_bucket(src):
        return src if src in ("jobhive", "freehire", "breadth", "ecr", "ats", "priority") else "md"


    buckets_fresh = {
        "jobhive": True,
        "freehire": TIER_MEDIUM in RUN_TIERS,
        "breadth": TIER_MEDIUM in RUN_TIERS,
        "ecr": TIER_MEDIUM in RUN_TIERS,
        "ats": TIER_HEAVY in RUN_TIERS,
        "priority": TIER_PRIORITY in RUN_TIERS,
        "md": TIER_LIGHT in RUN_TIERS,
    }

    final = listings_result.copy()
    current_by_key = {}
    for _idx, row in final.iterrows():
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
    for k in current_by_key:
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

    # Per-source observability: what each adapter was handed, what survived, how
    # long it took and how many of its rows were genuinely new after dedup. The
    # published feed cannot answer that (dedup makes every source look sole-source),
    # so this artifact is the only way to rank a source's value.
    _new_by_source = {}
    for _k in is_new:
        _src = _row_source(current_by_key[_k])
        _new_by_source[_src] = _new_by_source.get(_src, 0) + 1
    ingest_stats.merge_digest(_new_by_source, closed=len(closed), dead_links=len(dead_keys))
    ingest_stats.write_json(
        os.path.join(root_dir, "pages", "source_stats.json"),
        run_meta={
            "tier": ARGS.tier,
            "workers": WORKERS,
            "tiers_run": RUN_TIERS,
            "published": {"listings": len(listings_result),
                          "searchtern": len(searchtern_result)},
        },
    )
    print(ingest_stats.summary_line())

    # Persistent health: remember this run, then report failing/quiet sources
    # and the trailing-7d new-postings ranking.
    source_health.append(ingest_stats.snapshot(), _new_by_source)
    _health = source_health.report()
    for _f in _health["failures"]:
        print(f"  HEALTH ALERT: {_f['source']} non-ok for the last runs "
              f"(latest: {_f['status']})")
    for _q in _health["quiet"]:
        print(f"  HEALTH QUIET: {_q['source']} no data for {_q['hours_since_last']}h")
    if _health["best"]:
        _top = ", ".join(f"{b['source']} ({b['new_7d']})"
                          for b in _health["best"][:5])
        print(f"  HEALTH top-7d new: {_top}")

    # Sources manifest — every source the pipeline pulls from, with current row
    # counts. Served to the SearchTern web app for its "Data Sources" panel.
    API_SOURCES = [
        {"name": "Jobhive (ATS)", "url": "https://storage.stapply.ai/jobhive/v1/manifest.json", "type": "internship/newgrad", "count_key": "jobhive"},
        {"name": "freehire.me", "url": "https://freehire.me", "type": "internship/newgrad", "count_key": "freehire"},
        {"name": "Breadth feeds", "url": "https://github.com/KSaifStack/SearchTern-Listings/blob/main/Listings/breadth_sources.py", "type": "internship/newgrad", "count_key": "breadth"},
        {"name": "Early Career Radar", "url": "https://earlycareerradar.com/summer-internships", "type": "internship/newgrad", "count_key": "ecr"},
        {"name": "EchoJobs", "url": "https://echojobs.io", "type": "internship/tech", "count_key": "echojobs"},
        {"name": "Jobspy (Indeed)", "url": "https://github.com/zanellia/jobspy", "type": "internship", "count_key": "jobspy"},
        {"name": "ATS career-page probe", "url": "", "type": "internship/newgrad", "count_key": "ats"},
        {"name": "New ATS boards (Eightfold/Oracle/iCIMS/ADP/BambooHR/WorkforceNow)", "url": "https://github.com/KSaifStack/SearchTern-Listings/blob/main/Listings/ats_new_sources.py", "type": "internship/newgrad", "count_key": "ats"},
        {"name": "Priority ATS (hot companies)", "url": "https://github.com/KSaifStack/SearchTern-Listings/blob/main/Listings/priority_boards.py", "type": "all roles, refreshed every 15 min", "count_key": "priority"},
    ]
    sources_out = [
        {
            "name": s["name"],
            "url": s["url"],
            "type": s["type"],
            "season": s.get("season", ""),
            "count": int(per_source.get(s["name"], 0)),
        }
        for s in markdown_sources.MARKDOWN_SOURCES
    ] + [
        {
            "name": s["name"],
            "url": s["url"],
            "type": s["type"],
            "season": "",
            "count": int(per_source.get(s["count_key"], 0)),
        }
        for s in API_SOURCES
    ]
    sources_out.sort(key=lambda s: s["count"], reverse=True)
    sources_manifest = {
        "generated_at": obs,
        "count": len(sources_out),
        "sources": sources_out,
    }
    with open(os.path.join(root_dir, "pages", "sources.json"), "w", encoding="utf-8") as f:
        json.dump(sources_manifest, f, indent=2, ensure_ascii=False)
    print(f"  Sources manifest: {len(sources_out)} sources")
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

    # Rows with no scraped description get a synthesized one so the feed and
    # Google-for-Jobs JSON-LD always carry substantive text.
    _TYPE_LABEL = {
        "internship": "Internship",
        "new_grad": "New grad",
        "newgrad": "New grad",
        "full_time": "Full-time",
        "entry_level": "Early-career",
    }


    def _synthesize_desc(row):
        label = _TYPE_LABEL.get(str(row.get("job_type") or "").lower(), "Position")
        loc = str(row.get("location") or "").strip() or "United States (Remote)"
        remote = " Remote-eligible." if "remote" in loc.lower() else ""
        return (
            f"{label} opening: {row['role']} at {row['company']}. "
            f"Based in {loc}.{remote} "
            f"Apply directly through {row['company']}'s careers site."
        )


    if "description" in listings_result.columns:
        empty = listings_result["description"].isna() | (
            listings_result["description"].astype(str).str.strip() == ""
        )
        listings_result.loc[empty, "description"] = listings_result.loc[empty].apply(_synthesize_desc, axis=1)
        print(
            f"  Description after synthesize: {listings_result['description'].notna().sum():,}/{len(listings_result):,} rows"
        )

    # One dtype per column before anything is published — the JSON writer,
    # README tables and every downstream consumer get floats/strings, never a
    # mix, so a single odd adapter value cannot fail the run at write time.
    listings_result = canonical.canonicalize(listings_result)
    readme_result = canonical.canonicalize(readme_result)
    searchtern_result = canonical.canonicalize(searchtern_result)
    if not priority_df.empty:
        priority_df = canonical.canonicalize(priority_df)

    readme_role_lower = readme_result["role"].str.lower()
    readme_tech_mask = readme_role_lower.str.contains(TECH_KEYWORDS_RE, regex=True, na=False)
    readme_result = readme_result[readme_tech_mask]
    readme_generation.generate_readme(readme_result, output_dir="..")
    readme_generation.write_listings_json(
        listings_result, output_dir="..", mirror_dataframe=searchtern_result
    )

    _d_new, _d_total = discover_boards.update_state(searchtern_result)
    if _d_new:
        print(f"  Board discovery: +{_d_new} new employer ATS boards ({_d_total} tracked)")

    # Small high-frequency "fast lane" feed: recent postings from the curated
    # priority boards only, so a frequent workflow can refresh it without
    # committing the multi-MB main feeds.
    if not priority_df.empty:
        _pri_window = _to_dt(priority_df["date"]) >= now - pd.Timedelta(days=7)
        readme_generation.write_priority_json(priority_df[_pri_window], output_dir="..")

    readme_generation.write_feed_xml(searchtern_result, output_dir="..")

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
    r_pri, l_pri = source_contrib[TIER_PRIORITY][0], source_contrib[TIER_PRIORITY][1]
    print("\n--- Markdown source pull counts ---")
    for name, count in md_source_stats:
        print(f"  {name:<28} {count:>5,} rows")
    print("\n--- README stats ---")
    r_total = len(readme_result)
    r_internships = (readme_result["job_type"] == "internship").sum()
    r_new_grads = (readme_result["job_type"] == "new_grad").sum()
    print(f"Total listings  : {r_total:,}  (+{r_fh} freehire+indeed, +{r_ats} ats-probe, +{r_pri} priority, +{r_md} markdown)")
    print(f"  Internships   : {int(r_internships):,}")
    print(f"  New grad      : {int(r_new_grads):,}")

    print("\n--- Listings.json stats ---")
    l_total = len(listings_result)
    l_internships = (listings_result["job_type"] == "internship").sum()
    l_new_grads = (listings_result["job_type"] == "new_grad").sum()
    l_remote = (listings_result["is_remote"].astype(str).str.lower() == "true").sum()
    numeric_salary = pd.to_numeric(listings_result["salary_min"], errors='coerce')
    l_paid = (numeric_salary > 0).sum()
    print(f"Total listings  : {l_total:,}  (+{l_fh} freehire+indeed, +{l_ats} ats-probe, +{l_pri} priority, +{l_md} markdown)")
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
            _to_dt(md_final["date"]) >= now - pd.Timedelta(days=60)
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

        md_keys = set(_dedup_keys(md_final))
        md_new = md_keys - pre_md_listings_keys
        print(f"  Markdown rows surviving full json filter : {len(md_final):,}")
        print(f"  Net-new markdown rows in listings.json   : {len(md_new):,}")


if __name__ == "__main__":
    main()
