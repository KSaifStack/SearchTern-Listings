# SearchTern-Listings — implementation backlog

**Date:** 2026-11-10  
**Status:** planning only. Nothing in this backlog was implemented, deployed, committed, or pushed for this review.

## How to read this

- **P0** fixes the known publication failure and prevents further data loss. Do these before product work.
- **P1** makes publication durable and removes the git ceiling.
- **P2** makes the feed trustworthy enough to expose as a product.
- **P3** is explicitly deferred until a measured trigger appears.
- Effort is rough engineering time for one developer who knows the code; it is not a calendar promise.
- Every item has a testable acceptance condition. “Improve quality” is not acceptance.

The known root cause is assumed, not re-investigated: publication is scoped to the tiers that ran, and the light jobhive path can overwrite the larger all-source feed. The fix is activation and hardening of the existing snapshot/merge design, not a new ingestion system.

## Priority summary

| ID | Priority | Outcome | Effort | Depends on |
|---|---|---|---|---|
| P0-1 | P0 | Land the pending identity fix and country-page staging | 0.5–1 d | — |
| P0-2 | P0 | Every source has a durable snapshot sidecar | 1 d | P0-1 |
| P0-3 | P0 | Publication uses all eligible snapshots, not `RUN_TIERS` | 1–2 d | P0-2 |
| P0-4 | P0 | Feed writes are atomic; a crash cannot truncate | 0.5 d | P0-3 |
| P0-5 | P0 | One manifest reconciles published counts | 1 d | P0-3, P0-4 |
| P0-6 | P0 | Tests and timeouts gate every workflow | 0.25 d | — |
| P1-1 | P1 | TTL, missing-source, and last-known-good behavior are explicit | 1–2 d | P0-5 |
| P1-2 | P1 | One publisher writes one immutable artifact | 2–3 d | P1-1 |
| P1-3 | P1 | Feed leaves git; consumers read a manifest pointer | 2–4 d | P1-2 |
| P1-4 | P1 | Required fields and tri-state values pass a contract | 2–3 d | P0-3 |
| P2-1 | P2 | Source coverage is measured and targeted gaps are filled | 2–4 d | P1-4 |
| P2-2 | P2 | Trust/product surface backed by real freshness data | 3–5 d | P1-4, P1-3 |
| P3-1 | P3 | Dagster/Prefect/dbt/GE/OTel/Apify | — | measured trigger |

Total P0: roughly 4–6 engineering days. That is the smallest complete sequence that fixes the known failure without replacing the pipeline.

---

## P0-1 — Land the pending identity fix and country staging

**Outcome:** the feed stops losing rows to the `NA` identity collapse, and public country pages regenerate from the current run.

**Why first:** the publisher fix must union the correct rows. The current feed is 100% jobhive and the working tree contains the identity and workflow changes, but they are not in the measured deployed state.

**Change:**

- Apply the already-developed `Listings/identity.py` fix that treats `NA` as missing, not as a literal identity key.
- Keep the regression test in `Listings/test_identity.py`; add a case where an all-`NA` column cannot reduce a concatenated frame to one row.
- Stage `countries/` in the workflows that regenerate it, so the stale pages cannot survive another run.
- Do not change the publisher in this item; isolate the data fix.

**Files:** `Listings/identity.py`, `Listings/test_identity.py`, `.github/workflows/update-{light,medium,heavy,all}.yml`, generated `countries/`.

**Acceptance:**

- `python3 -m pytest -q` passes, including the new identity regression.
- A light run's unique row count is greater than 13,748 and the increase is explained by identity-preserved rows, not by a relaxed filter.
- `countries/US.md` timestamp/count reflects the same run as the feed; no page claims a September count.
- Re-running the same inputs produces the same count (no one-row collapse).
- Rollback is a revert of the identity/workflow files; no schema migration.

**Risk:** low. If the fix grows the feed past the 100 MB limit, stop and do P1-3 before publishing that artifact; do not commit a 100 MB file.

**Estimate:** 0.5–1 day.

---

## P0-2 — Record a sidecar for every source

**Outcome:** “what we know” is inspectable per source before the publisher decides anything.

**Why:** `snapshots.save()` gates caches, and `snapshots.record("jobhive", ...)` exists, but `Listings/cache/snapshots/` and `pages/snapshots.json` are absent from the published checkout. The publisher cannot distinguish “not fetched” from “failed” without that state.

**Change:**

- Ensure every cache write goes through `snapshots.save()` (already used by `_save_cache`) and every live-fetched source without a parquet cache calls `snapshots.record()`.
- Make the expected-source list explicit. A missing expected sidecar is a `missing` input, not an empty source.
- Ensure the snapshot directory is persisted with `Listings/cache/` in workflows.
- Write `pages/snapshots.json` from `snapshots.write_report()`; verify it is actually present after a run.
- Add a test fixture that creates one sidecar per expected source and asserts `collect()` sees all of them.

**Files:** `Listings/generate_listings.py` (cache/save call sites), `Listings/snapshots.py`, `Listings/test_snapshots.py`, `.github/workflows/update-*.yml`.

**Acceptance:**

- After a dry run, `Listings/cache/snapshots/*.json` contains one file per expected source.
- `pages/snapshots.json` exists and lists each source with `fetched_at`, `rows`, `status`, `ttl_hours`, and `fresh`.
- A missing sidecar appears as `missing` and does not silently become zero rows.
- A rejected sidecar (`rejected:empty`, `rejected:shrunken`, `rejected:write_failed`) is visible.
- No cache is loaded solely because the parquet file exists; the sidecar status is checked.

**Risk:** medium-low. Sidecars are additive, but a missing sidecar can become a false “missing source” alert if the expected list is wrong. Start with warnings, then fail closed after one clean run.

**Estimate:** 1 day.

---

## P0-3 — Make publication use all eligible snapshots

**Outcome:** a light run no longer erases medium/heavy coverage. This is the direct fix for the known 15K-class failure.

**Why:** `Listings/generate_listings.py:2197` still derives `searchtern_result` from the tier-filtered `listings_result`; `snapshots.merge()` has no production caller.

**Change:**

- Add a single `build_publish_frame(cache_dir, now)` boundary (new `Listings/publish.py` is acceptable; a function in `generate_listings.py` is also acceptable if it stays testable).
- Call `snapshots.collect()` → `snapshots.merge()` → final filters/writer. Do not pass `RUN_TIERS`, `listings_result`, or a tier list into the publisher.
- Keep the existing final filters (`_build_json_records`) after the union; they are the public contract, not the source-selection mechanism.
- Preserve source/provenance fields through the union. Do not restamp everything as jobhive at the end.
- Add a regression that simulates a light run with a fresh heavy snapshot and asserts the heavy rows survive.
- Add a regression that asserts the same set of snapshots produces the same feed regardless of which tier triggered the run.

**Files:** `Listings/generate_listings.py`, `Listings/publish.py` (new, if chosen), `Listings/snapshots.py`, `Listings/test_snapshots.py`, new `Listings/test_publish_contract.py`.

**Acceptance:**

- Test: 10 unique jobhive rows + 10 unique heavy rows + fresh sidecars → published frame contains 20 (before final filters that do not drop them).
- Test: running the publisher with `RUN_TIERS=["light"]` and with `RUN_TIERS=["all"]` over the same snapshots produces identical canonical rows.
- A light run with a missing heavy snapshot does not silently delete heavy rows from a previous successful feed; it either publishes LKG or marks the feed degraded.
- `source_counts` in the manifest show more than one provenance label once direct sources are retained.
- The feed is no longer 100% `source=jobhive` after a full all-source run.

**Risk:** medium. The union may surface stale snapshots or duplicates that were previously hidden. This is why P0-2 (status) and the precedence tests must land first.

**Estimate:** 1–2 days.

---

## P0-4 — Make feed writes atomic

**Outcome:** a process crash, disk-full error, or serialization failure cannot leave a truncated feed.

**Why:** `write_listings_json()` writes `pages/listings.json` and `pages/searchtern.json` directly (`Listings/readme_generation.py:121-145`). A partial write is visible to consumers immediately.

**Change:**

- Add `atomic_json_write(path, payload)` and `atomic_text_write(path, text)` helpers using a temp file in the same directory, `flush`/`fsync`, validation, `os.replace`, and directory `fsync`.
- Build both feed records first; write both temp files; validate both; then replace both. Do not replace one before the other passes.
- Apply the same helper to `priority.json` and `feed.xml`.
- On exception, remove temp files and leave the previous artifacts untouched.
- Add a test that monkeypatches the writer to raise after a partial temp write and asserts the old file is unchanged.

**Files:** `Listings/readme_generation.py`, new `Listings/test_atomic_write.py` (or the closest existing writer test).

**Acceptance:**

- Interrupted/raised write leaves the old `searchtern.json` byte-identical.
- A malformed record fails before `os.replace`; the old feed remains.
- `listings.json` and `searchtern.json` are replaced as a pair, not one at a time.
- `priority.json` and `feed.xml` use the same guarantee.
- No `json.dump` writes directly to a final public path.

**Risk:** low. The helper is mechanical; the pair-replace is the only subtle part.

**Estimate:** 0.5 day.

---

## P0-5 — Publish one reconciled manifest

**Outcome:** the feed has one authoritative count and the operator can see why it changed.

**Why:** `run_meta.total=9,337`, `health.run.published.searchtern=31,434`, and the feed is 13,748. Consumers cannot choose.

**Change:**

- Add `Listings/feed_manifest.py` (or a section of `publish.py`) that constructs the manifest described in the roadmap.
- Include `published.rows`, `published.bytes`, `published.sha256`, `source_counts`, input snapshot status, and `status`.
- Make `run_meta` and `health` derive their published count from the manifest, or explicitly label their fields as `pre_filter`, `candidate`, etc.
- Add a reconciliation check: `manifest.published.rows == len(feed)`, and each source count is ≤ its input count; the sum after dedup is reported separately.
- Fail the publish (keep old feed) when required fields or counts fail; produce `status=degraded` for non-critical source omissions.

**Files:** new `Listings/feed_manifest.py`, `Listings/generate_listings.py`, `Listings/source_health.py`, `Listings/ingest_stats.py`, tests.

**Acceptance:**

- `pages/feed-manifest.json` exists after a run.
- `manifest.published.rows == len(pages/searchtern.json)`.
- `manifest.published.sha256` matches the file.
- `health.run.published.searchtern` and `run_meta.total` either equal the manifest or are labelled with a different stage.
- A deliberately wrong count fails the test.
- A missing critical source produces `status=degraded` (or `failed` with LKG), never a silent `ok`.

**Risk:** medium. The manifest becomes a contract; define field names once and test them.

**Estimate:** 1 day.

---

## P0-6 — Gate workflows on tests and time

**Outcome:** a broken publisher cannot merge because CI never ran the tests, and a hung scrape cannot occupy the concurrency group indefinitely.

**Why:** the workflows run the generator but have no `pytest` step and no `timeout-minutes`. The 163 tests pass locally but are not a gate.

**Change:**

- Add a `test` job (or a pre-step) to the relevant workflows: install the pinned requirements, run `python3 -m pytest -q`.
- Add `timeout-minutes` sized to the observed run (heavy is the long one; do not use one value for all tiers).
- Keep `concurrency: job-listings` for fetch, but add a separate `publish-feed` group later.
- Cache pip dependencies if it materially reduces runtime; otherwise leave it alone.
- Do not add a new CI service.

**Files:** `.github/workflows/update-{light,medium,heavy,all,priority}.yml`, `Listings/requirements.txt` or existing dependency file.

**Acceptance:**

- A deliberately failing test blocks the workflow before publication.
- A hung fetch terminates at the configured timeout; the previous feed stays intact.
- The test command is visible in the workflow log and uses the same Python version as the generator.
- No workflow can publish without the test job succeeding.

**Risk:** low. The main cost is a few minutes per run; cache if needed.

**Estimate:** 0.25 day.

---

## P1-1 — TTL, missing-source, and last-known-good behavior

**Outcome:** “not fetched,” “failed,” “expired,” and “closed” are four different states, and a critical gap serves the last good feed instead of an empty one.

**Why:** the current feed is 31 hours old and jobhive TTL is 24 hours. Strict TTL without LKG would drop the entire feed. The snapshot module already documents that expiry is not closure; the publisher must implement the fallback.

**Change:**

- Add `require_expected_sources()` and a `missing_critical` result.
- Define critical sources explicitly (start with jobhive; add direct sources after a clean run).
- If a critical source is missing/expired/failed: do not publish an empty or partial feed; serve the previous manifest/artifact and alert.
- Add `last_known_good` to the manifest; keep at least the previous artifact.
- Add tests for: expired jobhive, rejected jobhive, missing heavy, empty union.
- Align TTL with schedule or document the LKG exception. Do not silently extend TTL to hide a missed run.

**Files:** `Listings/snapshots.py`, `Listings/publish.py`, `Listings/feed_manifest.py`, tests.

**Acceptance:**

- Expired jobhive + valid previous feed → publisher leaves the previous feed and reports `status=degraded`/`failed`, not zero rows.
- Missing heavy + valid jobhive → feed still publishes, manifest lists heavy as `missing`.
- Rejected jobhive → no publish; previous feed intact.
- `pages/snapshots.json` shows the reason for each source.
- No expiry path marks rows closed.

**Risk:** medium. Critical-source selection is a policy choice; start narrow.

**Estimate:** 1–2 days.

---

## P1-2 — One publisher, one immutable artifact

**Outcome:** exactly one process writes the feed, and the output is addressable by hash.

**Change:**

- Make tier workflows fetch-only. They write snapshots and diagnostics, not `pages/searchtern.json`.
- Add one `publish-feed` workflow/job with `concurrency: publish-feed` and `cancel-in-progress: false`.
- The publisher reads sidecars, builds the union, validates, writes the artifact, uploads it, verifies the uploaded hash, and writes the manifest.
- Publish the artifact as a GitHub Release asset initially; keep the manifest in the repo.
- Keep the old monolith write for one compatibility release, behind a flag, then remove it.
- Idempotency: same input hash → same artifact hash, or an explicit no-op.

**Files:** `.github/workflows/publish-feed.yml`, `.github/workflows/update-*.yml`, `Listings/publish.py`, `Listings/feed_manifest.py`.

**Acceptance:**

- No tier workflow can commit `pages/searchtern.json` or `pages/listings.json`.
- A publisher run produces one artifact, one manifest, and a verifiable `sha256`.
- Re-running with unchanged snapshots does not create a different feed (or cleanly no-ops).
- A failed upload leaves the previous manifest/artifact active.
- No two publishers run concurrently.

**Risk:** medium. Workflow handoff and release permissions need care; do it in staging first.

**Estimate:** 2–3 days.

---

## P1-3 — Move the feed out of git

**Outcome:** the 100 MB ceiling and multi-megabyte history churn stop constraining coverage.

**Why:** 13,748 rows is 55.8 MB; 34,212 rows projects to ~139 MB. The union cannot be committed. This is a structural limit, not a tuning problem.

**Change:**

- Artifact URL: GitHub Release asset (smallest) or object storage/CDN if measured need.
- Commit only `pages/feed-manifest.json` and small diagnostics.
- Keep the old `pages/searchtern.json` for one release as a compatibility copy.
- Update the MCP server to read the manifest and use the artifact URL; invalidate its process cache on `generated_at`/ETag.
- Update the backend to read the manifest URL, with fallback to the old path.
- Publish shards only if measured consumer benefit justifies the extra artifacts (the shard code already exists but is off by default).

**Files:** `Listings/searchtern_mcp.py`, backend consumer config (outside this repo, coordinate), `.github/workflows/publish-feed.yml`, `readme_generation.py` output policy.

**Acceptance:**

- The feed is consumable from the manifest URL without a git checkout.
- No feed commit exceeds the normal repository diff budget.
- MCP returns fresh data after the manifest changes; it does not cache forever.
- Backend falls back to the old path if the manifest is unavailable.
- Rollback to the previous artifact is one pointer change.
- The 34K union can be published without a GitHub file-limit error.

**Risk:** high consumer impact. Dual-write and dual-read first; do not remove the old path in the same release.

**Estimate:** 2–4 days, plus consumer coordination.

---

## P1-4 — Enforce the feed contract

**Outcome:** required fields and tri-state values are true for every published row.

**Change:**

- Required: `source`, `ats_type`, `ats_id` or `identity_unknown`, `company`, `role`, `location`, `link`, `date`, `job_type`, `observed_at`, `first_seen_at`.
- Fix `first_seen_at` so it is present on 100% of published rows and never after `observed_at`.
- Make `is_remote` a JSON boolean or explicit `null`; stop emitting `"true"`/`"false"` strings.
- Make `visa_sponsor` an enum (`true`/`false`/`unknown`) with an evidence source; do not equate empty with false.
- Make `country_iso` a two-letter code or explicit `null` with an `unknown_reason`; track coverage.
- Quarantine future dates; do not silently publish them.
- Set `verified_at` only after an actual liveness check, or remove it and expose `observed_at` honestly.
- Decide whether `direct_apply`, `employment_type`, and `seniority` will be populated; if not, remove them rather than publishing permanent nulls.
- Preserve `source` as aggregator provenance and `ats_type` as original ATS; never collapse the two.

**Files:** `Listings/generate_listings.py`, `Listings/readme_generation.py`, `Listings/freshness.py`, `Listings/identity.py`, `Listings/h1b_enrich.py`, tests.

**Acceptance:**

- Contract test on the published feed: 0 missing required fields; 0 future dates; 0 duplicate canonical URLs; 100% `first_seen_at`; 100% boolean/null `is_remote`; 100% enum `visa_sponsor`.
- Country coverage is reported; it may be below 100% only with an explicit unknown reason.
- A row with an unknown ATS identity is either excluded or explicitly labelled; it never becomes a shared dedupe key.
- `verified_at` is not equal to `observed_at` unless a liveness check actually ran.
- Public counts derive from the manifest.

**Risk:** medium. Backfilling `first_seen_at` needs a defensible source; do not invent dates. If the seen log cannot supply a date, mark the row `first_seen_unknown` rather than fabricating one.

**Estimate:** 2–3 days.

---

## P2-1 — Measure source coverage; fill only proven gaps

**Outcome:** source expansion is driven by unique published yield, not by an ATS registry percentage.

**Why:** the old “314 of 738 companies (42.5%)” figure is stale after new adapters. Do not repeat it as current coverage. The current feed is 100% jobhive; direct-source yield is unmeasurable until publication is fixed.

**Change:**

- After P0/P1, measure per source: fetched, valid, unique-before-dedup, survived-precedence, published, new, closed, truncated, failure rate, latency.
- Add targeted coverage only where the measured unique yield justifies it:
  - SuccessFactors/Taleo via permitted sitemaps/JSON-LD, where the current sitemap path can handle them.
  - Oracle/iCIMS where direct adapters already exist but are underused.
  - Do not add another broad aggregator.
- Do not use Apify until a specific blocked source has terms, cost, and measured unique yield.
- Keep the source inventory as a dated artifact, not a permanent claim.

**Files:** `Listings/source_stats.py` or `ingest_stats.py`, `Listings/source_selector.py`, `Listings/sitemap_sources.py`, `Listings/ats_new_sources.py`, docs.

**Acceptance:**

- A source scorecard shows unique published contribution and cost/latency per source.
- At least one targeted gap is closed only if it adds ≥X unique published rows (choose X before the work; e.g. 100 in-window roles).
- No new source is added if it only duplicates existing coverage.
- The public ATS count is generated from the registry, not hand-maintained.

**Risk:** medium. Source work is open-ended; scope it to measured gaps and a fixed time box.

**Estimate:** 2–4 days after P1.

---

## P2-2 — Trust and product surface

**Outcome:** users can see why a row is fresh, where it came from, and whether sponsorship is known; alerts are built on a feed that can support them.

**Change, in order:**

1. **Freshness/verification badge** using `first_seen_at`, `observed_at`, and a real liveness result. Do not use the current `verified_at` until it means something.
2. **Sponsorship filter** with tri-state semantics and an evidence link/source; do not present empty as “does not sponsor.”
3. **Manifest-backed public counts** on README/country pages; remove hand-written 5,230/15,821/15,748 claims.
4. **Saved searches/alerts** only after P0/P1; measure detection-to-publish and alert latency.
5. **MCP improvements**: manifest tool, artifact URL, cache invalidation, pagination; no new backend service unless measured.

**Acceptance:**

- Every badge/count is traceable to the manifest or a named source check.
- A user can distinguish “no sponsorship data” from “not a sponsor.”
- A feed age is visible and honest; no “15-minute” claim unless measured.
- Alerts do not fire on rows that are later removed by a failed publication.

**Risk:** product scope creep. Keep it to trust and access; do not build autofill/tracker/network.

**Estimate:** 3–5 days, after P1-4 and P1-3.

---

## P3 — Deferred tooling and product bets

Do not start these without the trigger.

| Item | Trigger to revisit | Why not now |
|---|---|---|
| Dagster | >20 independent scheduled asset groups; routine backfills; cross-run lineage needed | The failure is a publisher boundary, not orchestration. |
| Prefect | Same, and team prefers Python-native flows | Duplicate of Dagster decision; pick one, not both. |
| dbt | A warehouse becomes the system of record; multiple modeled consumers | No warehouse; duplicates pandas/DuckDB. |
| Great Expectations | Regulatory/warehouse data-quality requirement; multiple data owners | A 10–15 invariant Python contract covers current needs. |
| OpenTelemetry collector | Multiple deployed services; real on-call/paging; cross-service traces | Structured run events give most value without infrastructure. |
| Apify | One measured blocked source with acceptable terms and positive yield/cost | Paid, duplicate coverage; no current measured gap. |
| Git LFS | Never for the public feed | Adds client cost and does not remove the monolith. |
| Autofill/tracker/resume | A separate product decision with support/trust budget | Not a data-pipeline fix; Simplify/others own it. |
| Broad source expansion | After P1-3 and P2-1 scorecard | More rows without publication integrity is negative value. |

---

## Test matrix

Add these as fast, network-free tests. Use `tmp_path` and synthetic dataframes; do not call live sources.

| Test | Setup | Assertion |
|---|---|---|
| `test_identity_na_not_a_key` | concat frames with `NA` identity columns | rows are not collapsed to one |
| `test_light_publish_retains_heavy` | fresh jobhive + fresh heavy sidecars, `RUN_TIERS=["light"]` | both sources present in output |
| `test_publish_independent_of_run_tier` | same snapshots, different `RUN_TIERS` | identical canonical rows/hash |
| `test_precedence_wins` | same key in direct and jobhive | direct row wins |
| `test_expired_source_excluded` | expired heavy sidecar | heavy rows absent, manifest marks expired |
| `test_missing_jobhive_uses_lkg` | missing/expired jobhive, valid previous | no empty publish; status degraded/failed |
| `test_empty_union_does_not_publish` | no eligible frames | previous feed remains; error recorded |
| `test_count_reconciliation` | manifest rows ≠ feed rows | publish fails |
| `test_atomic_write_failure` | writer raises mid-temp | previous file byte-identical |
| `test_atomic_pair_write` | fail on second feed | neither final file replaced |
| `test_required_fields` | row missing `first_seen_at` | contract rejects row/feed |
| `test_future_date_quarantined` | date after observed | row excluded or marked, not silently published |
| `test_remote_boolean` | `"true"` string | normalized to boolean or rejected |
| `test_visa_unknown` | empty sponsorship | `unknown`, not `false` |
| `test_source_provenance` | direct ATS row | `source` is not overwritten to jobhive |
| `test_manifest_hash` | published artifact | manifest sha256 matches |
| `test_ci_runs_pytest` | workflow file check | test command exists; no publish without it |

Minimum for P0: identity, light-retention, precedence, empty union, atomic write, count reconciliation. The rest can follow with P1.

## Definition of done for the P0 sequence

All must be true at once:

1. A light run retains every fresh heavy/medium source; the feed is no longer 100% jobhive.
2. A crash during write leaves the previous feed intact.
3. `pages/feed-manifest.json` exists; its rows and hash match the feed.
4. `run_meta`, `health`, and the manifest agree or explicitly label their stage.
5. Tests run in CI and block a broken publish.
6. The known identity fix and country staging are in the deployed commit, not only the working tree.
7. The 100 MB ceiling is documented as the next constraint; no union is committed before P1-3.

## Metrics to watch

| Metric | Why | Initial target |
|---|---|---|
| `published_rows` | Effect of the fix | >13,748; union-consistent |
| `row_delta_pct` | Catches accidental loss/growth | investigate >10% unexplained |
| `source_fresh_ratio` | Snapshot health | 100% of expected critical sources |
| `feed_age_seconds` | Honest freshness | alert above 2× schedule interval |
| `first_seen_coverage` | Newness trust | 1.0 |
| `duplicate_url_rate` | Feed quality | 0 |
| `validation_failures` | Contract health | 0 successful publishes |
| `truncated_sources` | Coverage honesty | visible; 0 unexplained |
| `publish_duration_ms` | Operational headroom | trend, not a vanity target |
| `artifact_bytes` | Git/release planning | no feed commit above repo budget |

Do not publish a dashboard metric that the pipeline cannot compute from the manifest.

## Sequencing

**Day 1:** P0-1, P0-6 (identity + country staging, CI gate).  
**Day 2:** P0-2 (sidecars), start P0-3 (publisher boundary).  
**Day 3:** P0-3, P0-4 (retention + atomic writes).  
**Day 4:** P0-5 (manifest/reconciliation), verify the full P0 definition of done.  
**Week 2:** P1-1, P1-2, P1-4.  
**Week 3:** P1-3 consumer migration, then P2-1/P2-2.

This is a dependency order, not a commitment to four calendar days. The first useful checkpoint is after P0-3: the feed should stop being 100% jobhive.

## Considered but not in this backlog

- Replacing the source adapters.
- Adding another broad aggregator.
- Building a warehouse or a dbt project.
- Adding a general orchestrator.
- Adding a full data-quality platform.
- Rewriting the backend or its database.
- Application autofill, tracker, resume, or social features.
- Editing production data or committing the pending working-tree changes as part of this review.

## Related documents

- [review-2026-11-10.md](review-2026-11-10.md) — engineering and product review.
- [data-quality-audit-2026-11-10.md](data-quality-audit-2026-11-10.md) — measured feed audit.
- [architecture-roadmap-2026-11-10.md](architecture-roadmap-2026-11-10.md) — target design and migration phases.
- [snapshot-audit.md](snapshot-audit.md) — prior audit of the tier-scoped failure.
