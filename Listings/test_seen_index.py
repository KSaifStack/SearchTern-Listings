"""Seen-log identity: a requisition is new once, closes once, keeps its first date."""

import seen_index

FRESH = {"ats": True, "md": False}
NOW = "2026-02-01T00:00:00+00:00"
FIRST = "2026-01-01T00:00:00+00:00"


def _bucket(key):
    return key.split("|", 1)[0]


def _entry(ats="", req="", first=FIRST):
    return {"first": first, "last": first, "ats": ats, "req": req, "closed": ""}


def test_a_repunctuated_title_is_not_a_new_posting():
    seen = {"ats|acme|Engineer|NY": _entry("greenhouse", "4012345")}
    key = "ats|acme|Engineer I|NY"
    rid = {key: ("greenhouse", "4012345")}

    assert seen_index.new_keys({key}, rid, seen) == set()

    closed, out, first = seen_index.commit(seen, {key: object()}, rid, FRESH, _bucket, NOW)
    assert closed == []
    assert first[key] == FIRST                  # keeps the real first-seen date
    assert "ats|acme|Engineer|NY" not in out    # old key retired, never "closed"


def test_a_gone_requisition_closes_exactly_once():
    seen = {"ats|acme|Engineer|NY": _entry("greenhouse", "4012345")}

    closed, out, _ = seen_index.commit(seen, {}, {}, FRESH, _bucket, NOW)
    assert len(closed) == 1
    assert (closed[0]["ats_type"], closed[0]["ats_id"]) == ("greenhouse", "4012345")

    again, _, _ = seen_index.commit(out, {}, {}, FRESH, _bucket, NOW)
    assert again == []      # stamped closed instead of re-reported every run


def test_every_stamped_closure_is_reported_by_the_same_call():
    """The log and the run summary must never disagree.

    Regression for the 2026-11-08 crawl: main stamped 59,227 entries closed and
    then reported 0, because a duplicate closure loop in the pipeline ran first
    and ``commit`` returned an empty list from the already-stamped log.
    """
    seen = {
        "ats|acme|Engineer|NY": _entry("greenhouse", "4012345"),
        "ats|acme|Analyst|NY": _entry("greenhouse", "4012346"),
    }
    closed, out, _ = seen_index.commit(seen, {}, {}, FRESH, _bucket, NOW)
    stamped = [k for k, v in out.items() if v.get("closed") == NOW]
    assert sorted(c["key"] for c in closed) == sorted(stamped)
    assert len(closed) == 2


def test_a_bucket_that_was_not_fetched_never_closes():
    seen = {"ats|acme|Engineer|NY": _entry("greenhouse", "4012345")}
    closed, _, _ = seen_index.commit(seen, {}, {}, {"ats": False}, _bucket, NOW)
    assert closed == []


def test_legacy_entries_keep_the_text_rule():
    seen = {"md|acme|Engineer|NY": _entry()}
    assert seen_index.new_keys({"md|acme|Engineer|NY"}, {}, seen) == set()
    assert seen_index.new_keys({"md|acme|New Role|NY"}, {}, seen) == {"md|acme|New Role|NY"}

    closed, _, _ = seen_index.commit(seen, {}, {}, {"md": True}, _bucket, NOW)
    assert len(closed) == 1 and closed[0]["ats_id"] == ""


def test_rows_without_an_id_are_never_new_when_the_text_matches():
    seen = {"ats|acme|Engineer|NY": _entry()}
    key = "ats|acme|Engineer|NY"
    assert seen_index.new_keys({key}, {key: ("", "")}, seen) == set()
    assert seen_index.new_keys({"ats|acme|Other|NY"}, {key: ("", "")}, seen) == {"ats|acme|Other|NY"}
