"""Early Career Radar as a listings source.

earlycareerradar.com is an aggregator that republishes employer postings rather
than proxying the employer's ATS. Every job lives at /jobs/job_<hex> (or
/new-grad/jobs/job_<hex> for new-grad roles) and embeds schema.org JobPosting
JSON-LD, so extraction is the same path sitemap_sources already uses. The
sitemap is advertised in robots.txt and enumerates the full catalog.

Two differences from a career-site sitemap make it its own module rather than
another entry in sitemap_sources.default_hosts():

1. One host holds thousands of postings, so the URL slug carries no signal --
   every URL looks like a job. Selection has to happen after parsing.
2. addressCountry is absent on a large share of pages (~43% when sampled), so
   the ISO filter that works elsewhere silently passes them. _country_backstop()
   resolves geography from the locality string instead.

Their identifier.value is their own job_<hex> slug, not the employer's ATS
requisition id, so cross-source dedupe by req id is not possible; the link is
the only stable key.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time

import pandas as pd

import sitemap_sources as S

SITE = "https://earlycareerradar.com"
HOST = "earlycareerradar.com"
SITEMAP_URL = f"{SITE}/sitemap.xml"

JOB_PATH_RE = re.compile(r"/(?:new-grad/)?jobs/job_[a-f0-9]{6,}$")

# sitemap_sources._polite_get already serializes per host through its own lock
# and sleeps PER_HOST_DELAY_SECS before each request, so an outer throttle or a
# worker pool here buys nothing -- they just queue behind that lock. This single
# origin (robots: Allow: /) gets a lighter delay so a full sitemap crawl runs in
# roughly 20 minutes instead of two and a half hours.
S.PER_HOST_DELAY_SECS = 0.25
PER_URL_DELAY_SECS = S.PER_HOST_DELAY_SECS
DEFAULT_WORKERS = 1

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

# Countries that appear often enough in early-career feeds that admitting them
# by default is a visible bug. Anything matched here is rejected outright.
FOREIGN_TOKENS = {
    "india", "ireland", "canada", "united kingdom", "england", "scotland",
    "germany", "france", "spain", "italy", "netherlands", "switzerland",
    "sweden", "norway", "denmark", "finland", "poland", "portugal", "australia",
    "new zealand", "singapore", "japan", "china", "korea", "taiwan", "israel",
    "brazil", "mexico", "argentina", "chile", "colombia", "luxembourg",
    "belgium", "austria", "czech", "hungary", "romania", "greece", "turkey",
    "united arab emirates", "uae", "south africa", "nigeria", "kenya",
    "estonia", "lithuania", "latvia", "iceland", "malta", "cyprus", "philippines",
    "indonesia", "vietnam", "malaysia", "thailand", "pakistan", "bangladesh",
}

US_LOCATION_HINT = re.compile(
    r"(?:,\s*(?:" + "|".join(sorted(US_STATES)) + r")\b)"          # ", CA"
    r"|\b(?:remote|united states|u\.?s\.?a\.?|usa)\b",
    re.I,
)

# A trailing two-letter token after a comma reads as an ISO country code.
# "London, UK" and "Paris, FR" must not pass as US locations, and US state
# abbreviations must not be mistaken for countries, so states are excluded
# explicitly. Anything else in this shape is treated as non-US.
NON_US_ISO = {
    "UK", "GB", "IE", "DE", "FR", "ES", "IT", "NL", "BE", "LU", "CH", "AT",
    "SE", "NO", "DK", "FI", "IS", "PL", "CZ", "PT", "GR", "RO", "HU", "HR",
    "CA", "MX", "BR", "AR", "CL", "CO", "PE", "IN", "PK", "BD", "CN", "JP",
    "KR", "SG", "HK", "TW", "TH", "VN", "MY", "ID", "PH", "AU", "NZ", "ZA",
    "NG", "KE", "IL", "AE", "SA", "CR", "PA", "GT", "UY", "EE", "LV", "LT",
}

# Trailing-token rule plus city names. Cities are needed because pages often
# omit the country entirely ("London, UK" is common but so is a bare city).
FOREIGN_CITIES = {
    "london", "manchester", "bristol", "edinburgh", "glasgow", "cambridge uk",
    "dublin", "berlin", "munich", "hamburg", "paris", "lyon", "madrid",
    "barcelona", "milan", "rome", "amsterdam", "rotterdam", "brussels",
    "antwerp", "zurich", "geneva", "basel", "vienna", "stockholm", "oslo",
    "copenhagen", "helsinki", "lisbon", "porto", "warsaw", "krakow", "prague",
    "budapest", "bucharest", "athens", "istanbul", "ankara", "toronto",
    "vancouver", "montreal", "waterloo", "calgary", "ottawa", "bangalore",
    "bengaluru", "mumbai", "hyderabad", "chennai", "pune", "delhi", "gurgaon",
    "noida", "hyderabad", "shanghai", "beijing", "shenzhen", "guangzhou",
    "seoul", "tokyo", "osaka", "kyoto", "taipei", "singapore", "hong kong",
    "sydney", "melbourne", "brisbane", "auckland", "wellington", "cape town",
    "johannesburg", "lagos", "tel aviv", "haifa", "costa rica", "san jose costa rica",
    "panama city", "sao paulo", "rio de janeiro", "buenos aires", "santiago",
}


def _country_backstop(row: dict) -> tuple[bool, str]:
    """Decide US eligibility when addressCountry is missing.

    Returns (keep, reason). Rows that state a non-US country, or whose location
    reads as one, are dropped. Rows with no country signal at all are kept --
    over-inclusive is safer than discarding a real Austin posting.
    """
    iso = str(row.get("country_iso") or "").strip().upper()
    if iso:
        return iso == "US", f"iso={iso}"

    location = str(row.get("location") or "")
    role = str(row.get("role") or "")
    blob = f"{location} {role}".lower()

    # Multi-location pages render as "Tampa, FL · Hanover, NJ · Cambridge, MA,
    # US" -- inspect every segment, not just the head.
    segments = [s.strip() for s in re.split(r"[·|]| or ", location) if s.strip()]
    for seg in segments:
        tail = re.search(r",\s*([A-Za-z]{2})\s*\.?$", seg)
        if tail:
            token = tail.group(1).upper()
            # States first: CA is California here but Canada in "Vancouver, CA".
            # The Canadian collision is covered by FOREIGN_CITIES instead.
            if token in US_STATES or token == "US":
                continue
            if token in NON_US_ISO:
                return False, f"iso-token:{token}"
            return False, f"unknown-token:{token}"

    for token in FOREIGN_TOKENS | FOREIGN_CITIES:
        if re.search(rf"\b{re.escape(token)}\b", blob):
            return False, f"foreign:{token}"

    if US_LOCATION_HINT.search(blob):
        return True, "us-hint"
    # No location at all means nothing to verify geography against, and the
    # role text is not evidence of one.
    if not location.strip():
        return False, "no-location"
    return True, "unknown-kept"


def fetch_sitemap() -> list[str]:
    """Job page URLs from the advertised sitemap, newest-first by URL order."""
    if not S.robots_allows(SITEMAP_URL):
        raise SystemExit("robots.txt disallows the sitemap; refusing to fetch")
    html = S._polite_get(SITEMAP_URL, HOST)
    if not html:
        raise SystemExit(f"could not fetch {SITEMAP_URL}")
    urls = []
    seen = set()
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", html):
        loc = loc.strip()
        if JOB_PATH_RE.search(loc) and loc not in seen:
            seen.add(loc)
            urls.append(loc)
    return urls


def _row_is_newgrad(url: str) -> bool:
    return "/new-grad/jobs/" in url


def harvest(
    urls: list[str],
    limit: int | None = None,
    workers: int = DEFAULT_WORKERS,
    verbose: bool = True,
) -> pd.DataFrame:
    """Fetch and parse job pages, returning rows that pass the US backstop."""
    targets = urls[:limit] if limit else urls

    def one(url: str):
        html = S._polite_get(url, HOST)
        if not html:
            return None, "fetch-fail"
        row = S.parse_job_page("", url, html)
        if not row:
            return None, "no-jsonld"
        ok, why = _country_backstop(row)
        if not ok:
            return None, why.split(":")[0]
        row["link"] = url
        row["source"] = "ecr"
        # Their pages are aggregator mirrors, so the canonical link is theirs.
        row["tier_hint"] = "new-grad" if _row_is_newgrad(url) else "internship"
        row["direct_apply"] = "false"
        return row, "kept"

    if verbose:
        print(
            f"ECR: parsing {len(targets)} job pages "
            f"({workers} workers, 1 req / {PER_URL_DELAY_SECS}s aggregate)",
            flush=True,
        )

    rows: list[dict] = []
    dropped: dict[str, int] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for row, why in ex.map(one, targets):
            done += 1
            if row is not None:
                rows.append(row)
            else:
                dropped[why] = dropped.get(why, 0) + 1
            if verbose and done % 250 == 0:
                print(f"  {done}/{len(targets)} fetched, {len(rows)} kept", flush=True)

    cols = S.JOB_COLUMNS + ["source", "tier_hint", "direct_apply"]
    frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)
    for col in cols:
        if col not in frame.columns:
            frame[col] = None
    if verbose:
        print(f"ECR: {len(targets)} fetched -> {len(rows)} kept", flush=True)
        if dropped:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(dropped.items(), key=lambda x: -x[1]))
            print(f"     dropped: {detail}", flush=True)
    return frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="/tmp/ecr_rows.parquet")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = ap.parse_args()

    urls = fetch_sitemap()
    print(f"ECR: sitemap lists {len(urls)} job pages")
    frame = harvest(urls, args.limit, workers=args.workers)
    frame.to_parquet(args.out)
    print(f"ECR: wrote {len(frame)} rows -> {args.out}")
    if not frame.empty:
        print(frame.groupby("tier_hint").size().to_string())
        print(frame["employment_type"].value_counts().to_string())


if __name__ == "__main__":
    main()