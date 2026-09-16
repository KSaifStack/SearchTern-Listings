"""Self-check for the shared classification rules and helpers.

Run: python test_classify.py  (stdlib only — no pandas/duckdb needed)
"""
import re
import sys

import classify
from readme_utils import canonical_url, clean_company_name

TECH = re.compile(classify.TECH_KEYWORDS_RE, re.IGNORECASE)
INTERN = re.compile(classify.LISTINGS_INTERN_RE, re.IGNORECASE)
NEWGRAD = re.compile(classify.LISTINGS_NEWGRAD_RE, re.IGNORECASE)
EXCLUDE = re.compile(classify.TITLE_EXCLUDE_RE, re.IGNORECASE)

checks = [
    # (label, got==expected)
    # Word-boundary: "ui" must never match inside "Suite" / "Recruiting".
    ("'Suite' is not tech", TECH.search("Suite") is None),
    ("'Recruiting' is not tech", TECH.search("Recruiting") is None),
    ("'Recruiter' is not tech", TECH.search("Recruiter") is None),
    ("'Software Engineer' is tech", TECH.search("Software Engineer") is not None),
    ("'ML Intern' is tech", TECH.search("ML Intern") is not None),
    ("'Data Scientist' is tech", TECH.search("Data Scientist") is not None),
    # The Cooper St / `cooper` leak: "co-op"/"coop" must not match "cooper".
    ("'cooper st' is not an intern role",
     INTERN.search("Shift Manager, Cooper St") is None),
    ("'Co-op' is an intern role", INTERN.search("Software Co-op") is not None),
    ("'intern' matches", INTERN.search("2027 Intern") is not None),
    ("'internship' matches", INTERN.search("Internship") is not None),
    ("'student' matches", INTERN.search("Student Researcher") is not None),
    ("'stage' matches", INTERN.search("Stage Analyst") is not None),
    ("'parking' does not match intern", INTERN.search("Parking Attendant") is None),
    ("'new grad' matches newgrad", NEWGRAD.search("New Grad SWE") is not None),
    ("'entry-level' matches newgrad", NEWGRAD.search("Entry-Level Engineer") is not None),
    ("'Senior Manager' excluded", EXCLUDE.search("Senior Manager") is not None),
    ("'Senior Software Engineer Intern' not excluded",
     EXCLUDE.search("Senior Software Engineer Intern") is None),
    ("'Pharmacist' excluded", EXCLUDE.search("Pharmacist") is not None),
    # SQL conds derive from the same term lists as the Python regexes.
    ("intern SQL cond built", "\\bintern(?:ship)?\\b" in classify.intern_title_cond()),
    ("intern SQL has student term", "\\bstudent\\w*" in classify.intern_title_cond()),
    ("newgrad SQL has junior term", "\\bjunior\\w*" in classify.newgrad_title_cond()),
    ("tech SQL built", classify.titles_tech_cond().startswith("regexp_matches")),
    # canonical URL dedup key.
    ("canonical strips query/frag",
     canonical_url("https://www.Jobs.Example.com/a/b/") == "jobs.example.com/a/b"),
    ("canonical empty for non-http", canonical_url("mailto:x@y") == ""),
    # Blocked-company normalizer parity (readme_utils.BLOCKED_COMPANIES).
    ("mcdonald's→blocked form", clean_company_name("Mcdonald's") == "Mcdonald S"),
    ("starbucks→blocked form", clean_company_name("Starbucks") == "Starbucks"),
    ("ups→blocked form", clean_company_name("UPS") == "UPS"),
]

failures = []
for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)

if failures:
    print(f"\n{len(failures)} check(s) failed:", *failures, sep="\n  ")
    sys.exit(1)
print("\nAll checks passed.")