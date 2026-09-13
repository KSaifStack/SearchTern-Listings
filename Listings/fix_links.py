"""One-shot cleanup of already-committed listings: repair weird links.

Patches pages/listings.json and the README tables in place, using the same
clean_link/clean_company_name rules the pipeline now applies on regeneration.
Run from Listings/:  python fix_links.py
"""

import glob
import html
import json
import os
import re

from readme_utils import clean_company_name, clean_link

ROOT = os.path.join(os.path.dirname(__file__), "..")


def fix_listings_json():
    path = os.path.join(ROOT, "pages", "listings.json")
    records = json.load(open(path, encoding="utf-8"))

    out, seen = [], set()
    for rec in records:
        link = clean_link(rec.get("link"))
        if not link:
            continue
        company = clean_company_name(rec.get("company") or "")
        if not company:
            continue
        key = (company, rec.get("role") or "", rec.get("location") or "")
        if key in seen:
            continue
        seen.add(key)
        rec["link"] = link
        rec["company"] = company
        out.append(rec)

    json.dump(out, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"listings.json: {len(records)} -> {len(out)} records")


_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]*)"')


def _fix_row(row):
    hrefs = _HREF_RE.findall(row)
    if not hrefs:
        return row
    cleaned = [clean_link(html.unescape(h)) for h in hrefs]
    if any(not c for c in cleaned):
        return None
    it = iter(cleaned)
    return _HREF_RE.sub(lambda m: f'href="{html.escape(next(it), quote=True)}"', row)


def fix_readme():
    files = [os.path.join(ROOT, "README.md")] + sorted(
        glob.glob(os.path.join(ROOT, "pages", "README-*.md"))
    )
    dropped_total = 0
    for path in files:
        content = open(path, encoding="utf-8").read()
        rows = _ROW_RE.findall(content)
        if not rows:
            continue
        fixed_map, dropped, changed = {}, 0, False
        for row in rows:
            fixed = _fix_row(row)
            if fixed is None:
                dropped += 1
                fixed_map[row] = ""
            else:
                fixed_map[row] = fixed
                changed |= fixed != row
        if changed or dropped:
            content = _ROW_RE.sub(lambda m: fixed_map[m.group(0)], content)
            open(path, "w", encoding="utf-8").write(content)
        dropped_total += dropped
        print(f"{os.path.relpath(path, ROOT)}: dropped {dropped} rows")
    print(f"README: {dropped_total} aggregator/bad-link rows removed")


if __name__ == "__main__":
    fix_listings_json()
    fix_readme()