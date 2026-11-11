"""URL-derived identity: only id-bearing hosts, only values shaped like ids."""

import identity
import pandas as pd


def test_infer_greenhouse_job_path():
    assert identity.infer_ats(
        "https://boards.greenhouse.io/acme/jobs/4012345?utm_source=x"
    ) == ("greenhouse", "4012345")
    assert identity.infer_ats(
        "https://job-boards.greenhouse.io/acme/jobs/77123"
    ) == ("greenhouse", "77123")
    assert identity.infer_ats(
        "https://boards.greenhouse.io/embed/job_app?for=acme&token=99988"
    ) == ("greenhouse", "99988")


def test_infer_lever_ashby_workable():
    uuid = "0dfbe31e-89ef-4a1b-9c2d-1234567890ab"
    assert identity.infer_ats(f"https://jobs.lever.co/acme/{uuid}") == ("lever", uuid)
    assert identity.infer_ats(f"https://jobs.ashbyhq.com/acme/{uuid}") == ("ashby", uuid)
    assert identity.infer_ats("https://apply.workable.com/acme/j/ABC123XYZ/") == (
        "workable", "ABC123XYZ")


def test_form_routes_are_not_ids():
    # Both collapsed hundreds of rows onto one fake id before the 2026-11-07 audit.
    assert identity.infer_ats("https://jobs.ashbyhq.com/acme/application") == ("", "")
    assert identity.infer_ats("https://jobs.lever.co/acme/apply") == ("", "")
    assert identity.infer_ats("https://jobs.lever.co/acme") == ("", "")


def test_tenant_owned_ids_are_scoped():
    # Numeric ids restart per employer: icims/10863 was four companies' posting.
    assert identity.infer_ats(
        "https://careers-stifel.icims.com/jobs/10863/developer/job"
    ) == ("icims", "stifel/10863")
    assert identity.infer_ats(
        "https://dcsa.bamboohr.com/careers/102"
    ) == ("bamboohr", "dcsa/102")
    # Workday hides the req in the title, after the location segment.
    assert identity.infer_ats(
        "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Austin/Tech_Lead_R2600885"
    ) == ("workday", "acme/R2600885")


def test_smartrecruiters_id_drops_title_slug():
    assert identity.infer_ats(
        "https://jobs.smartrecruiters.com/PHONE/1000000000003993-data-scientist-3-5-years-"
    ) == ("smartrecruiters", "1000000000003993")


def test_unknown_or_idless_urls_stay_blank():
    for link in (
        "https://example.com/careers/senior-eng",  # unknown host: tail is a slug
        "https://boards.greenhouse.io/acme/jobs/12",  # too short to be a req id
        "not-a-url",
        "",
        None,
    ):
        assert identity.infer_ats(link) == ("", "")


def test_pairs_never_overwrites_a_published_id():
    frame = pd.DataFrame([{
        "ats_type": "Greenhouse", "ats_id": "from-api",
        "link": "https://boards.greenhouse.io/acme/jobs/12345",
    }])
    ats, req = identity.pairs(frame)
    assert ats.tolist() == ["Greenhouse"] and req.tolist() == ["from-api"]


def test_pairs_fills_na_from_a_concatenated_frame():
    # pandas 3 leaves a column the other frame never had as float NaN; NaN used
    # to read as an identified id, collapsing every such row onto one dedup key.
    with_ids = pd.DataFrame([{
        "ats_type": "greenhouse", "ats_id": "1234567",
        "link": "https://boards.greenhouse.io/acme/jobs/1234567",
    }])
    without_ids = pd.DataFrame([
        {"link": "https://boards.greenhouse.io/beta/jobs/7654321"},
        {"link": "https://jobs.lever.co/beta/0dfbe31e-89ef-4a1b-9c2d-1234567890ab"},
    ])
    merged = pd.concat([with_ids, without_ids], ignore_index=True)
    ats, req = identity.pairs(merged)
    assert ats.tolist() == ["greenhouse", "greenhouse", "lever"]
    assert req.tolist()[1:] == [
        "7654321", "0dfbe31e-89ef-4a1b-9c2d-1234567890ab"]


def test_fill_and_coverage():
    uuid = "0dfbe31e-89ef-4a1b-9c2d-1234567890ab"
    frame = pd.DataFrame([
        {"ats_type": "", "ats_id": "", "link": f"https://jobs.lever.co/acme/{uuid}"},
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
