"""H-1B sponsorship enrichment (employer + job-title level).

Two federal-ish sources, both cached so an unreachable source never blocks a run:

* USCIS H-1B Employer Data Hub CSVs -> `visa_sponsor` (company-level).
* h1bdata.info, which republishes DOL LCA disclosure rows including job title
  and base salary -> `visa_role`, `visa_role_count`, `visa_median_salary`.

`visa_sponsor` is "true" when USCIS shows approvals for the employer and ""
otherwise. "" means *no signal*, not a denial: large sponsors file under legal
entity names that may not match the feed's brand name.
"""
import csv
import io
import json
import os
import re
import time
from statistics import median
from urllib.parse import quote_plus

import pandas as pd

from readme_utils import http_get

YEARS = (2021, 2022, 2023)
URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{year}.csv"
CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache", "h1b_sponsors.json")
CACHE_MAX_AGE_DAYS = 30
_UA = {"User-Agent": "Mozilla/5.0 (compatible; SearchTern-Listings/1.0)"}

ROLE_URL = "https://h1bdata.info/index.php?em={company}&job=&city=&year={year}"
ROLE_YEARS = (2025, 2024)
ROLE_CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache", "h1b_roles.json")
ROLE_CACHE_MAX_AGE_DAYS = 45
ROLE_MAX_COMPANIES = 300
ROLE_FETCH_PER_RUN = 20
ROLE_TOP_TITLES = 20
ROLE_POLITE_DELAY_S = 0.5

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_LEGAL_SUFFIXES = frozenset({
    "inc", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "srl", "bv", "nv",
    "pty", "pte", "holdings", "group",
})


def normalize_name(value):
    text = _NON_WORD.sub(" ", str(value or "").strip().lower())
    words = text.split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def _cache_fresh(path):
    try:
        return time.time() - os.path.getmtime(path) <= CACHE_MAX_AGE_DAYS * 86400
    except OSError:
        return False


def _load_cache(path=CACHE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("approvals", {}), str(data.get("built_at", ""))
    except (OSError, ValueError):
        pass
    return {}, ""


def _save_cache(approvals, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "approvals": approvals},
            f, indent=1, sort_keys=True,
        )
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _parse_year(year):
    resp = http_get(URL.format(year=year), timeout=40, retries=2, headers=_UA)
    if resp is None or resp.status_code != 200:
        return None
    try:
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
    except (csv.Error, UnicodeDecodeError):
        return None
    return rows


def build_approvals():
    """employer -> total H-1B approvals across the fetched years."""
    approvals = {}
    for year in YEARS:
        rows = _parse_year(year)
        if rows is None:
            print(f"  x H-1B {year}: unavailable")
            continue
        n = 0
        for row in rows:
            name = normalize_name(row.get("Employer"))
            if not name:
                continue
            try:
                total = (int(row.get("Initial Approval") or 0)
                         + int(row.get("Continuing Approval") or 0))
            except ValueError:
                total = 0
            if total <= 0:
                continue
            approvals[name] = approvals.get(name, 0) + total
            n += 1
        print(f"  H-1B {year}: {n:,} employers with approvals")
    return approvals


def load_approvals(force_refresh=False):
    """Cache-first; falls back to the last good cache when USCIS is unreachable."""
    path = CACHE_PATH
    if not force_refresh and _cache_fresh(path):
        approvals, built_at = _load_cache(path)
        if approvals:
            print(f"  H-1B sponsor map: {len(approvals):,} employers (cache {built_at[:10]})")
            return approvals
    approvals = build_approvals()
    if not approvals:
        approvals, built_at = _load_cache(path)
        print(f"  H-1B sponsor map: {len(approvals):,} employers (fallback cache)")
        return approvals
    _save_cache(approvals)
    print(f"  H-1B sponsor map: {len(approvals):,} employers (refreshed)")
    return approvals


def add_visa_column(dataframe, approvals):
    if dataframe is None or dataframe.empty:
        return dataframe
    out = dataframe.copy()
    out["visa_sponsor"] = out["company"].map(normalize_name).map(
        lambda name: "true" if approvals.get(name, 0) > 0 else ""
    )
    n_yes = int((out["visa_sponsor"] == "true").sum())
    print(f"  visa_sponsor: {n_yes:,} of {len(out):,} rows backed by H-1B approvals")
    return out


_LEVEL_WORDS = frozenset({
    "senior", "sr", "staff", "lead", "principal", "ii", "iii", "iv", "junior",
    "jr", "intern", "internship", "co", "op", "new", "grad", "graduate",
    "entry", "associate", "mid", "level", "summer", "fall", "spring", "winter",
})


def _role_tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 1 and w not in _LEVEL_WORDS}


def _tok_hit(left, right):
    if left == right:
        return True
    # shortcut: 5-char prefix fuses science/scientist, develop/developer
    # without a stemmer; revisit if mismatches show up in real data
    return len(left) >= 5 and len(right) >= 5 and left[:5] == right[:5]


def _median_salary(values):
    nums = []
    for value in values:
        text = re.sub(r"[^0-9]", "", str(value or ""))
        if text:
            nums.append(int(text))
    return int(median(nums)) if nums else None


def _parse_role_rows(html):
    rows = []
    for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", raw, re.S)]
        if len(cells) < 3:
            continue
        title = re.sub(r"\s+", " ", cells[1]).strip()
        if not title or title.lower() == "job title":
            continue
        rows.append({"title": title.upper(), "salary": _median_salary([cells[2]])})
    return rows


def _aggregate_titles(rows):
    buckets = {}
    for row in rows:
        bucket = buckets.setdefault(row["title"], [0, []])
        bucket[0] += 1
        if row["salary"]:
            bucket[1].append(row["salary"])
    titles = [
        {"title": title, "count": count, "median_salary": _median_salary(salaries)}
        for title, (count, salaries) in buckets.items()
    ]
    titles.sort(key=lambda t: (-t["count"], t["title"]))
    return titles[:ROLE_TOP_TITLES]


def _fetch_company_roles(name):
    for year in ROLE_YEARS:
        url = ROLE_URL.format(company=quote_plus(name), year=year)
        resp = http_get(url, timeout=45, retries=2, headers=_UA)
        if resp is None or resp.status_code != 200:
            continue
        titles = _aggregate_titles(_parse_role_rows(resp.text))
        if titles:
            return titles
    return []


def _load_role_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("companies"), dict):
            return data["companies"]
    except (OSError, ValueError):
        pass
    return {}


def _save_role_cache(companies, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "companies": companies}, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _stale(entry, max_age_days=ROLE_CACHE_MAX_AGE_DAYS):
    try:
        age = time.time() - time.mktime(time.strptime(entry["fetched_at"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, TypeError, ValueError):
        return True
    return age > max_age_days * 86400


def load_roles(company_names, path=ROLE_CACHE_PATH):
    cache = _load_role_cache(path)
    wanted = {}
    for name in company_names:
        key = normalize_name(name)
        if key:
            wanted.setdefault(key, str(name).strip())

    targets = [key for key in sorted(wanted, key=lambda k: k)
               if key not in cache or _stale(cache[key])]
    fetched = 0
    for key in targets:
        if fetched >= ROLE_FETCH_PER_RUN:
            break
        titles = _fetch_company_roles(key)
        if not titles and len(key.split()) > 1:
            titles = _fetch_company_roles(key.split()[0])
        cache[key] = {"label": wanted[key], "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "titles": titles}
        fetched += 1
        time.sleep(ROLE_POLITE_DELAY_S)

    if fetched:
        _save_role_cache(cache, path)
    matched = sum(1 for key in wanted if cache.get(key, {}).get("titles"))
    print(f"  H-1B role data: {matched:,} of {len(wanted):,} companies "
          f"({fetched} fetched this run, {len(cache):,} cached)")
    return cache


def match_title(role, titles):
    role_tokens = _role_tokens(role)
    if not role_tokens:
        return None
    best = None
    best_key = (0.0, 0)
    for entry in titles or []:
        title_tokens = _role_tokens(entry.get("title"))
        if not title_tokens:
            continue
        shared = sum(1 for token in role_tokens
                     if any(_tok_hit(token, other) for other in title_tokens))
        score = shared / len(role_tokens)
        if (score >= 0.6 and shared >= 2) or role_tokens == title_tokens:
            key = (score, int(entry.get("count") or 0))
            if key > best_key:
                best_key = key
                best = entry
    return best


def add_role_columns(dataframe, roles):
    if dataframe is None or dataframe.empty:
        return dataframe
    out = dataframe.copy()
    companies = out["company"].map(normalize_name)
    role_list = out["role"].astype(str)

    matched_titles = []
    matched_counts = []
    matched_pay = []
    for name, role in zip(companies, role_list):
        entry = roles.get(name) or {}
        hit = match_title(role, entry.get("titles")) if entry.get("titles") else None
        matched_titles.append(hit["title"] if hit else "")
        matched_counts.append(int(hit["count"]) if hit else 0)
        matched_pay.append(hit.get("median_salary") if hit else None)

    out["visa_role"] = matched_titles
    out["visa_role_count"] = matched_counts
    out["visa_median_salary"] = matched_pay
    n_role = sum(1 for t in matched_titles if t)
    print(f"  visa_role: {n_role:,} of {len(out):,} rows match a sponsored job title")
    return out


def write_h1b_json(approvals, roles, output_dir="."):
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    payload = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": {
            "employers": "https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub",
            "roles": "https://h1bdata.info (DOL LCA disclosure data)",
        },
        "fiscal_years": list(YEARS),
        "role_years": list(ROLE_YEARS),
        "employer_count": len(approvals),
        "employers": dict(sorted(approvals.items())),
        "roles": {key: {"label": entry.get("label", ""),
                        "fetched_at": entry.get("fetched_at", ""),
                        "titles": entry.get("titles", [])}
                  for key, entry in sorted(roles.items()) if entry.get("titles")},
    }
    path = os.path.join(pages_dir, "h1b.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    size_mb = os.path.getsize(path) / 1e6
    print(f"Wrote {path} — {len(approvals):,} employers, "
          f"{len(payload['roles']):,} role profiles ({size_mb:.1f} MB)")
    return path


if __name__ == "__main__":
    sample = (
        "<table><tr><th>EMPLOYER</th><th>JOB TITLE</th><th>BASE SALARY</th></tr>"
        "<tr><td>ACME LLC</td><td>SOFTWARE ENGINEER</td><td>150,000</td></tr>"
        "<tr><td>ACME LLC</td><td>SOFTWARE ENGINEER</td><td>170,000</td></tr>"
        "<tr><td>ACME LLC</td><td>DATA SCIENTIST</td><td>160,000</td></tr></table>"
    )
    rows = _parse_role_rows(sample)
    assert len(rows) == 3, rows
    titles = _aggregate_titles(rows)
    assert titles[0]["title"] == "SOFTWARE ENGINEER" and titles[0]["count"] == 2
    assert titles[0]["median_salary"] == 160000, titles[0]
    assert match_title("Software Engineer Intern", titles)["count"] == 2
    assert match_title("Accountant", titles) is None
    assert normalize_name("Acme, Inc.") == "acme"

    approvals = load_approvals()
    assert approvals, "no H-1B approvals parsed — source regression"
    df = pd.DataFrame([{"company": "Google", "role": "Software Engineer"}])
    df = add_visa_column(df, approvals)
    assert "visa_sponsor" in df.columns, "column not added"
    print(f"self-check ok: {len(approvals):,} employers, visa_sponsor={df['visa_sponsor'][0]!r}")
