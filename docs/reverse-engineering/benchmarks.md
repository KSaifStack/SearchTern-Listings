# Benchmark notes and reproducibility

## Observations, not a comparative benchmark

Environment: headless Chrome 156, Linux x86-64, same host, 2026-11-02, unauthenticated. Sites had different query semantics, ordering, network paths and cache history. No CPU/network throttling or independent trials were controlled. Do not compare these observations as if they were cold/warm performance tests.

| Target | Query/access | Observed metric | Not measured |
|---|---|---|---|
| Simplify | `software engineer intern`, public `/jobs` search | One earlier document navigation: responseStart 84.5 ms, DOMContentLoaded 1,045.1 ms, load 3,254.7 ms, document transferSize 14,130 bytes. Later resource timing entries for two `multi_search` requests: 170 and 256 ms; 30 `h3` elements in DOM, not a validated result count. | TTF first query result, total query latency, page two, result total, response payload bytes, cache state. The navigation was not to the final query. |
| SearchTern | Same string entered at `/jobs`; 15 visible table rows | One document navigation: responseStart 150 ms, DOMContentLoaded 178 ms, load 178 ms, transferSize 2,225 bytes. One `/recent` resource duration 64 ms; cross-origin resource transfer and decoded size both 0, not a measured zero-byte payload. | TTF first query result, total query time, page two, total results, response payload bytes, cache state. `/recent` is not demonstrated to be the search API. |
| LinkedIn | Same keyword; sign-in overlay | Title showed 7,000+ roles. | Search/page latency, reliable total, pagination, API or payload. |
| Indeed | Same keyword | Request blocked. | All search metrics. |
| Handshake | Computer-science internship category, not identical query | Public page opened. | Comparable query and all search metrics. |
| ZipRecruiter | Software-engineer-intern category | Human verification challenge. | All search metrics. |

SearchTern's own search path was measured separately on 2026-11-06: payload
size, parse, query p50/p95, the fixed 200 ms debounce, LCP and feed cadence are
in [search-speed.md](search-speed.md). Those figures describe SearchTern only
and do not make the cross-platform comparison below available.

The requested all-platform identical-query, first-result, load, API, payload, result-count, pagination and cold/warm comparisons are **not available**. Respect blocks and sign-in requirements; arrange authorized accounts/partner access or accept gaps.

Previous isolated ingestion run (not a search benchmark): `--tier priority --workers 8`, 216.26 s end-to-end; 56/56 priority board fetches reported successful, 345 classified priority rows, 27,648 exported search-feed rows, 34 source labels. It ran with copied historical caches, changed dedup rules and live upstream data; its `new=12577` is not an independently verified net-new count. It did not measure detection-to-searchable latency or source-level duplicate/failure rates. No matching baseline was captured; no speedup can be claimed.

## Procedure for future controlled trials

1. Obtain access and approval for each site's normal UI and SearchTern staging. Use the same query `software engineer intern`, region United States and equivalent role/date filters where supported; record unavoidable mismatches. Do not replay undocumented endpoints, evade challenges, scrape bulk data, or exceed stated rate limits. Record HTTP 401/403/429 and stop that target when blocked.
2. Use the same browser build, viewport, machine, network and clock. For each platform alternate trial order; measure at least five independent sessions, with a fresh browser profile for cold trials and a separately repeated query in the same profile for warm trials. A new profile does not guarantee a cold CDN or server cache.
3. Start a monotonic timer immediately before submit; record first visible relevant result, complete first-page state, query request start/end, response status, transferred and decoded bytes where exposed, rendered card count and reported total. If cross-origin timing or payload size is unavailable, record `unavailable`, not zero. Capture sanitized request metadata only; avoid cookies, personal data and tokens.
4. Activate the ordinary page-two control. Measure click-to-first-new-result, request timing and page count; if unavailable record the reason. Do not compare pages with different sizes without reporting the sizes.
5. Report individual trials and median/p95 only where sample size supports the statistic. Preserve raw timestamps, selected controls, browser version, query and filter descriptions, time zone, test date, errors, and the exact warm/cold definitions.
6. For ingestion, run before and after on a controlled fixture replay or equivalent isolated snapshot and the same source responses. Measure source start/end, validation, publication and search visibility separately; compare unique stable IDs after alias reconciliation. Do not infer search freshness from an artifact write. Test production-like concurrency and failure/retry scenarios separately.

## Reproduce the limited REA observation

REA documents Node >=22.23.4 in the 22.x line (or a supported newer release). Install REA in a disposable project-local environment with scripts reviewed, start a Chrome-family browser and open Simplify's public jobs UI. Example using an existing loopback CDP endpoint:

```sh
rea list-browser-targets http://127.0.0.1:PORT --format json
rea inspect-web-page http://127.0.0.1:PORT TARGET_ID --observation-ms 500 --format json
```

REA observes traffic only from attachment onward and filters by origin by default; capture earlier requests with an explicitly consented fresh observation. Do not retain HAR cookies, authentication headers, PII, or unneeded third-party request URLs. See [REA](https://github.com/morluto/rea) for prerequisites and output contracts. The saved evidence file here is sanitized metadata, not a complete raw REA evidence bundle.

## Source probe benchmark (2026-11-02)

Not an ingestion benchmark. One unauthenticated GET per URL from this host,
12–15 s timeout, custom User-Agent, no credentials or challenge solving.
Records: [source-probe-2026-11-02.json](source-probe-2026-11-02.json).

| Target | Status | Type | Bytes read | ms |
|---|---|---:|---:|---:|
| jobhive manifest (already used) | 200 | json | 40,563 | 275 |
| arbeitnow (already used) | 200 | json | 200,000 | 135 |
| freehire (already used) | 200 | json | — | — |
| simplify `/jobs` | 200 | html | 144,099 | 160 |
| jobdataapi anonymous query 1 | 200 | json | 153,311 | 1,177 |
| jobdataapi anonymous query 2 | 403 | — | — | 354 |
| headcountlabs `sample-records.json` | 200 | json | 11,415 | 439 |
| jobvite `/careers/` | 200 | html | 60,000 | 1,875 |
| wellfound `/jobs` | 200 | html | 200,000 | 157 |
| ycombinator `/jobs` | 200 | html | 200,000 | 406 |
| echojobs sitemap | **429** | — | — | 139 |
| hiring.cafe api | 403 | — | — | 191 |
| workatastartup `/jobs` | 406 | — | — | 405 |
| usajobs api | 401 | problem+json | — | — |
| adzuna api | 400 | — | — | 243 |
| jooble api | 403 | — | — | 54 |
| jobsekr | DNS | — | — | 32 |

This measures reachability and response shape only. Bytes are read-capped, so
they are not payload sizes, and a single request says nothing about sustained
throughput or rate limits.

## Feed yield measurement (reproducible, local)

```sh
python docs/reverse-engineering/source_yield.py pages/searchtern.json
```

On the committed 2026-10-29 feed: 20,901 rows, 20,844 normalized unique links,
0 rows missing a link, 57 duplicate links across sources. Per-source rows and
freshness are tabulated in
[source-inventory.md](source-inventory.md#3-measured-value-of-what-is-already-ingested).

Limit that must travel with these numbers: dedup already ran on
`(company, role, location)`, so "sole-source" is near 100% for nearly every
source by construction. The measurement supports volume and freshness claims
only. It does **not** establish unique-employer yield, and it is four days stale
at capture time.

## Pre-dedup source benchmark (2026-11-02)

`python docs/reverse-engineering/pre_dedup_benchmark.py --limit 1000`

Read-only. Fetches a bounded sample per source and reports what the published
feed cannot: `discovered → valid → duplicate_in_batch → already_in_feed → new →
updated`, plus ms per 1,000 discovered. Evidence:
[pre-dedup-2026-11-02.json](pre-dedup-2026-11-02.json).

| Source | Status | Found | Valid (intern/new-grad) | In feed | New | ms | ms/1k |
|---|---|---:|---:|---:|---:|---:|---:|
| jobhive:adp | ok | 1,000 | 13 | 0 | 13 | 864 | 864 |
| jobhive:apple | ok | 1,000 | 13 | 0 | 13 | 855 | 855 |
| headcountlabs:sample | ok | 3 | 0 | 0 | 0 | 417 | 139,091 |
| headcountlabs:api | skipped | — | — | — | — | — | — |
| jobdataapi:api | skipped | — | — | — | — | — | — |
| freehire | ok | 20 | 9 | 0 | 9 | 627 | 31,359 |
| arbeitnow | ok | 300 | 25 | 0 | 25 | 196 | 655 |

What these numbers do and do not say:

- The jobhive rows are an **unfiltered head-of-file sample** (no `ORDER BY`), so
  "13 valid of 1,000" is the intern share of that bucket, not a yield claim.
  It quantifies why a 5.1 M-row corpus becomes a ~9.5 k-row feed.
- `headcountlabs:sample` is a 3-record public projection, explicitly not the API
  contract. Zero valid rows here is a property of the sample, not of the API.
- `freehire` and `arbeitnow` show 100% "new" because the feed snapshot is four
  days old and link/key matching is exact. Treat as a smoke test of the matcher,
  not as an incremental-value measurement.
- The two keyed sources are skipped by design. No subscription was bought and no
  key was invented; see [paid-feeds-evaluation.md](paid-feeds-evaluation.md).
- Per-1,000 timings include TLS setup and DuckDB httpfs warm-up on first read, so
  they overstate steady-state cost and are not comparable across transports
  (parquet range reads vs JSON page).

## Jobhive corpus freshness (2026-11-02)

Measured over remote parquet with DuckDB: 39–70% of each sampled bucket is
older than 90 days, and postings from 2014–2021 remain in the corpus. Full table
and consequences in [jobhive-staleness.md](jobhive-staleness.md).
