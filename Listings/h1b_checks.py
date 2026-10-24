"""Offline self-checks for the H-1B role-level pipeline (no network)."""
import sys

import pandas as pd

import h1b_enrich as H
from readme_utils import visa_cell

SAMPLE = (
    "<table><tr><th>EMPLOYER</th><th>JOB TITLE</th><th>BASE SALARY</th>"
    "<th>LOCATION</th><th>SUBMIT DATE</th><th>START DATE</th></tr>"
    "<tr><td>ACME LLC</td><td>SOFTWARE ENGINEER</td><td>150,000</td>"
    "<td>AUSTIN, TX</td><td>01/02/2025</td><td>02/01/2025</td></tr>"
    "<tr><td>ACME LLC</td><td>SOFTWARE ENGINEER</td><td>170,000</td>"
    "<td>AUSTIN, TX</td><td>01/03/2025</td><td>02/01/2025</td></tr>"
    "<tr><td>ACME LLC</td><td>DATA SCIENTIST</td><td>160,000</td>"
    "<td>AUSTIN, TX</td><td>01/04/2025</td><td>02/01/2025</td></tr></table>"
)


def check_parse():
    rows = H._parse_role_rows(SAMPLE)
    assert len(rows) == 3, rows
    assert rows[0]["title"] == "SOFTWARE ENGINEER"
    assert rows[0]["salary"] == 150000
    assert H._parse_role_rows("") == []
    assert H._parse_role_rows(None) == []
    print("  parse ok")


def check_aggregate():
    titles = H._aggregate_titles(H._parse_role_rows(SAMPLE))
    assert titles[0]["title"] == "SOFTWARE ENGINEER", titles
    assert titles[0]["count"] == 2
    assert titles[0]["median_salary"] == 160000, titles[0]
    assert len(titles) == 2
    print("  aggregate ok")


def check_match():
    titles = H._aggregate_titles(H._parse_role_rows(SAMPLE))
    assert H.match_title("Software Engineer Intern", titles)["count"] == 2
    assert H.match_title("Senior Software Engineer, Payments", titles)["count"] == 2
    assert H.match_title("Data Science Intern", titles)["title"] == "DATA SCIENTIST"
    assert H.match_title("Accountant", titles) is None
    assert H.match_title("", titles) is None
    assert H.match_title("Software Engineer", []) is None
    print("  match ok")


def check_columns():
    roles = {"acme": {"label": "Acme", "titles": H._aggregate_titles(H._parse_role_rows(SAMPLE))}}
    df = pd.DataFrame([
        {"company": "Acme, Inc.", "role": "Software Engineer Intern"},
        {"company": "Acme", "role": "Recruiter"},
        {"company": "Unknown Co", "role": "Software Engineer"},
    ])
    out = H.add_role_columns(df, roles)
    assert list(out["visa_role"]) == ["SOFTWARE ENGINEER", "", ""], list(out["visa_role"])
    assert list(out["visa_role_count"]) == [2, 0, 0]
    assert out["visa_median_salary"][0] == 160000
    assert out["visa_median_salary"][1] is None

    out = H.add_visa_column(out, {"acme": 40})
    assert list(out["visa_sponsor"]) == ["true", "true", ""]
    assert [visa_cell(r) for _, r in out.iterrows()] == ["role", "company", ""]
    print("  columns ok")


def check_names():
    assert H.normalize_name("Acme, Inc.") == "acme"
    assert H.normalize_name("GOOGLE LLC") == "google"
    assert H.normalize_name("") == ""
    assert H.normalize_name(None) == ""
    print("  names ok")


if __name__ == "__main__":
    check_parse()
    check_aggregate()
    check_match()
    check_columns()
    check_names()
    print("h1b checks passed")
    sys.exit(0)
