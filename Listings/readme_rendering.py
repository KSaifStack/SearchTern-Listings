import glob
import os
from datetime import datetime, timezone

from readme_utils import (
    APPLY_BUTTON,
    BASE_URL,
    COUNTRY_NAMES,
    MAX_COMPANY_LEN,
    MAX_ROLE_LEN,
    NORMALIZED_BLOCKED_COMPANIES,
    build_table,
    clean_company_name,
    days_display,
    format_company,
    format_location,
    is_english,
    normalize_company,
    truncate,
)


def build_country_index(dataframe, total_rows: int) -> str:
    if "country_iso" not in dataframe.columns:
        return ""

    counts = dataframe["country_iso"].fillna("Unknown").value_counts().reset_index()
    counts.columns = ["country_iso", "count"]
    counts = counts.sort_values("count", ascending=False)

    rows = []
    for _, row in counts.iterrows():
        code = str(row["country_iso"]).strip().upper()
        count = int(row["count"])

        if code in ("UNKNOWN", "NAN", "") or count < 10:
            continue

        name = COUNTRY_NAMES.get(code, f"🌐 {code}")
        pct = round((count / total_rows) * 100, 1)
        link = f"https://github.com/KSaifStack/searchtern-listings/blob/main/countries/{code}.md"
        rows.append(f"| [{name}]({link}) | {count:,} | {pct}% |")

    table = "| Country | Listings | Share |\n|---------|----------|-------|\n" + "\n".join(rows)
    return f"""### 🌍 Browse by Country

{table}

"""


def generate_country_pages(dataframe, output_dir="."):
    """Generate a dedicated README page for each country."""
    if "country_iso" not in dataframe.columns:
        return

    countries_dir = os.path.join(output_dir, "countries")
    os.makedirs(countries_dir, exist_ok=True)

    for old in glob.glob(os.path.join(countries_dir, "*.md")):
        os.remove(old)

    for code, group in dataframe.groupby("country_iso"):
        code = str(code).strip().upper()
        if code in ("UNKNOWN", "NAN", ""):
            continue
        
        name = COUNTRY_NAMES.get(code, code)
        count = len(group)
        today = datetime.now(timezone.utc).strftime("%B %d, %Y")

        rows_html = []
        prev_company = None
        prev_age = None

        for _, row in group.iterrows():
            company_name = str(row["company"]).strip()
            cleaned_company = clean_company_name(company_name)
            if not cleaned_company:
                continue

            if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
                continue

            company = truncate(cleaned_company, MAX_COMPANY_LEN)
            role = truncate(str(row["role"]).strip(), MAX_ROLE_LEN)
            location = str(row["location"]).strip()
            date = str(row["date"]).strip()
            link = str(row["link"]).strip()

            if not is_english(role) or not is_english(company):
                continue

            age = days_display(date)
            company_cell = format_company(company, prev_company, age, prev_age, link)
            prev_company = company
            prev_age = age

            rows_html.append(
                "<tr>\n"
                f'<td style="word-break:break-word; overflow-wrap:anywhere;">{company_cell}</td>\n'
                f'<td style="word-break:break-word; overflow-wrap:anywhere;">{__import__("html").escape(role)}</td>\n'
                f'<td style="word-break:break-word; overflow-wrap:anywhere;">{format_location(location)}</td>\n'
                f'<td align="center" style="white-space:nowrap; overflow-wrap:anywhere; width:8%;"><a href="{__import__("html").escape(link, quote=True)}" style="display:inline-block; max-width:100%;"><img src="{APPLY_BUTTON}" width="60" alt="Apply" style="display:block; max-width:100%; height:auto; margin:0 auto;"></a></td>\n'
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


def build_header(current_page: int, total_pages: int, total_rows: int, dataframe=None) -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    nav = build_nav(current_page, total_pages)

    if current_page == 1:
        country_section = build_country_index(dataframe, total_rows) if dataframe is not None else ""
        return f"""<div align="center">

<img src="https://raw.githubusercontent.com/KSaifStack/SearchTern/main/frontend/src/assets/Logo.png" alt="SearchTern Logo" width="200"/>

# SearchTern

### The all-in-one internship platform for college students

[![Website](https://img.shields.io/badge/Visit-SearchTern.com-1D9E75?style=for-the-badge)](https://searchtern.ksaif.dev/)
[![Listings](https://img.shields.io/badge/Internships-{total_rows:,}-blue?style=for-the-badge)](https://github.com/KSaifStack/jobscraper)
[![Updated](https://img.shields.io/badge/Updated-{today.replace(' ', '%20')}-orange?style=for-the-badge)](https://github.com/KSaifStack/jobscraper)

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
