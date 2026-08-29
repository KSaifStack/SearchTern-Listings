import concurrent.futures
import html
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

# GitHub README sources to add. None of these overlap with SimplifyJobs
# Internships/Off-Season, vanshb03, or SearchTern-Listings, so they add
# genuinely new coverage here.
MARKDOWN_SOURCES = [
    {
        "name": "SimplifyJobs New Grad",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
        "type": "newgrad",
        "season": "2027",
        "format": "html",
    },
    {
        "name": "speedyapply SWE 2027",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md",
        "type": "internship",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "speedyapply SWE New Grad USA",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md",
        "type": "newgrad",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "speedyapply AI 2027",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/README.md",
        "type": "internship",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "speedyapply AI New Grad USA",
        "url": "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/NEW_GRAD_USA.md",
        "type": "newgrad",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "zapplyjobs Internships 2027",
        "url": "https://raw.githubusercontent.com/zapplyjobs/Internships-2027/main/README.md",
        "type": "internship",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "zapplyjobs New Grad 2027",
        "url": "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md",
        "type": "newgrad",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "zapplyjobs New Grad SWE 2027",
        "url": "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Software-Engineering-Jobs-2027/main/README.md",
        "type": "newgrad",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "zapplyjobs New Grad Data Science 2027",
        "url": "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027/main/README.md",
        "type": "newgrad",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "zapplyjobs Canada 2027",
        "url": "https://raw.githubusercontent.com/zapplyjobs/Canada-Internships-2027/main/README.md",
        "type": "internship",
        "season": "2027",
        "format": "markdown",
    },
    {
        "name": "sndsh404 Summer 2027",
        "url": "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md",
        "type": "internship",
        "season": "2027",
        "format": "markdown",
    },
]

_TAG_RE = re.compile(r"<[^>]+>")
_HTML_LINK_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)")
_BARE_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")

_STATE_RE = re.compile(
    r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI"
    r"|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT"
    r"|VA|WA|WV|WI|WY|DC)\b"
)
_PROV_RE = re.compile(r"\b(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b")

HEADER_ALIASES = {
    "company": "company",
    "role": "role",
    "position": "role",
    "title": "role",
    "location": "location",
    "locations": "location",
    "application": "link",
    "posting": "link",
    "apply": "link",
    "apply link": "link",
    "link": "link",
    "referral": "link",
    "age": "date",
    "posted": "date",
    "added": "date",
    "date": "date",
    "date posted": "date",
    "posted at": "date",
    "posting date": "date",
}


def _strip_tags(text):
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _norm_header(text):
    h = _strip_tags(text).strip("*").strip()
    h = h.split("(")[0].strip()
    return h.lower()


def _extract_link(raw):
    if not raw:
        return ""
    m = _MD_LINK_RE.search(raw)
    if m:
        return m.group(2)
    links = _HTML_LINK_RE.findall(raw)
    if links:
        for u in links:
            if "simplify.jobs" not in u:
                return u
        return links[0]
    m = _BARE_URL_RE.search(raw)
    if m:
        return m.group(1)
    return ""


def _clean_location(raw):
    if not raw:
        return ""
    raw = re.sub(r"<summary>.*?</summary>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<br\s*/?>", ", ", raw, flags=re.IGNORECASE)
    t = _strip_tags(raw)
    t = re.sub(r"\s*\([^)]*\)", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    return t


def _parse_relative_age(text):
    m = re.match(r"(\d+)\s*([a-zA-Z]+)", text)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    now = datetime.now(timezone.utc)
    if unit.startswith("mo") or unit.startswith("month"):
        delta = timedelta(days=30 * n)
    elif unit.startswith("w") or unit.startswith("week"):
        delta = timedelta(weeks=n)
    elif unit.startswith("y"):
        delta = timedelta(days=365 * n)
    elif unit.startswith("h"):
        delta = timedelta(hours=n)
    elif unit.startswith("m"):
        delta = timedelta(minutes=n)
    else:
        delta = timedelta(days=n)
    return (now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(text):
    t = _strip_tags(text).strip()
    if not t or t.lower() in ("-", "—", "n/a", "na", "asap", "unknown"):
        return ""
    if t.lower() == "today":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00Z"
    return _parse_relative_age(t) or ""


def _parse_html_tables(md):
    tables = []
    for tbl in re.findall(r"<table.*?</table>", md, re.DOTALL | re.IGNORECASE):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tbl, re.DOTALL | re.IGNORECASE):
            cells = re.findall(r"<t[dh].*?</t[dh]>", tr, re.DOTALL | re.IGNORECASE)
            if cells:
                rows.append(cells)
        tables.append(rows)
    return tables


def _parse_md_tables(md):
    tables = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.lstrip().startswith("|"):
            i += 1
            continue
        tbl_lines = []
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            tbl_lines.append(lines[i].strip())
            i += 1

        def _split(cells):
            return [c.strip() for c in cells.strip().strip("|").split("|")]

        header = _split(tbl_lines[0])
        rows = []
        for ln in tbl_lines[1:]:
            cells = _split(ln)
            if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells):
                continue
            rows.append(cells)
        tables.append((header, rows))
    return tables


def _column_map(fields, cols):
    mapping = {}
    for field, col in enumerate(fields):
        key = HEADER_ALIASES.get(_norm_header(col))
        if key and key not in mapping:
            mapping[key] = field
    return mapping


def _parse_html_source(md):
    records = []
    for rows in _parse_html_tables(md):
        header_fields = None
        for row in rows:
            is_header = any(c.lstrip().lower().startswith("<th") for c in row)
            if is_header:
                header_fields = row
                break
        if header_fields is None:
            continue
        colmap = _column_map([_strip_tags(h) for h in header_fields], header_fields)
        if "company" not in colmap or "role" not in colmap:
            continue

        prev_company = ""
        for row in rows:
            is_header = any(c.lstrip().lower().startswith("<th") for c in row)
            if is_header:
                continue
            if len(row) < 2:
                continue
            company = _strip_tags(row[colmap["company"]]) if "company" in colmap else ""
            role = _strip_tags(row[colmap["role"]]) if "role" in colmap else ""
            location = _clean_location(row[colmap["location"]]) if "location" in colmap else ""
            link = _extract_link(row[colmap["link"]]) if "link" in colmap else ""
            date = _parse_date(row[colmap["date"]]) if "date" in colmap else ""

            if company in ("", "↳", "&raquo;") and prev_company:
                company = prev_company
            else:
                company = company.strip("*").strip()
                prev_company = company

            if not company or not role or not location:
                continue
            records.append({
                "company": company,
                "role": role,
                "location": location,
                "date": date,
                "link": link,
                "is_remote": str("remote" in location.lower()).lower(),
            })
    return records


def _parse_md_source(md):
    records = []
    for header, rows in _parse_md_tables(md):
        colmap = _column_map([_norm_header(h) for h in header], header)
        if "company" not in colmap or "role" not in colmap:
            continue

        prev_company = ""
        for row in rows:
            company = _strip_tags(row[colmap["company"]]).strip("*").strip() if "company" in colmap else ""
            role = _strip_tags(row[colmap["role"]]).strip("*").strip() if "role" in colmap else ""
            location = _clean_location(row[colmap["location"]]) if "location" in colmap else ""
            link = _extract_link(row[colmap["link"]]) if "link" in colmap else ""
            date = _parse_date(row[colmap["date"]]) if "date" in colmap else ""

            if company in ("", "↳") and prev_company:
                company = prev_company
            else:
                prev_company = company

            if not company or not role or not location:
                continue
            records.append({
                "company": company,
                "role": role,
                "location": location,
                "date": date,
                "link": link,
                "is_remote": str("remote" in location.lower()).lower(),
            })
    return records


def _infer_country(location):
    if not location:
        return ""
    loc_lower = location.lower()
    if re.search(r"\b(?:usa|u\.?s\.?a\.?|united states|us)\b", loc_lower):
        return "US"
    if re.search(r"\b(?:canada|canadian)\b", loc_lower):
        return "CA"
    if _STATE_RE.search(location):
        return "US"
    if _PROV_RE.search(location):
        return "CA"
    return ""


def fetch_source(source):
    resp = requests.get(source["url"], timeout=20)
    if resp.status_code != 200:
        print(f"  ✗ {source['name']}: HTTP {resp.status_code} — skipped")
        return [], source
    md = resp.text
    records = (
        _parse_html_source(md) if source["format"] == "html" else _parse_md_source(md)
    )
    print(f"  ✓ {source['name']}: parsed {len(records):,} rows")
    return records, source


def fetch_and_parse(infer_country=None):
    infer_country = infer_country or _infer_country
    stats = []

    print("Fetching markdown sources...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_source, s) for s in MARKDOWN_SOURCES]
        fetched = [fut.result() for fut in concurrent.futures.as_completed(futs)]

    all_rows = []
    for records, source in fetched:
        stats.append((source["name"], len(records)))
        if not records:
            continue

        job_type = "internship" if source["type"] == "internship" else "new_grad"
        for r in records:
            loc = r["location"]
            all_rows.append({
                "company": r["company"],
                "role": r["role"],
                "location": loc,
                "date": r["date"],
                "link": r["link"],
                "is_remote": r["is_remote"],
                "salary_min": None,
                "salary_max": None,
                "salary_currency": None,
                "country_iso": infer_country(loc),
                "job_type": job_type,
                "source": source["name"],
            })

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df, stats

    df = df.drop_duplicates(subset=["company", "role", "location"], keep="first")
    return df, stats