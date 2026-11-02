# SearchTern competitor research — 2026-11-02

## Documents

| File | Contents |
|---|---|
| [source-inventory.md](source-inventory.md) | Every adapter audited, candidate sources with probe evidence, measured per-source volume/freshness, licensing boundaries |
| [competitors.md](competitors.md) | Intake model per platform, verified vs inferred, freshness patterns worth copying |
| [paid-feeds-evaluation.md](paid-feeds-evaluation.md) | jobdataapi and Headcount Labs: auth, pagination, quality, freshness, cost, yield — and why neither is integrated |
| [jobhive-staleness.md](jobhive-staleness.md) | Measured stale-listing problem in the primary source, and the closure check that fixes it without a new vendor |
| [roadmap.md](roadmap.md) | Prioritized implementation order with a "done when" per item |
| [benchmarks.md](benchmarks.md) | Source probe results, pre-dedup yield, feed yield, search timings, controlled-trial procedure |
| [source-probe-2026-11-02.json](source-probe-2026-11-02.json) | Sanitized raw probe records |
| [evidence-2026-11-02b.json](evidence-2026-11-02b.json) | jobdataapi 403 sequence, Headcount documented contract, jobhive bucket freshness, ATS gap counts |
| [pre-dedup-2026-11-02.json](pre-dedup-2026-11-02.json) | Benchmark output for the accessible sources |
| [source_yield.py](source_yield.py) | Read-only per-source yield/freshness measurement over a published feed |
| [pre_dedup_benchmark.py](pre_dedup_benchmark.py) | Read-only pre-dedup benchmark: discovered/valid/duplicate/in-feed/new/updated + ms per 1,000 |

## Scope and evidence rules

This is a continuation of the ingestion audit (isolated priority run: 216.26 s). The task was research only: no production feed or application-tracking data was modified. Findings below are **observed** in this session, **documented** by the named platform, or **hypotheses** needing measurement. Vendor assertions do not establish the architecture of every feature.

Tooling: `morluto/rea` 6.8.0, local CLI `inspect-web-page` against a headless Chrome 156 DevTools endpoint; saved, sanitized evidence metadata is in [rea-simplify-evidence.json](rea-simplify-evidence.json). REA attached after navigation, so earlier network activity was not captured by REA. Its script-bundle analysis exceeded a 15-second total deadline; JavaScript source analysis is therefore **not complete**. Browser requests observed separately via agent-browser. Node 22.23.1 was below REA's published requirement (22.23.4), though the passive inspection command succeeded; results may be affected. No authentication, challenge circumvention, automated bulk collection, or credentials were used.

## Competitors

| Platform | Verified evidence | Unknown / access boundary |
|---|---|---|
| Simplify | Public `/jobs` provided keyword search, location, job type, experience and category filters; query `software engineer intern` displayed job cards. Browser observed POST `https://js-ha.simplify.jobs/multi_search?use_cache=true&cache_ttl=120` with 200 and a separate job/company detail GET. URL parameters indicate a cache request, **not** proof of an actual hit or cache implementation. Landing page describes roles added hourly. | No response bodies retained; cursor, total count, pagination rules, cache-hit headers, source intake and dedup logic not established. Bundle analysis timed out. |
| LinkedIn | Public search page for the same query loaded with title reporting 7,000+ roles; a sign-in prompt covered further access. [ATS integration documentation](https://business.linkedin.com/hire/hiring-integrations/ats-integrations) describes distributing job postings and syncing ATS data. | Counts are not comparable to SearchTern; no authenticated search, paging, API or refresh inspection. |
| Indeed | [Indeed ATS integration](https://www.indeed.com/hire/ats-integration) says ATS Sync transfers job/candidate data and describes 350+ integrations. | Public search returned `Request Blocked`; no search or page benchmark. |
| Handshake | [XML feed documentation](https://support.joinhandshake.com/hc/en-us/articles/32426275339927-FAQs-XML-Job-Feed) says that **this feed type** is processed once per day, removed jobs close on the next sync, and jobs can take up to 24 hours to appear. It states existing posts are not automatically updated when XML content changes. [Import documentation](https://support.joinhandshake.com/hc/en-us/articles/34276134429719-EDU-Jobs-Imported-into-Handshake-from-External-Sites) describes external imports matched to verified employers and restricted availability. | The accessible computer-science internships page is a curated category, not the identical query; no inference about freshness for other Handshake sources, search internals, or dedup. |
| ZipRecruiter | Search results identified [partner documentation](https://www.ziprecruiter.com/partner/documentation/) describing Jobs API and XML feeds; direct page retrieval returned 403, so this is a search-result snippet, not independently inspected documentation. | Search returned a Cloudflare human challenge; do not bypass. No API, cache, pagination or latency evidence. |

**Why competitors feel faster — hypothesis only:** indexed paged search, cached query responses, smaller result cards, prefetching, and incremental ingestion are plausible; this investigation cannot rank causes or attribute latency differences. Simplify's observed request is consistent with cached search but does not confirm these mechanisms.

## SearchTern audit: verified from this checkout unless noted

- Shared `requests.Session` has 32-connection HTTP adapters, per-call timeouts, and 429/503 retry handling: [`Listings/readme_utils.py`](../../Listings/readme_utils.py). Several sources use bounded `ThreadPoolExecutor`s; `--workers` and `SEARCHTERN_WORKERS` are parsed in [`generate_listings.py`](../../Listings/generate_listings.py).
- Priority is scheduled every 15 minutes, light hourly, medium and heavy every two hours, all every four hours. All workflows use the same `job-listings` concurrency group; a long run can delay or replace pending scheduled work. See [workflow files](../../.github/workflows/).
- Priority workflow commits only `pages/priority.json`; the main search-feed JSON is produced later by the pipeline (`readme_generation.py`) and consumed by the deployed backend according to a code comment. Whether production reads priority.json is **unverified**. Consequently 15-minute feed publication does not establish 15-minute search visibility.
- A priority run first retrieves the Jobhive manifest and executes DuckDB `read_parquet` queries (`generate_listings.py`, near `build_job_query` and `Fetching jobhive manifest`) before fetching priority boards. Source groups and final outputs are not published one source at a time.
- Workday probes default to 200 jobs; SmartRecruiters defaults to 1,000. A later-page failure can leave partial data and is not represented as a failed full snapshot (`generate_listings.py`, `_fetch_company_jobs`). The jobhive query partitions by company/title/location, so distinct requisitions with those fields equal may be lost before the newer URL dedup stage.
- Tier parquet snapshots and `seen.json` live under `Listings/cache`; the current checkout has atomic parquet writes and some empty-refresh fallback. Per-board preservation on partial failures, durable source-specific freshness and application-history isolation cannot be established solely from these files.
- Browser observation of `https://searchtern.net/jobs`: `/recent`, `/count`, and multiple `/jobs/<id>` GETs appeared in the initial page request log; entering the same query yielded results and page buttons. This does **not** establish backend query plans or whether additional search requests were hidden by the capture scope. An `api.searchtern.net/recent` resource timing was 64 ms in one navigation; cross-origin resource byte counts were 0 and not reliable.
- **PostgreSQL not audited:** this checkout contains the listing generator, not the deployed backend's PostgreSQL schema, queries, plans or config. No `EXPLAIN`, indexes, database upsert behavior, connection pool, or tracking database was inspected. Access to the backend source and a safe read-only staging database is needed.

## Five highest-priority gaps and roadmap

1. **Visibility path and fast-lane critical path.** Instrument `source_detected_at`, `validated_at`, published revision and `searchable_at`; verify whether the backend consumes the priority feed. Prototype an independent priority-only path in staging, then compare p50/p95 detection-to-searchable time and page-result latency. Do not introduce competing writes without atomic merge and recovery tests.
2. **Identity and partial-source safety.** Use stable `(ATS, employer, requisition ID)` identities and conservative cross-source alias matching; correct the upstream DuckDB partition. Preserve the last successful snapshot for each failed board or page and never infer closures from incomplete fetches. Test equal titles with distinct IDs, aliases, missing pages, and tracking history.
3. **Search-serving diagnosis.** Obtain backend code and read-only `EXPLAIN (ANALYZE, BUFFERS)` on representative staging data. Record p50/p95 query, serialization, transfer, render, first-result and page-two timings; inspect indexes, pool saturation, query plans, and payloads. Choose index/cursor/cache changes only after the plan and correctness tests justify them.
4. **Coverage and pagination.** Log per-board page counts, truncation, HTTP failures, fetched rows, valid rows, overlap and net-new unique postings. Raise or remove caps only with source-specific limits and rate budgets; validate company discovery on known ATS URL patterns. Expand through permitted employer/ATS feeds, prioritizing proven unique early-career yield rather than scraping protected competitor endpoints.
5. **Reliable incremental publication and measurement.** Persist per-source validators/watermarks and atomic upserts; publish successful batches independently with idempotent replay and tombstone safeguards. Record retries, cache hits, failures, duplicate rate, and freshness distributions; prune only explicitly owned caches with a dry run and tests proving user tracking remains untouched.

## Benchmarks and reproduction

See [benchmarks.md](benchmarks.md). A controlled five-platform cold/warm comparison was **not achieved**, and neither the one-off navigation timings nor ingestion time establish a product speed ranking. No new sources or performance fixes were implemented by this research task.
