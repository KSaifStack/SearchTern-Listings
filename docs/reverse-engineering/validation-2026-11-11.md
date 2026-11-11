# SearchTern-Listings — validation phase, 2026-11-11

**Scope:** validate the publication phase after the snapshot, atomic-write, manifest,
and last-known-good work. Nothing was deployed, committed, pushed, or written to the
production checkout; no filter was widened.

**Method:** one controlled all-tier ingestion in `.staging/validation-2026-11-11`
(a gitignored copy with empty caches and pages), then a publication-only rerun of the
resulting snapshots with the new writer in `.staging/coherent-2026-11-11`.

```bash
SEARCHTERN_WORKERS=16 python generate_listings.py --tier all --force --ecr-limit 500
```

Started 14:01 UTC, published 14:19 UTC (~18 minutes), no traceback. The ECR cap is
deliberate: an uncapped crawl parses ~13.6k pages at roughly 0.25 s/page. JobSpy hit
LinkedIn HTTP 429 and Google returned no data; Indeed supplied the rows. The run is an
all-tier ingestion with a bounded ECR stage and a partial JobSpy pull, not a clean
multi-site pull.

The ingestion used the code as it stood before this phase, so its manifest has no
generation id — that is the baseline. The coherent generation was then produced by a
publication-only rerun of the run's own snapshots and published rows with the new
writer, which is what the generation checks below describe.

## 1. Sidecar coverage and timestamps

All 11 expected sources have a sidecar. `missing=0`, `unexpected=0`, `invalid=0`,
`seeded=0`.

| Source | Status | `fetched_at` (UTC) | Age h | TTL h | Rows | Parquet mtime |
|---|---|---|---:|---:|---:|---|
| ats_new | ok | 14:02:52.947754 | 0.88 | 48 | 794 | 14:02:52.945737 |
| breadth | ok | 14:01:36.809695 | 0.90 | 72 | 35 | 14:01:36.808822 |
| echo | rejected:empty | 14:13:09.153941 | 0.71 | 168 | 0 | — |
| ecr | ok | 14:01:07.491134 | 0.91 | 72 | 287 | 14:01:07.489855 |
| heavy | ok | 14:11:56.490782 | 0.73 | 48 | 3,193 | 14:11:56.488129 |
| jobhive | ok | 14:01:02.258021 | 0.91 | 24 | 44,263 | — |
| jobspy | ok | 14:16:24.667045 | 0.65 | 72 | 313 | 14:16:24.665830 |
| light | ok | 14:12:33.432176 | 0.72 | 48 | 41,325 | 14:12:33.420088 |
| medium | ok | 14:01:31.512705 | 0.90 | 72 | 5,427 | 14:01:31.509828 |
| priority | ok | 14:12:19.114737 | 0.72 | 24 | 360 | 14:12:19.112104 |
| skill | rejected:empty | 14:13:05.434258 | 0.71 | 168 | 0 | — |

Nine sources are `ok`; `echo` and `skill` are `rejected:empty` because both returned
zero rows. That is visible and counted, not silently turned into a missing source.

**Timestamp rules.** Every sidecar carries `provenance=fetch`, `source_match=true`,
policy class and policy TTL. For sources with a parquet, `fetched_at` and the parquet
mtime differ by 0.0 s: the sidecar was written by the same fetch that wrote the data.
That agreement is corroboration, never proof. Specifically:

- `jobhive` has **no parquet** (it is queried live). Its cache mtime cannot prove
  freshness at all; only the fetch sidecar can, and it is the base feed's only
  evidence.
- `echo` and `skill` have no parquet either, and no rows to serve.
- A seeded sidecar records `provenance=cache_mtime` and `provenance_ok=false`; it is
  always ineligible, even when the parquet is young.
- A future-dated `fetched_at` is ineligible.
- TTL is read from policy, not from the sidecar, so stale metadata cannot extend a
  cache's life.

This phase tightened the vocabulary. `is_fresh` now means a successful
(`ok`/`ok:recovered`) fetch inside TTL; a rejected parse is never “fresh”.
`is_servable` is the publication rule: a rejected parse with previous rows inside TTL
may still serve the previous snapshot, and is reported as `rejected` and
`serving_previous`. `echo`/`skill` are therefore `rejected`, not `expired`, and not
`fresh`. New tests pin mtime-cannot-refresh, rejected-empty-not-servable, unknown
status, source/filename mismatch, forged TTL, and one valid sidecar per expected
source.

## 2. Source diversity

The coherent feed is **35,293 rows with 45 source labels**; `jobhive` is 13,027 rows
(**36.91%**). The baseline run was 34,885 rows, 45 labels, 37.36% jobhive.

| Precedence class | Rows |
|---|---:|
| markdown (community feeds) | 17,045 |
| jobhive | 13,027 |
| aggregators (freehire/breadth/jobspy/ecr) | 3,050 |
| direct ATS | 2,075 |
| priority ATS | 96 |
| specialty | 0 |

Largest labels: jobhive 13,027; freehire 2,668; aprameyak Summer 2027 2,206; `ats`
2,075; vanshb03 Summer 2027 1,744; SuryaHarikrishnan Other 2027 1,546; GodlyDonuts
1,302; aprameyak New Grad 2027 1,103; lilguy Live Feed 1,001. Direct ATS, priority,
markdown and aggregator rows all survive dedup and precedence; the feed is not a
renamed single-source slice. Specialty is zero only because `echo` and `skill` were
empty, which the sidecar ledger states explicitly.

## 3. One published generation

The publication is now one transaction (`atomic_fs.write_pairs`) over:

- `pages/listings.json`, `pages/searchtern.json`, `pages/feed-manifest.json`
- `README.md` and every `pages/README-N.md`
- every `countries/*.md`
- `pages/feed.xml`
- reconciled `pages/run_meta.json`, `pages/health.json`, `pages/source_stats.json`

Every file carries `run_id = <UTC second>-<sha256(searchtern)[:12]>`, either in the
manifest and the legacy artifacts or in a hidden marker
(`<!-- searchtern:generation … -->`). `feed_manifest.audit()` verifies:

- manifest byte counts, SHA-256, and (in tests) rows against the files;
- `run_meta`/`health`/`source_stats` generation, published counts, and
  `publication_status=published`;
- README rows and pages in the manifest against the actual README files and `<tr>`
  counts;
- the generation marker on README, every README page, every country page and RSS;
- the manifest's snapshot-ledger `generated_at` against `pages/snapshots.json`.

Coherent rerun result:

```text
run_id          20261111T144857Z-2125bceb7028
searchtern      35,293 rows / 76,285,485 bytes
listings        23,671 rows / 40,253,255 bytes
README          16,443 rendered rows / 32 pages
manifest        4,020 bytes
audit           ok
```

README is intentionally a different window: 60-day tech subset, while `searchtern.json`
is the 90-day broader feed. The manifest now states `published.readme.{rows,pages,
window_days,generation}`, so the difference is labelled rather than mistaken for a
stale page. The same audit against the pre-phase baseline fails (no run id, no markers),
so the check is not vacuous.

If the post-commit audit fails and a previous generation exists, the previous
generation is restored; if it fails on the very first publication there is nothing to
restore and the run fails loudly.

## 4. Last-known-good and crashes at every stage

`preserve()` now copies the whole surface — feed pair, manifest, README pages, country
pages, RSS, count/health artifacts and the snapshot ledger — into
`pages/last-known-good/gen-*`, then replaces `current.json`. Two generations are kept.
`recover()` restores every file together, removes pages that exist only in the newer
generation, and re-verifies the manifest.

Measured on the real 35,293-row generation:

| Check | Result |
|---|---|
| Surface files per generation | 39 (8 pages artifacts, README, 30 country pages) |
| Generations retained | 2 + `current.json` |
| Pointer | `gen-20261111T144909-380061455`, run `20261111T144857Z-2125bceb7028` |
| Torn `searchtern.json` + torn `README.md` | recovered |
| Files changed after recovery | none (`changed_after_recovery: []`) |
| Audit after recovery | ok |

The crash matrix (`Listings/test_publication_stages.py`) fails each stage in turn:

- pre-write gate (missing required source, empty feed);
- README render, country render, legacy-reconciliation build;
- every transaction stage — first backup, every replacement, last replacement —
  by making `os.replace` fail once; after each failure the previous generation is
  byte-identical, no `.tmp` or `.bak` files remain, and the audit still passes;
- audit failure after commit (previous generation restored);
- first publication with no recoverable generation (fails loudly);
- preserve failure after a valid publish (published generation stays coherent);
- blocked publish (`mark_blocked` repoints legacy artifacts at the last published
  generation instead of advertising candidate counts);
- torn current generation healed by `prepare()`, and recovery refused when no LKG
  exists.

## 5. The 100 MB limit, measured

Measured from the coherent 35,293-row artifact, not projected:

| Artifact | Rows | Bytes | Bytes/row | gzip -6 |
|---|---:|---:|---:|---:|
| `pages/searchtern.json` | 35,293 | 76,285,485 | 2,161.5 | 14,527,781 |
| `pages/listings.json` | 23,671 | 40,253,255 | 1,700.5 | 6,338,176 |
| `pages/feed-manifest.json` | — | 4,020 | — | 1,291 |
| `pages/feed.xml` | 200 items | 103,271 | — | 10,524 |
| shards (2 parts, 20k rows/part) | 35,293 | 14,313,678 gz total; largest 13,353,551 | — | — |

At 2,161.5 bytes/row:

| Threshold | Rows |
|---|---:|
| GitHub 50 MiB warning | 24,255 (**already exceeded**) |
| GitHub 100 MB decimal block | 46,264 |
| GitHub 100 MiB block | 48,511 |
| Headroom from 35,293 rows | 10,971 |
| 2 GiB release-asset limit | ~993,515 |

A commit of the current feed already earns the 50 MiB warning. Each such commit adds
about 14.5 MB compressed to history (~87 MB/day at six runs/day). The previous
42,674-row projection at 2,363.1 bytes/row was 100.84 MB; this run measures
2,161.5 bytes/row, so the exact crossing moves to ~46.3k rows, not away from the wall.

| Option | Measured cost | Benefit | Verdict |
|---|---|---|---|
| Commit the monolith | 76.3 MB file, ~14.5 MB/commit history | no new infrastructure | Stop before ~46k rows |
| Shards in git | 14.3 MB gz/run, 2 parts, largest 13.4 MB | streamable; one part per consumer page | Optional read optimization, not the size fix; still adds ~14 MB/run to git |
| GitHub Release asset + manifest | 76.3 MB asset vs 2 GiB per-asset limit; 4.0 KB manifest commit; existing `GITHUB_TOKEN`; previous asset is the rollback pointer | removes the 100 MB wall and feed churn, no new service | **Smallest reliable solution** |
| External object storage/CDN | new credentials, bucket, lifecycle and monitoring | unlimited scale, independent retention | Defer until release bandwidth or retention is measured insufficient |

**Recommendation.** Publish `searchtern.json` as an immutable GitHub Release asset and
commit only `pages/feed-manifest.json`. Keep the monolithic `pages/searchtern.json` for
one compatibility release, migrate the MCP reader and backend to the manifest URL, then
stop committing the feed. Shards stay opt-in for client efficiency; external storage
stays deferred. Do not split the feed into shards as a substitute for leaving git: the
total commit volume and the consumer migration remain.

## 6. Missing, expired, partial, failed, recovered

Covered by the suite, including the cases this phase added:

- missing critical sidecar blocks publication; missing non-critical source publishes a
  degraded feed and names the missing source;
- expired critical sidecar blocks; expired non-critical snapshot is excluded, the
  partial run still publishes known rows, and status is `degraded`;
- rejected critical sidecar blocks; a rejected non-critical source with previous rows
  is served as `serving_previous`, an empty rejected source is not;
- empty feed and required-source rejection leave the previous generation byte-identical;
- failed write at any transaction stage restores every old file;
- manifest/feed mismatch is detected before legacy counts are patched;
- physical recovery restores feed, manifest, README, countries, RSS, health and count
  artifacts together, and removes pages from the newer generation;
- recovery without an LKG fails rather than overwriting unknown state.

## 7. Verification actually run

```text
cd Listings && python3 -m pytest -q
223 passed

python3 -m ruff check atomic_fs.py feed_manifest.py publish.py snapshots.py \
  readme_generation.py readme_rendering.py generate_listings.py \
  test_publication_stages.py test_snapshots.py test_publish.py
All checks passed

python3 -m py_compile atomic_fs.py feed_manifest.py publish.py snapshots.py \
  readme_generation.py readme_rendering.py generate_listings.py
COMPILE_OK

git diff --check
(clean)
```

Read-only staging audits were produced by `.staging/scripts/inspect_run.py` and
`.staging/scripts/coherent_publish.py` (gitignored, not part of the change).

## 8. Limits and next steps

- **Release-asset migration is still required.** The feed is 76.3 MB with ~11k rows of
  100 MB headroom and is already over the 50 MiB warning threshold.
- **`echo` and `skill` returned zero rows.** `rejected:empty` is visible; `skill`
  turned out to be a parser regression and is fixed (§9). `echo` is still blocked.
- **ECR was capped at 500 rows** and JobSpy was partial (LinkedIn 429, Google no data).
  Diversity is real, but not every source had a clean full pull.
- **The coherent generation is a publication-only rerun**, not a second fetch: it
  unions the run's snapshots with its published feed as the live frame. It is evidence
  for the writer and recovery path, not a new ingestion result.
- **README is the 60-day tech subset**, not the 90-day feed. The manifest labels it.
- **LKG is local runtime state.** It survives a torn write in the same workspace; a
  fresh CI checkout still relies on the previous committed feed until release assets
  provide durable history.
- **No production change.** Both directories are isolated staging; workflows were not
  edited in this phase.

Next: implement release-asset publishing under the manifest contract, run one deployed
all-tier cycle, and confirm the release asset, manifest, health, source stats and README
describe the same generation on the runner.

## 9. Retry: SkillExchange was a parser regression, not an empty board

Re-running the two zero-row sources showed they fail for different reasons.

**`skill` (fixed).** `https://skillexchange.xyz/jobs?page=1` answered 200 with 50
`slugId` job objects, while the parser extracted 0 — the board had moved its cards
inside a React Query dehydrated payload:

```text
"state":{"data":{"jobs":[{"id":...,"slugId":57606,"role":...}]}}
```

Each card no longer begins with `{"slugId":`, which was the only anchor the old
extractor used, so every page parsed as empty. `_extract_jobs` now reads the
`"jobs":[...]` arrays through a balanced-bracket scan, keeps the legacy `{"slugId":`
scan as a fallback for the previous layout, and dedupes by `slugId`. The page title
had also changed to "Jobs · RemoteSnipe" (same host, same job URLs).

Measured after the fix:

| Stage | Before | After |
|---|---:|---:|
| Live self-check, 12 pages | 0 rows | 600 rows (12 × 50) |
| Detail link spot-check | — | `…/job/senior-data-engineer/57606` → 200 |
| Pipeline snapshot | `rejected:empty`, 0 rows | `status: ok`, 16 rows, `provenance: fetch` |
| Published generation | no `skillexchange` rows | 16 rows, `source_counts.skillexchange = 16` |

600 fetched rows become 16 published rows because `_classify_freehire` and the
country/tech filters drop rows with no resolvable country (the board is mostly bare
"Remote") and non-eligible titles. That is the existing filter doing its job, not a
second regression.

The end-to-end check ran in a fresh isolated copy (`.staging/retry-2026-11-11`) with an
empty cache: a light-tier pass fetched the board and saved the snapshot, then a
publication-only pass over the same caches published generation
`20261111T162209Z-991b471e7977` (31,003 rows, 20,705 listings, README 13,300 rows over
26 pages). `skill` is reported fresh and the generation is internally consistent.

**`echo` (still blocked).** `echojobs.io` answers 429 for the sitemap, `robots.txt`, and
the homepage from this host, with a browser UA and after backoff. That is an IP-level
block, not a parser or header problem, so the source keeps recording
`rejected:empty` (0 rows) and stays visible as degraded. No code change is warranted;
it needs a different egress address to recover.

Verification:

```text
cd Listings && python3 -m pytest -q
228 passed        # 5 new regression tests in test_skillexchange.py

python3 -m ruff check skillexchange.py test_skillexchange.py
All checks passed
```
