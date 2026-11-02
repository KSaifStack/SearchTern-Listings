# FUTURE.md — plan for the next agent

Read [FIXES.md](FIXES.md) first for what already changed, then
[roadmap.md](roadmap.md) for the evidence behind the ordering. This file is the
work queue: what to do next, in what order, how to know it worked, and the traps
that cost time already.

Rule that governs everything here: **no source is integrated before it
demonstrates measurable incremental value.** `pages/source_stats.json` is now the
instrument that decides that. Do not add a paid feed, an ATS adapter, or a
scraper without a `new_per_second` figure for it.

---

## 0. Verify the two changes from 2026-11-02 on a live run

Nothing below is worth doing until these are confirmed in production.

1. Run the scheduled priority workflow once (or `--tier priority` against a
   throwaway copy). Confirm `pages/source_stats.json` exists, has
   `totals.discovered > 0`, and that `jobhive-closure` appears with
   `boards_voted > 0`.
2. Compare `totals.new` against `pages/run_meta.json` → `new`. They must match;
   if they diverge, `_row_source` and the digest key disagree and the per-source
   `new` counts are being attributed to the wrong bucket.
3. Check `totals.closed` did **not** jump by thousands. A large jump means a
   board returned a partial snapshot that still recorded a vote — the exact
   failure `closure.py` is written to prevent. If it happens, tighten
   `record_snapshot` to require a plausible row count, not just ≥1.

**Acceptance:** `source_stats.json` present, totals reconcile, closure drops
plausibly (tens to low hundreds), feed size flat or slightly lower.

---

## 1. Per-row dispositions (finishes P0)

`source_stats` tracks each source in aggregate: discovered, valid, new. It does
not yet say what happened to an *individual* row, which is what an operator needs
when one employer's postings mysteriously vanish.

- Emit `dropped_filter`, `dropped_stale`, `merged_into=<key>` per row, aggregated
  per source into the same artifact.
- Cheapest implementation: one pass in the digest section where `current_by_key`
  already exists — every row not in `current_by_key` was dropped, and the
  last stage that touched it can be tracked with a small counter in
  `_feed_scope_filter`, `_dedupe` call sites, and the age filters.
- Do **not** put this per row in the published feed; it would double feed size.

**Acceptance:** for a fixture run, `discovered = valid + dropped_*` for every
source, exactly.

---

## 2. Extend verified closure (highest value per line of code)

Closure currently covers 11 ATS kinds and only the boards that ran this cycle.

- **Workday: done 2026-11-02.** `closure.workday_req_id` prefers the
  `externalPath` tail, falls back to regex-gated `bulletFields[0]`
  (bulletFields is a label like "Regular Employee" on some tenants);
  verified live against ally/nvidia/ebay CXS endpoints and wired into both
  probe snapshot paths. `workday` is in `VERIFIABLE_ATS`. HTTP 422 (tenant
  moved data centres) drops that employer's discovery cache for
  next-run rediscovery. Staging check: `Listings/validate_closure.py`
  (4 real boards incl. Workday, 1,296 live ids, all rules green). Remaining:
  re-verify the join once a jobhive parquet with Workday rows is sampled
  (`ats_id` shape on that side is still assumed).
- **Cooldown honesty.** The heavy tier skips boards inside a 24 h cooldown and
  reuses the cached snapshot. A snapshot older than the run still votes, which is
  fine (it is still a complete board state), but the vote should carry its age so
  a 23 h old snapshot can be labelled as such. Add `snapshot_age_hours` to the
  closure record and surface the max in `source_stats`.
- **Snapshot-diff question, unanswered.** Before trusting absence at all, diff two
  jobhive snapshots ≥24 h apart and count ids that disappear. If almost nothing
  ever disappears, absence is still a signal (the board crawl is the authority),
  but the *magnitude* tells you how much of the corpus is quietly dead. This is
  one DuckDB query over two parquet URLs; do it and record the number in
  [jobhive-staleness.md](jobhive-staleness.md).
- **Global corporate-suffix strip.** `ats_new_sources._clean_employer`
  strips Inc/LLC/Corp/... locally for the five new adapters because
  hiringOrganization names break cross-source dedup ("GDMS" vs
  "GDMS, Inc."). The shared `clean_company_name` still keeps suffixes;
  promote the strip once the dedup impact is measured on a full run.

**Trap:** never let a board that returned HTTP 200 with zero postings vote. An
ATS board that is mid-deploy returns an empty list, and treating that as "all
these jobs closed" deletes an employer's entire presence for a cycle.

---

## 3. Identity dedup (roadmap P2, unblocks everything about paid feeds)

`dedup.py` keys on canonical URL, falling back to `(company, role, location)`.
The fallback merges two distinct requisitions with the same title and location.

- Use `(ats, employer, requisition_id)` when present — jobhive and jobdataapi
  both publish ids (`ats_type`/`ats_id`, `ext_id`).
- Keep the current key as the last resort for README tables, which have no ids.
- Measure before/after on the committed snapshot: distinct requisitions that
  currently collapse, and cross-source duplicate links (currently 57 across
  20,844).

**Acceptance:** two same-title requisitions from one employer survive as two
rows; duplicate link count goes to zero without losing distinct jobs.

---

## 4. Per-source max-age for README-derived sources

jobhive/README tables carry dead postings far longer than the live feeds
(measured 35.7 d median / 61.5 d p90 for jobhive vs 11.7 d for jobspy). The
60/90-day lookback is the only defence.

- Add a per-source `max_age_days` at ingest, starting at 45 d for README-table
  sources, leaving ATS and live feeds alone.
- Make it data, not code: one dict in `classify.py` or a small `source_policy.py`.

**Acceptance:** p90 age for README sources drops below the configured cap, and
the `dropped_stale` counter from §1 attributes the drop.

---

## 5. ATS coverage gap — probe before writing adapters

314 of 738 registry companies (42.5%) sit on an ATS with no adapter:
SuccessFactors 68, Oracle Cloud HCM 51, iCIMS 40, Avature 26, Taleo 18,
Eightfold 24. Full table in
[source-inventory.md](source-inventory.md#6-ats-coverage-gap-counted-from-the-same-registry-the-adapter-uses).

Order: **sitemap first, adapter second.** `sitemap_sources.py` is already
robots-gated, JSON-LD-aware and per-host delayed, so it proves whether a vendor's
postings are reachable before anyone writes a parser.

1. Run `--sitemap` against a slice of SuccessFactors/Taleo hosts, record parsed
   rows per host in `source_stats`.
2. Only for hosts that parse reliably, write the vendor adapter.
3. Oracle Cloud HCM is the exception worth checking early: it exposes a public
   `hcmRestApi/resources/latest/recruitingCEJobRequisitions` JSON endpoint, so it
   may not need the sitemap path at all.
4. **Eightfold stays out.** Its `robots.txt` disallows all. Do not build it.

**Acceptance per host:** robots check, a live 200, at least one parsed posting,
and a per-source row count.

---

## 6. Paid feeds — blocked on keys, then on a rule fixed in advance

Both adapters exist and skip cleanly (`pre_dedup_benchmark.py`). What is missing
is keys and a decision rule.

- Headcount Labs: free 100,000 records/month, $0.01/1,000 after, 30 req/60 s.
  Daily re-check, **no freshness SLA** → never the 15-minute lane.
- jobdataapi: 131,617 records visible anonymously, then 403. Carries `ext_id` +
  ATS-shaped `application_url` → feeds identity dedup directly.

**Rule to fix before the trial starts:** keep the feed only if net-new unique
employers per 1,000 records beats `jobspy` (11.7 d median freshness) inside
budget. One week at hourly cadence, measured with `source_stats`.

---

## 7. Search-side performance — still blocked

Why SearchTern feels slower than Simplify cannot be answered from this
repository: it contains the feed generator, not the deployed backend. No
`EXPLAIN (ANALYZE, BUFFERS)`, no index list, no cache configuration.

Do not tune indexes or add caches blind, and do not rank competitor speed from
the uncontrolled browser timings in [benchmarks.md](benchmarks.md). What is
needed is backend source plus a read-only staging plan.

---

## Tools and features worth building next

Ordered by ratio of value to effort, for agents that come after this file.

1. **`source_stats` history.** Keep the last N runs in `pages/source_stats.json`
   or a `digests/source-stats-<date>.json`, and alert when a source's `valid`
   halves run-over-run. Today a silently broken adapter is invisible until
   someone notices the feed shrank.
2. **Adapter-level self-checks.** Most adapters already have an `__main__`
   self-check (`echojobs.py`, `jobspy_source.py`, `breadth_sources.py`). Make
   them assert *volume*, not just shape — ">0 rows" passes on a partial outage.
3. **Dead-link budget.** `_verify_link` checks only the first 200 new postings.
   Track how many survive and publish the rate; a rising dead-link rate is the
   earliest signal that a source is serving retired postings.
4. **Scheduler visibility.** Five workflows share `concurrency: job-listings`.
   Record which tier the last run actually was in `source_stats.run`, so a
   cancelled or starved tier is visible rather than inferred.
5. **Cache pruning.** `Listings/cache/` grows per tier with no expiry policy.
   Prune tier caches by age, never `seen.json` (it is the closure/tracking
   history and deleting it would resurrect every closed posting as new).

---

## Traps that already cost time

- **Feed-level yield is a lie.** Dedup runs before publication, so every source
  looks ~100% sole-source. Only pre-dedup measurement means anything.
- **jobhive `apply_url` is null for the entire ADP bucket** (0 of 5,674 rows). Any
  check keyed on `apply_url` silently loses it. Key on `url`/`ats_id`.
- **jobhive has no closure field.** Do not look for one; it does not exist. Absence
  from a board crawl is the only honest signal.
- **EchoJobs rate-limits at ~1 s/page.** A retry loop makes it worse; stop on 429.
- **Boardsweep is not a listings source** — it reads LinkedIn/Indeed to score a CV.
  It is a consumer of the platforms that already block us.
- **Jobsekr does not resolve** (NXDOMAIN).
- **The ruff binary may not be on PATH**; `python -m ruff` works.
