import html
import os
import re
import glob  
from datetime import datetime, timezone

# GitHub render limit
GITHUB_FILE_SIZE_LIMIT = 512000
SIZE_BUFFER = 2560

# Inactivity threshold in days
INACTIVE_THRESHOLD_DAYS = 60

# Base GitHub URL for nav links
BASE_URL = "https://github.com/KSaifStack/jobscraper/blob/main/"

# Max character lengths before truncation
MAX_COMPANY_LEN = 24
MAX_ROLE_LEN    = 60

# Blocked companies
BLOCKED_COMPANIES: set[str] = {}

# FAANG+ companies
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


# ── Helpers ────────────────────────────────────────────────────────────────────

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


def format_company(company: str, prev_company: str, age: str, prev_age: str) -> str:
    """↳ for consecutive same-company rows, 🔥 for FAANG+."""
    if company == prev_company and age == prev_age:
        return "↳"
    escaped = html.escape(company)
    if company.lower() in FAANG_PLUS:
        return f"🔥 <strong>{escaped}</strong>"
    return f"<strong>{escaped}</strong>"


def build_table(rows: list[str]) -> str:
    """Wrap rows in a full HTML table and keep it within GitHub's viewport width."""
    return (
        '<table style="width:100%; table-layout:fixed; border-collapse:collapse;">\n'
        '<colgroup>\n'
        '<col style="width:12%">\n'
        '<col style="width:48%">\n'
        '<col style="width:25%">\n'
        '<col style="width:8%">\n'
        '<col style="width:7%">\n'
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


def build_header(current_page: int, total_pages: int, total_rows: int) -> str:
    """Build the top section of each README page."""
    nav       = build_nav(current_page, total_pages)
    page_info = f"Page {current_page} of {total_pages} — {total_rows:,} total listings"

    if current_page == 1:
        return (
            "# SearchTern Job Listings\n\n"
            "Auto-generated from jobhive. Updated each scrape run.\n\n"
            f"**{page_info}**\n\n"
            f"{nav}\n\n"
        )
    return (
        f"# SearchTern Job Listings — Page {current_page}\n\n"
        f"**{page_info}**\n\n"
        f"{nav}\n\n"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

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

    # ── Build all rows ──────────────────────────────────────────────────────
    all_rows        = []
    prev_company    = None
    prev_age        = None
    skipped_blocked  = 0
    skipped_inactive = 0
    skipped_foreign  = 0

    for _, row in dataframe.iterrows():
        company  = str(row["company"]).strip()
        role     = str(row["role"]).strip()
        location = str(row["location"]).strip()
        date     = str(row["date"]).strip()
        link     = str(row["link"]).strip()

        # Skip blocked companies
        if company in BLOCKED_COMPANIES:
            skipped_blocked += 1
            continue

        # Skip foreign language listings (non-ASCII title or company)
        if not is_english(role) or not is_english(company):
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
        company  = truncate(company, MAX_COMPANY_LEN)
        role     = truncate(role, MAX_ROLE_LEN)

        age          = days_display(date)
        company_cell = format_company(company, prev_company, age, prev_age)
        prev_company = company
        prev_age     = age

        all_rows.append(
            "<tr>\n"
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{company_cell}</td>\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{html.escape(role)}</td>\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{format_location(location)}</td>\n'
            f'<td align="center" style="white-space:nowrap;"><a href="{link}">Apply</a></td>\n'
            f'<td style="white-space:nowrap;">{age}</td>\n'
            "</tr>"
        )

    total_rows = len(all_rows)

    # ── Split rows into pages by byte size ─────────────────────────────────
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

    # ── Write each page ────────────────────────────────────────────────────
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
            build_header(page_num, total_pages, total_rows)
            + build_table(page_rows)
            + f"\n\n{nav}\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        size_kb = len(content.encode("utf-8")) / 1024
        print(f"Wrote {filepath} — {len(page_rows):,} rows, {size_kb:.1f} KB")
        files_written.append(filepath)

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\nTotal rows written  : {total_rows:,}")
    print(f"Pages created       : {total_pages}")
    print(f"Skipped (blocked)   : {skipped_blocked}")
    print(f"Skipped (inactive)  : {skipped_inactive}")
    print(f"Skipped (foreign)   : {skipped_foreign}")

    return files_written