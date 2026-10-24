"""Self-check for the employer-board discovery parser. Stdlib only.

Run: python test_discover_boards.py
"""
import os
import sys
import tempfile

import discover_boards as D


class FakeRows:
    def __init__(self, company, link):
        self._d = {"company": company, "link": link}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __len__(self):
        return len(self._d["company"])


PARSE_CASES = [
    ("https://job-boards.greenhouse.io/stripe/jobs/123", ("Greenhouse", "stripe")),
    ("https://boards.greenhouse.io/databricks/jobs/1", ("Greenhouse", "databricks")),
    ("https://boards.greenhouse.io/stripe", ("Greenhouse", "stripe")),
    ("https://job-boards.greenhouse.io/embed/job_app?for=x", None),
    ("https://jobs.lever.co/palantir/abc-123/apply", ("Lever", "palantir")),
    ("https://jobs.ashbyhq.com/openai/xyz/application", ("Ashby", "openai")),
    ("https://jobs.smartrecruiters.com/Visa/12345", ("SmartRecruiters", "visa")),
    ("https://apply.workable.com/avalore/j/abc123", ("Workable", "avalore")),
    ("https://apply.workable.com/j/abc", None),
    ("https://ats.rippling.com/bluewave/jobs/uuid", ("Rippling", "bluewave")),
    ("https://idtus.pinpointhq.com/en/postings/uuid", ("Pinpoint", "idtus")),
    ("https://bestlogisticsgroup.recruitee.com/o/slug", ("Recruitee", "bestlogisticsgroup")),
    ("https://lhigroup.teamtailor.com/jobs/123-assembler", ("Teamtailor", "lhigroup")),
    ("https://ninjaholdings.breezy.hr/p/abc", ("Breezy", "ninjaholdings")),
    ("https://open.jobs.personio.com/job/2761670", ("Personio", "open")),
    ("https://personio.jobs.personio.de/job/1834171", ("Personio", "personio")),
    ("https://www.teamtailor.com/jobs", None),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x",
     ("Workday", "nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite")),
    ("https://globalhr.wd5.myworkdayjobs.com/Relx/job/123",
     ("Workday", "globalhr.wd5.myworkdayjobs.com/wday/cxs/globalhr/Relx")),
    ("https://simplify.jobs/p/abc", None),
    ("https://i.imgur.com/logo.png", None),
    ("https://example.com/jobs/1", None),
    ("", None),
    (None, None),
    (12345, None),
]


def check_parse():
    failures = 0
    for link, want in PARSE_CASES:
        got = D.parse_link(link)
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  parse_link({link!r}) -> {got} (want {want})")
        if not ok:
            failures += 1
    return failures


def check_state():
    failures = 0
    tmp = tempfile.mkdtemp()
    D._CACHE_DIR = tmp
    D._STATE_PATH = os.path.join(tmp, "discovered_boards.json")

    rows = FakeRows(
        ["Stripe", "Databricks", "Palantir", "Stripe"],
        ["https://job-boards.greenhouse.io/stripe/jobs/1",
         "https://boards.greenhouse.io/databricks/jobs/2",
         "https://jobs.lever.co/palantir/abc",
         "https://job-boards.greenhouse.io/stripe/jobs/9"],
    )
    new, total = D.update_state(rows, now=1000)
    checks = [
        ("first run finds 3 boards (dup counted once)", (new, total) == (3, 3)),
    ]
    new2, total2 = D.update_state(rows, now=2000)
    checks.append(("second run adds nothing", (new2, total2) == (0, 3)))

    state = D._load()
    checks.append(("stripe seen_count == 2", state["Greenhouse|stripe"]["seen_count"] == 2))
    checks.append(("stripe last_seen updated", state["Greenhouse|stripe"]["last_seen"] == 2000))
    checks.append(("stripe company name recorded", state["Greenhouse|stripe"]["company"] == "Stripe"))

    base = [("Some Co", "Greenhouse", "stripe")]
    merged = D.merge_into_probe(base)
    merged_keys = [(a, s) for _n, a, s in merged]
    checks.append(("merge keeps base entry", merged[0] == ("Some Co", "Greenhouse", "stripe")))
    checks.append(("merge does not re-add overlapping board",
                   merged_keys.count(("Greenhouse", "stripe")) == 1))
    checks.append(("merge appends databricks + palantir",
                   ("Greenhouse", "databricks") in merged_keys
                   and ("Lever", "palantir") in merged_keys))
    checks.append(("merge length == base + 2", len(merged) == 3))

    D.MAX_DISCOVERED = 2
    big = FakeRows(
        ["A", "B", "C"],
        ["https://boards.greenhouse.io/a/jobs/1",
         "https://boards.greenhouse.io/b/jobs/1",
         "https://boards.greenhouse.io/c/jobs/1"],
    )
    _new3, total3 = D.update_state(big, now=3000)
    checks.append(("MAX_DISCOVERED caps the pool", total3 == 2))
    D.MAX_DISCOVERED = 500

    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures += 1
    return failures


if __name__ == "__main__":
    fails = check_parse() + check_state()
    if fails:
        print(f"\n{fails} check(s) failed.")
        sys.exit(1)
    print("\nAll checks passed.")
