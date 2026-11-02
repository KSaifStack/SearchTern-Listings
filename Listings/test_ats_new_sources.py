"""Self-check for ats_new_sources.py. Fixture-only, no network.

Run: python test_ats_new_sources.py
"""

import ats_new_sources as A

EF_POSITION = {
    "id": "446721884415", "name": "Software Intern - Data Center",
    "locations": ["San Diego, California, United States of America"],
    "postedTs": 1793577600, "workLocationOption": "onsite",
    "positionUrl": "/careers/job/446721884415",
}

ORACLE_REQ = {
    "Title": "Civil Engineering Intern", "Id": "95247",
    "PostedDate": "2026-10-15",
    "PrimaryLocation": "Denver, CO, United States",
    "WorkplaceType": "On-site",
    "ShortDescriptionStr": "<p>Intern role</p>",
}

ICIMS_LD = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "JobPosting",
 "title": "Civil Engineering Intern",
 "datePosted": "2026-09-14T04:00:00.000Z",
 "employmentType": "INTERN",
 "hiringOrganization": {"@type": "Organization", "name": "Kimley-Horn"},
 "jobLocation": [{"address": {"@type": "PostalAddress",
   "addressLocality": "Fort Collins", "addressRegion": "CO",
   "addressCountry": "US"}}],
 "description": "<p>Intern role</p>",
 "url": "https://careers-kimley-horn.icims.com/jobs/25174/x/job"}
</script></head></html>"""

ADP_REQ = {
    "jobTitle": "Engineering Intern", "reqId": "5001223952006",
    "postingDate": "2026-11-02T17:23:40Z",
    "jobDescription": "<p>Intern role</p>",
    "requisitionLocations": [{"city": "Tampa", "state": "FL", "country": "US"}],
}

BAMBOO_JOB = {
    "jobOpeningName": "Propulsion Intern",
    "datePosted": "2026-10-20",
    "description": "<p>Intern role</p>",
}


def test_eightfold_row():
    row = A._eightfold_row("Qualcomm", "qualcomm.eightfold.ai", EF_POSITION)
    assert row["role"] == "Software Intern - Data Center", row
    assert row["link"] == "https://qualcomm.eightfold.ai/careers/job/446721884415", row
    assert "2026" in row["date"], row
    assert row["is_remote"] == "false", row
    assert row["location"].startswith("San Diego"), row


def test_oracle_boards_from_links():
    links = [
        "https://emit.fa.ca3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/job/95247",
        "https://hdhl.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1007777",
        "https://boards.greenhouse.io/acme/jobs/1",
    ]
    out = A.oracle_boards_from_links(links)
    assert out == {"emit.fa.ca3.oraclecloud.com": ("CX_2001", 1),
                   "hdhl.fa.us6.oraclecloud.com": ("CX_1", 1)}, out


def test_icims_ld_parsing():
    import re

    blocks = A._LD_RE.findall(ICIMS_LD)
    assert len(blocks) == 1, blocks
    # Exercise the same extraction the live parser uses.
    import json

    data = json.loads(blocks[0].strip())
    assert data["@type"] == "JobPosting", data
    assert data["datePosted"].startswith("2026-09-14"), data


def test_icims_link_ordering():
    links = [("2", "senior-accountant", "https://p/jobs/2/x/job"),
             ("1", "civil-engineering-intern", "https://p/jobs/1/y/job")]
    ordered = sorted({jid: (slug, url) for jid, slug, url in links}.items(),
                     key=lambda kv: (0 if A._INTERN_SLUG_RE.search(kv[1][0]) else 1,
                                     kv[0]))
    assert [jid for jid, _ in ordered] == ["1", "2"], ordered


def test_icims_link_with_query_string():
    # Live shape 2026-11-02: job links end in /job?in_iframe=1.
    html = ('<a href="https://careers-kimley-horn.icims.com/jobs/27516/'
            'civil-engineering-analyst/job?in_iframe=1" class="iCIMS_Anchor">')
    hits = A._JOB_LINK_RE.findall(html)
    assert len(hits) == 1 and hits[0][1] == "27516", hits


def test_clean_employer():
    assert A._clean_employer("General Dynamics Mission Systems, Inc.") == \
        "General Dynamics Mission Systems", A._clean_employer("x")
    assert A._clean_employer("PPC Partners Inc") == "PPC Partners"
    assert A._clean_employer("Kimley-Horn") == "Kimley-Horn"
    assert A._clean_employer("Sargent & Lundy") == "Sargent & Lundy"


def test_row_shape():
    row = A._row("Acme", "Intern", "Denver, CO", "2026-01-01",
                 "https://example.com/j/1", False, description="x")
    assert set(A.STD_COLS) <= set(row), row
    assert row["link"].startswith("http"), row


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
    print("\n  test_ats_new_sources: all checks passed")
