# Snapshot / unified-publication audit

Read-only audit, 2026-11-09. No production code, feed or workflow was changed by
this audit. Numbers are from the state of `origin/main` at `21e2395c` (feed
committed 01:36 UTC) plus the committed caches that came with it.

## 1. What the current pipeline does

Refresh and publication are the same pass. `generate_listings.py` walks sources
in fixed blocks, each guarded by `if TIER_X in RUN_TIERS`:

| line | block | gate |
|---|---|---|
| 1684 | freehire (medium) | medium |
| 1730 | breadth feeds | medium |
| 1784 | RSS | light |
| 1815 | new ATS adapters | medium |
| 1856 | ATS probe (heavy) | heavy |
| 1904 | priority boards | priority |
| 1978 | markdown tables | light |
| 2037 | SkillExchange | light |
| 2072 | EchoJobs | light |
| 2136 | JobSpy | light |

Each block does `_dedup_across(existing, new)` then `pd.concat` into
`listings_result` and `readme_result`. Whatever ran is what gets published;
there is no step that reads a source it did not refresh this run.

Publication writes (`generate_listings.py:2646-2690`, `readme_generation.py:105-130`):

- `pages/listings.json` — tech subset after `_feed_scope_filter`
- `pages/searchtern.json` — every role type, same scope filter
- `pages/priority.json` — priority rows from the last 7 days
- `README.md`, `countries/*`, `pages/*.xml`
- `pages/run_meta.json`, `pages/health.json`, `pages/source_stats.json`, `pages/sources.json`
- shards + `manifest.json` only when `SEARCHTERN_SHARDS=1` (off)

Caches (`Listings/cache/tier_*.parquet`) are the de facto snapshots. Every row
carries `observed_at`, so a snapshot's age is derivable per row today. Writes go
through `canonical.write_parquet_atomic` (tmp + replace). `seen.json` is the
identity/closure log, written atomically, 16 MB.

CI: five workflows, two concurrency groups. `light` hourly, `medium` and `heavy`
every 2h, `all` every 4h, `priority` every 15 min on its own group (deliberate:
sharing the group starved the fast lane). Each tier job commits `README.md
pages/ digests/ Listings/cache/` minus `pages/priority.json`; the priority job
commits only `pages/priority.json`.

## 2. Measurements

Published now (01:36 UTC run):

```
pages/searchtern.json   13,984 rows, 100% jobhive
pages/listings.json      4,927 rows, 100% jobhive
pages/priority.json         83 rows
run_meta.per_source      {"jobhive": 9346}
run_meta.sources_this_run jobhive+, md+, freehire-, breadth-, rss-, ecr-, ats-, priority-
```

Snapshots on disk, with the age their own `observed_at` claims:

| snapshot | rows | observed_at | undated | postings >90d | no link |
|---|---|---|---|---|---|
| light (markdown) | 40,506 | 11-09 01:29 | 12,797 | 10,435 | 7,924 |
| medium (freehire) | 5,359 | 11-08 23:45 | 0 | 111 | 0 |
| ecr | 5,120 | 11-08 23:45 | 0 | 506 | 0 |
| ats_new | 801 | 11-08 23:45 | 0 | 116 | 0 |
| jobspy | 451 | 11-09 01:29 | 0 | 0 | 0 |
| heavy (ATS probe) | 439 | 11-09 00:09 | 0 | 63 | 0 |
| priority | 355 | 11-08 20:45 | 0 | 80 | 0 |
| breadth / echo / skill | 32 / 92 / 12 | 11-08 / **09-18** / 10-27 | 0 | 0 | 0 |
| hn / remoteok / startupjobs | 3 / 6 / 21 | **09-23** | 0 | ≤1 | 0 |

Projected union publication (jobhive published set as baseline, each snapshot
deduped by `dedup_keys` after applying that source's own filter — markdown's
60-day window, EU exclusion and ASCII rule included):

| snapshot | rows after its filter | net-new keys |
|---|---|---|
| jobhive (today) | 13,984 | — |
| markdown | 11,767 | 11,044 |
| medium | 5,182 | 4,287 |
| ecr | 5,120 | 3,215 |
| jobspy | 451 | 451 |
| heavy | 416 | 392 |
| ats_new | 704 | 491 |
| priority | 355 | 202 |
| breadth / echo / others | 149 | 146 |
| **union** | | **34,212 keys (2.45x today)** |

Tech-keyed rows in the same pool: 26,337 against 4,927 published in
`listings.json`. This is an upper bound: the scope filter, the closure filter
against live ATS boards, and the 120-day seen cutoff are not applied.

Duplicate pressure: 62.6% of a naive all-snapshot frame collapses under
`dedup_keys`, and 989 of 40,506 markdown keys overlap the published feed — the
sources heavily index the same requisitions, so a union publisher mostly
prevents *loss*, it does not mostly add coverage.

Cost of the current shape: `.git` is 3.4 GB, `pages/` is 83 MB, `Listings/cache`
is 1.3 GB, and each of the 8 feed commits in the last 8 hours rewrote 17-18 files
(126k-142k changed lines). A 2.45x feed would make each of those commits roughly
2.5x larger.

## 3. Findings

**F1 — publication is tier-scoped, so the hourly light run erases coverage.**
Every source block is gated by `RUN_TIERS`. A light run publishes jobhive +
markdown + skill + echo + jobspy and nothing else, then commits over the feed a
full run wrote. Measured: 13,984 rows, 100% jobhive, on a machine whose caches
hold 34k more publishable keys.

**F2 — caches are snapshots without a contract.** No `fetched_at`, `status`,
`etag`, or row-count record beside the parquet; the only freshness signal is the
max `observed_at` inside the rows. `echo`, `hn`, `remoteok`, `startupjobs` are
43-52 days old and still reintroduced by any light run.

**F3 — no success gate.** `_save_cache` is called with whatever the adapter
returned. Only the markdown block guards emptiness (`if md_df.empty: load cached
else: save`). A source that answers 200 with zero parseable rows replaces a good
snapshot with an empty one, and the next run publishes the loss.

**F4 — feed writes are not atomic.** `write_listings_json` opens
`pages/listings.json` and `pages/searchtern.json` with `"w"` and streams
`json.dump` into them; a crash mid-write leaves a truncated feed that the CI
commit step will happily commit. Parquet and `seen.json` already use tmp +
replace.

**F5 — the published counts disagree, and none is labelled.** The same run
reports `run_meta.total = 9,346`, `health.run.published.searchtern = 31,449`,
and writes a 13,984-row `searchtern.json`. Three numbers with three different
meanings (post-scope tech subset, pre-scope, actually written), each unlabelled,
and the artifacts are written at different points in the run.

**F6 — two publishers, no serialisation between them.** The tier group
serialises four workflows, but `priority` runs on its own group by design, so two
publishing processes can interleave; both stage overlapping paths and rely on
`git pull --rebase -X theirs`. There is no single publish step, so "rollback"
means reverting a commit that rewrites generated files while `cache/` and
`seen.json` keep the newer state.

**F7 — closure is already partly snapshot-aware, and must stay that way.**
`seen_index.commit(..., fresh_buckets, ...)` only votes on sources refreshed this
run, which is why an unfetched tier cannot close its rows. That property is the
one thing the current design gets right and a union publisher must preserve: TTL
expiry must remove rows from the feed without ever stamping them closed.

**F8 — the cheap version of this is small; the correct version is not.** Rows are
already in caches with per-row `observed_at` and atomic writes, so a minimum
publisher can be built on top of `_load_cached` without touching adapters. The
expensive parts are the success gate, the duplicate-precedence policy, atomic
feed writes, and the fact that a 2.45x feed cannot keep being committed hourly to
a 3.4 GB repo.

## 4. Seams where the refactor goes

- `_load_cached` / `_save_cache` (generate_listings.py:199-223) — snapshot read
  and write, with sidecar metadata and the success gate.
- Source blocks (the ten `if TIER_X in RUN_TIERS` blocks) — become
  `refresh_<source>()` returning a frame; the tier list becomes a refresh policy.
- `buckets_fresh` (line 2258) — derived from snapshot metadata rather than
  `RUN_TIERS`, so closure keeps voting on genuinely fresh sources only.
- `searchtern_result` / `listings_result` construction (line 2205) — the single
  merge point for every eligible snapshot.
- `write_listings_json` (readme_generation.py:105) — tmp + replace, and one
  manifest listing what was published with its counts.
- Commit step in all five workflows — publish once, from one job.

## 5. Phases and exit criteria

1. **Audit** — this document. Nothing changes. Gate: your approval.
2. **Snapshot layer** — sidecar per source (`fetched_at`, `status`, `rows`,
   `etag`, `source_class`), success gate (200 + row count inside a band of the
   previous snapshot), TTL config. Writes sidecars but publishes exactly what it
   publishes today. Gate: sidecars appear for all sources across one full cycle,
   no change in feed row counts.
3. **Shadow publisher** — compute the union feed alongside the current one, write
   `pages/shadow-searchtern.json` only. Compare keys added/removed, closure
   impact, and size for at least four runs. Gate: a reviewed diff, including
   every row the union would drop.
4. **Rollout** — light publishes the union; keep the previous feed as
   `pages/rollback/searchtern.json` for one cycle. Gate: 24h with no regression
   in closure counts, feed size, or CI commit time.

## 6. Decisions needed before phase 2

1. **TTL per source class** — how old a snapshot may be before its rows leave the
   feed (suggested: jobhive 3h, priority 1h, markdown 24h, boards/ECR 48h,
   echo/hn/remoteok 7d, then drop).
2. **Precedence** — the order duplicates resolve (suggested: priority > direct
   ATS > jobhive > markdown > ECR/freehire > aggregators).
3. **Where a 34k-row feed lives** — committing ~2.5x the current `pages/` every
   hour is not sustainable in a 3.4 GB repo. Options: shards to release assets
   (the migration doc's step 3), or publish to the site/CDN and stop committing
   the feed.
4. **Does TTL expiry ever mean "closed"?** Proposal: no. Expiry removes from the
   feed and nothing else; closure stays with `seen_index` and stale verification.

## 7. Risks

- Feed growth is the binding constraint, not the merge logic.
- Duplicate precedence decides which row's text (salary, location, description)
  survives, so precedence is a data-quality decision, not a formality.
- The success gate is the difference between "a source is down for a day" and "a
  source's rows are gone", so its band needs to be chosen per source, not
  globally.
