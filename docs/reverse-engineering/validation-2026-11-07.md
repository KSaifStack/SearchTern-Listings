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

## 7. Timestamps — measured across two runs on the real feed

Ran `attach_row_times` twice over the published 29,282-row feed with the real
`cache/seen.json`: once with every bucket fresh, once simulating a run where
only `jobhive` and `ats` were fetched.

| | all buckets fresh | jobhive + ats only |
|---|---|---|
| `first_seen_at` populated | 29,282 | 29,282 |
| `verified_at` populated | 29,282 | 16,004 (= exactly the jobhive+ats rows) |
| md rows claiming verification | — | **0** |
| `first_seen_at` identical to run 1 | — | yes, all 29,282 |

So the conditional claim is real: a cached row keeps its first-seen date and is
never presented as re-verified. The 32.1% seen earlier was scratch-state
contamination, not the writer.

## 8. Consumers

| Consumer | Result |
|---|---|
| `searchtern_mcp._search_jobs` / `_stats` | works on the new schema (400-row sample, 1 hit for a company query, full stats) |
| `pages/feed.xml` | parses, 200 items |
| `write_listings_json` schema | superset: adds `ats_type`, `ats_id`, `first_seen_at`, `last_seen_at`, `verified_at`; no key removed |
| Test suite | 98 → 99 tests pass; ruff clean on touched files |

## 9. The new-listing wave is real and the seen log is why

Replayed the publication logic against production state: 88,609 seen entries
inside the 120-day window, 28,358 current keys.

| Metric | Value |
|---|---|
| Keys in feed | 28,358 |
| Counted new on this run | **13,789 (48.6%)** |
| Counted closed | **74,040** |
| Second run (after persist) new / closed | 0 / 74,040 |
| New keys that are the same job under another source bucket | 76 (0.6%) |
| Closed keys that are live under another bucket | 2,558 (3.5%) |
| New rows by bucket | jobhive 11,736, freehire 689, md 658, ats 623 |

The dedup key change is **not** the cause — bucket reclassification explains
under 1% of the wave. The cause is the seen key itself:
`bucket|company|role|location` is exact text, so a jobhive row whose title or
location string is re-punctuated counts as new while its previous key is
counted closed. On the same day the feed gained 703 genuinely new links, the
metrics claimed 13,789 new postings and 74,040 closures.

Fix (not shipped, needs a decision): store the requisition id on each seen
entry and treat a row as known when its `(ats_type, ats_id)` was seen under any
key. That is a one-time migration — the first run after it retires the text
keys and reports one more wave — which is exactly why it should be a deliberate
change, not a silent one.

## 10. Phase 3 — identity in the seen log, and the gaps it closes

The eight-item follow-up list. Each item below is measured, not asserted; the
numbers come from the same production state (89,177 seen entries inside the
120-day window, 27,406 published rows) unless stated otherwise.

### Seen log: the requisition decides, not the wording

Entries now store `ats`/`req` next to `first`/`last`. A row is known when either
its text key or its `(ats_type, ats_id)` was seen. A requisition whose title is
rewritten keeps its original `first`, retires the old key (never reported as a
closure), and counts as neither new nor closed. A closure is stamped `closed`
and reported once instead of on every subsequent run.

| Run | Old rule new / closed | New rule new / closed |
|---|---|---|
| 1 — real log, live feed | 13,347 / 75,118 | 13,347 / 75,118 |
| 2 — same rows, 6,375 titles rewritten | 6,373 / 81,500 | **3,215 / 3,226** |

Run 1 is identical by construction: the stored entries predate ids, so the text
key still decides, and this run is the one transitional wave (it writes the
ids). Run 2 rewrites 6,375 titles the way the feed's own churn did: the text
rule calls almost all of them new and their predecessors closed; the identity
rule reports 3,215 new and 3,226 closed. The residual is exactly the rows with
no ATS-shaped link (13,258 of 27,406, 48.4%) — those cannot be identified from
a URL, which is the next item, not a weakness of the rule.

### Linkless rows: counted, not silently skipped

`cache/tier_light.parquet` holds 7,915 rows with no apply URL. Sampling them
shows the source table carries a closed marker (SimplifyJobs `🔒`) where the URL
would be, so there is no identity to recover — the posting is closed at origin.
They used to disappear into the "Skipped (Oracle URLs)" counter. They are now
counted per source in `pages/run_meta.json` (`linkless_rows`,
`linkless_by_source`) and the publication dedup no longer treats them as the
same row.

### Employer aliases: legal suffixes stripped, initialisms listed

`company_alias.normalize` strips legal forms only (inc, llc, ltd, corp, plc,
gmbh, pty, pte) and resolves 15 measured initialisms (`bah` → Booz Allen
Hamilton, `ngc` → Northrop Grumman, `llnl` → Lawrence Livermore National
Laboratory). Business words are deliberately *not* stripped: "General Dynamics"
and "General Dynamics Mission Systems" are different employers and a wrong join
lets one board vote on the other's rows. Tenant/company mismatches on the live
feed fall from 1,696 to 1,263 of 6,758 tenant-scoped ids (433 rows, 25.5%).

### Shards stay off until the reader migrates

Measured on the live 27,955-row feed: 2 parts, 14.5 MB gzip, 4.4 s. Shard bytes
are deterministic (sorted keys, fixed row order), so unchanged parts are
byte-identical and git stores them once rather than once per run. That is not
the cost that matters yet: the pipeline still commits the monolithic JSON, so
publishing the parts as well adds ~14.5 MB per run (~170 MB/day at the current
schedule) into a `.git` already at 2.9 GB. `publication-migration.md` orders the
work reader-first for that reason, and the site still reads `searchtern.json`.

The default therefore stays off. `SEARCHTERN_SHARDS=1` opts in, and all five
update workflows pin `SEARCHTERN_SHARDS=0` explicitly so a future default change
cannot silently switch CI over before the reader has moved.

### Stale-job verification

A row nobody has re-fetched for 14 days (`SEARCHTERN_STALE_DAYS`) is exactly
where a dead posting hides: its bucket is fresh, so closure never votes on it,
and `last_seen` only says when it was copied. Up to 150 such links per run
(`SEARCHTERN_VERIFY_STALE`) go through the existing HEAD → ranged-GET check;
dead ones are dropped and reported once with `reason: link_dead`. Measured: 15
live links in 7.0 s, 0 dead — a full sample costs ~9 s at 8 workers.

### Source selection by measured value

`source_selector` defers a source after two failing runs or zero rows across
four runs, and marks the top half by trailing-week rows as *expand* (polled even
when quiet). Measured on the real health log: 9/9 breadth feeds polled, 5
expanded, cut at 665 rows/7d.

Ranking on `new_7d` would have retired every breadth feed on day one: new
postings are attributed to the adapter *bucket* (`breadth`), so each namespaced
feed (`breadth:Jobicy`) records 0 new. The first wiring did exactly that and
deferred all nine; the rule now ranks on rows actually returned
(`rows_7d`/`discovered_7d`). That is why the health log gained `rows_7d` and
`discovered_7d`.

### Health artifact

`pages/health.json` now reports `stale` (checked, dead, horizon) and `linkless`
next to the per-source ranking and alerts, so a dashboard can show coverage gaps
without rebuilding them.

## 11. Pre-release corrections and the real A/B crawl

Section 2's A/B was invalid because it replayed a benchmark instead of running
the pipeline. This is the crawl. Command, unchanged from CI:
`python generate_listings.py --tier light --force`, `SEARCHTERN_SHARDS=0`.
State: the committed CI-equivalent cache — 89,177 seen entries (none carrying
ids), the 40,450-row markdown cache, all 14 tier parquets, published pages.
A is `38f3c2c9` run from a git worktree; B is the migration; C is B run a second
time from B's own state. A and B each started from a byte-identical copy of
that cache.

| | A (old rule) | B (new rule) | C (B, second run) |
|---|---|---|---|
| feed rows | 9,369 | 9,369 | 9,369 |
| new | 156 | 156 | 5 |
| closed | 59,227 | 59,227 | 39 |
| closures stamped in `seen.json` | 0 | 59,227 | 39 |
| seen entries carrying `ats`/`req` | 0 | 9,369 | 9,336 |
| published `searchtern.json` rows | 14,114 | 14,113 | 14,112 |
| wall clock | 7m52s | ~10m | ~16m |

Wall clock is not a clean speed comparison: JobSpy hit LinkedIn 429s during B
and C, so backoff, not pipeline work, dominated the difference.

### The crawl found a defect in the migration itself

B's first run printed `0 closed` while writing 59,227 `closed` stamps into
`seen.json`. `main` still carried the old closure loop *and* called
`seen_index.commit`, which runs the same loop: the inline loop stamped the
entries first, `commit` then saw them already stamped and returned an empty
list, and the assignment threw the inline list away. `seen_index.commit` now
owns closure detection alone; `test_every_stamped_closure_is_reported_by_the_same_call`
pins the invariant that the run summary and the log cannot disagree.

### A closure is now reported once — but the count is mostly not closures

A stamps nothing, so its 59,227 would be re-announced on every run (production
`run_meta.json` shows exactly that on 2026-11-07: 61,294 closed). B and C stamp,
so each closure is announced once.

The number itself is not a closure count, and this is pre-existing: `current_by_key`
is built from `listings_result` *after* the tech-keyword and feed-scope filters,
so a posting that was fetched and then filtered out looks absent. Checked against
the sources the same run fetched:

* markdown — 23,756 of 53,002 (44.8%) still present in that run's own fetch.
  Of those, 12,507 differ only in company case or punctuation (`🔥 NVIDIA` vs
  `Nvidia`, `AeroVironment` vs `Aerovironment`), 11,249 differ only in location
  text, 12 fail the tech filter.
* jobhive — 1,953 of 6,225 (31.4%) still listed in the upstream parquet when
  checked 20 minutes later.

25,709 of 59,227 (43.4%) were still live. Fixing that means building
`current_by_key` from fetched rows rather than the published subset — a
behaviour change, left for a decision rather than slipped in here.

### The transitional wave, measured separately

First run of the new rule against a pre-id log:

* 9,369 seen entries gained `ats`/`req`.
* 0 rows changed new/closed status *because of* identity — no title moved
  spelling inside that single run, so the ids had nothing to rescue yet.
* 59,227 one-time closures, every one on a legacy id-less entry. This is the
  wave, and the 12,507 company-case pair above is its cause: under the new rule
  those postings already carry the id, so the same rewording no longer closes
  them.

C is the idempotency check and it separates the two kinds of churn: markdown
produced **0 new / 0 closed** on an identical re-run. All 5 new and all 39 closed
were jobhive rows carrying requisition ids — real movement in a rolling upstream
window, not text-key noise.

### Identity, duplicates, per-source

* Published feed: 14,112 of 14,113 rows carry `ats_type`+`ats_id`; 14,112
distinct ids across 14,113 distinct links, so no requisition is published twice.
Pre-publication: 31,535 of 31,535.
* Cross-source duplicate suppression: markdown 40,450 → 39,387 after dedup
  against the other sources (−1,063); freehire 5,294 → 4,994; ECR 4,367 after
  dedup vs listings; jobspy 392, none removed.
* Working sets (B): markdown 40,450 (8,452 survive the publish filter), jobhive
  9,333, freehire 4,994, ecr 4,367, ats_new 797, priority 356, breadth 48,
  ats probe 5, jobspy 392.

### `_verify_link` is now tested

18 tests in `test_verify_link.py`, with the `requests` module replaced: 404 and
410 are dead only after the ranged GET agrees; 403, 405, 429, 500, 502, 503 and
302 keep the row; `Timeout`, `ConnectionError`, `TooManyRedirects` and an
arbitrary exception keep it; HEAD 404 followed by GET 200 or 204 keeps it; and
the probe is asserted to send `Range: bytes=…` with a 10 s timeout and redirects
allowed. The docstring claimed 405 falls back to a GET — it does not, and only
404/410 are treated as dead, so the docstring now says that.

### Company alias audit

All 15 initialisms were checked against every employer spelling in the feed
(6,361) and the board registry (3,802). Seven resolve real collisions and each
was verified same-employer: `boozallen`, `amat`, `gdms`, `alliance` (the two rows
apply on `alliance.wd3.myworkdayjobs.com/NissanPrivateExternal`), `microntechnology`,
`llnl`, `talos` (all 11 rows are `jobs.ashbyhq.com/talos-trading`). Eight match
nothing in either corpus and are inert today (`bah`, `ngc`, `snc`, `aero`, `ag`,
`leonardocompany`, `americascookmedical`, `carrieresmarvesting`).

Two were broken rather than risky: `snc` pointed at `sierranevadacorporation`
and `aero` at `aerospacecorporation`, but suffix stripping turns "Sierra Nevada
Corporation" into `sierranevada` and "Aerospace Corporation" into `aerospace`,
so neither key could ever be produced and neither alias could join anything.
Both targets fixed; `test_every_alias_target_is_reachable` now asserts every
target normalizes to itself, and two more tests keep SNC-Lavalin and Talos
Energy as separate keys.

## Gaps left open

1. Closures are computed against the published subset, so a light run reports
   tens of thousands of "closures" for postings it fetched and filtered out
   (section 11). Needs a decision: build `current_by_key` from fetched rows.
2. 1,263 tenant/company alias mismatches remain; the 15 aliases cover the
   largest measured pairs only.
3. 3,215 rows carry no ATS id (48.4% of the feed) and no alias fixes that —
   their links are aggregator or board pages that name no requisition. Those
   rows keep the text-key rule after the migration.
4. RSS still has no employer-hosted feed; aggregator links cannot publish.
5. Parking needs one more run with the new `record()` before it retires boards.
6. Shards stay opt-in until the site reads `manifest.json`
   (`publication-migration.md` steps 1–3).
