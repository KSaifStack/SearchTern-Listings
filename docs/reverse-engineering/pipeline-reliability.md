# Pipeline reliability & performance sprint (2026-11-03)

Evidence-backed audit of the ingestion pipeline. All numbers measured, not estimated.

## 1. Benchmarks

### Cache inventory (committed state, 2026-11-03)

| tier cache | rows | notes |
|---|---|---|
| tier_light | 41,824 | markdown + breadth + jobspy mix |
| tier_medium | 2,825 | **100% freehire** — no ats_new rows in the committed snapshot |
| tier_heavy | 274 | 100% ats (probe) |
| tier_priority | 350 | hot boards, 7d window |
| tier_ats_new | — | **not committed**; regenerated each medium run |
| seen.json | 65,108 keys / 11.6 MB | load 0.11 s, dumps 0.07 s — not a bottleneck |

### Micro-benchmarks (synthetic 20k-row frame)

- `dedup_keys`: **0.21 s / 20k rows** — not a bottleneck.
- `_to_dt`: already vectorized (`pd.to_datetime(format="mixed")`); per-element
  fallback exists only for pandas<2. Not a bottleneck.
- `import generate_listings` (after fix): side-effect free, ~20 s
  (pandas+duckdb import cost — acceptable, paid once per run).

### New-ATS live re-validation (read-only, 2026-11-03, first board each)

| ATS | rows | secs | note |
|---|---|---|---|
| eightfold / Qualcomm | 9 (2 pp, intern query) | 2.7 | no 429 this run |
| oracle / WSP CX_2001 | 2,000 (cap, truncated) | 35.2 | Swedish-language rows — mostly EU-filtered downstream (wasted fetch, see 3) |
| icims / GDMS | 20 | 14.6 | "Intern Engineer" present |
| adp / Pennoni | 232 | 4.2 | fastest per-row yield |
| bamboohr / Lunar Outpost | 11 | 8.3 | "Project Management Intern - Summer 2027" present |

Medium-tier freshness (committed cache): median 5 d, p75 6 d, max 4,120 d
(epoch junk in freehire dates — caught by the 60/90 d windows downstream,
but it pollutes the cache; see opportunities).

## 2. Fixes shipped

1. **`generate_listings.py` runs only via `main()`** (was: import executes the
   whole pipeline). Evidence it mattered: a bare `import generate_listings`
   during this audit ran a full 4-tier scrape and rewrote three tracked tier
   caches (restored via git). No test or tool imports it at runtime, and
   `test_dedup.py`'s AST-extraction of `_save_cache` still passes unchanged.
   Side benefit: `--help`/bad flags now exit instantly — `parse_args` moved
   ahead of the Jobhive manifest fetch.
2. **Atomic `_save_seen` / `_save_last_hash`** (tmp + `os.replace`), matching
   `_save_cache` and `discover_boards`. A crash mid-write can no longer leave
   a truncated 11 MB seen.json.
3. **Regression test `Listings/test_main_guard.py`**: AST check that no
   top-level statement does work at import, subprocess check that import is
   silent with exit 0, atomic-write checks for both state files.

## 3. Audit results (no change needed, or deferred deliberately)

- **Overlapping runs: impossible by construction.** All five workflows share
  `concurrency.group: job-listings` with `cancel-in-progress: false` — runs
  serialize. Tier caches and `discovered_boards.json` use tmp+replace.
  Downside (not fixed): head-of-line blocking — the 15-min priority run
  queues behind a long `all`+ECR crawl. See opportunities.
- **Closure votes: sound.** New-ATS ids are excluded from Jobhive closure
  votes until their id shapes are verified against jobhive (`generate_listings`
  skips `record_snapshot` for `_rows` boards). Jobhive closure only fires on
  complete re-crawls; failed/empty boards cast no vote. Workday uses the
  externalPath-first req-id parser.
- **Stale filtering:** 60 d README / 90 d listings windows, plus 120 d seen
  cutoff. Ascii-only + EU exclusion applied before publish.
- **Overlap between adapters:** ATS probe (heavy) and ats_new (medium) both
  stamp `source="ats"` but write separate tier caches and dedup against the
  same `readme_result`, so cross-counting in `source_contrib` is consistent.

## 4. Top bottlenecks / risks (ranked)

1. ~~Import runs pipeline~~ — fixed, tested.
2. **Head-of-line blocking** across schedules (single concurrency group).
   Priority freshness degrades whenever `all`+ECR runs long.
3. **Oracle site selection**: WSP `CX_2001` returns Swedish requisitions that
   the EU filter drops — ~35 s/fetch of mostly discarded rows. A US
   siteNumber for the same host would convert this to yield.
4. **Freehire epoch dates** (max age 4,120 d) ride the cache until the window
   filters drop them. Filtering `date < 2010` at fetch costs nothing.
5. **`readme_generation` pages writers still write in place**
   (`write_listings_json`, `write_priority_json`, `write_feed_xml`,
   `sources.json`, digests). Single-writer serialization makes tears
   unlikely, but a kill -9 mid-write ships a truncated feed. Same tmp+replace
   pattern applies.

## 5. Opportunities (not taken, why)

- Separate `concurrency.group` for priority: safe, but changes GH Actions
  behavior — needs a quiet-period watch. Left for the next sprint.
- US Oracle siteNumbers, epoch-date guard, atomic pages writes: each <10
  lines, all queued in FUTURE.md rather than rushed into this change.
- `tier_ats_new.parquet` stays uncommitted (regenerated per medium run);
  committing it would let offline audits measure new-ATS yield without live
  probes.
