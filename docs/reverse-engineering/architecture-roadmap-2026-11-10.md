# SearchTern-Listings — architecture roadmap

**Date:** 2026-11-10  
**Scope:** target publication architecture and migration order. No production code, deployment, commit, or push was made for this document.

## 1. Principles

1. **Preserve the working ingestion layer.** The adapters, conditional HTTP cache, normalization, identity, dedup, closure, and source-health modules have earned their place. Replace the publication decision, not the fetch layer.
2. **Snapshots are the system of record for “what we know.”** A tier is a fetch schedule, not a publication boundary. A light run may refresh less, but it must not erase a heavy source it did not fetch.
3. **One publisher.** Exactly one process decides the feed. Independent tier workflows may fetch and persist snapshots; they may not each write and commit a different feed.
4. **Validate before replace.** A failed parse, failed reconciliation, or interrupted write leaves the previous artifact addressable.
5. **No new framework without a measured trigger.** The current scale does not justify Dagster, Prefect, dbt, Great Expectations, or an OTLP collector. Add structure, not a platform.
6. **The manifest is the public contract.** Counts, hashes, freshness, and source status come from one file. README and country pages derive from it.
7. **Leave git before the feed outgrows it.** The feed can be served from an immutable release asset or object store; git should hold the manifest/pointer, not a 139–173 MB JSON.

## 2. Current versus target

### Current

```text
GitHub Actions (light / medium / heavy / all / priority)
        |
        v
fetch sources -> per-tier parquet caches -> tier-filtered frame
        |
        v
_feed_scope_filter(listings_result)
        |
        v
write pages/listings.json + pages/searchtern.json (direct open/write)
        |
        v
independent workflow commit (git add pages/ ...; git pull --rebase -X theirs)
        |
        v
consumers scrape the monolith
```

Failure properties:

- A light run publishes only what light fetched. Heavy/medium rows disappear even though their caches are valid.
- The published count is not the union count; `run_meta`, `health`, and the artifact disagree.
- A process death during `json.dump` can leave a truncated feed.
- There is no last-known-good pointer; the current commit is the only version.
- Every feed commit adds a large blob to a 3.6 GiB history.
- `snapshots.merge()` exists and is tested, but is not called by `generate_listings.py`.
- `snapshots.write_report()` is called, but no `pages/snapshots.json` exists in the published checkout, so the ledger is not yet an operational artifact.

### Target

```text
sources
  |
  v
per-source snapshot (parquet) + sidecar
  |   rows, status, fetched_at, ttl, etag, rejections, truncation
  v
validation / contract gate
  |
  v
unified publisher (eligible snapshots, precedence, dedup, final filters)
  |
  v
immutable feed artifact (release asset / object store)
  + manifest (rows, bytes, sha256, source ages, status)
  |
  v
atomic pointer commit (pages/feed-manifest.json)
  |
  v
consumers: manifest -> artifact; fallback -> previous artifact
```

Properties:

- Fetch and publish are separate concerns.
- A source’s absence from this run is explicit (`not_fetched`), not an implicit deletion.
- Only one publisher writes a feed.
- The previous good artifact remains until the new one passes validation.
- The monolith leaves git before it crosses the file limit.
- The manifest can be consumed without downloading the feed.

## 3. Design decisions

### 3.1 Snapshot classes and TTL

Keep the policy in `Listings/snapshots.py`; it is already explicit and tested.

| Class | Sources | TTL | Schedule implication |
|---|---|---:|---|
| `priority_ats` | priority boards | 24 h | 15-minute workflow; stale after a missed day |
| `direct_ats` | heavy, ats_new, sitemap | 48 h | 2-hour heavy schedule; tolerant of one missed cycle |
| `jobhive` | jobhive | 24 h | hourly/4-hourly; current feed was 31 h old at review |
| `markdown` | light | 48 h | hourly light |
| `aggregators` | medium, breadth, jobspy, ecr | 72 h | 2-hour medium |
| `specialty` | echo, hn, remoteok, skill, startupjobs, runway | 7 days | opt-in/irregular |

**Critical correction:** TTL expiry must not produce an empty feed. At review, the only published feed was 31 hours old, which is already beyond the 24-hour jobhive TTL. If the unified publisher had been enabled with a strict TTL and no fallback, it could have published zero jobhive rows. The publisher needs a `missing_critical_snapshot` path that serves last-known-good and raises an alert. Expiry is “not refreshed,” never “closed.”

### 3.2 Eligibility and precedence

`snapshots.merge()` already implements the important property: union every eligible snapshot in precedence order, not the tiers that ran.

- Precedence: `direct_ats` → `priority_ats` → `jobhive` → `markdown` → `aggregators` → `specialty`.
- Each frame is deduped before union; already-seen keys are removed from lower-precedence frames.
- The publisher must not pass `RUN_TIERS` or the current `listings_result` as the union input.

The production call should be a named function with a testable boundary:

```python
def build_publish_frame(cache_dir, now):
    sidecars = snapshots.collect(cache_dir, now)
    require_expected_sources(sidecars, expected=EXPECTED_SOURCES)
    frame = snapshots.merge(sidecars, load=lambda name: _load_cached(name), now=now)
    if frame.empty:
        raise PublishError("no eligible rows")
    return frame
```

`EXPECTED_SOURCES` should be explicit, not “whatever files exist.” A missing expected source should be visible, even if the publisher can proceed with a degraded feed.

### 3.3 One publisher

Recommended split:

- `update-light`, `update-medium`, `update-heavy`, `update-all`, `update-priority`: fetch and persist snapshots only. They must not write `pages/searchtern.json`, `pages/listings.json`, or commit a feed.
- `publish-feed`: reads snapshots, validates, writes one artifact, updates one manifest, commits only the pointer + small diagnostics. This can be a separate workflow triggered after a successful fetch, or the final stage of a single “all” job. A separate job is clearer and prevents a tier workflow from publishing by accident.
- `concurrency: publish-feed` with `cancel-in-progress: false` so a publisher is never interrupted mid-write.
- The publisher is idempotent for a given input hash. Re-running it with the same snapshots produces the same artifact hash, or at least the same row count and canonical content.

Do not run two publishers against different tier selections. That is the current failure with extra steps.

### 3.4 Atomic artifact write

Current `write_listings_json()` opens both final paths directly:

```python
with open(export_path, "w", ...) as f:      # listings.json
    json.dump(...)
with open(mirrored_path, "w", ...) as f:    # searchtern.json
    json.dump(...)
```

(`Listings/readme_generation.py:121-145`)

Required pattern:

```text
write temp file in same directory
  -> flush + fsync
  -> parse/validate temp
  -> os.replace(temp, final)
  -> fsync directory
  -> repeat for every artifact
  -> write manifest last
```

Rules:

- Validate the temp file before replacing the live file.
- Never replace `searchtern.json` before `listings.json` validation succeeds; a partial pair is a failed publication.
- Write the manifest last, after every feed artifact is durable.
- On any exception, remove the temp file and leave the previous artifact untouched.
- `write_priority_json` and `write_feed_xml` need the same treatment; they are smaller but have the same failure shape.
- Prefer one shared helper (`atomic_json_write`, `atomic_text_write`) over repeating temp/replace logic.

### 3.5 Manifest contract

A small JSON file committed to the repo can point at the artifact and carry the audit surface. Proposed fields:

```json
{
  "schema_version": 1,
  "run_id": "2026-11-10T14:05:00Z-7f3a",
  "generated_at": "2026-11-10T14:05:00Z",
  "publisher_version": "1",
  "status": "ok",
  "artifact": {
    "url": "https://github.com/.../releases/download/feed-2026-11-10/feed.json",
    "path": "feed.json",
    "bytes": 89000000,
    "sha256": "...",
    "rows": 21784
  },
  "previous": {
    "run_id": "...",
    "url": "...",
    "sha256": "..."
  },
  "inputs": {
    "snapshots": [
      {"name": "jobhive", "class": "jobhive", "rows": 9300, "age_hours": 1.2,
       "status": "ok", "fresh": true},
      {"name": "heavy", "class": "direct_ats", "rows": 40000, "age_hours": 3.0,
       "status": "ok", "fresh": true}
    ],
    "expected": 17,
    "present": 17,
    "fresh": 16,
    "expired": ["..."],
    "missing": []
  },
  "checks": {
    "required_fields": "pass",
    "duplicate_urls": 0,
    "counts_reconciled": true,
    "future_dates": 0
  },
  "feed": {
    "rows": 21784,
    "bytes": 89000000,
    "source_counts": {"jobhive": 9300, "heavy": 12484},
    "first_seen_coverage": 1.0,
    "median_age_days": 22,
    "p90_age_days": 54
  }
}
```

Notes:

- Do not put secrets or internal tokens in the manifest.
- `status` must be `ok`, `degraded`, or `failed`; `degraded` is not silently `ok`.
- `previous` is the rollback pointer. Consumers should use it when the current artifact fails a checksum or validation.
- `feed.source_counts` is after dedup/precedence, not raw fetch counts. Keep raw counts in `inputs`.
- `first_seen_coverage` is a first-class metric. It should be 1.0 before the product advertises newness.
- The manifest should be small enough to commit on every publish (a few KB), not a copy of the feed.

### 3.6 Last-known-good and rollback

Without new infrastructure, GitHub Releases are the smallest viable artifact store:

1. Publisher writes `feed.json` and `manifest.json` to a temp directory.
2. Upload both as release assets tagged by `run_id` or date.
3. Validate the uploaded asset by downloading it and checking `sha256` and row count.
4. Commit `pages/feed-manifest.json` pointing at the verified asset.
5. Keep at least the previous release asset. Never delete the current or previous until a newer one is verified.

Rollback is a pointer change, not a rebuild:

```text
pages/feed-manifest.json -> previous release asset
```

If a CDN/object store is later justified, the same manifest contract applies; only `artifact.url` changes. Do not couple the contract to GitHub.

For the transition, keep `pages/searchtern.json` as a compatibility artifact until every consumer reads the manifest. Do not remove the old path and the new path in the same release.

### 3.7 Consumer migration

Known consumers:

- The deployed backend scrapes `pages/searchtern.json` (code comment in `readme_generation.py:131-136`).
- The MCP server reads `pages/searchtern.json` or `SEARCHTERN_DATA_URL`, loads the whole file, and caches it for the process lifetime (`Listings/searchtern_mcp.py:87-103`).
- The RSS feed reads a 200-row projection.

Migration order:

1. Publish the manifest and immutable artifact while **also** writing the old monolith for one release.
2. Update the MCP server to read the manifest, stream/parse the artifact, and support a `manifest` tool. Invalidate on `generated_at`/ETag instead of caching forever.
3. Update the backend to read the manifest URL, with a fallback to its current file.
4. Once traffic/telemetry confirms the manifest path, stop committing the monolith.
5. Keep the previous artifact for at least one full rollback window.

Do not switch consumers to shards and remove the monolith in one step.

### 3.8 Observability

Do not add a collector first. Add structured events that a collector could consume later:

```json
{
  "run_id": "...",
  "stage": "publish",
  "source": "jobhive",
  "status": "ok",
  "rows_in": 9337,
  "rows_out": 9300,
  "duration_ms": 1234,
  "error_class": null
}
```

Minimum dashboard/SLO inputs:

- feed age (manifest `generated_at`)
- published rows and bytes
- per-source fresh/expired/missing/rejected counts
- truncation and failure counts
- row delta versus previous run
- required-field coverage
- duplicate canonical URL rate
- validation failure count
- publish duration and upload duration

Alert rules should be derived from schedule intervals, not arbitrary promises:

- critical source missing or expired beyond 2× its expected interval;
- feed age above 2× the publisher interval;
- validation failure;
- artifact checksum mismatch;
- published row change beyond a configured band (both shrink and unexplained growth).

Do not alert on every source failure; the existing source-health distinction between “failed/truncated” and “quiet” is useful.

## 4. Migration phases

### Phase 0 — Land the known data fix

**Goal:** stop reviewing the wrong input. Apply the already-developed `NA`-identity fix and stage `countries/` so the public pages regenerate. This is not a re-investigation.

**Exit:** identity regression tests pass; one light run produces the expected larger row set; country pages reflect the run.

### Phase 1 — Contract and manifest

**Goal:** make publication countable before making it bigger.

- Add `FeedManifest`/`PublishReport` with the fields above.
- Reconcile `run_meta`, `health`, `source_stats`, and the feed count.
- Add `pytest` to CI; add `timeout-minutes`.
- Fail the run when required counts are inconsistent.

**Exit:** the feed, manifest, health, and run metadata agree; CI blocks a bad publish.

### Phase 2 — Snapshot preservation

**Goal:** every source has a durable, explicit state.

- Write a sidecar for every cache and every live-fetched source; the `jobhive` record already exists but must be joined by all others.
- Make `_load_cached` check sidecar existence/status; do not silently load a rejected/expired snapshot.
- Write `pages/snapshots.json` from the same ledger (the code path exists but the artifact is absent).
- Add tests for missing sidecars, rejected snapshots, and TTL.
- Resolve the 31-hour-feed vs 24-hour-TTL contradiction with LKG, not by weakening TTL silently.

**Exit:** an operator can answer “what do we know, how old is it, and why is it not published?” from the repository.

### Phase 3 — Unified publisher

**Goal:** eliminate the known tier-scoped failure.

- Move the union to `build_publish_frame()`; remove `RUN_TIERS` from the publication decision.
- Make each tier workflow fetch-only.
- Add one publisher with a single concurrency group.
- Add regression tests: light-run retention, precedence, expiry, missing critical source, empty union.

**Exit:** a light run cannot reduce the union by failing to fetch heavy sources; a missing source is visible as missing, not as deletion.

### Phase 4 — Atomicity and last-known-good

**Goal:** a failed run cannot damage the feed.

- Shared atomic writer for JSON/text/XML.
- Validate temp before replace.
- Manifest last.
- Release asset + pointer; previous artifact retained.
- Rollback test: point at previous, verify consumers.

**Exit:** simulated disk/write/validation failures leave the previous feed intact; rollback is one pointer change.

### Phase 5 — Leave git

**Goal:** remove the size ceiling.

- Publish the feed as a release asset.
- Commit only `pages/feed-manifest.json` and small diagnostics.
- Keep the old monolith for one compatibility release.
- Migrate MCP and backend to the manifest URL.

**Exit:** no feed commit exceeds the repository’s normal diff budget; the 100 MB ceiling no longer applies to the feed.

### Phase 6 — Product trust

**Goal:** expose only what the data can prove.

- Freshness/verification badge backed by `first_seen_at` and a real liveness check.
- Tri-state sponsorship filter.
- Alerts/saved searches after publication is stable.
- Public counts generated from the manifest.

**Exit:** every public number and badge is traceable to the manifest or a named source check.

## 5. Why this order

- Fixing the publisher before the identity fix would union the wrong rows.
- Adding atomicity before the publisher would protect the wrong feed.
- Adding a manifest before durable snapshots would record incomplete inputs.
- Moving to releases before atomicity/LKG would move the failure, not remove it.
- Adding Dagster/Prefect now would make the orchestrator more sophisticated while the product still publishes one source.

The ordering is dependency-driven, not preference-driven.

## 6. Sizing and the git ceiling

Projection uses the measured 4,061.6 bytes/row. This is an estimate for architecture, not a promise that the union will reach any exact row count.

| Scenario | Rows | Projected size |
|---|---:|---:|
| Current feed | 13,748 | 55.8 MB |
| Identity fix only (prior estimate) | ~21,800 | ~88.5 MB |
| `health` published count | 31,434 | ~127.7 MB |
| Snapshot union (prior audit) | 34,212 | ~139.0 MB |
| Phase-4 union (prior audit) | 42,674 | ~173.3 MB |

GitHub’s hard file limit is about **24,621 rows at 100 MB** or **25,817 at 100 MiB** at the current bytes/row. The union cannot be committed as one file. This is why Phase 5 is not optional if the product wants the all-source feed.

Do not use Git LFS as the consumer contract: it adds client setup, bandwidth/cost, and still leaves the backend and MCP dependent on a large object. A release asset plus manifest is simpler and cacheable.

## 7. Alternatives considered

| Alternative | Verdict | Why |
|---|---|---|
| Keep tier-scoped publication, add more tiers | Reject | Reproduces the known failure; more schedules mean more ways to erase coverage. |
| Naively concat all parquet caches | Reject | Includes stale, failed, or partial snapshots; no precedence, no TTL, no reasons. |
| Use `snapshots.merge` with no manifest | Partial | Fixes union but leaves counts, artifact integrity, and consumer contract unresolved. |
| Add Dagster/Prefect to orchestrate | Reject now | The failure is a publisher boundary, not missing orchestration. Revisit after measured triggers. |
| Add Great Expectations | Reject now | A Python contract with 10–15 invariants is enough; a platform adds setup and docs without a current consumer. |
| Add dbt | Reject now | No warehouse; duplicates pandas/DuckDB transforms. |
| Add OpenTelemetry collector | Defer | Structured events first; collector when there are multiple services and an actual on-call need. |
| Add Apify actors | Defer | Paid, duplicate coverage; use only for a measured blocked source. |
| Commit feed to git + Git LFS | Reject | Does not remove consumer complexity or bandwidth cost; history still grows. |
| Object storage/CDN immediately | Defer | GitHub Releases is the smallest step; CDN after measured latency/bandwidth. |
| Separate publish workflow | Accept | One writer, explicit fetch/publish split, easier tests and rollback. |

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Unified publisher includes stale/failed snapshots | Sidecar status + TTL + explicit `missing/expired/rejected` in manifest; fail closed on critical sources. |
| TTL expiry empties the feed | Serve last-known-good; alert; do not treat expiry as closure. |
| New publisher changes row semantics | Golden fixture tests on precedence, dedup, and final filters; compare counts before/after on one run. |
| Atomic replace leaves mismatched pair | Write both feeds to temp, validate both, replace both, write manifest last; on failure keep previous pair. |
| Release asset becomes unavailable | Manifest `previous` pointer; keep at least two assets; checksum verification. |
| Consumers break during migration | Dual-write old monolith for one release; switch consumers behind a flag; keep fallback. |
| Counts drift again | Manifest is authoritative; health/README derived; CI asserts equality. |
| Snapshot sidecars absent in CI | Persist `Listings/cache/snapshots/` with the cache; add a test that every expected source has a sidecar. |
| Publisher upload exceeds job time | Measure; current run is ~23 minutes; release upload is small relative to fetch. Split upload from fetch if needed. |
| PR review surface grows | Keep each phase behind tests and a rollback; no big-bang migration. |

## 9. Acceptance criteria for the architecture

The roadmap is complete when all of these are true:

1. A light-tier run cannot remove a row that exists in a fresh heavy-tier snapshot.
2. `RUN_TIERS` appears in fetch code, never in the publisher.
3. One process writes the feed; no tier workflow commits a feed.
4. A simulated crash during write leaves the previous feed byte-identical.
5. `pages/feed-manifest.json` exists and its row count, byte size, and hash match the artifact.
6. `run_meta`, `health`, and the manifest report the same published count, or the difference is labelled by stage.
7. A missing critical snapshot produces LKG plus an alert, not an empty feed.
8. A failed validation produces LKG plus an alert, not a partial feed.
9. A light run and an all run over the same snapshots produce the same feed hash.
10. The MCP server reads the manifest and does not cache a feed forever.
11. CI runs the relevant tests and a timeout on every workflow.
12. The feed no longer needs to be committed to git to be consumed.

## 10. Explicit non-goals

- Replacing the source adapters.
- Adding another broad aggregator.
- Building a warehouse, a dbt project, or a feature store.
- Adding a general workflow orchestrator.
- Building an application tracker, autofill, or resume product.
- Rewriting the backend or its database.
- Promising a freshness SLA before the manifest measures it.

## Related documents

- [review-2026-11-10.md](review-2026-11-10.md) — evidence, peer comparison, product review.
- [data-quality-audit-2026-11-10.md](data-quality-audit-2026-11-10.md) — current field and count audit.
- [implementation-backlog-2026-11-10.md](implementation-backlog-2026-11-10.md) — ordered work with tests.
- [snapshot-audit.md](snapshot-audit.md) — prior snapshot failure analysis.
- [publication-migration.md](publication-migration.md) — earlier shard/manifest work.
