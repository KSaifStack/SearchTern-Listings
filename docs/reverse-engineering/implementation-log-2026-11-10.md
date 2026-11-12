# Implementation log — publication integrity, 2026-11-10

**Scope:** implement the first P0 publication fixes from the backlog. No deploy, commit, or push.
**Status:** P0-2 through P0-6, provenance hardening, physical LKG, and two controlled isolated all-tier runs are complete. Release-asset migration (P1-3) and a live deployment remain.

## Starting state

- Feed: 13,748 rows, 55.8 MB, 100% `source=jobhive`.
- The identity fix and `countries/` staging already existed as uncommitted working-tree changes before this implementation. I verified them but did not write them.
- `snapshots.merge` had tests but no production caller.
- `write_listings_json` wrote `listings.json` and `searchtern.json` directly with `open(..., "w")`.
- There was no `pages/feed-manifest.json`.

## What changed

### 1. Production union boundary

**File:** `Listings/publish.py` (new)
**Integrated at:** `Listings/generate_listings.py:2184`

`build_publish_frame` now:

1. Collects every sidecar in `Listings/cache/snapshots/`.
2. Calls `snapshots.merge` over the eligible snapshots.
3. Splits the current run's frame by provenance (`jobhive`, `ats`, `priority`, `md`, `freehire`, `breadth`, `jobspy`, `ecr`, specialty).
4. Re-inserts each live group at its snapshot precedence, so:
   - a direct-ATS row still beats a jobhive duplicate;
   - a live jobhive row beats a cached markdown duplicate;
   - a live row beats its cached copy inside the same class.
5. Deduplicates once more in precedence order.

The publication input no longer depends on `RUN_TIERS`. A light run keeps medium/heavy rows from snapshots that are still inside TTL.

**Why the first version was corrected:** appending the entire live frame after the snapshot union put a markdown cache ahead of a live jobhive duplicate. The tests now pin the class order explicitly.

### 2. Correct jobhive sidecar count

**File:** `Listings/generate_listings.py:722, 2210`

`jobhive_live_count` is captured immediately after the jobhive query. The sidecar now records the jobhive fetch size, not `len(_seen_frame)` (which is the union after every source is merged). This prevents the snapshot ledger from claiming the union as jobhive rows.

### 3. Atomic feed writes and manifest

**File:** `Listings/readme_generation.py:125-267`

- Added `_atomic_write_pairs`, which stages every file, fsyncs, backs up the old files, replaces all finals, and restores the old files if any replacement fails.
- `write_listings_json` now writes `listings.json`, `searchtern.json`, and `feed-manifest.json` as one three-file transaction.
- `write_priority_json` and `write_feed_xml` use the same atomic single-file path.
- `feed-manifest.json` contains:
  - `generated_at`
  - `status`
  - `published.{listings,searchtern}.{rows,bytes,sha256}`
  - `source_counts` from the published searchtern records
  - `inputs.snapshots` summary (count, fresh, expired, rejected)

The manifest is written atomically with the feeds, so a crash cannot leave a manifest pointing at a partial feed. Its row counts and hashes are tested against the files.

## Tests

New:

- `Listings/test_publish.py` — 6 tests: unfetched heavy snapshot survives, direct beats live jobhive, live jobhive beats cached markdown, expired snapshot excluded, missing cache returns live, duplicate published once.
- `Listings/test_atomic_publish.py` — 4 tests: both feeds written, manifest rows/bytes/hashes reconcile, single-write failure keeps the old file, pair failure rolls back both feeds.

Commands actually run:

```text
cd Listings && python3 -m pytest -q test_publish.py test_atomic_publish.py test_listings_json.py test_integration.py test_snapshots.py
27 passed in 1.06s

cd Listings && python3 -m pytest -q
173 passed in 4.95s

cd Listings && python3 -m py_compile publish.py generate_listings.py readme_generation.py test_publish.py test_atomic_publish.py
COMPILE_EXIT=0

git diff --check
(no output)
```

## P0-2, P0-6, TTL, LKG, and reconciliation (same day)

### Sidecar coverage and TTL

- `snapshots.EXPECTED_SOURCES` now names the sources a complete run should
  leave a sidecar for; `OPTIONAL_SOURCES` covers opt-in sitemap/runway/RSS.
- `snapshots.seed()` backfills a sidecar for a legacy parquet using the file
  mtime, not the current clock, so seeding cannot make an old cache look fresh.
- `_load_cached()` now checks the sidecar before reading the parquet. Expired
  caches are withheld; a rejected latest parse may serve the previous snapshot,
  but is reported. The ledger is no longer inferred from parquet existence.
- `snapshots.reconcile_rows()` repairs a sidecar row count without changing
  `fetched_at` or `status`.
- `snapshots.report()` exposes `expected`, `missing`, `unexpected`, `seeded`,
  and full `expired`/`rejected` lists. The manifest carries those lists too.
- `jobhive` now records its live count before the union; `rss` records its live
  result as well. Zero rows is `empty`, never `ok`.
- Closure voting now comes from `snapshots.collect()`, not `RUN_TIERS`. An
  expired or rejected cache cannot vote to close a listing, so TTL expiry stays
  a publication decision and never becomes a closure signal.

### Publication gate and last-known-good

- `publish.assess()` treats `jobhive` as the only critical source for now.
  Missing, expired, rejected, or empty jobhive blocks publication; a missing
  non-critical source marks the feed `degraded` but still publishes known rows.
- On a blocked publish, `pages/publish-status.json` records the blockers and
  the previous manifest pointer. The feed and manifest are not touched; the
  process exits non-zero so a workflow cannot commit a partial result.
- `write_listings_json(..., required_sources=..., minimum_rows=...)` rejects an
  empty or critical-source-missing feed before staging any temp file. Existing
  feeds stay byte-identical.
- A successful manifest carries `last_known_good` from the previous manifest
  (generation time plus published row/byte/hash entries). The untouched previous
  files are the recovery copy until the release-asset work adds a durable
  addressable artifact.

### Count reconciliation

- New `Listings/feed_manifest.py` verifies manifest byte counts and SHA-256
  against the feed; row parsing is opt-in for tests because it doubles peak
  memory on the production file.
- After the atomic write, reconciliation patches `run_meta.json`, `health.json`,
  and `source_stats.json` with the manifest's published counts and pointer.
  `run_meta.total` is retained as a candidate-stage count and labelled
  `total_stage: pre_write_candidate_rows` instead of pretending to be published.

### CI gate and timeouts

- `update-{light,medium,heavy,all,priority}.yml` install pytest and run
  `cd Listings && python -m pytest -q` before the scraper.
- Job timeouts: light 20m, medium 40m, heavy 45m, all 90m, priority 20m.
- `test_workflows.py` fails if a publisher drops pytest, drops a timeout, or
  runs the scraper before the tests.

### Measured feed-size budget

- Current committed feed: **13,748 rows / 55,838,379 bytes / 4,061.6 bytes per
  row**. At that size GitHub's 100 MB ceiling is roughly **24,621 rows**.
- A synthetic 400-row writer fixture serialized to **356,402 bytes / 891.0
  bytes per row**; `test_feed_size.py` records that measured budget against the
  real manifest. The fixture is deliberately smaller than production records,
  so it is a schema regression test, not a production ceiling estimate.
- The controlled all-tier run supersedes the earlier projection: **42,674 rows**
  at the measured 2,363.1 bytes/row is **100.84 MB**, already over the hard
  limit. No union was committed and release-asset migration remains mandatory
  before that volume is published.

### Additional tests and results

New/updated tests cover missing, expired, and rejected critical snapshots;
missing non-critical degradation; empty-feed refusal; required-source refusal;
manifest `last_known_good`; manifest row/hash verification; reconciliation of
all three legacy artifacts; corrupted manifest counts; sidecar seeding from
mtime; stale class/TTL metadata; row-count repair; workflow gates; and the
serialized feed budget.

```text
cd Listings && python3 -m py_compile publish.py snapshots.py readme_generation.py \
  feed_manifest.py generate_listings.py test_publish.py test_atomic_publish.py \
  test_snapshots.py test_feed_manifest.py test_feed_size.py test_workflows.py
COMPILE_OK

cd Listings && python3 -m pytest -q
190 passed in 5.88s

cd Listings && python3 -m pytest -q test_publish.py test_atomic_publish.py \
  test_snapshots.py test_feed_manifest.py test_feed_size.py test_workflows.py \
  test_listings_json.py test_integration.py test_shadow_publish.py test_main_guard.py
64 passed in 3.22s

git diff --check
(no output)
```

## Controlled all-tier validation (same day)

**Method.** Two isolated staging runs, both with `SEARCHTERN_WORKERS=16` and
`--tier all --force --ecr-limit 500`; no production file or git state was
touched:

```text
# first run: empty cache and empty pages
.staging/all-tier-2026-11-10/Listings
# second run: post-fix code, preserved cache/pages
.staging/all-tier-2026-11-10-rerun/Listings
```

The ECR cap was deliberate: the uncapped fallback began parsing 13,596 pages at
about 0.25 s/page. Both runs used the embedded listing (12,027 records) and kept
500 US rows; this is an all-tier run with a bounded ECR stage, not a full ECR
sitemap crawl. Each took about 19 minutes and exited 0 with no traceback.

### Sidecar and timestamp audit

The first run wrote nine sidecars, all `status=ok`, `provenance=fetch`,
`source_match=true`. `echo` and `skill` returned no rows and left no sidecars,
so the manifest was `degraded` with those two `missing`.

`snapshots.save()` now treats zero rows as `rejected:empty` even when the
previous count is zero, and the skill/echo/jobspy branches call `_save_cache`
unconditionally. The rerun confirmed the fix: all 11 expected sidecars exist;
nine are `status=ok`, `echo` and `skill` are `rejected:empty`, and there are no
`missing`, `expired`, or `invalid` entries. All 11 carry `provenance=fetch` and
`source_match=true`; ages at the 20:58 assessment were 0.07-0.24 h.

The first run's `pages/snapshots.json` was stamped with the run-start time
(17:23) while sidecars were fetched later (17:24-17:39), producing negative
ages. The report is now stamped at assessment time, and future-dated sidecars
are ineligible; a regression test covers the future-date rejection.

JobSpy hit LinkedIn HTTP 429 and Google returned no data, but Indeed supplied
scraped records -> 272 classified rows. That is a partial-source result, not a
clean multi-site pull.

### Actual serialized output (final rerun)

| Artifact | Rows | Bytes | Bytes/row |
|---|---:|---:|---:|
| `pages/searchtern.json` | 35,422 | 83,705,252 | 2,363.1 |
| `pages/listings.json` | 23,655 | 48,145,907 | 2,035.3 |

The union was 66,895 -> 89,680 rows before final scope/tech filters; the
pre-write frame was 75,277 and the final published feed was 35,422. `jobhive` is
11,451 rows, or **32.33%** of the feed; there are 45 source labels, so the feed
is no longer 100% jobhive. The larger non-jobhive contributors were direct ATS
(4,381), freehire (2,492), and the markdown feeds.

An intermediate rerun exposed a union-clock regression: it passed the run-start
timestamp into `build_publish_frame`, so sidecars written later in the run were
rejected as future-dated and the feed fell to the live-only 25,894 rows. The
union now uses a post-fetch clock; the final rerun restored the union above and
the full 35,422-row feed.

At the measured 2,363.1 bytes/row, GitHub's 100 MB ceiling is **42,317 rows**
(100 MiB: 44,373 rows). Current headroom is **6,895 rows**. A 42,674-row union
would project to **100.84 MB**, which is already over the hard limit. The
release-asset migration is now required before the next coverage increase, not
just before some future one.

Gzip at level 9: `searchtern.json` = 14,767,831 bytes (5.67x), `listings.json` =
7,008,800 bytes (6.87x). The final LKG generation verified successfully and
contained 131,854,656 bytes across the three feed/manifest files.

Count coherence after reconciliation:

- manifest / `health.run.published` / `source_stats.run.published` = 23,655
  listings and 35,422 searchtern rows;
- `run_meta.total` = 43,988 is retained and labelled
  `total_stage=pre_write_candidate_rows`;
- README = 20,674 rows over 15,712 rendered rows and 31 pages. This is the
  60-day README window, not the 90-day JSON feed, so it is intentionally not the
  same count. README is now generated only after the feed is accepted.

The 45 source labels also exposed a precedence bug: markdown rows carry their
feed name, not `md`, so `_source_class` was treating them as `specialty`. The
publication adapter now maps every `markdown_sources.MARKDOWN_SOURCES` name to
`markdown` precedence; the regression test covers this mapping.

### Last-known-good and crash handling

- `feed_manifest.preserve()` writes versioned copies under
  `pages/last-known-good/gen-*`, replaces `current.json` only after the copy is
  complete, and keeps two generations. The directory is gitignored, so runtime
  recovery data is not committed.
- `feed_manifest.prepare()` verifies the current manifest, restores the physical
  LKG when the current generation is torn, and refuses to overwrite an unknown
  state when no LKG exists.
- `feed_manifest.recover()` restores all three artifacts together and verifies
  the result. `python generate_listings.py --recover` is the operator entry point.
- The manifest carries both the previous-generation metadata and a static
  `recovery` pointer to `pages/last-known-good/current.json`.
- Recovery was exercised against the final controlled-run artifact: the three
  restored files were byte-identical, `feed_manifest.verify()` passed on the
  restored 23,655 / 35,422-row generation, and two physical generations were
  retained.

New crash/recovery tests cover: pre-write required-source and empty-feed
rejection; staging/first-replace failure; pair rollback; manifest replace
failure; physical LKG restore; healing a torn current generation before the next
write; refusal without an LKG; missing LKG recovery.

### Validation checks

```text
cd Listings && python3 -m ruff check snapshots.py publish.py feed_manifest.py \
  readme_generation.py generate_listings.py test_snapshots.py test_publish.py \
  test_atomic_publish.py test_feed_manifest.py test_feed_size.py test_workflows.py
All checks passed!

cd Listings && python3 -m py_compile snapshots.py publish.py feed_manifest.py \
  readme_generation.py generate_listings.py
COMPILE_OK

cd Listings && python3 -m pytest -q
201 passed in 5.54s

git diff --check
(no output)
```

### Publication-storage comparison

| Option | Benefit | Cost/risk | Verdict |
|---|---|---|---|
| Commit monolith | No new infra | 100 MB wall, ~132 MB LKG locally, whole-file commits and history churn | Stop before growth |
| Shards in git | Streamable, one part per consumer page | Total gzip is still 14.77 MB/run; adds to git unless monolith stops; consumer migration | Keep as optional read optimization, not the size fix |
| GitHub Release asset + manifest | Removes 100 MB limit and git churn; existing `GITHUB_TOKEN`; previous asset is a pointer | Upload/read coordination; release retention | **Smallest reliable fix** |
| External object storage/CDN | Unlimited scale, independent retention | New credentials, bucket, cost, and monitoring | Defer until release bandwidth/retention is measured insufficient |

Recommended next architecture: upload the immutable feed as a GitHub Release
asset, commit only a small manifest with URL/hash/rows, keep the monolithic
`pages/searchtern.json` for one compatibility release, then migrate the backend
and MCP reader. Shards remain opt-in for client efficiency, not a substitute for
moving the full artifact out of git.

## Remaining limits

- **Release-asset migration is required before the next coverage increase.**
  The measured 35,422-row feed is 83.7 MB with only 6,895 rows of 100 MB
  headroom; a 42,674-row union projects to 100.84 MB.
- **No production deployment.** Both runs were isolated staging, not a live
  workflow; no commit or push was made.
- **The rerun remained `degraded`.** All 11 sidecars now exist and reconcile, but
  `echo` and `skill` are correctly `rejected:empty`; their sources still returned
  no rows.
- **ECR was capped at 500 rows** and JobSpy was partial because LinkedIn
  returned HTTP 429. Source diversity is real, but not every source had a clean
  full pull.
- **LKG is local runtime state.** It survives a torn write in the same
  workspace; a fresh CI checkout still relies on the previous committed feed
  until release assets provide durable history.
- **README counts are a different window.** Feed and README now share an
  accepted publication, but the README is the 60-day tech subset and the JSON
  feed is the 90-day broader set.
- **Deferred source coverage.** No filters were widened and no new source was
  added.

## Risks to watch on the first deployed run

1. **Row growth.** The measured 42,674-row projection is already 100.84 MB. Confirm `pages/feed-manifest.json` rows/bytes and move the feed out of git before publishing more coverage.
2. **New source labels.** Direct/jobhive/markdown precedence is tested. A new source name not in `SOURCE_CLASS` or `MARKDOWN_SOURCES` still falls to specialty; add the mapping and a test with the source.
3. **Long-run clocks.** Freshness is evaluated at assessment time, and future-dated sidecars are rejected. Watch for runner clock skew on the first deployed run.
4. **Atomic pair backup disk use.** Each run temporarily holds the previous `listings.json`/`searchtern.json` as `.bak.<pid>` files plus the physical LKG. The workflow's disk headroom should be checked before the first production run.

## Files changed by this implementation

- `Listings/publish.py` — new union boundary.
- `Listings/generate_listings.py` — integrate union, capture jobhive count, pass snapshot report.
- `Listings/readme_generation.py` — atomic writes, manifest.
- `Listings/test_publish.py` — new union regressions.
- `Listings/test_atomic_publish.py` — new atomic/manifest regressions.
- `Listings/feed_manifest.py` — manifest verification and legacy count reconciliation.
- `Listings/test_feed_manifest.py` — reconciliation and mismatch regressions.
- `Listings/test_feed_size.py` — serialized feed-size budget.
- `Listings/test_workflows.py` — CI test/timeout gate.
- `.github/workflows/update-{light,medium,heavy,all,priority}.yml` — pytest step and job timeouts.
- `.gitignore` — excludes the physical `pages/last-known-good/` recovery state.
- `docs/reverse-engineering/implementation-log-2026-11-10.md` — this log.

Pre-existing working-tree changes not written here: `Listings/identity.py`, `Listings/test_identity.py`, and the four workflow files that stage `countries/`.

## 2026-11-11 release-asset deployment

The first post-fix light run failed after the scraper and five push retries while the
feed was 88.95 MB. The release-asset migration is now implemented; see
[release-assets-2026-11-11.md](release-assets-2026-11-11.md). The feed pair is
uploaded to the rolling `feed-latest` release, verified by hash, and excluded from
the commit; only the manifest and small surface are committed. The backend consumer
migration is still external.

## Next step

The 2026-11-11 validation phase ([validation-2026-11-11.md](validation-2026-11-11.md))
re-ran the staging ingestion, verified all expected sidecars and diversity, added the
one-transaction generation contract and full-surface LKG recovery, and measured the
100 MB wall on 35,293 real rows.

The post-fix staging run now has complete sidecars, a full union, and a
byte-identical LKG recovery. Next:

1. Implement release-asset publishing and keep only the manifest/pointer in git;
   the 42,674-row projection already exceeds the 100 MB limit.
2. Run one deployed all-tier cycle and confirm the release asset, manifest,
   health, source_stats, and README describe the same generation.
3. Re-check `echo` and `skill`; `rejected:empty` is now visible, but neither
   source has recovered.
