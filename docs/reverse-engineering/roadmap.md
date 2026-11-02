# Prioritized roadmap — source expansion and ingestion value

Ordered by evidence strength, not by ambition. Each item states what would
prove it worked. Current baseline: [source-inventory.md](source-inventory.md),
[benchmarks.md](benchmarks.md).

## P0 — Make source value measurable (blocks everything else)

The published feed cannot rank sources: dedup runs before publication, so
surviving rows are source-exclusive by construction (57 duplicate links across
20,844, every source near 100% "sole"). Any source added today would be
indistinguishable from a duplicate.

- Emit a per-run `source_stats` artifact: for each source — HTTP status, pages
  consumed, truncation flag, retries, bytes, wall time, rows fetched.
- Emit a post-dedup disposition per row: `kept`, `merged_into=<stable id>`,
  `dropped_stale`, `dropped_filter`.
- Stamp `fetched_at` and, where the source exposes one, `source_item_id`.
- Extend `source_yield.py` to consume those two artifacts instead of the final
  feed.

**Done when:** a run prints net-new unique employers per source per minute of
fetch, and re-running the same fixture set reproduces the numbers.

## P1 — Stop the two measured freshness regressions

1. **EchoJobs 429.** The adapter uses a 1 s delay and 100 detail fetches, and
   the host returned 429 on 2026-11-02. Add a cooldown after a 429, honor
   `Retry-After` if present, and make delay/cap configurable like
   `SEARCHTERN_WORKERS`.
2. **README-table staleness.** `jobhive` (35.7 d median, 61.5 d p90) and the
   GitHub README sources (27–46 d median, 60+ d p90) carry rows that are almost
   certainly closed. `jobspy` (11.7 d) and `freehire` (12.8 d) show what fresher
   intake looks like. Add a per-source max-age at ingest, starting conservative
   (e.g. 45 d for README sources) so a table edit cannot drag dead postings in.

**Done when:** a run reports zero 429s from EchoJobs, and p90 age for README
sources drops below the configured cap.

## P2 — Identity-based dedup

`(company, role, location)` cannot separate two requisitions with the same
title, and it produced 57 surviving duplicate links. jobhive and jobdataapi
both publish external requisition ids.

- Use `(ats, employer, requisition_id)` when available, canonical URL next,
  fuzzy key last.
- Keep the existing key as the fallback for README tables, which have no id.

**Done when:** two same-title requisitions from one employer survive as two
rows, and the cross-source duplicate link count goes to zero without losing
distinct jobs.

## P3 — Trial the keyed feeds, medium tier only

None of these belong in the 15-minute lane: documented refresh is daily.

| Source | Access | Cost | Why it might be worth it | Risk |
|---|---|---|---|---|
| USAJobs | free API key + `User-Agent` email | free | Federal internships/new-grad; population barely overlaps the current feed | low volume for tech |
| Headcount Labs | bearer token | free 1,000 jobs/mo, paid from $10/mo | deduplicated records with direct apply links | daily refresh; quota consumed per job |
| jobdataapi | API key | paid, anonymous testing limited (first query worked, second 403) | ATS-sourced with `ext_id`/`application_url` → feeds P2 directly | redistribution terms unreviewed |
| Adzuna | app id/key | free tier | broad aggregation | overlap and terms unreviewed |

Trial protocol: fetch into the existing cache layout, measure with P0's
instrumentation for one week, keep only if net-new unique employers per request
beats a current source. Do not add a paid source before P0 exists, or the spend
is unjustifiable.

## P4 — Employer-direct expansion through the existing sitemap path

Reuse `sitemap_sources.py` (robots-gated, JSON-LD, per-host delay) rather than
writing new scrapers:

- **Jobvite** — `/careers/` 200 but slow (1.9 s); needs the existing delay.
- **JazzHR** — board pages are HTML; the guessed JSON endpoint returned HTML.
  Sitemap/JSON-LD only.
- **BambooHR** — needs tenant discovery, then
  `{tenant}.bamboohr.com/careers/list` is public.
- **Getro VC portfolio boards** — `api.getro.com` needs a per-board collection
  token read from the public board page; one config row per board.

**Done when:** each added host has a robots check, a live 200 with at least one
parsed posting, and a per-source row count in `source_stats`.

## P5 — Documented rejects (revisit only if access changes)

Boardsweep (not a listings source — it reads LinkedIn/Indeed), Jobsekr (DNS
NXDOMAIN), Wellfound (Turnstile + `robots.txt` `/_jobs/`), Work at a Startup
(406), hiring.cafe (403), LinkedIn/Indeed/ZipRecruiter (sign-in or block).
Do not spend effort circumventing these; the cost is unbounded and the
permission is absent.

## P6 — Deferred: why search feels slow

Blocked on the deployed backend, which is not in this checkout. Needs backend
source plus read-only `EXPLAIN (ANALYZE, BUFFERS)` on staging. Do not tune
indexes or add caches before that, and do not rank competitor speed from the
uncontrolled timings in [benchmarks.md](benchmarks.md).

## Sequencing rationale

P0 first because it converts every later decision from opinion into a number.
P1 second because both regressions are already measured and cheap. P2 third
because it is a prerequisite for judging P3 — without identity dedup, a paid
feed looks redundant even when it is not. P3 and P4 are additive only after
that. P6 waits on access, not on effort.

---

## Second pass — 2026-11-02

What changed since the roadmap above was written, and what is now blocked on
someone other than this investigation.

**P0 is half done.** The read-only measurement exists
([`pre_dedup_benchmark.py`](pre_dedup_benchmark.py)) and reports per-source
`discovered / valid / duplicate_in_batch / already_in_feed / new / updated` plus
ms per 1,000. What is still missing is the *pipeline-side* part: emitting
`source_stats` and per-row dispositions from `generate_listings.py`. Without
that, the harness measures a bounded sample, not a production run.

**The paid-feed decision is blocked on keys, not analysis.**
[paid-feeds-evaluation.md](paid-feeds-evaluation.md) records everything that can
be learned without one. jobdataapi's anonymous allowance is exhausted after a
single query (403 thereafter), and Headcount Labs requires a bearer token for
`/v1/jobs`. Both adapters are written and skip cleanly.

- Headcount Labs: free allowance is 100,000 records/month (not the 1,000 a
  third-party listing claims), $0.01/1,000 after that, 30 req/60 s, 1,000
  req/day. Hourly polling at 250/page costs about $0.80/month.
- Blocker that no key fixes: it re-checks daily and publishes **no freshness
  SLA**, so it can never serve the 15-minute lane.

**New P1: closure, not age.** [jobhive-staleness.md](jobhive-staleness.md) shows
39–70% of every sampled jobhive bucket is older than 90 days, with postings from
2014–2021 still present and no `status`/`closed_at`/`last_seen` field. The
60/90-day lookback is the only defence, so it produces both false positives
(closed 3 days ago, posted 20 days ago) and false negatives (open rolling
posting dropped at day 61).

Cheap fix, no new dependency: for the 12 ATS platforms this repo already polls,
mark a jobhive row `dropped_absent` when `(ats_type, employer, ats_id)` is
missing from a **successfully completed** board snapshot, and `unknown_liveness`
when that fetch failed. Never infer closure from a partial or failed fetch.

**Coverage: 42.5% of the ATS registry is unreadable today.** 314 of 738
companies sit on an ATS without an adapter (SuccessFactors 68, Oracle Cloud HCM
51, iCIMS 40, Avature 26, Taleo 18, and more — table in
[source-inventory.md](source-inventory.md#6-ats-coverage-gap-counted-from-the-same-registry-the-adapter-uses)).
Probe these through the existing robots-gated `sitemap_sources` path before
writing a vendor adapter. Eightfold stays out: its `robots.txt` disallows all.

**Revised order.** P0 pipeline instrumentation → jobhive closure check (P1) →
one-week keyed trial at hourly cadence for Headcount Labs / jobdataapi, decided
by a rule fixed in advance (net-new unique per 1,000 records must beat `jobspy`
at 11.7 d median freshness, within budget) → sitemap probes for
SuccessFactors/Taleo → iCIMS/Oracle. Search-side work (old P6) stays blocked on
backend access.

---

## Third pass — implemented 2026-11-02

P0 and the new P1 from the second pass are now in the code, not just planned.
Details, measured evidence, and the checks actually run are in
[FIXES.md](FIXES.md); the work queue that follows is in [FUTURE.md](FUTURE.md).

- **P0 (pipeline half): done.** `Listings/ingest_stats.py` collects per-source
  discovered / valid / new / seconds / status and writes
  `pages/source_stats.json` every run, including `new_per_second` — the figure
  the acceptance criterion asked for.
- **P1 (closure): done for the boards this repo crawls.** `Listings/closure.py`
  drops a jobhive row when its employer's board was re-crawled and its `ats_id`
  is gone, and records no vote for a board that failed. Join verified: 30/30
greenhouse priority boards match jobhive employer names, covering 7,268 rows;
  `ats_id` equals the apply-URL tail exactly.
- **P1 (EchoJobs 429): done.** Stops on a 429 instead of retrying into it, delay
  and page cap are env-tunable, and the outcome is reported as `rate_limited`.
- **Still open:** per-row dispositions, identity dedup, the keyed trials,
  and all ATS-gap work. Every one of them is in
  [FUTURE.md](FUTURE.md) with acceptance criteria.
- **Workday closure: done 2026-11-02** (req-id parser + staging check green
  on 4 real boards); only the jobhive-side `ats_id` shape re-verification
  is left, recorded in FUTURE.md.
- **Still blocked:** search-side performance, on access to the deployed backend
  rather than on effort.
