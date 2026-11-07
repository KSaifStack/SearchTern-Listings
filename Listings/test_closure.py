"""Self-check for verified closure. stdlib + pandas only, no network.

Run: python test_closure.py
"""
import closure
import pandas as pd


def frame(rows):
    return pd.DataFrame(rows, columns=["company", "ats_type", "ats_id", "role"])


def test_link_ids():
    ids = closure.ids_from_links([
        "https://boards.greenhouse.io/acme/jobs/4012345",
        "https://job-boards.greenhouse.io/10alabs/jobs/4266196009?utm_source=x",
        "https://jobs.lever.co/acme/1a2b3c4d-0000-0000-0000-000000000000/",
        "https://jobs.ashbyhq.com/acme/abc-123#apply",
        "https://example.com/jobs",
        "",
        None,
    ])
    assert ids == {
        "4012345",
        "4266196009",
        "1a2b3c4d-0000-0000-0000-000000000000",
        "abc-123",
    }, ids


def test_employer_key():
    assert closure.employer_key("Scale AI") == closure.employer_key("scaleai")
    assert closure.employer_key("  Cohere  ") == "cohere"
    assert closure.employer_key(None) == ""


def test_drops_closed_rows_only():
    closure.reset()
    closure.record_snapshot("Greenhouse", "Scale AI", ["1", "2"])
    df = frame([
        ["Scale AI", "greenhouse", "1", "open"],      # still listed -> keep
        ["Scale AI", "greenhouse", "9", "closed"],    # board re-crawled, id gone -> drop
        ["Ramp", "ashby", "7", "unrelated"],          # board never crawled -> keep
        ["Scale AI", "", "", "no ids"],               # no identifier -> keep
        ["Acme", "successfactors", "5", "other ats"],  # not a verifiable ATS -> keep
    ])
    kept, dropped, unverified = closure.filter_closed(df)
    assert dropped == 1, (kept, dropped)
    assert list(kept["role"]) == ["open", "unrelated", "no ids", "other ats"], kept
    assert unverified == 3, unverified


def test_failed_board_never_deletes():
    """An empty or failed fetch records no vote, so its rows survive."""
    closure.reset()
    assert closure.record_snapshot("Greenhouse", "Acme", []) == 0
    assert closure.record_snapshot("Greenhouse", "Acme", [None, ""]) == 0
    df = frame([["Acme", "greenhouse", "42", "still open"]])
    kept, dropped, unverified = closure.filter_closed(df)
    assert dropped == 0 and len(kept) == 1, (kept, dropped)
    assert unverified == 1, unverified


def test_vote_is_per_board_not_per_platform():
    """A second board on the same ATS must not be swept up in another's snapshot.

    Beta's board never ran, so its rows are untouched even though Beta's id
    collides with an id Acme's board reported as dead.
    """
    closure.reset()
    closure.record_snapshot("Greenhouse", "Acme", ["2"])
    df = frame([
        ["Acme", "greenhouse", "2", "acme open"],     # live on Acme's board
        ["Acme", "greenhouse", "1", "acme closed"],   # gone from Acme's board -> drop
        ["Beta", "greenhouse", "1", "beta never crawled"],  # untouched
    ])
    kept, dropped, _ = closure.filter_closed(df)
    assert dropped == 1, dropped
    assert list(kept["role"]) == ["acme open", "beta never crawled"], kept


def test_unverifiable_ats_is_ignored():
    closure.reset()
    assert closure.record_snapshot("SuccessFactors", "Acme", ["1"]) == 0
    assert closure.covered() == {"boards": 0, "live_ids": 0}
    df = frame([["Acme", "successfactors", "5", "row"]])
    kept, dropped, unverified = closure.filter_closed(df)
    assert dropped == 0 and unverified == 1


def test_missing_columns_is_safe():
    closure.reset()
    closure.record_snapshot("Greenhouse", "Acme", ["1"])
    df = pd.DataFrame([{"company": "Acme", "role": "x"}])
    kept, dropped, unverified = closure.filter_closed(df)
    assert dropped == 0 and len(kept) == 1 and unverified == 1


def test_workday_req_id_from_posting():
    # Shape observed live on ally/nvidia/ebay CXS endpoints 2026-11-02.
    assert closure.workday_req_id({
        "bulletFields": ["R2600885"],
        "externalPath": "/job/Detroit-MI/Title_R2600885",
    }) == "R2600885"
    assert closure.workday_req_id({
        "bulletFields": [],
        "externalPath": "/job/US-CA-Santa-Clara/Title_JR2022276",
    }) == "JR2022276"
    assert closure.workday_req_id({"bulletFields": [], "externalPath": ""}) is None
    assert closure.workday_req_id({}) is None


def test_workday_ids_from_links():
    ids = closure.ids_from_links([
        "https://ally.wd1.myworkdayjobs.com/job/Detroit-MI/Title_R2600885",
        "https://ebay.wd5.myworkdayjobs.com/job/Runnemede/Photographer_R0077357",
        "https://example.com/wday/cxs/ally/Ally",  # site path, not a posting
    ], ats_type="Workday")
    assert ids == {"R2600885", "R0077357"}, ids


def test_workday_snapshot_votes_and_closes():
    closure.reset()
    assert closure.record_snapshot("Workday", "Ally", ["R2600885"]) == 1
    df = frame([
        ["Ally", "workday", "R2600885", "open"],
        ["Ally", "workday", "R0000001", "closed"],
    ])
    kept, dropped, unverified = closure.filter_closed(df)
    assert dropped == 1 and list(kept["role"]) == ["open"], kept
    assert unverified == 0, unverified


def test_drop_internal_columns_keeps_identity():
    closure.reset()
    df = frame([["Acme", "greenhouse", "1", "x"]])
    df["_vote"] = True
    out = closure.drop_internal_columns(df)
    # Identity is published data: dropping it published 0% identifiable rows.
    assert list(out["ats_type"]) == ["greenhouse"]
    assert list(out["ats_id"]) == ["1"]
    assert "_vote" not in out.columns
    assert list(out.columns) == ["company", "ats_type", "ats_id", "role"]


def test_empty_frame():
    closure.reset()
    closure.record_snapshot("Greenhouse", "Acme", ["1"])
    kept, dropped, unverified = closure.filter_closed(pd.DataFrame())
    assert dropped == 0 and unverified == 0 and kept is not None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
    closure.reset()
    print("\n  test_closure: all checks passed")
