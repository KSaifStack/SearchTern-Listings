"""Runnable self-check for markdown_sources JSON + markdown parsers.

No framework. Run from Listings/:  python test_md_parsers.py
"""

import json
import sys

import markdown_sources as ms


def run_checks():
    sample = {
        "updatedAt": "2026-09-16T09:16:16.907Z",
        "jobs": [
            {
                "company": "Cox",
                "title": "Entry Level Software Engineer - Austin, TX",
                "location": "Austin, TX",
                "eligibility": "New Grad",
                "posted": "2026-09-15",
                "url": "https://applyguy.ai/jobs/1",
                "listingUrl": "https://cox.wd1.myworkdayjobs.com/job/1",
            },
            {"company": "NoLink", "title": "Engineer", "location": "Remote", "posted": "2026-09-15"},
            {"company": "Bare", "title": "Engineer Intern", "location": "New York, NY",
             "posted": "2026-09-14", "url": "https://applyguy.ai/jobs/2"},
        ],
    }
    records = ms._parse_json_source(json.dumps(sample))
    assert len(records) == 2, f"expected 2 (bare url), got {len(records)}"
    assert records[0]["company"] == "Cox"
    assert records[0]["role"] == "Entry Level Software Engineer - Austin, TX"
    assert records[0]["location"] == "Austin, TX"
    assert records[0]["date"] == "2026-09-15"
    assert records[0]["link"] == "https://cox.wd1.myworkdayjobs.com/job/1", "listingUrl preferred"
    assert records[0]["is_remote"] == "false"
    assert records[1]["link"] == "https://applyguy.ai/jobs/2", "url fallback when listingUrl missing"

    assert ms._parse_json_source("not json") == []
    assert ms._parse_json_source(json.dumps({"updatedAt": "x", "jobs": "nope"})) == []
    assert ms._parse_json_source("") == []

    bare_list = json.dumps([
        {"company": "TikTok", "title": "SWE Intern", "location": "San Jose, CA",
         "url": "https://themuse.com/jobs/tiktok/1", "posted_at": "2026-08-04T20:22:16Z"},
        {"company": "Jabil", "title": "Data Analytics Intern", "location": "Lexington, KY",
         "url": "https://themuse.com/jobs/jabil/2"},
    ])
    records = ms._parse_json_source(bare_list)
    assert len(records) == 2, f"bare list: expected 2, got {len(records)}"
    assert records[0]["company"] == "TikTok"
    assert records[0]["date"] == "2026-08-04T20:22:16Z", "posted_at accepted"
    assert records[1]["link"] == "https://themuse.com/jobs/jabil/2"
    assert records[1]["is_remote"] == "false"

    md = """| Company | Role | Location | Apply | Posted |
|---|---|---|---|---|
| Acme | SWE Intern | Austin, TX | [Apply](https://acme.com/x) | 2026-09-16 |
| Beta | Research Intern | Seattle, WA | <a href="https://beta.com/y"><img/></a> | 2d |
"""
    records = ms._parse_md_source(md)
    assert len(records) == 2
    assert records[0]["link"] == "https://acme.com/x"
    assert records[1]["link"] == "https://beta.com/y", "html <a> fallback"
    assert records[1]["date"], "relative '2d' date parsed"

    print("  test_md_parsers: all checks passed")


if __name__ == "__main__":
    run_checks()
    sys.exit(0)