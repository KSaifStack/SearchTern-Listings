# SearchTern-Listings — data-quality audit

**Date:** 2026-11-10  
**Checkout:** `2601eb97`  
**Feed measured:** `pages/searchtern.json`, observed `2026-11-09T07:07:34Z`  
**Method:** full-file load of all 13,748 records; no sampling. Counts were computed read-only from the published JSON and adjacent artifacts.

## Executive summary

The current feed is internally valid JSON and has strong core fields: every row has a company, role, location, date, link, category, job type, `observed_at`, and `last_seen_at`. It is not, however, a trustworthy product feed yet:

- **It is 100% `source=jobhive`.** The feed is not all-source; it is the jobhive slice that happened to be published.
- **`first_seen_at` is missing on 66.8% of rows.** The feed cannot reliably say what is new.
- **`is_remote` is null on 60.7% of rows** and the field is a string (`"true"`/`"false"`), not a boolean.
- **`country_iso` is missing on 17.2% of rows.**
- **`direct_apply`, `employment_type`, and `seniority` are absent/null on every row.**
- **`visa_sponsor` is a string with only `"true"` or empty.** “No” and “unknown” are the same value.
- **Count artifacts disagree by more than 3×.** `run_meta.total=9,337`; `health.run.published.searchtern=31,434`; actual feed `13,748`.
- **The public documentation is stale.** `README.md` says 5,230, `countries/US.md` says 15,821 as of 2026-09-24, and `docs/marketing/claims.md` says US 15,748. None is the current feed.

This is a data-contract problem as much as a volume problem. Adding rows without fixing these fields will make the product less trustworthy, not more useful.

## 1. Feed composition

| Artifact | Rows | Bytes | Notes |
|---|---:|---:|---|
| `pages/searchtern.json` | 13,748 | 55,838,379 | Main feed; 4,061.6 bytes/row |
| `pages/listings.json` | 4,869 | 22,346,172 | Tech/CS subset |
| `pages/priority.json` | 83 | 509,524 | Fast lane; timestamp 2026-11-08 21:30, older than main feed |
| `pages/feed.xml` | 200 items | 88,442 | RSS projection, not a full feed |

### Source vs underlying ATS

All 13,748 rows carry `source=jobhive`. That does **not** mean one ATS: `ats_type` is populated with the upstream ATS. The feed is jobhive-sourced, with a diverse underlying ATS mix.

| `ats_type` | Rows |
|---|---:|
| smartrecruiters | 2,904 |
| icims | 2,777 |
| greenhouse | 2,060 |
| phenom | 1,287 |
| cornerstone | 764 |
| lever | 649 |
| ashby | 519 |
| jazzhr | 458 |
| oracle | 350 |
| eightfold | 330 |
| teamtailor | 300 |
| breezy | 183 |
| recruitee | 182 |
| dayforce | 178 |
| darwinbox | 153 |
| other | 654 |

`ats_id` is present on 13,747 rows (99.99%); `ats` is `None` on every row and `ats_name` is not a usable published field. The identity key should be documented as `(source, ats_type, ats_id)` or `(ats_type, ats_id)`, not `ats`, and the schema should stop carrying a permanently empty `ats` field.

**Why this matters:** the “100% jobhive” headline is often read as “only one source.” The more precise statement is “only the jobhive aggregation path was published, although those rows came from many ATSs.” The fix must preserve that distinction; otherwise the next audit will again confuse aggregator provenance with ATS coverage.

## 2. Field coverage

Measured on all 13,748 rows. “Populated” means not `None`, empty string, empty list, or empty object.

| Field | Populated | Rate | Audit note |
|---|---:|---:|---|
| `company`, `role`, `location`, `date`, `link`, `category`, `job_type`, `source` | 13,748 | 100.0% | Core row contract holds |
| `observed_at`, `verified_at`, `last_seen_at` | 13,748 | 100.0% | All three are the same run timestamp |
| `ats_id` | 13,747 | 100.0% | One row lacks an ATS id |
| `description` | 13,698 | 99.6% | 50 empty |
| `country_iso` | 11,384 | 82.8% | 2,364 missing; 32 distinct codes |
| `first_seen_at` | 4,566 | 33.2% | **9,182 missing**; only usable on one-third of rows |
| `salary_min` | 746 | 5.4% | `salary_max` 741; either bound 753 (5.5%) |
| `is_remote` | — | — | `"true"` 841 (6.1%), `"false"` 4,565 (33.2%), `null` 8,342 (60.7%) |
| `visa_sponsor` | 1,374 | 10.0% | String `"true"` only; 12,374 empty; no `false` or `unknown` |
| `direct_apply` | 0 | 0.0% | Absent/null on every row |
| `employment_type` | 0 | 0.0% | Absent/null on every row |
| `seniority` | 0 | 0.0% | Absent/null on every row |
| `h1b_role` | 0 | 0.0% | Not present in the published schema |
| `ats` | 0 | 0.0% | `None` on every row; `ats_type` is the usable field |

### What each gap breaks

- **`first_seen_at` missing:** “new since your last visit,” alerts, and any `new` metric are unreliable. A row can be in the feed and never have a defensible first-seen time.
- **`is_remote` mostly null:** a remote filter silently excludes 60.7% of rows, even though those rows may be remote. The string representation also makes consumers guess whether the value is JSON boolean, Python boolean, or text.
- **`country_iso` missing:** country pages, geo search, and “US-only” counts are incomplete. The current US total is 6,526 (47.5%), not the 15,748 claimed in marketing.
- **`visa_sponsor` string:** a consumer cannot distinguish “this employer has an H-1B signal,” “this employer does not sponsor,” and “we have no data.” The current field collapses the last two.
- **`direct_apply` zero:** the feed cannot tell a direct employer application link from an aggregator redirect. That is a trust and quality signal, not a cosmetic field.
- **`employment_type`/`seniority` zero:** consumers cannot filter internships vs new-grad by structured data; they must trust `job_type` or parse titles.
- **`verified_at` equals `observed_at`:** the field does not mean “the apply link was checked.” It is a run stamp. Do not expose it as verification.

## 3. Freshness and date quality

| Measure | Result |
|---|---|
| `date` range | 2026-09-09 to 2026-12-10 |
| Median age at review | about 24 days |
| p90 age | about 55 days |
| Maximum age | 62 days |
| Minimum (negative age) | −30 days |
| Future-dated rows | 1 (`2026-12-10`) |
| `observed_at` range | single value, `2026-11-09T07:07:34Z` |
| `first_seen_at` range | 2026-09-24 to 2026-11-08 |
| Feed age at review | about 31 hours |

The feed is within the broad freshness window but not “fresh” in the product sense. A 31-hour-old feed cannot support a 15-minute or hourly freshness claim. One future-dated row is small in volume but invalid as a posting date unless the source genuinely schedules publication; it should be quarantined or explained, not silently published.

`first_seen_at` is not simply “missing for new rows”: 66.8% of all rows lack it. Whatever the writer/index cause, the published result cannot support a newness contract. This should be treated as a contract failure, not a warning.

## 4. Identity and duplication

| Check | Result |
|---|---|
| Rows with `ats_id` | 13,747 / 13,748 (99.99%) |
| Rows with `source=jobhive` | 13,748 / 13,748 (100%) |
| Duplicate canonical apply URLs | 0 (enforced by the writer) |
| Duplicate `(company, role, location)` keys | 13 keys, 13 extra rows |
| Duplicate `(company, role, location, link)` keys | Not separately measured; should be zero by URL rule |

The absence of duplicate canonical URLs is good; `_build_json_records` dedupes on the canonicalized link (`Listings/readme_generation.py:90-118`). The 13 duplicate role/location keys are not automatically wrong: distinct requisitions can share a company/title/location but have different links. They should be inspected, not blindly collapsed. The real identity risk is the one missing `ats_id` and any future cross-source merge that relies on title/location rather than `(ats_type, ats_id)`.

**Contract recommendation:** require either a stable ATS identity or an explicit `identity_unknown` reason. Do not let an empty id silently become a dedupe key, which is the class of bug already fixed in the working tree.

## 5. Counts, health, and closure

| Artifact | Field | Value |
|---|---|---:|
| `pages/run_meta.json` | `total` | 9,337 |
| `pages/run_meta.json` | `per_source.jobhive` | 9,337 |
| `pages/health.json` | `totals.valid` | 115,322 |
| `pages/health.json` | `totals.discovered` | 499,497 |
| `pages/health.json` | `totals.new` | 2 |
| `pages/health.json` | `totals.failed` | 4 |
| `pages/health.json` | `totals.closed` | 31,375 |
| `pages/health.json` | `run.published.searchtern` | 31,434 |
| `pages/health.json` | `run.published.listings` | 9,337 |
| Actual | `len(pages/searchtern.json)` | 13,748 |
| `pages/source_stats.json` | `totals.sources` | 89 |
| `pages/source_stats.json` | `totals.discovered` | 499,497 |
| `pages/source_stats.json` | `totals.valid` | 115,322 |
| `pages/source_stats.json` | `totals.new` | 2 |
| `pages/source_stats.json` | `totals.failed` | 4 |

The numbers are not all intended to be identical: `discovered` is pre-filter, `valid` is post-normalization, `run_meta.total` is the jobhive query result, and `published` should be the actual output. The defect is that none of the artifacts defines or reconciles those stages. A consumer sees three “totals” and no manifest.

`pages/health.json` is `healthy=false` with 9 alerts:

- `icims:Kimley-Horn` — truncated
- `icims:Novelis` — truncated
- `icims:Peraton` — truncated
- `oracle:WSP` — truncated
- `priority` — quiet for 55.2 hours
- plus the corresponding degraded alerts for the four truncated sources

The four truncations are real source-quality signals. They are not necessarily feed data loss, because the rows may still arrive from jobhive; but they mean the direct-source coverage is incomplete and should not be reported as complete.

Closure reports **31,375 closed** and **0 dead links**, with `stale_checked=0`. That combination is not proof that no link is dead; it means no stale-link check ran. Do not market “0 dead links” from this artifact. Closure from a successfully completed board is a different signal from an HTTP liveness check, and the two must stay separate.

## 6. Public documentation is inconsistent

| Surface | Claim | Current measured reality |
|---|---:|---:|
| `README.md:10,17` | 5,230 internships/new-grad roles | 13,748 total feed |
| `countries/US.md:3` | 15,821 listings as of 2026-09-24 | 13,748 total; 6,526 US rows |
| `docs/marketing/claims.md:34` | US 15,748 | 6,526 US rows |
| `docs/marketing/claims.md:75` | 49 sources across 72 countries | `health.json` reports 89 source records; 32 country codes in feed |

Two things are true at once: the feed is much larger than the README says, and much smaller than the marketing pages say. Both erode trust. Country pages are generated separately and the current HEAD workflows do not stage `countries/`; the working tree contains the staging fix, but it is not part of the measured deployed state. The durable fix is to generate every count from one manifest and fail the build when a page header disagrees with the feed.

## 7. Data contract

The feed should not be considered publishable unless these invariants hold. This is intentionally smaller than a full Great Expectations suite.

### Required fields

`source`, `ats_type`, `ats_id` (or explicit `identity_unknown`), `company`, `role`, `location`, `link`, `date`, `job_type`, `observed_at`, `first_seen_at`.

### Invariants

1. `link` starts with `https://` or `http://` and canonicalizes to a non-empty, unique key.
2. `company`, `role`, and `location` are non-empty after cleaning.
3. `date` parses to a timezone-aware timestamp and is not in the future, or the row is quarantined with a reason.
4. `first_seen_at <= observed_at`; both are timezone-aware.
5. `source` is an allow-listed provenance label; it is never inferred after the fact.
6. `ats_type` is the original ATS; `source` is the aggregation path. They are different concepts.
7. `is_remote` is JSON boolean or explicit `null`; the string `"true"` is not a boolean contract.
8. `visa_sponsor` is one of `true`, `false`, `unknown`; it carries an evidence/source field if exposed.
9. `country_iso` is a two-letter code or explicit `null` with an `unknown_reason`; absence is counted, not hidden.
10. `verified_at` is set only by an actual successful liveness check; a run stamp is not verification.
11. `manifest.published.rows == len(feed)` and per-source counts reconcile to the union before final filters.
12. Every expected source has a snapshot sidecar with `rows`, `status`, `fetched_at`, `ttl_hours`, and `fresh`.
13. No source disappears from publication without a reason: `expired`, `rejected`, `failed`, or `not_selected`.
14. A failed validation never replaces the previous feed; the last-known-good artifact remains addressable.

### Current failures against the contract

| Severity | Failure | Evidence |
|---|---|---|
| Critical | One publication path only (`jobhive`); all-source union not wired | 13,748/13,748 `source=jobhive`; `generate_listings.py:2197`; `snapshots.merge` has no production caller |
| Critical | Counts do not reconcile | 9,337 vs 31,434 vs 13,748 |
| Critical | No durable snapshot ledger in the published checkout | No `pages/snapshots.json`; no `Listings/cache/snapshots/` |
| Critical | Non-atomic feed writes | `readme_generation.py:121-145` opens the final paths directly |
| Critical | `first_seen_at` missing on 66.8% | 9,182 null |
| High | Remote filter unreliable | 8,342 null; 841 true; 4,565 false |
| High | Country incomplete | 2,364 missing (17.2%) |
| High | Sponsorship unknown vs no is collapsed | 12,374 empty; 1,374 `"true"`; no `false` |
| High | Four truncated sources; priority quiet 55.2h | `health.json` alerts |
| High | Tests do not gate workflows | No `pytest` step in `.github/workflows/*.yml` |
| Medium | Three important fields are permanently empty | `direct_apply`, `employment_type`, `seniority` |
| Medium | Salary coverage is low | 746 `salary_min` (5.4%); 753 either bound |
| Medium | One future-dated row | `2026-12-10` |
| Medium | 13 duplicate company/role/location keys | 13 extra rows; inspect, do not auto-collapse |
| Low | `ats` is a dead field; `ats_type` is the real one | `ats=None` on all rows; `ats_type` populated |
| Low | Public counts are stale | README/countries/marketing |

## 8. Target thresholds

These are proposed gates, not current results. They are deliberately modest and measurable from the feed and manifest.

| Metric | Target | Current |
|---|---:|---:|
| Required-field coverage | 100% | Core fields 100%; `first_seen_at` 33.2% |
| `first_seen_at` coverage | 100% | 33.2% |
| Duplicate canonical URL rate | 0% | 0% |
| Future-dated rows | 0, or quarantined with reason | 1 |
| `date` age p90 | ≤ 60 days | about 55 days |
| `country_iso` coverage | ≥ 95%, with explicit unknown reason | 82.8% |
| `is_remote` | 100% valid boolean/null; null rate tracked | 60.7% null |
| `visa_sponsor` | 100% enum (`true`/`false`/`unknown`) | only `"true"`/empty |
| `source` provenance | 100% populated; direct-source labels preserved | 100% jobhive |
| Count reconciliation | 0 contradictions across manifest/health/run_meta | 3 conflicting totals |
| Source failures | 100% surfaced with `failed`/`truncated`/`expired` reason | 4 truncations, 9 alerts |
| Feed age | visible in manifest; alert above 2× schedule interval | 31 hours, no manifest |
| Test gate | pytest runs on every workflow | absent |

## 9. Fix order

1. **Publish from eligible snapshots, not `RUN_TIERS`.** This is the known failure and the only way row count becomes meaningful.
2. **Write the manifest and reconcile counts.** Do not add fields until the feed knows what it published.
3. **Fix `first_seen_at` and provenance.** Newness and source are core trust fields.
4. **Make booleans and enums explicit.** `is_remote`, `visa_sponsor`, and country unknown need real states.
5. **Quarantine future dates and preserve closure/liveness distinctions.**
6. **Generate public counts from the manifest.** Retire hand-maintained or independently generated claims.
7. **Decide whether the zero-coverage fields are populated or removed.** A permanently empty field is a promise the product does not keep.

## 10. Method and limitations

- Read-only: `python3` loaded `pages/searchtern.json`, `listings.json`, `priority.json`, `run_meta.json`, `health.json`, and `source_stats.json`.
- Field counts use a populated check of `None`, `""`, `[]`, and `{}`; `False` is treated as a value, and the audit separately reports the string `"true"`/`"false"` values.
- Date ages use the review date; the feed was observed 2026-11-09T07:07Z, so ages move by one day depending on the clock used.
- Duplicate role/location keys are exact lowercase string comparisons, not fuzzy company aliases.
- No backend/PostgreSQL data was inspected; this audit describes the published artifacts, not what the site stores or serves.
- No source was fetched; a source could have changed since 2026-11-09.
- The working tree contains pending identity/workflow changes; this audit measures the committed feed at `2601eb97`, not those pending changes.

## Related documents

- [review-2026-11-10.md](review-2026-11-10.md) — engineering and product review.
- [architecture-roadmap-2026-11-10.md](architecture-roadmap-2026-11-10.md) — target publication design.
- [implementation-backlog-2026-11-10.md](implementation-backlog-2026-11-10.md) — ordered fixes and acceptance tests.
- [snapshot-audit.md](snapshot-audit.md) — prior audit that established the tier-scoped failure and snapshot plan.
