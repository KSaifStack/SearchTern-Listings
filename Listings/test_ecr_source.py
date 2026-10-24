"""Checks for the Early Career Radar source module."""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecr_source as E


def _flight(blob):
    return "self.__next_f.push([1," + json.dumps(blob) + "])"


def _job(**kw):
    base = {
        "id": "job_1234abcd5678ef90",
        "company": "Example Corp",
        "title": "Software Engineer Intern",
        "location": "Austin, TX",
        "mode": "Hybrid",
        "postedAt": "2026-10-21T00:00:00Z",
        "applyUrl": "https://boards.greenhouse.io/examplecorp/jobs/1",
        "placeCountries": ["United States"],
    }
    base.update(kw)
    return base


def run():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            fails += 1

    # Slug shapes: only real job pages, not category or landing pages.
    check("plain job url matches", bool(E.JOB_PATH_RE.search("/jobs/job_034b6fc11f0201ee")))
    check("new-grad job url matches", bool(E.JOB_PATH_RE.search("/new-grad/jobs/job_04bd42a93f828726")))
    check("category page rejected", not E.JOB_PATH_RE.search("/new-grad"))
    check("landing page rejected", not E.JOB_PATH_RE.search("/summer-internships"))
    check("short hex rejected", not E.JOB_PATH_RE.search("/jobs/job_abc"))

    check("tier hint intern", E._row_is_newgrad("/jobs/job_0123456789ab") is False)
    check("tier hint new-grad", E._row_is_newgrad("/new-grad/jobs/job_0123456789ab") is True)

    # Geography. The real failure mode is addressCountry missing, so these
    # all arrive with country_iso blank and must be decided from the location.
    cases = [
        ({"country_iso": "", "location": "London, UK", "role": "Quant Intern"}, False),
        ({"country_iso": "", "location": "Costa Rica, San Jose", "role": "AI SWE Intern"}, False),
        ({"country_iso": "", "location": "Bangalore, India", "role": "Data Intern"}, False),
        ({"country_iso": "", "location": "Toronto, ON", "role": "Co-op"}, False),
        ({"country_iso": "", "location": "Vancouver, CA", "role": "Co-op"}, False),
        ({"country_iso": "", "location": "Paris, FR", "role": "Intern"}, False),
        ({"country_iso": "", "location": "Zurich, Switzerland", "role": "Quant"}, False),
        ({"country_iso": "", "location": "San Jose, Costa Rica", "role": "Intern"}, False),
        ({"country_iso": "", "location": "Austin, TX, US", "role": "SWE Intern"}, True),
        ({"country_iso": "", "location": "Livermore, CA", "role": "Intern"}, True),
        ({"country_iso": "", "location": "Santa Clara, CA", "role": "Product Intern"}, True),
        ({"country_iso": "", "location": "New York, NY", "role": "BA Internship"}, True),
        ({"country_iso": "", "location": "Cambridge, MA", "role": "Intern"}, True),
        # Multi-location: country only appears on the last segment.
        ({"country_iso": "", "location": "Tampa, FL · Hanover, NJ · Cambridge, MA, US", "role": "Intern"}, True),
        ({"country_iso": "", "location": "Miami, FL · Chicago, IL · Toronto, ON", "role": "Intern"}, False),
        ({"country_iso": "", "location": "Remote", "role": "Intern"}, True),
        ({"country_iso": "", "location": "Texas, US", "role": "Undergrad Intern"}, True),
        # Explicit ISO wins over any string reading.
        ({"country_iso": "US", "location": "London, UK", "role": "Intern"}, True),
        ({"country_iso": "GB", "location": "London", "role": "Intern"}, False),
        ({"country_iso": "", "location": "", "role": "Intern"}, False),
    ]
    for row, expected in cases:
        got, why = E._country_backstop(row)
        check(
            f"backstop {'keep' if expected else 'drop'}: {row['location'][:38] or '(blank)'} [{why}]",
            got == expected,
        )

    # Every US state abbreviation must survive the trailing-token rule; a state
    # that collides with a country code (CA, IN, OR, DE, GA, MA) is the risk.
    collide = ["CA", "IN", "OR", "DE", "GA", "MA", "MS", "MT", "NE", "PA", "AL", "LA"]
    for st in collide:
        ok, why = E._country_backstop({"country_iso": "", "location": f"Springfield, {st}", "role": "Intern"})
        check(f"state {st} kept as US ({why})", ok)

    # Listing-page payload: the fast path that replaced the sitemap crawl.
    jobs = [
        _job(),
        _job(id="job_2", title="New Grad Software Engineer", applyUrl="",
             location="New York, NY", mode="Remote"),
        _job(id="job_3", placeCountries=["Germany"], location="Berlin, Germany"),
        _job(id="job_4", placeCountries=[], location="Tampa, FL"),
        _job(id="job_5", company=""),
        _job(id="job_6", placeCountries=[], location="Berlin, DE"),
        _job(id="job_7", closed=True),
    ]
    html = "<html><script>" + _flight('{"initialJobs":' + json.dumps(jobs) + "}") + "</script></html>"

    check("payload yields every embedded record", len(E._listing_jobs(html)) == len(jobs))
    check("no payload yields nothing", E._listing_jobs("<html></html>") == [])

    row = E._listing_row(jobs[0])
    check("us row is kept", row is not None)
    if row:
        check("apply url wins as the link", row["link"] == jobs[0]["applyUrl"])
        check("direct apply flagged", row["direct_apply"] == "true")
        check("country is US", row["country_iso"] == "US")
        check("internship tier hint", row["tier_hint"] == "internship")
        check("hybrid is not remote", row["is_remote"] == "false")
        check("date truncated to a day", row["date"] == "2026-10-21")

    grad = E._listing_row(jobs[1])
    check("new-grad tier hint", bool(grad) and grad["tier_hint"] == "new-grad")
    check("fallback link points at the aggregator",
          bool(grad) and grad["link"] == f"{E.SITE}/jobs/job_2")
    check("no apply url means not direct apply", bool(grad) and grad["direct_apply"] == "false")
    check("remote mode flagged", bool(grad) and grad["is_remote"] == "true")

    check("non-us row dropped", E._listing_row(jobs[2]) is None)
    check("us row without a country kept", E._listing_row(jobs[3]) is not None)
    check("row without a company dropped", E._listing_row(jobs[4]) is None)
    check("foreign location dropped", E._listing_row(jobs[5]) is None)

    original = E.S._polite_get
    try:
        E.S._polite_get = lambda *a, **k: html
        frame = E.harvest_listing(verbose=False)
    finally:
        E.S._polite_get = original
    check("closed postings skipped", len(frame) == 3)
    check("frame carries the standard columns", set(E.S.JOB_COLUMNS) <= set(frame.columns))
    check("every row is stamped as ecr", (frame["source"] == "ecr").all())

    print(f"\n{'all checks passed' if not fails else f'{fails} FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())