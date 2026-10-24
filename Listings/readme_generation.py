import glob
import html
import json
import math
import os
from datetime import datetime, timezone
from email.utils import format_datetime

from classify import role_category
from readme_rendering import build_header, generate_country_pages
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
    clean_link,
    clean_location,
    days_display,
    format_company,
    format_location,
    is_english,
    normalize_company,
    truncate,
    visa_cell,
)



def _json_safe(value):
    """NaN and NaT compare unequal to themselves; emit them as null so the
    output is valid JSON instead of the non-standard `NaN`/`NaT` tokens."""
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        pass
    return value


def _build_json_records(dataframe):
    """Apply the shared final filters and return the JSON-ready records."""
    clean_records = []
    seen = set()

    for _, row in dataframe.iterrows():
        company = str(row["company"]).strip()
        role = str(row["role"]).strip()
        date = str(row["date"]).strip()
        link = clean_link(row.get("link"))

        if not link.startswith(("http://", "https://")):
            continue

        row = row.copy()
        row["link"] = link

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
        # Keep JSON strict: NaN/Inf → null, or strict parsers (browser
        # fetch().json()) hard-fail on the whole feed.
        for k, v in record.items():
            if isinstance(v, float) and not math.isfinite(v):
                record[k] = None
        record["company"] = cleaned_company
        record["role"] = role
        record["category"] = role_category(role)
        record["location"] = clean_location(record.get("location", ""))
        if not (record.get("country_iso") or "").strip():
            record["country_iso"] = "US"
        record = {k: _json_safe(v) for k, v in record.items()}

        key = (record["company"], record["role"], record["location"])
        if key in seen:
            continue
        seen.add(key)
        clean_records.append(record)

    return clean_records


def write_listings_json(dataframe, output_dir=".", mirror_dataframe=None):
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    clean_records = _build_json_records(dataframe)

    export_path = os.path.join(pages_dir, "listings.json")
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(clean_records, f, indent=2, ensure_ascii=False, default=str, allow_nan=False)

    print(f"Wrote {export_path} — {len(clean_records):,} listings")

    # The deployed backend scrapes pages/searchtern.json (not listings.json),
    # so write it in the same run instead of syncing by hand (a manual sync
    # once shipped an uncategorized copy and broke the app's role-category
    # filter). When a `mirror_dataframe` is supplied, searchtern.json is the
    # broader feed — every role type, not just the CS/tech subset.
    mirror_records = (
        clean_records if mirror_dataframe is None else _build_json_records(mirror_dataframe)
    )
    mirrored_path = os.path.join(pages_dir, "searchtern.json")
    with open(mirrored_path, "w", encoding="utf-8") as f:
        json.dump(mirror_records, f, indent=2, ensure_ascii=False, default=str, allow_nan=False)

    print(f"Wrote {mirrored_path} — {len(mirror_records):,} listings")

    

def write_priority_json(dataframe, output_dir="."):
    """Write the small, high-frequency "fast lane" feed.

    Only the curated priority boards' recent postings live here, so a workflow
    can refresh it every few minutes without committing the 38MB searchtern.json.
    Output is sorted and stripped of volatile fields (observed_at, which is
    restamped every run) so the file only changes when a posting actually does —
    otherwise the fast-lane workflow would commit churn on every cycle.
    """
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    if dataframe is None or dataframe.empty:
        records = []
    else:
        records = _build_json_records(dataframe)
    for record in records:
        record.pop("observed_at", None)

    records.sort(key=lambda r: (
        str(r.get("company", "")).lower(),
        str(r.get("role", "")).lower(),
        str(r.get("location", "")).lower(),
        str(r.get("link", "")),
    ))

    path = os.path.join(pages_dir, "priority.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False, default=str, allow_nan=False)

    print(f"Wrote {path} — {len(records):,} priority listings")


def _feed_time(record):
    try:
        value = datetime.fromisoformat(str(record.get("date", "")).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    except ValueError:
        return datetime.now(timezone.utc)


def write_feed_xml(dataframe, output_dir=".", limit=200):
    """Write a newest-first RSS 2.0 feed of the live listings.

    Feed readers and importer tools (IFTTT, Zapier, Discord bots) can subscribe
    here instead of polling the multi-MB JSON, and each item links straight to
    the application URL. Rows without a date are stamped with the run time so
    the feed never contains silent gaps.
    """
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    if dataframe is None or dataframe.empty:
        records = []
    else:
        records = _build_json_records(dataframe)
    records.sort(key=_feed_time, reverse=True)
    records = records[:limit]

    items = []
    for record in records:
        title = f"{record['role']} at {record['company']}"
        bits = [b for b in (record.get("location"), record.get("category"),
                            record.get("job_type")) if b]
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(title)}</title>\n"
            f"      <link>{html.escape(record['link'])}</link>\n"
            f"      <guid isPermaLink=\"true\">{html.escape(record['link'])}</guid>\n"
            f"      <pubDate>{format_datetime(_feed_time(record))}</pubDate>\n"
            f"      <description>{html.escape(' | '.join(str(b) for b in bits))}</description>\n"
            "    </item>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>SearchTern — new internships and new-grad roles</title>\n"
        "    <link>https://searchtern.net</link>\n"
        "    <description>Fresh software engineering internships and entry-level roles, "
        "posting by posting.</description>\n"
        "    <language>en-us</language>\n"
        f"    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>\n"
        '    <atom:link href="https://raw.githubusercontent.com/KSaifStack/SearchTern-Listings/main/pages/feed.xml" rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items) + ("\n" if items else "")
        + "  </channel>\n"
        "</rss>\n"
    )

    path = os.path.join(pages_dir, "feed.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)

    print(f"Wrote {path} — {len(records):,} feed items")


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
        link = clean_link(row.get("link"))

        cleaned_company = clean_company_name(company)
        if not cleaned_company:
            skipped_oracle += 1
            continue

        if not link:
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
            f'<td style="text-align:center; white-space:nowrap;">{visa_cell(row)}</td>\n'
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
        content = nav + build_table(page_rows) + f"\n\n{__import__('readme_rendering').build_nav(page_num, total_pages)}\n"

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
