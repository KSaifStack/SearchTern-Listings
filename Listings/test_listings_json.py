"""Runnable self-check for write_listings_json output hygiene.

No framework. Run from Listings/:  python test_listings_json.py
"""

import json
import os
import sys
import tempfile

import pandas as pd

import readme_generation
from readme_utils import clean_company_name, clean_link


def run_link_checks():
    # Oracle Cloud search-page link -> direct job URL
    assert clean_link(
        "https://ecnf.fa.us2.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX_1#301904"
    ) == "https://ecnf.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/301904"
    # non-numeric job id (Oracle W-requisitions)
    assert clean_link(
        "https://eofd.fa.us6.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=CX_1#W735657"
    ) == "https://eofd.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/W735657"
    # cased site token survives
    assert clean_link(
        "http://epuc.fa.ap1.oraclecloud.com/?keyword=&mode=jobs&lang=en&site_number=cx_4#29741"
    ) == "https://epuc.fa.ap1.oraclecloud.com/hcmUI/CandidateExperience/en/sites/cx_4/job/29741"
    # canonical Oracle URL is left alone (only tracking stripped)
    assert clean_link(
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210774074?utm_source=Simplify&ref=Simplify"
    ) == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210774074"
    # http upgraded to https, tracking dropped, duplicate param kept once
    assert clean_link(
        "https://careers.aqr.com/jobs?gh_jid=8156993&gh_jid=8156993&utm_source=Simplify&ref=Simplify"
    ) == "https://careers.aqr.com/jobs?gh_jid=8156993&gh_jid=8156993"
    assert clean_link("http://block.xyz/careers/jobs/5108007008?gh_jid=5108007008") \
        == "https://block.xyz/careers/jobs/5108007008?gh_jid=5108007008"
    # aggregator portals -> empty (dropped), incl. regional adzuna + comeet screens
    assert clean_link("https://www.adzuna.com/details/5878071325?utm_medium=api&utm_source=freehire.me") == ""
    assert clean_link("https://www.adzuna.co.uk/jobs/details/5876244768") == ""
    assert clean_link("https://www.adzuna.com.au/details/5875606216") == ""
    assert clean_link("https://sequoia-connect.com/job-description-details/?i=abc") == ""
    assert clean_link("https://himalayas.app/companies/x/jobs/y?utm_source=freehire.me") == ""
    assert clean_link("https://remoteok.com/remote-jobs/remote-junior-data-analyst-hiredbuddy-1135673") == ""
    assert clean_link("https://builtin.com/jobs/1234") == ""
    assert clean_link("https://www.indeed.com/viewjob?jk=abc123") == ""
    assert clean_link("") == ""

    # zapply.jobs short links all redirect to the generic /jobs/ page, so the
    # tracking code is decoded back to the canonical ATS job URL
    assert clean_link(
        "https://zapply.jobs/l/d/sr-WesternDigital-744000143171017?s=gh-new-grad-jobs-2027"
    ) == "https://jobs.smartrecruiters.com/WesternDigital/744000143171017"
    assert clean_link(
        "https://zapply.jobs/l/d/ashby-gritt-46af6e69-40fc-4e53-940e-a99757137523"
    ) == "https://jobs.ashbyhq.com/gritt/46af6e69-40fc-4e53-940e-a99757137523"
    assert clean_link(
        "https://zapply.jobs/l/d/lever-diversified-automation-827a092d-b8a3-4ca9-a84a-e8c236d1aabc"
    ) == "https://jobs.lever.co/diversified-automation/827a092d-b8a3-4ca9-a84a-e8c236d1aabc"
    assert clean_link(
        "https://zapply.jobs/l/d/greenhouse-rocketlab-7987159003"
    ) == "https://boards.greenhouse.io/rocketlab/jobs/7987159003"
    # company slugs containing hyphens survive (job id matched from the right)
    assert clean_link(
        "https://zapply.jobs/l/d/lever-woven-by-toyota-ba39a024-c4c3-4966-a696-95db0e1dc445"
    ) == "https://jobs.lever.co/woven-by-toyota/ba39a024-c4c3-4966-a696-95db0e1dc445"
    # unrecognized zapply kinds have no recoverable URL -> dropped
    assert clean_link("https://zapply.jobs/l/d/workday-bah-bah-jobs-R0249225") == ""
    assert clean_link("https://zapply.jobs/l/d/google-94172495052972742") == ""
    assert clean_link("https://zapply.jobs/l/d/amazon-4ad0d1fe-19dd-414e-812b-4daac6e6335f") == ""
    assert clean_link("https://zapply.jobs/jobs/") == ""

    # company names with a URL glued on get the URL stripped
    assert clean_company_name("Ridgeline https://boards.greenhouse.io/ridgeline/jobs/7990742003") == "Ridgeline"
    assert clean_company_name("Epic Games https://epicgames.com/careers/jobs/6183293004?gh_jid=6183293004") == "Epic Games"
    # already-tokenized URL remnant (no http:// prefix) is also cut
    assert clean_company_name("Ridgeline Https Boards Greenhouse Io Ridgeline Jobs 7990742003") == "Ridgeline"
    assert clean_company_name("Johns Hopkins https://careers.jhuapl.edu/jobs/59997?icims=1") == "Johns Hopkins"

    print("  test_listings_json: clean_link/company checks passed")


def run_checks():
    df = pd.DataFrame([
        {
            "company": "Acme", "role": "Software Engineer Intern",
            "location": "Austin, TX", "date": "2026-09-01T00:00:00+00:00",
            "link": "https://example.com/a", "is_remote": None,
            "salary_min": float("nan"), "salary_max": None,
            "salary_currency": None, "country_iso": "US",
            "job_type": "internship", "employment_type": None,
            "seniority": None, "source": None,
        },
        {  # no link -> must be dropped
            "company": "NoLink", "role": "Software Engineer",
            "location": "Remote", "date": "2026-09-01T00:00:00+00:00",
            "link": "", "is_remote": None, "salary_min": float("nan"),
            "salary_max": None, "salary_currency": None, "country_iso": "US",
            "job_type": "new_grad", "employment_type": None,
            "seniority": None, "source": None,
        },
        {  # duplicate of row 0 after cleaning -> must be dropped
            "company": "Acme ", "role": "Software Engineer Intern",
            "location": "Austin, TX", "date": "2026-09-02T00:00:00+00:00",
            "link": "https://example.com/dup", "is_remote": None,
            "salary_min": float("nan"), "salary_max": None,
            "salary_currency": None, "country_iso": "",
            "job_type": "internship", "employment_type": None,
            "seniority": None, "source": None,
        },
    ])

    with tempfile.TemporaryDirectory() as tmp:
        readme_generation.write_listings_json(df, output_dir=tmp)
        path = os.path.join(tmp, "pages", "listings.json")
        raw = open(path, encoding="utf-8").read()

        assert "NaN" not in raw, "literal NaN leaked into JSON"
        assert "NaT" not in raw, "literal NaT leaked into JSON"
        records = json.loads(raw)

    assert len(records) == 1, f"expected 1 clean record, got {len(records)}"
    rec = records[0]
    assert rec["link"] == "https://example.com/a"
    assert rec["company"] == "ACME"
    assert rec["country_iso"] == "US"
    assert rec["salary_min"] is None, "NaN salary must become null"
    assert rec["is_remote"] is None

    print("  test_listings_json: all checks passed")


if __name__ == "__main__":
    run_link_checks()
    run_checks()
    sys.exit(0)
