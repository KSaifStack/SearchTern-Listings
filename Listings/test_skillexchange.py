"""Regression tests for the SkillExchange flight-payload parser.

2026-11-11 the board moved job cards inside a React Query dehydrated payload
("state":{"data":{"jobs":[...]}}) and every card stopped beginning with
{"slugId":, so the old anchor parsed 0 rows from a page holding 50 jobs.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skillexchange as SE


def _flight(payload):
    """Embed a flight payload the way the site does (quotes backslash-escaped)."""
    return '<script>self.__next_f.push([1,"' + payload.replace('"', '\\"') + '"])</script>'


def _job(slug_id, role="Software Engineer Intern", company="Example Corp"):
    return {
        "id": "job_" + str(slug_id),
        "slugId": slug_id,
        "role": role,
        "workMode": "Remote",
        "locations": ["Remote"],
        "publishedDate": "2026-11-11T16:00:01.076Z",
        "minSalary": 0,
        "maxSalary": 0,
        "companyInfo": {"companyName": company},
    }


def _nested_html(jobs):
    data = {
        "state": {
            "mutations": [],
            "queries": [
                {"dehydratedAt": 1, "state": {"data": {"jobs": jobs}}}
            ],
        }
    }
    return _flight("5:" + json.dumps(["$", "$L1c", None, data]))


def test_nested_jobs_array_is_parsed():
    html = _nested_html([_job(57606), _job(57607, role="Data Analyst")])
    jobs = SE._extract_jobs(html)
    assert [j["slugId"] for j in jobs] == [57606, 57607]
    assert jobs[1]["role"] == "Data Analyst"


def test_duplicate_slug_ids_are_collapsed():
    dup = _job(57606)
    html = _nested_html([dup, dict(dup)])
    assert len(SE._extract_jobs(html)) == 1


def test_legacy_slugid_objects_still_parse():
    legacy = _flight(
        '2:{"slugId":42,"role":"SWE Intern",'
        '"companyInfo":{"companyName":"Old Co"}}'
    )
    jobs = SE._extract_jobs(legacy)
    assert [j["slugId"] for j in jobs] == [42]


def test_payload_without_jobs_is_empty_not_an_error():
    assert SE._extract_jobs(_flight('1:{"state":{"data":{"jobs":[]}}}')) == []
    assert SE._extract_jobs(_flight('1:{"state":{"data":{"jobs":"nope"}}}')) == []


def test_normalize_builds_rows_with_working_links():
    jobs = SE._extract_jobs(_nested_html([_job(57606)]))
    rows = SE._normalize(jobs, infer_country=lambda loc: "US")
    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "Example Corp"
    assert row["link"] == "https://skillexchange.xyz/job/software-engineer-intern/57606"
    assert row["country_iso"] == "US"
    assert row["salary_min"] is None
