import json
import re
import time

import pandas as pd

from readme_utils import http_get

# SkillExchangeXYZ job board. Jobs are rendered server-side into Next.js flight
# payloads (self.__next_f.push chunks); each job card embeds a full JSON object
# keyed by "slugId". Pages are newest-first at /jobs?page=N.
PAGE_URL = "https://skillexchange.xyz/jobs?page={page}"
# ponytail: cap at the newest N pages. Yield falls off fast (general tech board,
# not internship-focused); raise if a full-board sweep is ever wanted.
MAX_PAGES = 12
PAGE_DELAY_SECS = 2  # honors robots.txt Crawl-delay

JOB_LINK = "https://skillexchange.xyz/job/{slug}/{slug_id}"

_PUSH_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def _backslash_unescape(s):
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in "\\\"rnt":
                out.append({'\\': "\\", '"': '"', 'r': "\r", 'n': "\n", 't': "\t"}[nxt])
                i += 2
            elif nxt == "u":
                out.append(s[i:i + 6].encode().decode("unicode_escape"))
                i += 6
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _extract_jobs(html):
    text = _backslash_unescape("".join(_PUSH_CHUNK_RE.findall(html)))
    jobs = []
    start = 0
    while True:
        i = text.find('{"slugId":', start)
        if i < 0:
            break
        depth = 0
        j = i
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(text):
            break
        try:
            jobs.append(json.loads(text[i:j + 1]))
        except json.JSONDecodeError:
            pass
        start = j + 1
    return jobs


def _slugify(role):
    slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
    return slug or "job"


def _normalize(jobs, infer_country):
    rows = []
    for job in jobs:
        role = (job.get("role") or "").strip()
        if not role:
            continue
        info = job.get("companyInfo") or {}
        company = info.get("companyName") or ""
        if not company:
            continue
        locations = [loc for loc in (job.get("locations") or []) if loc and loc.lower() != "anywhere"]
        work_mode = (job.get("workMode") or "").strip()
        if locations:
            loc = ", ".join(locations)
        else:
            loc = work_mode or "Remote"
        min_sal = job.get("minSalary") or 0
        max_sal = job.get("maxSalary") or 0
        rows.append({
            "company": company,
            "role": role,
            "location": loc,
            "date": job.get("publishedDate", ""),
            "link": JOB_LINK.format(slug=_slugify(role), slug_id=job.get("slugId", "")),
            "is_remote": str(work_mode.lower() == "remote").lower(),
            "salary_min": min_sal if min_sal > 0 else None,
            "salary_max": max_sal if max_sal > 0 else None,
            "salary_currency": "USD" if (min_sal > 0 or max_sal > 0) else None,
            "country_iso": infer_country(loc) if infer_country else "",
        })
    return rows


def fetch_and_parse(infer_country=None):
    all_jobs = []
    for page in range(1, MAX_PAGES + 1):
        resp = http_get(PAGE_URL.format(page=page), timeout=30, retries=3)
        if resp is None or resp.status_code != 200:
            print(
                f"  x SkillExchange page {page}: "
                f"{resp.status_code if resp is not None else 'unreachable'} — stopped"
            )
            break
        jobs = _extract_jobs(resp.text)
        all_jobs.extend(jobs)
        print(f"  SkillExchange page {page}: {len(jobs):,} rows")
        if page < MAX_PAGES:
            time.sleep(PAGE_DELAY_SECS)

    df = pd.DataFrame(_normalize(all_jobs, infer_country))
    if not df.empty:
        df = df.drop_duplicates(subset=["company", "role", "location"], keep="first")
    return df, [("SkillExchange board", len(all_jobs))]


if __name__ == "__main__":
    df, stats = fetch_and_parse()
    print(stats)
    assert not df.empty, "no jobs parsed — parser regression"
    cols = {"company", "role", "location", "date", "link",
            "is_remote", "salary_min", "salary_max", "salary_currency", "country_iso"}
    assert cols <= set(df.columns), f"missing columns: {cols - set(df.columns)}"
    assert df["link"].str.startswith("https://skillexchange.xyz/job/").all()
    print(f"self-check ok: {len(df):,} rows")