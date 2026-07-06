import glob
import json
import os
from datetime import datetime, timezone

import readme_rendering
from readme_utils import (
    APPLY_BUTTON,
    GITHUB_FILE_SIZE_LIMIT,
    INACTIVE_THRESHOLD_DAYS,
    MAX_COMPANY_LEN,
    MAX_ROLE_LEN,
    NORMALIZED_BLOCKED_COMPANIES,
    SIZE_BUFFER,
    build_table,
    clean_company_name,
    days_display,
    format_company,
    format_location,
    is_english,
    normalize_company,
    truncate,
)



def write_listings_json(dataframe, output_dir="."):
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    clean_records = []

    for _, row in dataframe.iterrows():
        company = str(row["company"]).strip()
        role = str(row["role"]).strip()
        date = str(row["date"]).strip()

        cleaned_company = clean_company_name(company)
        if not cleaned_company:
            continue
        if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
            continue
        if not is_english(role) or not is_english(cleaned_company):
            continue

        try:
            posted = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - posted).days > INACTIVE_THRESHOLD_DAYS:
                continue
        except Exception:
            pass

        record = row.to_dict()
        record["company"] = truncate(cleaned_company, MAX_COMPANY_LEN)
        record["role"] = truncate(role, MAX_ROLE_LEN)
        clean_records.append(record)

    export_path = os.path.join(pages_dir, "listings.json")
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(clean_records, f, indent=2, ensure_ascii=False, default=str)

    print(f"Wrote {export_path} — {len(clean_records):,} listings")



def generate_readme(dataframe, output_dir="."):
    """
    Writes job listings into README files split by GitHub's render limit.
    Page 1 → README.md (root)
    Page 2+ → pages/README-2.md, pages/README-3.md, etc.
    """
    pages_dir = os.path.join(output_dir, "pages")

    if os.path.exists(pages_dir):
        old_pages = glob.glob(os.path.join(pages_dir, "README-*.md"))
        for old_file in old_pages:
            os.remove(old_file)

    all_rows = []
    prev_company = None
    prev_age = None
    skipped_blocked = 0
    skipped_inactive = 0
    skipped_foreign = 0
    skipped_oracle = 0

    for _, row in dataframe.iterrows():
        company = str(row["company"]).strip()
        role = str(row["role"]).strip()
        location = str(row["location"]).strip()
        date = str(row["date"]).strip()
        link = str(row["link"]).strip()

        cleaned_company = clean_company_name(company)
        if not cleaned_company:
            skipped_oracle += 1
            continue

        if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
            skipped_blocked += 1
            continue

        if not is_english(role) or not is_english(cleaned_company):
            skipped_foreign += 1
            continue

        try:
            posted = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - posted).days > INACTIVE_THRESHOLD_DAYS:
                skipped_inactive += 1
                continue
        except Exception:
            pass

        company = truncate(cleaned_company, MAX_COMPANY_LEN)
        role = truncate(role, MAX_ROLE_LEN)
        age = days_display(date)
        company_cell = format_company(company, prev_company, age, prev_age, link)
        prev_company = company
        prev_age = age

        all_rows.append(
            "<tr>\n"
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{company_cell}</td>\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{__import__("html").escape(role)}</td>\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{format_location(location)}</td>\n'
            f'<td align="center" style="white-space:nowrap; overflow-wrap:anywhere; width:8%;"><a href="{__import__("html").escape(link, quote=True)}" style="display:inline-block; max-width:100%;"><img src="{APPLY_BUTTON}" width="60" alt="Apply" style="display:block; max-width:100%; height:auto; margin:0 auto;"></a></td>\n'
            f'<td style="white-space:nowrap;">{age}</td>\n'
            "</tr>"
        )

    total_rows = len(all_rows)

    pages = []
    current_page_rows = []
    current_size = 0

    for row_html in all_rows:
        row_size = len(row_html.encode("utf-8"))
        if current_size + row_size > (GITHUB_FILE_SIZE_LIMIT - SIZE_BUFFER) and current_page_rows:
            pages.append(current_page_rows)
            current_page_rows = [row_html]
            current_size = row_size
        else:
            current_page_rows.append(row_html)
            current_size += row_size

    if current_page_rows:
        pages.append(current_page_rows)

    total_pages = len(pages)
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

        nav = build_header(page_num, total_pages, total_rows, dataframe if page_num == 1 else None)
        content = nav + build_table(page_rows) + f"\n\n{readme_rendering.build_nav(page_num, total_pages)}\n"

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
