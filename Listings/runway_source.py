"""Runway Explore job source (shard-limited).

app.joinrunway.io/explore is an early-career/entry-level aggregator. It publishes
11 sitemap shards under /sitemap/*.xml totaling ~510k job URLs, each pointing
to a server-rendered page with clean JobPosting JSON-LD. By default, it is
harvested in a shard-limited fashion, so that running it remains practical
(time-wise) while still yielding a meaningful volume of postings.

The trade-off: this is an aggregator, not the employer's ATS. directApply is true
on many pages, and canonical links point back to Runway rather than to the
employer site. The content mix is broad (includes hourly roles), so downstream
filters (tech keywords, employmentType, 60-day freshness) are relied on heavily.
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

HOST = "app.joinrunway.io"
SITE = "https://app.joinrunway.io"
SITEMAP_INDEX = f"{SITE}/sitemap.xml"
SITEMAP_SHARD = f"{SITE}/sitemap/%d.xml"

JOB_PATH_RE = re.compile(r"/explore/job/[a-zA-Z0-9]+$")

DEFAULT_SHARDS = "0,1"  # 100k URLs is about 7–8 hours at 0.25s; practical default
PER_URL_DELAY_SECS = 0.25
MAX_WORKERS = 1  # serialized by host lock; more workers queue, not speed up

INTERN_TIER_HINT = re.compile(r"intern", re.I)

# classify.TECH_KEYWORDS_RE is tuned for ATS feeds that are mostly engineering
# already, and includes bare tokens (qa, it, ai, ml, data, production,
# analyst). On a general-purpose board that lets through Cargill "General
# Production", "Sanitation Production", and "Grind - General Production", which
# measured ~400 false positives per 4,000 pages sampled. This stricter gate is
# local to Runway so classify's shared behavior is untouched for every other
# source. Requires an engineering/software/AI/data-science anchor, or a
# senior-ish software/intern/graduate marker.
TECH_STRICT_RE = re.compile(
    r"\b(?:swe|sde|sdet|devops|sre|software|developer|programmer|coder|"
    r"full[ -]?stack|back[ -]?end|front[ -]?end|machine learn(?:ing)?|"
    r"deep learn(?:ing)?|artificial intellig(?:ence)?|computer vision|"
    r"data scien\w*|data engineer\w*|data analy\w*|data scientist\w*|"
    r"analytics engineer\w*|business intelligence|llm|nlp|"
    r"cybersecurity|information security|security engineer\w*|"
    r"embedded|firmware|fpga|asic|robotics|compilers?|"
    r"site reliability|infrastructure engineer\w*|platform engineer\w*|"
    r"qa engineer\w*|quality engineer\w*|test engineer\w*|"
    r"engineering technologist|systems engineer\w*|"
    r"technical program\w*|solutions engineer\w*|"
    r"web develop\w*|mobile develop\w*|ios engineer\w*|android engineer\w*)\b"
    # Bare AI/ML/research, but require a research/engineering noun nearby so a
    # title like "Masters Thesis ... Ai Sweden" (an employer name bleeding into
    # the role field) does not qualify.
    r"|\b(?:ai|ml)\b(?=[^,]{0,24}\b(?:research|engineer|intern|graduate|"
    r"scientist|specialist|developer|program|model|architect))\b"
    r"|\bresearch (?:engineer|intern|scientist)\b",
    re.I,
)


def fetch_shard_urls(shards: list[int]) -> list[str]:
    seen = set()
    urls = []
    for s in shards:
        url = SITEMAP_SHARD % s
        if not S.robots_allows(url):
            raise SystemExit(f"robots.txt disallows {url}")
        html = S._polite_get(url, HOST)
        if not html:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", html):
            loc = loc.strip()
            if JOB_PATH_RE.search(loc) and loc not in seen:
                seen.add(loc)
                urls.append(loc)
    return urls


def _parse_page(url: str, html: str):
    for raw in S._LD_RE.findall(html or ""):
        try:
            blob = json.loads(raw.strip())
        except Exception:
            continue
        for node in S._iter_ld_nodes(blob):
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not any(k == "JobPosting" for k in kinds):
                continue
            title = S._strip_html(str(node.get("title") or ""))
            if not title:
                continue
            org = S._first(node.get("hiringOrganization"), {})
            org_name = S._strip_html(str(org.get("name") or "")) if isinstance(org, dict) else ""
            addr = S._address(node)
            parts = [
                addr.get("addressLocality", ""),
                addr.get("addressRegion", ""),
                addr.get("addressCountry", ""),
            ]
            location = ", ".join([str(p) for p in parts if p])
            lo, hi, cur = S._base_salary(node)
            employment = S._first(node.get("employmentType"), "")
            if isinstance(employment, dict):
                employment = employment.get("name", "")
            is_intern = INTERN_TIER_HINT.search(title) or str(employment).upper() == "INTERN"
            return {
                "company": (org_name or "").strip(),
                "role": title,
                "location": location,
                "date": str(node.get("datePosted") or "")[:10],
                "link": url,
                "is_remote": S._remote(node, addr),
                "salary_min": lo,
                "salary_max": hi,
                "salary_currency": cur,
                "country_iso": S._country_code(addr),
                "employment_type": str(employment) if employment else None,
                "seniority": None,
                "tier_hint": "internship" if is_intern else "full-time",
            }
    return None


def harvest(urls: list[str], limit: int | None = None, workers: int = MAX_WORKERS, verbose: bool = True) -> pd.DataFrame:
    targets = urls[:limit] if limit else urls

    def one(url: str):
        html = S._polite_get(url, HOST)
        if not html:
            return None, "fetch-fail"
        row = _parse_page(url, html)
        if not row:
            return None, "no-jsonld"
        return row, "kept"

    if verbose:
        print(
            f"Runway: parsing {len(targets)} job pages "
            f"({workers} workers, 1 req / {PER_URL_DELAY_SECS}s aggregate)",
            flush=True,
        )

    rows: list[dict] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for row, why in ex.map(one, targets):
            done += 1
            if row is not None:
                rows.append(row)
            if verbose and done % 250 == 0:
                print(f"  {done}/{len(targets)} fetched, {len(rows)} kept", flush=True)

    cols = S.JOB_COLUMNS + ["source", "tier_hint"]
    frame = pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)
    for col in cols:
        if col not in frame.columns:
            frame[col] = None
    if verbose:
        print(f"Runway: {len(targets)} fetched -> {len(rows)} kept", flush=True)
    return frame


def _parse_shards(s: str) -> list[int]:
    out = []
    for p in s.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            v = int(p)
            if 0 <= v <= 10:
                out.append(v)
        except ValueError:
            pass
    return sorted(set(out)) or [0, 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", default=DEFAULT_SHARDS, help="Comma-separated sitemap shard numbers 0–10")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="/tmp/runway_rows.parquet")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = ap.parse_args()

    S.PER_HOST_DELAY_SECS = PER_URL_DELAY_SECS
    shards = _parse_shards(args.shards)
    urls = fetch_shard_urls(shards)
    frame = harvest(urls, args.limit, workers=args.workers)
    frame.to_parquet(args.out)
    print(f"Runway: wrote {len(frame)} rows -> {args.out}")
    if not frame.empty:
        print(frame["tier_hint"].value_counts().to_string())


if __name__ == "__main__":
    main()