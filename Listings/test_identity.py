"""URL-derived identity: only id-bearing hosts, never a guess."""

import identity
import pandas as pd


def test_infer_greenhouse_job_path():
    assert identity.infer_ats(
        "https://boards.greenhouse.io/acme/jobs/4012345?utm_source=x"
    ) == ("greenhouse", "4012345")
    assert identity.infer_ats(
        "https://job-boards.greenhouse.io/acme/jobs/77"
    ) == ("greenhouse", "77")
    assert identity.infer_ats(
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=999"
    ) == ("greenhouse", "999")


def test_infer_lever_ashby_workable():
    assert identity.infer_ats("https://jobs.lever.co/acme/0dfbe31e-89ef") == (
        "lever", "0dfbe31e-89ef")
    assert identity.infer_ats("https://jobs.ashbyhq.com/acme/abc-123") == (
        "ashby", "abc-123")
    assert identity.infer_ats("https://apply.workable.com/acme/j/ABC123/") == (
        "workable", "ABC123")


def test_infer_workday_reads_requisition_not_title():
    # The req id is inside the last path segment, after the title.
    assert identity.infer_ats(
        "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Austin/Tech_Lead_R2600885"
    ) == ("workday", "R2600885")


def test_unknown_or_idless_urls_stay_blank():
    for link in (
        "https://jobs.lever.co/acme",              # board, no posting
        "https://example.com/careers/senior-eng",  # unknown host: tail is a slug
        "not-a-url",
        "",
        None,
    ):
        assert identity.infer_ats(link) == ("", "")


def test_pairs_never_overwrites_a_published_id():
    frame = pd.DataFrame([{
        "ats_type": "Greenhouse", "ats_id": "from-api",
        "link": "https://boards.greenhouse.io/acme/jobs/1",
    }])
    ats, req = identity.pairs(frame)
    assert ats.tolist() == ["Greenhouse"] and req.tolist() == ["from-api"]


def test_fill_and_coverage():
    frame = pd.DataFrame([
        {"ats_type": "", "ats_id": "", "link": "https://jobs.lever.co/acme/abc"},
        {"ats_type": "", "ats_id": "", "link": "https://example.com/x"},
    ])
    filled = identity.fill(frame)
    assert filled["ats_type"].tolist() == ["lever", ""]
    assert identity.coverage(filled)["pct"] == 50.0
    assert identity.coverage(pd.DataFrame())["pct"] == 0.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
