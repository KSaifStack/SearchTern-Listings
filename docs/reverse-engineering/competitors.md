# Competitor mechanisms — what is verified and what is not

Companion to [source-inventory.md](source-inventory.md). This file records only
what could be checked, and separates it from inference. Nothing here is a
capability claim copied from marketing copy.

## Intake model, per platform

| Platform | Verified | Inference (hypothesis) |
|---|---|---|
| **Jobhive / Stapply** | Publishes a static manifest plus parquet/csv snapshots at `storage.stapply.ai/jobhive/v1/` (200, JSON, 40.6 KB). SearchTern already consumes it. The upstream project is `stapply-ai/ats-scrapers`. | A batch ATS crawl published as snapshots, not a query API. Snapshot cadence is not stated in the manifest SearchTern reads, so freshness is whatever the publisher chose. |
| **Simplify** | Public job search renders and answers a query anonymously. Browser observed `POST https://js-ha.simplify.jobs/multi_search?use_cache=true&cache_ttl=120` plus a separate job/company detail GET. Landing copy says roles are added hourly. Two GitHub job lists (Summer 2027, New Grad, Off-Season) are already ingested by SearchTern. | `multi_search` with `use_cache`/`cache_ttl` is consistent with a cached search layer, but the parameters alone do not prove hits, a cache implementation, or a TTL that is honoured. Hourly intake is a vendor statement, not measured. |
| **EchoJobs** | Public sitemap of jobs; SearchTern crawls it with a 100-fetch detail cap and 1 s delay. Probe returned **429** on 2026-11-02. | Rate limiting is per-caller; the cap and delay SearchTern uses are above what the host now tolerates. |
| **Boardsweep** | Landing page states it is *not* a job board: it reads LinkedIn and Indeed ads to score a CV against them. | No listing corpus of its own to ingest. Its ingestion is a consumer of two platforms that already block SearchTern. |
| **Wellfound** | `/jobs` serves HTML with a Turnstile challenge; `robots.txt` disallows `/_jobs/`. | Search is behind anti-bot. |
| **Y Combinator** | `workatastartup.com/jobs` returns **406**; `ycombinator.com/jobs` is a marketing shell. | Listings are login-scoped. |
| **Headcount Labs** | Public `sample-records.json` (11.4 KB) and `/docs/`. *Documented*: bearer token, free 1,000 jobs/month, paid from $10/month, records deduplicated with direct apply links, every source rechecked at least daily. | Daily refresh is the ceiling — incompatible with the 15-minute priority lane. |
| **jobdataapi.com** | Anonymous first query returned `count: 131617` with ATS-shaped records (`ext_id`, `application_url`, `experience_level`); a second anonymous query returned **403**. *Documented*: 53.4 M jobs, ATS-sourced, key-gated paid plans. | Records carry enough identity to dedupe on `(ats, employer, ext_id)` instead of title text — the useful property here. |

## Why competitors feel faster

Previously observed, still **unproven as a causal ranking**: Simplify issues a
cached multi-search call, SearchTern's page issues `/recent`, `/count`, and
per-job requests; LinkedIn/Indeed/ZipRecruiter could not be benchmarked at all
(see [benchmarks.md](benchmarks.md)). Plausible contributors, in the order they
should be tested:

1. A single aggregated search request versus one request per job.
2. Server-side cached query responses keyed by filter set.
3. Smaller listing payloads (search cards, not full descriptions).
4. Paged cursors with a small page size.
5. Incremental intake that updates an index without a full rebuild.

No measurement in this session separates these. SearchTern's own search path
remains unaudited because the deployed backend (PostgreSQL, indexes, query
plans) is not in this checkout.

## Freshness mechanisms worth copying, cheaply

These are architecture patterns visible in how the platforms publish, and each
maps onto an existing SearchTern structure:

- **Snapshot + stable identity.** jobdataapi and Jobhive both publish records
  with an external requisition id. SearchTern's feed still keys dedup on
  `(company, role, location)`, which cannot distinguish two requisitions with
  the same title.
- **Cache keyed by filter set with a short TTL.** Only useful once the backend
  is instrumented; not something the generator can prove.
- **Per-source recheck cadence.** Headcount Labs advertises ≥ daily per source;
  SearchTern currently re-polls most buckets on a fixed global schedule rather
  than per-source yield.
