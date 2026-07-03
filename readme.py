import html
import os
import re
import glob  
from datetime import datetime, timezone

GITHUB_FILE_SIZE_LIMIT = 512000
SIZE_BUFFER = 2560

INACTIVE_THRESHOLD_DAYS = 60

# Base GitHub URL for nav links
BASE_URL = "https://github.com/KSaifStack/jobscraper/blob/main/"

APPLY_BUTTON = "https://i.imgur.com/fbjwDvo.png"

MAX_COMPANY_LEN = 20
MAX_ROLE_LEN    = 48

BLOCKED_COMPANIES: set[str] = {
    "aoglobelife",
    "aoglobelifebrittmarieyarian",
    "aoglobelifesholaadebayo",
    "americanincomelif",
    "aoglobelifebrittm",
    "destinationknot",
    "themcquadeorganiz",
    "globelifeaillisar",
}
NORMALIZED_BLOCKED_COMPANIES = {name.strip().lower() for name in BLOCKED_COMPANIES}


def is_oracle_cloud_url(company: str) -> bool:
    """Detect Oracle Cloud ATS URLs masquerading as company names."""
    normalized = company.strip().lower()
    return (
        ".fa." in normalized and "oracl" in normalized  # e.g., "ejhp.fa.us6.oraclecloud.com"
        or normalized.endswith(".oraclecloud.com")
        or "fa-" in normalized and "saasfaprod" in normalized  # e.g., "fa-evlj-saasfaprod1..."
    )


def is_careers_portal_url(company: str) -> bool:
    """Detect careers portal URLs masquerading as company names."""
    normalized = company.strip().lower()
    # Match patterns like careers.marsh.com, careers.cencora.com, etc.
    return "careers." in normalized and re.search(r"\.\w{2,}(?:\.\w{2,})?$", normalized) is not None


FAANG_PLUS: set[str] = {
    "airbnb", "adobe", "amazon", "amd", "anthropic", "apple",
    "asana", "atlassian", "bytedance", "cloudflare", "coinbase",
    "crowdstrike", "databricks", "datadog", "doordash", "dropbox",
    "duolingo", "figma", "google", "ibm", "instacart", "intel",
    "linkedin", "lyft", "meta", "microsoft", "netflix", "notion",
    "nvidia", "openai", "oracle", "palantir", "paypal", "perplexity",
    "pinterest", "ramp", "reddit", "rippling", "robinhood", "roblox",
    "salesforce", "samsara", "servicenow", "shopify", "slack", "snap",
    "snapchat", "spacex", "splunk", "snowflake", "stripe", "square",
    "tesla", "tinder", "tiktok", "uber", "visa", "waymo", "x",
}

COUNTRY_NAMES = {
    "US": "🇺🇸 United States",
    "CA": "🇨🇦 Canada",
    "GB": "🇬🇧 United Kingdom",
    "AU": "🇦🇺 Australia",
    "NZ": "🇳🇿 New Zealand",
    "IE": "🇮🇪 Ireland",
    "SG": "🇸🇬 Singapore",
    "IN": "🇮🇳 India",
    "DE": "🇩🇪 Germany",
    "NL": "🇳🇱 Netherlands",
    "FR": "🇫🇷 France",
    "JP": "🇯🇵 Japan",
    "BR": "🇧🇷 Brazil",
    "MX": "🇲🇽 Mexico",
    "CH": "🇨🇭 Switzerland",
    "AT": "🇦🇹 Austria",
    "BE": "🇧🇪 Belgium",
    "SE": "🇸🇪 Sweden",
    "ES": "🇪🇸 Spain",
    "LU": "🇱🇺 Luxembourg",
    "IT": "🇮🇹 Italy",
    "PL": "🇵🇱 Poland",
    "NO": "🇳🇴 Norway",
    "DK": "🇩🇰 Denmark",
    "FI": "🇫🇮 Finland",
    "MT": "🇲🇹 Malta",
    "PT": "🇵🇹 Portugal",
    "CZ": "🇨🇿 Czech Republic",
    "CY": "🇨🇾 Cyprus",
    "RO": "🇷🇴 Romania",
}

CANONICAL_DOMAIN_MAP: dict[str, str] = {
    "baesystems": "BAE Systems",
    "marsh": "Marsh",
    "cencora": "Cencora",
    "geaerospace": "GE Aerospace",
    "ascension": "Ascension",
}


def clean_company_name(company: str) -> str:
    """Normalize company values that are domains or noisy tokens.

    - Unescape HTML entities
    - Convert domain-like names (jobs.foo.com, careers.foo.org) to a readable form
    - Remove common suffixes like 'careers', trailing digits, and punctuation
    - Title-case the final result while preserving known canonical mappings
    Returns empty string when the name should be skipped.
    """
    if not company:
        return ""
    name = html.unescape(str(company)).strip()
    if not name:
        return ""

    lower = name.lower()

    # If it's an Oracle Cloud ATS URL, normalize to a friendly label
    if is_oracle_cloud_url(lower):
        return "Oracle Cloud"

    # If it's a careers portal URL like careers.foo.com, try to extract the SLD
    if is_careers_portal_url(lower):
        m = re.search(r"careers\.([a-z0-9-]+)", lower)
        if m:
            sld = m.group(1)
            if sld in CANONICAL_DOMAIN_MAP:
                return CANONICAL_DOMAIN_MAP[sld]
            s = re.sub(r"[-_0-9]+", " ", sld).strip()
            return " ".join(w.capitalize() for w in s.split())

    # If it looks like a domain with dots and no spaces, extract the SLD
    if "." in lower and " " not in lower:
        # Try to capture the second-level domain
        m = re.search(r"(?:^(?:www|jobs|careers|apply)\.)?([a-z0-9-]+)(?:\.[a-z0-9-]+)+$", lower)
        if m:
            sld = m.group(1)
            if sld in CANONICAL_DOMAIN_MAP:
                return CANONICAL_DOMAIN_MAP[sld]
            # split hyphens and digits, then title-case
            s = re.sub(r"[-_0-9]+", " ", sld).strip()
            return " ".join(w.capitalize() for w in s.split())

    # Remove common noise suffixes and trailing digits (e.g., dmcengineering2024 -> dmcengineering)
    cleaned = re.sub(r"(?:careers|jobs|portal|apply|recruitment|ats|jobsat)$", "", lower)
    cleaned = re.sub(r"\d+$", "", cleaned)
    # Replace non-letter/number with spaces
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned).strip()
    if not cleaned:
        return ""

    # If the cleaned token is a short acronym-like string, return uppercased
    if cleaned.isupper() or (len(cleaned) <= 4 and cleaned.isalpha()):
        return cleaned.upper()

    return " ".join(w.capitalize() for w in cleaned.split())




def is_english(text: str) -> bool:
    """Return False if text contains non-ASCII characters (catches foreign listings)."""
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters."""
    text = text.strip()
    if len(text) > max_len:
        return text[:max_len - 3] + "..."
    return text


def normalize_company(company: str) -> str:
    """Normalize company names so blocking works consistently."""
    return str(company).strip().lower()


def days_display(date_str) -> str:
    """Convert ISO timestamp to human-readable age string."""
    try:
        posted = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        days = max((datetime.now(timezone.utc) - posted).days, 0)
        if days == 0:
            return "Today"
        elif days > 30:
            return f"{days // 30}mo"
        else:
            return f"{days}d"
    except Exception:
        return str(date_str)


def format_location(location) -> str:
    """Format location — collapse 4+ locations into a dropdown."""
    if not location or str(location) == "nan":
        return "N/A"
    parts = [p.strip() for p in str(location).split(",") if p.strip()]
    if len(parts) <= 3:
        return "<br>".join(parts)
    joined = "<br>".join(parts)
    return f"<details><summary><strong>{len(parts)} locations</strong></summary>{joined}</details>"


def format_company(company: str, prev_company: str, age: str, prev_age: str, link: str = None) -> str:
    """Return company HTML. Wraps in anchor when a job URL is provided.

    - Returns '↳' for consecutive same-company rows (keeps previous visual cue).
    - Adds 🔥 for FAANG+ companies.
    """
    escaped = html.escape(company)
    if company.lower() in FAANG_PLUS:
        base = f"🔥 <strong>{escaped}</strong>"
    else:
        base = f"<strong>{escaped}</strong>"

    if company == prev_company and age == prev_age:
        cell = f"↳ {base}"
    else:
        cell = base

    if link:
        safe_link = html.escape(link, quote=True)
        return f'<a href="{safe_link}">{cell}</a>'
    return cell


def build_table(rows: list[str]) -> str:
    """Wrap rows in a full HTML table and keep it within GitHub's viewport width."""
    return (
        '<table style="width:100%; table-layout:fixed; border-collapse:collapse;">\n'
        '<colgroup>\n'
        '<col style="width:14%">\n'
        '<col style="width:46%">\n'
        '<col style="width:24%">\n'
        '<col style="width:8%">\n'
        '<col style="width:8%">\n'
        '</colgroup>\n<thead>\n<tr>\n'
        '<th style="text-align:left; white-space:normal;">Company</th>\n'
        '<th style="text-align:left; white-space:normal;">Role</th>\n'
        '<th style="text-align:left; white-space:normal;">Location</th>\n'
        '<th style="text-align:center; white-space:normal;">Application</th>\n'
        '<th style="text-align:center; white-space:normal;">Age</th>\n'
        "</tr>\n</thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody>\n</table>"
    )

def build_country_index(dataframe, total_rows: int) -> str:
    if "country_iso" not in dataframe.columns:
        return ""

    counts = (
        dataframe["country_iso"]
        .fillna("Unknown")
        .value_counts()
        .reset_index()
    )
    counts.columns = ["country_iso", "count"]
    counts = counts.sort_values("count", ascending=False)

    rows = []
    for _, row in counts.iterrows():
        code  = str(row["country_iso"]).strip().upper()
        count = int(row["count"])
        
        # Skip unknown and tiny countries (less than 10 listings)
        if code in ("UNKNOWN", "NAN", "") or count < 10:
            continue

        name = COUNTRY_NAMES.get(code, f"🌐 {code}")
        pct  = round((count / total_rows) * 100, 1)
        link = f"https://github.com/KSaifStack/searchtern-listings/blob/main/countries/{code}.md"
        rows.append(f"| [{name}]({link}) | {count:,} | {pct}% |")

    table = (
        "| Country | Listings | Share |\n"
        "|---------|----------|-------|\n"
        + "\n".join(rows)
    )

    return f"""### 🌍 Browse by Country

{table}

"""

def generate_country_pages(dataframe, output_dir="."):
    """Generate a dedicated README page for each country."""
    if "country_iso" not in dataframe.columns:
        return

    countries_dir = os.path.join(output_dir, "countries")
    os.makedirs(countries_dir, exist_ok=True)

    # Clear old country pages
    for old in glob.glob(os.path.join(countries_dir, "*.md")):
        os.remove(old)

    for code, group in dataframe.groupby("country_iso"):
        code    = str(code).strip().upper()
        name    = COUNTRY_NAMES.get(code, code)
        count   = len(group)
        today   = datetime.now(timezone.utc).strftime("%B %d, %Y")

        rows_html = []
        prev_company = None
        prev_age     = None

        for _, row in group.iterrows():
            company_name = str(row["company"]).strip()
            
            # Clean company name for country pages
            cleaned_company = clean_company_name(company_name)
            if not cleaned_company:
                continue

            # Skip blocked companies
            if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
                continue

            company  = truncate(cleaned_company, MAX_COMPANY_LEN)
            role     = truncate(str(row["role"]).strip(), MAX_ROLE_LEN)
            location = str(row["location"]).strip()
            date     = str(row["date"]).strip()
            link     = str(row["link"]).strip()

            if not is_english(role) or not is_english(company):
                continue

            age          = days_display(date)
            company_cell = format_company(company, prev_company, age, prev_age, link)
            prev_company = company
            prev_age     = age

            rows_html.append(
                "<tr>\n"
                f'<td style="word-break:break-word;">{company_cell}</td>\n'
                f'<td style="word-break:break-word;">{html.escape(role)}</td>\n'
                f'<td style="word-break:break-word;">{format_location(location)}</td>\n'
                f'<td align="center"><a href="{link}"><img src="{APPLY_BUTTON}" width="60" alt="Apply"></a></td>\n'
                f'<td style="white-space:nowrap;">{age}</td>\n'
                "</tr>"
            )

        if not rows_html:
            continue

        content = (
            f"# {name} — SearchTern Listings\n\n"
            f"**{count:,} listings** as of {today}\n\n"
            f"[← Back to all listings]({BASE_URL}README.md)\n\n"
            + build_table(rows_html)
            + "\n"
        )

        filepath = os.path.join(countries_dir, f"{code}.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"Wrote countries/{code}.md — {len(rows_html):,} rows")

def build_nav(current_page: int, total_pages: int) -> str:
    """Build previous/next nav links."""
    links = []
    if current_page > 1:
        prev_url = (
            BASE_URL + "README.md"
            if current_page == 2
            else BASE_URL + f"pages/README-{current_page - 1}.md"
        )
        links.append(f"[← Previous page]({prev_url})")
    if current_page < total_pages:
        next_url = BASE_URL + f"pages/README-{current_page + 1}.md"
        links.append(f"[Next page →]({next_url})")
    return " | ".join(links)


def build_header(current_page: int, total_pages: int, total_rows: int,dataframe=None) -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    nav   = build_nav(current_page, total_pages)

    if current_page == 1:
        country_section = build_country_index(dataframe, total_rows) if dataframe is not None else ""
        return f"""<div align="center">

<img src="https://raw.githubusercontent.com/KSaifStack/SearchTern/main/frontend/src/assets/Logo.png" alt="SearchTern Logo" width="200"/>

# SearchTern

### The all-in-one internship platform for college students

[![Website](https://img.shields.io/badge/Visit-SearchTern.com-1D9E75?style=for-the-badge)](https://searchtern.ksaif.dev/)
[![Listings](https://img.shields.io/badge/Internships-{total_rows:,}-blue?style=for-the-badge)](https://github.com/KSaifStack/jobscraper)
[![Updated](https://img.shields.io/badge/Updated-{today.replace(" ", "%20")}-orange?style=for-the-badge)](https://github.com/KSaifStack/jobscraper)

</div>

---

> 🎓 **{total_rows:,} internships & new grad roles** updated twice daily from 49 ATS platforms worldwide.
> Finding an internship has never been harder — students are sending 500–1000+ applications just to land one.
> SearchTern breaks that cycle. [**Start your search →**](https://searchtern.ksaif.dev/)

---

{country_section}

---

### 📊 Data powered by jobhive

This dataset is pulled and filtered from [**jobhive**](https://github.com/stapply-ai/ats-scrapers) —
an open-source project by [Stapply](https://data.stapply.ai) that scrapes job listings directly
from ATS platforms (Greenhouse, Lever, Ashby, Workday and 45 more) where companies actually post.
No LinkedIn reposts. No duplicates. One source of truth.

> If you find jobhive useful, consider ⭐ starring their repo: [stapply-ai/ats-scrapers](https://github.com/stapply-ai/ats-scrapers)

---

**Page {current_page} of {total_pages}**

{nav}

"""
    return (
        f"#  SearchTern Internship Listings — Page {current_page}\n\n"
        f"**Page {current_page} of {total_pages} — {total_rows:,} total listings** | "
        f"[← Back to main listing](https://github.com/KSaifStack/jobscraper/blob/main/README.md)\n\n"
        f"{nav}\n\n"
    )



def generate_readme(dataframe, output_dir="."):
    """
    Writes job listings into README files split by GitHub's 500KB render limit.
    Page 1 → README.md (root)
    Page 2+ → pages/README-2.md, pages/README-3.md, etc.
    """
    pages_dir = os.path.join(output_dir, "pages")

    if os.path.exists(pages_dir):
        old_pages = glob.glob(os.path.join(pages_dir, "README-*.md"))
        for old_file in old_pages:
            os.remove(old_file)

    # Builds all rows 
    all_rows        = []
    prev_company    = None
    prev_age        = None
    skipped_blocked  = 0
    skipped_inactive = 0
    skipped_foreign  = 0
    skipped_oracle   = 0

    for _, row in dataframe.iterrows():
        company  = str(row["company"]).strip()
        role     = str(row["role"]).strip()
        location = str(row["location"]).strip()
        date     = str(row["date"]).strip()
        link     = str(row["link"]).strip()

        # Clean company name (strip domains, careers portals, noisy tokens)
        cleaned_company = clean_company_name(company)
        if not cleaned_company:
            skipped_oracle += 1
            continue

        # Skip blocked companies
        if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
            skipped_blocked += 1
            continue

        # Skip foreign language listings (non-ASCII title or company)
        if not is_english(role) or not is_english(cleaned_company):
            skipped_foreign += 1
            continue

        # Skip inactive listings
        try:
            posted = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - posted).days > INACTIVE_THRESHOLD_DAYS:
                skipped_inactive += 1
                continue
        except Exception:
            pass

        # Truncate long fields
        company  = truncate(cleaned_company, MAX_COMPANY_LEN)
        role     = truncate(role, MAX_ROLE_LEN)
        age          = days_display(date)
        company_cell = format_company(company, prev_company, age, prev_age, link)
        prev_company = company
        prev_age     = age

        all_rows.append(
            "<tr>\n"
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{company_cell}</td>\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{html.escape(role)}</td>\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{format_location(location)}</td>\n'
            f'<td align="center" style="white-space:nowrap; overflow-wrap:anywhere; width:8%;"><a href="{link}" style="display:inline-block; max-width:100%;"><img src="{APPLY_BUTTON}" width="60" alt="Apply" style="display:block; max-width:100%; height:auto; margin:0 auto;"></a></td>\n'
            f'<td style="white-space:nowrap;">{age}</td>\n'
            "</tr>"
        )

    total_rows = len(all_rows)

    # Split rows into pages by byte size
    pages             = []
    current_page_rows = []
    current_size      = 0

    for row_html in all_rows:
        row_size = len(row_html.encode("utf-8"))
        if current_size + row_size > (GITHUB_FILE_SIZE_LIMIT - SIZE_BUFFER) and current_page_rows:
            pages.append(current_page_rows)
            current_page_rows = [row_html]
            current_size      = row_size
        else:
            current_page_rows.append(row_html)
            current_size += row_size

    if current_page_rows:
        pages.append(current_page_rows)

    total_pages = len(pages)

    #  Writes on each page
    files_written = []

    for i, page_rows in enumerate(pages):
        page_num = i + 1
        filepath = (
            os.path.join(output_dir, "README.md")
            if page_num == 1
            else os.path.join(pages_dir, f"README-{page_num}.md")
        )

        if page_num > 1:
            os.makedirs(pages_dir, exist_ok=True)

        nav     = build_nav(page_num, total_pages)
        content = (
            build_header(page_num, total_pages, total_rows,dataframe if page_num == 1 else None)
            + build_table(page_rows)
            + f"\n\n{nav}\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        size_kb = len(content.encode("utf-8")) / 1024
        print(f"Wrote {filepath} — {len(page_rows):,} rows, {size_kb:.1f} KB")
        files_written.append(filepath)

    generate_country_pages(dataframe, output_dir)

    print(f"\nTotal rows written  : {total_rows:,}")
    print(f"Pages created       : {total_pages}")
    print(f"Skipped (Oracle URLs): {skipped_oracle}")
    print(f"Skipped (blocked)   : {skipped_blocked}")
    print(f"Skipped (inactive)  : {skipped_inactive}")
    print(f"Skipped (foreign)   : {skipped_foreign}")

    return files_written