"""Checks for the Early Career Radar source module."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecr_source as E


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

    print(f"\n{'all checks passed' if not fails else f'{fails} FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run())