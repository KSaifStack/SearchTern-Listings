# Live validation of the phase-2 changes (2026-11-07)

Every number below was produced on this machine against the real feed and real
sources. Where a measurement is invalidated by test-harness state, it says so
instead of being reported as a result.

## 1. Identity audit — found two real defects, both fixed

Audited the 29,282 published rows by re-deriving `(ats_type, ats_id)` from every
link with `identity.pairs`.

| Defect | Rows affected | Cause | Fix |
|---|---|---|---|
| `ashby/application` (164 companies) | 363 | `/application` is the shared apply route, read as an id | shape validation: Ashby/Lever ids must be UUIDs |
| `lever/apply` (61 companies) | 172 | `/apply` is a route | same |
| `icims/10863` (4 companies) | 4 | iCIMS/BambooHR/Workday number requisitions per employer | ids are stored tenant-scoped (`stifel/10863`) |
| `...-data-scientist-3-5-years-` | 3,530 | SmartRecruiters path = `<id>-<title slug>` | keep the leading numeric id only |

After the fix: **0 route ids, 0 ids spanning more than one employer.** 264 ids
still map to several company *names* (LLNL / Lawrence Livermore National
Laboratory / …Llnl, Talos / Talos Trading, Shield / Shield Ai). Those are one
employer under aliases — a company-normalization gap, not a false match.

Coverage with tenant scoping: **14,520 / 29,282 (49.6%)**, down 0.4 points from
52.0% because the rejected route ids were fake.

## 2. Full pipeline crawl — ran, but the A/B is invalid

`--tier all --ecr` completed in **450 s** with `SEARCHTERN_PROBE_MAX_BOARDS=200`
(previous heavy run: 719 s at 4,000 boards) and published a 57.8 MB
`searchtern.json`.

It is **not** a usable before/after: the scratch copy's `cache/seen.json` had
been written by three earlier partial runs, so the run reported 79,621
"closed" keys and published 14,364 rows. The tier-split runs proved the
mechanism: a light-tier-only run published 31,586 rows in 262 s, a medium-tier
run then published 14,381 because the markdown merge is light-tier-gated. A
clean A/B needs one uninterrupted run against the production cache, which lives
in CI (`Listings/cache/` is a 911 MB gitignored cache).

## 3. Publication defect found by the crawl

`readme_generation._build_json_records` deduped every record on
`canonical_url(link)` without checking that the key existed. **7,920 markdown
rows have an empty canonical form and collapsed onto one row.** Fixed: an empty
key is never a duplicate. Covered by
`test_integration.test_links_with_no_canonical_form_are_not_all_duplicates`.

This alone explains a large part of the markdown attrition seen in the run
(40,308 markdown rows → 19,064 rows after the builder, even alone).

## 4. Enrichment — 20 real rows, live network

Rows taken from the published feed with the description cleared, `workers=4`:

| Metric | Value |
|---|---|
| Planned / fetched / filled | 20 / 20 / 10 (50%) |
| Failures | 0 |
| Wall time | 5.49 s → **274 ms/row at 4 workers** |
| Description length | mean 526 chars, median 300 |

The 10 misses are pages whose meta description is absent or shorter than the
40-char floor (e.g. `careers.molsoncoors.com` job pages). A miss leaves the row
for the synthesizer, which is the intended behaviour.

## 5. Board parking — real state, activates on the third visit

- Production probe state: 6,683 boards, **6,458 still legacy floats** with no
  `zero` counter → nothing is parkable until the new `record()` writes them.
- After one crawl: 2,450 boards carry counters, 230 yielded zero rows, **0 have
  a 3-visit streak** — parking starts doing work on the third consecutive zero
  visit, exactly as designed.
- Recovery is bounded: `skip_boards` retries a parked board once
  `PROBATION_HOURS` (7 days) pass. Verified by unit test and by a simulated
  three-visit streak.

## 6. RSS — 4 feeds configured, 3 alive, nothing publishable

| Feed | Result |
|---|---|
| `weworkremotely.com/remote-jobs.rss` | 100 rows |
| `remotive.com/remote-jobs/feed` | 13 rows |
| `himalayas.app/jobs/rss` | 20 rows |
| `remoteok.com/remote-jobs.rss` | HTTP 410 (dead feed) |

133 rows in 3.18 s, all with a date and a description. 2 survive the
intern/new-grad classifier. **0 publish**, because the publisher's `clean_link`
rejects aggregator-hosted links — correct policy: republishing someone else's
board URL is not a direct posting. Value from RSS therefore requires
employer-run feeds; the adapter is wired (`SEARCHTERN_RSS_URLS`, empty by
default) and tested.

Two bugs were found and fixed while doing this: `rss_source` was never imported
into the pipeline, and feed titles keep the employer (`Bybit: Animator Intern`)
so the aggregator host was being published as the company.

## 7. Timestamps

`last_seen_at` and `verified_at` are present on 100% of rows; `first_seen_at` is
only as complete as the seen log, which was contaminated in the scratch copy
(32.1%). Cross-run stability is what the design guarantees: `first_seen_at` is
read from the seen log and never overwritten, and `verified_at` is only stamped
for rows whose source bucket was re-fetched. Covered by
`test_published_record_keeps_identity_and_timestamps` and
`test_unfetched_source_never_claims_verification`; a clean multi-run check is
still owed against production state.

## 8. Consumers

| Consumer | Result |
|---|---|
| `searchtern_mcp._search_jobs` / `_stats` | works on the new schema (400-row sample, 1 hit for a company query, full stats) |
| `pages/feed.xml` | parses, 200 items |
| `write_listings_json` schema | superset: adds `ats_type`, `ats_id`, `first_seen_at`, `last_seen_at`, `verified_at`; no key removed |
| Test suite | 98 → 99 tests pass; ruff clean on touched files |

## Gaps left open

1. Clean A/B crawl against the production cache (needs CI state or a full
   cache copy).
2. Company-name normalization: 264 ids map to 2–3 aliases of one employer.
3. Markdown canonicalisation: 7,920 rows have no canonical URL; they are now
   kept but still cannot be URL-deduped.
4. RSS has no employer-hosted feed configured; aggregator feeds cannot publish.
5. Parking needs three consecutive runs with the new `record()` before it
   retires anything.
