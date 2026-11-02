# Fixes applied — 2026-11-02

What actually changed in the ingestion ecosystem this session, why, and how it
was verified. Evidence behind the decisions lives in the sibling files; this one
is the change log an agent should read before touching the pipeline again.

Scope: reliability, observability, freshness. No production feed was written
during this work — every measurement below is read-only against the published
corpus or the committed caches.

---

## 1. Per-source observability — `Listings/ingest_stats.py` (+ tests)

**Problem.** The published feed cannot rank a source. Dedup runs *before*
publication, so whatever survives is source-exclusive by construction: measured
57 duplicate links across 20,844 rows, every source near 100% "sole source".
Any new source is therefore indistinguishable from a duplicate, which is why
[roadmap.md](roadmap.md) put this at P0 and called everything else blocked on it.

**What it does.** A stdlib-only collector. Adapters call
`ingest_stats.record(source, discovered=..., valid=..., seconds=..., status=...)`;
the pipeline folds the digest's post-dedup counts back in with
`merge_digest(new_by_source)` and writes `pages/source_stats.json` at the end of
every run.

Each source row carries `discovered`, `valid`, `new`, `seconds`, `calls`,
`status`, `notes`, and `new_per_second` — new postings per second of fetch
time.This is the figure the P0 acceptance criterion asked for and it is the only
one that is not inflated by overlap with another source.

**Wired into:** jobhive, freehire, each breadth feed individually, ECR,
markdown sources (per source), jobspy (per site), echojobs, ats-probe,
priority-probe, and the closure stage. Every run prints
`source_stats: N sources, X discovered, Y valid, Z new, F degraded`.

**Why a module and not counters in the pipeline:** the pipeline is a 2,000-line
top-level script, so counters there are untestable. `ingest_stats` imports
nothing from the pipeline, so its tests run without triggering the scraper —
the same trick `test_dedup.py` uses for `_save_cache`.

**Verified.** `test_ingest_stats.py` (6 checks): accumulation across calls,
status/note retention, `new_per_second` only computed when time is known,
digest attribution, JSON artifact shape, and that a bad count is absorbed
instead of crashing a four-hour run.

**Not yet verified:** no live end-to-end run, because that writes
`pages/listings.json` and `pages/priority.json` (production feeds). The wiring
is exercised on the next scheduled run; check `pages/source_stats.json` exists
and its `totals.discovered` is non-zero.

---

## 2. Jobhive closure — `Listings/closure.py` (+ tests)

**Problem.** jobhive never removes a closed posting. Its per-ATS parquet has no
`status` / `closed_at` / `last_seen` field and 39–70% of every sampled bucket is
older than 90 days, with postings from 2014 still present
([jobhive-staleness.md](jobhive-staleness.md)). Age is therefore the only
liveness signal the corpus offers, and it is wrong in both directions: a role
posted 20 days ago and filled yesterday still shows as open, while a genuinely
open rolling internship vanishes at day 61.

**What it does.** jobhive rows carry `ats_type` and `ats_id`, and this repo
already crawls many of those boards. When a board returns rows, its ids are a
complete snapshot for that employer, so a jobhive row whose board was re-crawled
this run and whose id is gone is closed.

Three rules keep it from eating the feed:

1. **Only a board that returned at least one job votes.** A failed, empty,
   truncated or rate-limited fetch records nothing.
2. **A vote covers one employer and one ATS**, never a whole platform — a second
   board on the same ATS that timed out is not swept up in the first board's
   snapshot.
3. **Nothing else changes.** Platforms with no adapter here (ADP, Amazon, Apple)
   keep the age rule and are reported as unverifiable rather than guessed at.

`build_job_query` now selects `ats_type` and `ats_id`; both are dropped again
(`drop_vote_columns`) before any feed is written, so the published schema is
byte-identical to before.

**Join correctness, verified against the live corpus.** For greenhouse,
`ats_id` equals the last path segment of `url` exactly
(`job-boards.greenhouse.io/10alabs/jobs/4266196009` →
`ats_id=4266196009`), which is what `ids_from_links` extracts. Employer-name
matching was measured on 2026-11-02:

| measure | value |
|---|---:|
| greenhouse priority boards | 30 |
| distinct jobhive greenhouse employers | 4,788 |
| priority boards matched by name | **30 / 30** |
| jobhive greenhouse rows covered | **7,268** |

30/30 means the name join is not the weak link for the curated boards; the
7,268 covered rows are the subset where closure is now verified rather than
inferred from age.

**Verified.** `test_closure.py` (8 checks): link→id extraction, employer-key
normalisation, closed-vs-live rows, failed board never deletes, per-board (not
per-platform) scoping including an id collision across employers, unverifiable
ATS ignored, missing columns safe, and vote columns actually removed.

**Known limits.** 12 ATS kinds are verifiable (the 11 above plus `workday`),
and only for boards that ran this cycle. Workday joined 2026-11-02 via
`closure.workday_req_id` (`bulletFields[0]`, else the `externalPath` tail),
wired into both probe snapshot paths; staging check
(`Listings/validate_closure.py`) green on 4 real boards incl. Workday. A board
whose crawl was skipped by the heavy tier's cooldown votes using the last good
snapshot, not this run's.

---

## 3. EchoJobs 429 — `Listings/echojobs.py`

**Problem.** 1.0 s between 100 detail fetches returned HTTP 429 on 2026-11-02
(recorded in [roadmap.md](roadmap.md) as P1). `http_get` retries a 429 twice per
URL, so the adapter responded to rate limits by hammering harder — the one
behaviour that makes the next run worse.

**Fix.** Three changes:

- On a 429 the run **stops fetching** and publishes what it already parsed,
  instead of retrying through the remaining queue.
- Delay and page cap are env-tunable: `SEARCHTERN_ECHOJOBS_DELAY` (default
  raised 1.0 → 1.2 s) and `SEARCHTERN_ECHOJOBS_MAX_FETCHES` (default 100).
- The outcome is recorded through `ingest_stats` with `status="rate_limited"`,
  so a throttled source shows as `degraded` in `source_stats.json` instead of
  silently returning a smaller frame.

**Verified.** Compiles and lints clean. Deliberately not re-fetched 100 pages to
reproduce the 429 in this session — the rate limit is the thing being respected.

---

## 4. What was deliberately not changed

- **No new vendor integrated.** Both paid feeds are still blocked on keys, and
  [paid-feeds-evaluation.md](paid-feeds-evaluation.md) concludes value is
  unproven. Wiring one in now would violate the "no integration before
  measurable incremental value" rule this session was given.
- **No ATS adapters added.** 42.5% of the 738-company registry sits on an
  unreadable ATS, but probing them is [FUTURE.md](FUTURE.md) work, not a change
  to ship blind.
- **No production feed, cache, or workflow touched.**
- **`(company, role, location)` dedup left as is.** It can merge two distinct
  requisitions with the same title and location (roadmap P2), but fixing it
  correctly needs an identity key the README tables do not have, and getting it
  wrong deletes real jobs.

---

## Checks actually run

```
python -m py_compile generate_listings.py closure.py ingest_stats.py echojobs.py   # exit 0
python -m ruff check .                                                             # clean
python test_closure.py          # 8/8 PASS
python test_ingest_stats.py     # 6/6 PASS
python test_dedup.py            # PASS
python test_priority_boards.py  # PASS (56 boards, 3 ATS kinds)
python test_classify.py         # PASS
python test_http_backoff.py     # PASS
python test_discover_boards.py  # PASS
python test_ecr_source.py       # PASS
```

`test_h1b_enrich.py` fails on a pre-existing missing-value assertion recorded in
the brain handoff before this session; it is unrelated to these changes and was
not touched.
