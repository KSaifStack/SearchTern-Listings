"""Verified closure: drop jobhive rows that a completed board crawl no longer lists.

jobhive snapshots never remove a closed posting. The per-ATS parquet has no
`status`, `closed_at` or `last_seen` field, and 39-70% of every sampled bucket is
older than 90 days with postings from 2014 still present (measured in
docs/reverse-engineering/jobhive-staleness.md). Age is therefore the only
liveness signal the corpus itself offers, and it is wrong in both directions: a
role posted 20 days ago and filled yesterday still shows as open, while a
genuinely open rolling internship disappears at day 61.

Jobhive rows do carry `ats_type` and `ats_id`, and this repo already crawls many
of those boards directly. When a board returns a usable snapshot we know exactly
which ids on it are live, so a jobhive row whose board was re-crawled this run
and whose id is gone is closed. Three rules keep that honest:

* Only a board that returned at least one job votes. A failed, empty, truncated
  or rate-limited fetch records nothing, so it can never mark its rows dead --
  that is the failure mode that would quietly empty the feed.
* A vote covers one employer and one ATS, never a whole platform. A second
  board on the same ATS that timed out is not swept up in the first board's
  snapshot.
* Nothing else changes. Rows from platforms with no adapter here (ADP, Amazon,
  Apple) keep the age rule they already had; they are reported as
  unverifiable rather than guessed at.
"""
import re

# ATS platforms whose ids jobhive publishes in the shape this repo's adapters
# produce. Others are left alone even when they appear in the board registry.
# Workday is verifiable: the CXS API returns no numeric id field, but every
# posting carries a stable requisition id in `bulletFields[0]` (R2600885,
# JR2022276) that also trails the `externalPath` after the last underscore.
# Verified live against ally/nvidia/ebay CXS endpoints 2026-11-02.
VERIFIABLE_ATS = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "workday", "workable",
    "rippling", "pinpoint", "recruitee", "teamtailor", "breezy", "personio",
})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Workday requisition ids: 1-4 alpha chars + 4+ digits (R2600885, JR2022276).
_WORKDAY_REQ_RE = re.compile(r"^([A-Za-z]{1,4}\d{4,})$")

# (ats_type, employer key) -> set of live ids from a completed crawl this run.
_COMPLETE = {}


def reset():
    """Drop all recorded snapshots. Called at the start of every run and by tests."""
    _COMPLETE.clear()


def workday_req_id(posting_or_link):
    """Return the stable requisition id for one Workday posting or URL.

    Prefers the `externalPath` tail after the last underscore
    (`/job/Detroit-MI/Title_R2600885` -> `R2600885`), falls back to
    `bulletFields[0]`. The path tail is authoritative: bulletFields[0] is a
    free-text label on some tenants (e.g. "Regular Employee") and must never
    outrank the path. Both candidates are regex-gated; returns None when
    neither yields a req-shaped value, so callers record no vote rather than
    a junk id.
    """
    if isinstance(posting_or_link, dict):
        req = workday_req_id(posting_or_link.get("externalPath") or "")
        if req:
            return req
        bullets = posting_or_link.get("bulletFields") or []
        if bullets:
            first = str(bullets[0] or "").strip()
            if _WORKDAY_REQ_RE.match(first):
                return first
        return None
    path = str(posting_or_link or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if "/" not in path:
        return None
    tail = path.rsplit("/", 1)[-1]
    candidate = tail.rsplit("_", 1)[-1].strip() if "_" in tail else ""
    if candidate and _WORKDAY_REQ_RE.match(candidate):
        return candidate
    return None


def req_ids_from_workday_postings(postings):
    """Extract requisition ids from raw Workday CXS `jobPostings`."""
    out = set()
    for posting in postings or ():
        req = workday_req_id(posting)
        if req:
            out.add(req)
    return out


def ids_from_links(links, ats_type=None):
    """Extract the ATS's own id for a posting from its apply URL.

    Every verifiable ATS puts the requisition id last in the path --
    `boards.greenhouse.io/acme/jobs/4012345`, `jobs.lever.co/acme/<uuid>`,
    `jobs.ashbyhq.com/acme/<uuid>` -- and jobhive stores that same value in
    `ats_id`. Workday hides the req id inside the path tail
    (`/job/<location>/<Title>_<REQ>`), so pass `ats_type="Workday"` (or a
    posting dict via `workday_req_id`) to get the `R…`/`JR…` id instead.
    """
    out = set()
    if ats_type is not None and str(ats_type).strip().lower() == "workday":
        for link in links or ():
            req = workday_req_id(link)
            if req:
                out.add(req)
        return out
    for link in links or ():
        path = str(link or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if "/" not in path:
            continue
        tail = path.rsplit("/", 1)[-1]
        if tail and tail.lower() not in ("jobs", "job", "apply"):
            out.add(tail)
    return out


def employer_key(name):
    """Normalize an employer name for joining a board to a jobhive row.

    jobhive carries the ATS's own company string while the board registry uses a
    display name, so "Scale AI" and "scaleai" must land on the same key.
    """
    if name is None:
        return ""
    return _NON_ALNUM.sub("", str(name).casefold())


def record_snapshot(ats_type, company, ids):
    """Register the live ids from one successfully crawled board.

    `ids` may contain None/empty values; they are dropped. An empty set records
    nothing: "this board returned no jobs" cannot be distinguished from "this
    board returned nothing usable", and only the safe reading is allowed.
    """
    if not ats_type:
        return 0
    ats = str(ats_type).strip().lower()
    if ats not in VERIFIABLE_ATS:
        return 0
    key = employer_key(company)
    if not key:
        return 0
    live = {str(i).strip() for i in (ids or ()) if str(i or "").strip()}
    if not live:
        return 0
    _COMPLETE[(ats, key)] = live
    return len(live)


def covered() -> dict:
    """Summarize what voted this run: {boards, live_ids}."""
    return {
        "boards": len(_COMPLETE),
        "live_ids": sum(len(v) for v in _COMPLETE.values()),
    }


def verifiable(row) -> bool:
    """True when this row's board was crawled this run, so absence means closed."""
    ats = str(row.get("ats_type") or "").strip().lower()
    if ats not in _voting_ats():
        return False
    return (ats, employer_key(row.get("company"))) in _COMPLETE


def _voting_ats():
    """ATS kinds that actually voted this run."""
    return {ats for ats, _ in _COMPLETE}


def filter_closed(df, columns=("ats_type", "ats_id")):
    """Drop rows whose employer board was re-crawled and no longer lists them.

    Returns (kept_df, dropped_count, unverified_count). Rows without an
    identifier, from an un-crawled board, or on an unverifiable ATS are kept
    untouched and counted as unverified -- never silently reclassified.
    """
    if df is None or len(df) == 0:
        return df, 0, 0
    if not _COMPLETE:
        return df, 0, len(df)
    ats_col, id_col = columns
    if ats_col not in df.columns or id_col not in df.columns:
        return df, 0, len(df)

    voting = _voting_ats()
    ats = df[ats_col].astype(str).str.strip().str.lower()
    ids = df[id_col].astype(str).str.strip()
    companies = df["company"].map(employer_key)

    keyed = ats.map(lambda a: a in voting) & ids.ne("") & ids.ne("nan")
    unverified = int((~keyed).sum())
    if not keyed.any():
        return df, 0, unverified

    idx = df.index[keyed]
    live = {
        (ats[i], companies[i]): _COMPLETE[(ats[i], companies[i])]
        for i in idx
        if (ats[i], companies[i]) in _COMPLETE
    }
    drop = [
        i for i in idx
        if (ats[i], companies[i]) in live and ids[i] not in live[(ats[i], companies[i])]
    ]
    if not drop:
        return df, 0, unverified
    return df.drop(index=drop), len(drop), unverified


def drop_internal_columns(df):
    """Drop the pipeline's own helper columns before publishing.

    This used to drop ``ats_type``/``ats_id`` on the theory that they were only
    join columns. They are not: they are the requisition's identity, they are
    what the ATS itself returned, and dropping them published 0% identifiable
    rows -- which silently disabled identity dedup downstream. Only columns this
    pipeline invents (``_``-prefixed) are internal.
    """
    if df is None or len(df) == 0:
        return df
    internal = [c for c in df.columns if str(c).startswith("_")]
    return df.drop(columns=internal) if internal else df
