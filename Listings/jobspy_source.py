import pandas as pd


def _to_rows(df):
    rows = []
    for r in df.to_dict("records"):
        title = str(r.get("title") or "").strip()
        company = str(r.get("company") or "").strip()
        location = str(r.get("location") or "").strip()
        job_url = str(r.get("job_url") or "").strip()
        if not title or not company or not location or not job_url:
            continue
        posted = r.get("date_posted") or ""
        if posted:
            try:
                posted = pd.to_datetime(posted, errors="coerce", utc=True)
                if pd.isna(posted):
                    posted = ""
                else:
                    posted = posted.strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                posted = str(posted)
        min_sal = r.get("min_amount") or r.get("salary_min") or None
        max_sal = r.get("max_amount") or r.get("salary_max") or None
        currency = r.get("currency") or r.get("salary_currency") or None
        if min_sal is not None and not pd.isna(min_sal):
            min_sal = float(min_sal)
        else:
            min_sal = None
        if max_sal is not None and not pd.isna(max_sal):
            max_sal = float(max_sal)
        else:
            max_sal = None
        remote = r.get("is_remote") or ""
        rows.append({
            "company": company,
            "role": title,
            "location": location,
            "date": posted,
            "link": job_url,
            "is_remote": str(str(remote).lower() in ("true", "1", "yes")).lower(),
            "salary_min": min_sal,
            "salary_max": max_sal,
            "salary_currency": currency if currency and not pd.isna(currency) else None,
            "country_iso": "",
        })
    return rows


_SITE_SEARCHES = [
    # (site_name, search_term, hours_old, results_wanted)
    ("linkedin", "software engineer internship", 168, 250),
    ("linkedin", "new grad software engineer", 168, 250),
    ("linkedin", "data science machine learning intern", 168, 200),
    ("linkedin", "finance business analyst internship", 168, 200),
    ("indeed", "software engineer internship OR new grad OR entry level", 72, 200),
    ("zip_recruiter", "software engineer internship OR new grad OR entry level", 72, 200),
    ("google", "software engineer internship OR new grad jobs near United States since yesterday", 72, 200),
]


def fetch_and_parse(infer_country=None):
    # ponytail: CI hits these boards from a datacenter IP — Expect the boards to 429/block
    # the runner after a few runs. The reliable path is running this module locally from a
    # residential IP; CI degrades gracefully to an empty frame whenever blocked. Swap in
    # `proxies=[...]` if hosted scraping ever needs to be dependable.
    try:
        import jobspy
    except ImportError:
        print("  x JobSpy: lib not installed (pip install python-jobspy) — skipped")
        return pd.DataFrame(), []

    frames = []
    stats = []
    for site, term, hours, wanted in _SITE_SEARCHES:
        kwargs = dict(
            site_name=[site],
            search_term=term,
            results_wanted=wanted,
            hours_old=hours,
            verbose=0,
        )
        if site == "indeed":
            kwargs["country_indeed"] = "USA"
        if site == "google":
            kwargs["google_search_term"] = term + " jobs near United States since yesterday"
        if site == "linkedin":
            # JobSpy follows LinkedIn guest-job-view links; descriptions stay off by
            # default (one extra request per job) — titles/companies/locations still land.
            kwargs["linkedin_fetch_description"] = False
        try:
            df = jobspy.scrape_jobs(**kwargs)
        except Exception as e:
            print(f"  x JobSpy {site}: {type(e).__name__}: {e}")
            continue
        if df is None or df.empty:
            print(f"  x JobSpy {site}: no rows returned")
            continue
        frames.append(df)
        stats.append((f"jobspy ({site})", len(df)))
        print(f"  ✓ JobSpy {site}: {len(df):,} rows")

    if not frames:
        print("  x JobSpy: no rows returned")
        return pd.DataFrame(), []

    df = pd.concat(frames, ignore_index=True)
    rows = _to_rows(df)
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.drop_duplicates(subset=["company", "role", "location"], keep="first")
        if infer_country:
            out["country_iso"] = out["location"].map(infer_country)
    print(f"  ✓ JobSpy total: {len(out):,} rows from {len(df):,} scraped")
    return out, stats


if __name__ == "__main__":
    df, stats = fetch_and_parse()
    print(stats)
    assert not df.empty, "no jobs parsed — scraper regression"
    cols = {"company", "role", "location", "date", "link",
            "is_remote", "salary_min", "salary_max", "salary_currency", "country_iso"}
    assert cols <= set(df.columns), f"missing columns: {cols - set(df.columns)}"
    assert df["link"].str.startswith("http").all()
    print(f"self-check ok: {len(df):,} rows")