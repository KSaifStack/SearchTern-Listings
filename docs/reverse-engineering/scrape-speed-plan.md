# Scrape speed and quality plan (2026-11-07)

What the fastest open scrapers in this niche actually do, what SearchTern
measures today, and the ordered fixes. Every number here was read from this
repo or measured on 2026-11-07; nothing is quoted from a vendor counter.

## 1. What the field does

| Project | Pattern worth copying | Source |
|---|---|---|
| `stapply-ai/ats-scrapers` (jobhive) — the feed we already ingest 49% of rows from | One `BaseScraper` per ATS with a tiny `fetch()`; canonical schema with `global_id = {ats}:{ats_id}` and `apply_url` separate from `url`; browser path isolated behind an env flag | github.com/stapply-ai/ats-scrapers, `docs/JOB_SCHEMA.md` |
| `smresponsibilities/jobscraper_hourly` (13,700 boards, hourly) | **Board rotation** ordered by `last_polled_epoch`, **two-phase fetch** (title/location screened before descriptions), zero-database state | github.com/smresponsibilities/jobscraper_hourly |
| `Riley-Dorrington/job-board-aggregator` (1M rows) | **Worker counts tuned per platform** (50 Workday, 30 GH/Lever/iCIMS, 10 BambooHR, 5 Ashby/Paylocity); ~25k-row gzipped chunks + manifest; prune >30 d | github.com/Riley-Dorrington/job-board-aggregator |
| `mherzog4/job-boards` | Per-board **ETag → `If-None-Match`**, 304 = "unchanged board, no rows", gated so filters don't silently lose rows | github.com/mherzog4/job-boards `openwiki/workflows/job-scrape.md` |
| `openroles` (PyPI) | **2 requests in flight per host**, robots crawl-delay honoured and re-read daily, gzip, conditional requests, honest UA, 429/503 = leave host alone for the run | pypi.org/project/openroles |
| `GetAnything-1/ats-jobs-scraper` | The Workday traps: `limit > 20` returns an **empty array, not an error**; public endpoints exist to be discovered, so cache hard and stay slow on small boards | github.com/GetAnything-1/ats-jobs-scraper |

Six patterns repeat: bounded per-run work, conditional GET, two-phase fetch,
per-host concurrency, canonical ids, and chunked artifacts with a manifest.

## 2. What SearchTern measures today

| Fact | Value | Where |
|---|---|---|
| Last full run wall time | **719.3 s** | `pages/source_stats.json` |
| Rows discovered by that run | 139,174 (73,477 from `ats-probe`) | same |
| Net-new rows from it | **26** | `pages/run_meta.json` |
| `ats-probe` history | 6 consecutive runs, 73k–244k discovered, **0 new** | `Listings/cache/source_health.jsonl` |
| Boards in registry / probe state entries | 4,000 / 6,683 | `Listings/cache/{discovered_boards,ats_probe_state}.json` |
| Board concurrency | global `SEARCHTERN_WORKERS=12`, per-provider `HOST_CONCURRENCY=4` | `readme_utils.py` |
| Per-request latency (measured) | GH 140 ms, Ashby 374 ms, Lever 781 ms | live probe |
| ETag on board APIs | present on all three (`W/"…"`) | live probe |
| Conditional GET in the probe path | **none** (only `sitemap_sources` uses `requests-cache`) | `readme_utils.http_get` |
| Published payload | 65 MB `searchtern.json`, 34 MB `listings.json`, no manifest, no shards | `pages/` |
| Repo weight | `.git` 2.9 GB, `digests/` 247 MB, 31 `README-N.md` copies | `du` |
| README claim vs artifacts | **16,390** vs 18,120 vs 29,282 rows | `README.md`, `pages/*.json` |
| Links not on an employer ATS | **46%** (1,369 `go.vansh.nyc`, 605 LinkedIn) | `pages/listings.json` |
| Duplicate `(company, role)` rows | 18,120 rows → 15,021 unique (**17%**) | same |
| Field coverage | salary 4.6%, seniority 10%, employment_type 10%, description median 190 chars, visa 17% | same |
| Source concentration | jobhive 14,287 / 29,282 (**49%**), median age 35.7 d | `docs/reverse-engineering/roadmap.md` |
| SEO surface | no sitemap, no robots.txt, no per-job page (site is an SPA) | repo + live fetch |

## 3. The one-line diagnosis

The pipeline is optimised for **discovery volume**, but the product needs
**freshness per board**. It re-downloads the whole registry, then throws 99.98%
of it away at dedup. Speed has to come from probing less, not from probing
faster.

## 4. Fixes, in order

| # | Fix | Why | Acceptance test |
|---|---|---|---|
| 1 | **Per-run probe budget + oldest-first rotation** (this commit: `Listings/probe_budget.py`, `SEARCHTERN_PROBE_MAX_BOARDS`, default 1200) | Bounds wall time instead of letting the registry decide it; deferred boards keep their timestamp so nobody starves | `--tier heavy` wall time ≤ 300 s at cap 1200; every board still probed within 2× cooldown |
| 2 | **Conditional GET on board APIs** — per-board ETag/`If-Modified-Since` in `http_get`, 304 replays the stored body (the pattern already proven in `sitemap_sources._session()`) | All three major APIs send ETags; an unchanged board should cost ~0 bytes | second run: 304s logged, bytes downloaded drop ≥ 80% while row count is unchanged |
| 3 | **Two-phase fetch** — parse list payload, screen title/location first, fetch descriptions only for survivors | Descriptions dominate payload on Greenhouse/Workday; most rows are filtered anyway | bytes per run down; no change in published row count |
| 4 | **Per-platform worker caps** — replace the single global 12 with the measured split (~30 GH/Lever/iCIMS, 10 BambooHR, 5 Ashby) keeping the existing per-provider semaphore | Ashby is the tightest limiter; one global number is either too fast for it or too slow for Workday pods | zero 429s in `source_health.jsonl` over 3 runs at the new caps |
| 5 | **Retire zero-yield boards** — after N runs with 0 net-new, drop the board's frequency by a factor, not its entry (uses the `new` column `source_health.jsonl` already records) | 6 runs, 0 new: this is the actual waste | probe seconds fall while net-new per run is flat |
| 6 | **Sharded, gzipped consumer artifacts + `manifest.json`** — ~25k-row chunks, gzip, unconditional manifest with counts, `generated_at`, per-field coverage | "Fastest to scrape from" is a consumer property too: today every client pulls 65 MB | cold client fetch to first filtered result ≤ 2 s; gzip ratio recorded in the manifest |
| 7 | **Canonical ids + identity dedup** — `(ats, employer, requisition_id)` first, canonical URL next, fuzzy key last (roadmap P2) | 17% of rows are repeats of the same job | duplicate link count 0; two same-title requisitions survive |
| 8 | **Prefer the employer URL** — resolve `go.vansh.nyc`/LinkedIn redirects to the ATS link, add a liveness check with the existing `dead_links` counter | 46% of links don't land on the source, and dead links are the fastest trust killer | non-ATS share < 10%; `dead_links` reported per run |
| 9 | **One generated numbers page** — README badge/line rendered from `run_meta.json` + `source_stats.json`; `/numbers` on the site | The pitch is "checkable numbers"; today three files disagree | one command regenerates README numbers; a test fails if README and artifacts differ |
| 10 | **Indexable job pages + sitemap + `schema.org/JobPosting`** for rows posted in the last 14 days on covered boards | Free acquisition; the site currently has no crawlable page | sitemap validates; Google Jobs sees the markup |
| 11 | **Artifacts out of git** — releases/CDN for `digests/`, `pages/*.json`, drop the 31 `README-N.md` copies | 2.9 GB clone is a distribution tax | repo clone < 50 MB, workflows unchanged for readers |
| 12 | **Fix the two measured freshness regressions** (already P1 in `roadmap.md`): EchoJobs 429 cooldown, per-source max-age at ingest | Feeding is stale by construction on README tables | zero 429s; p90 source age under the configured cap |

Steps 1 and 2 are the speed work. 3–5 cut bytes, 6–8 make the output worth
scraping, 9–11 fix credibility and distribution, 12 is already planned.

## 5. What was measured for this commit

- Registry 4,000 boards; state file 6,683 entries; one pass at the old
  behaviour = 719 s for 0 net-new.
- Handler latency 140–781 ms per board, so 1,200 boards / 12 workers is
  ~35 s of transfer plus per-provider backpressure — an order of magnitude
  below the old sweep.
- Rotation proved by test (`Listings/test_probe_budget.py`): 12 boards, cap 3,
  4 runs → every board probed exactly once, each run probes 3.
