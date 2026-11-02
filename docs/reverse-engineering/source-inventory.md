# Source inventory and evaluation — 2026-11-02

Continuation of the ingestion audit. Everything here is **observed** in this
checkout or with a single unauthenticated request per URL from this host on
2026-11-02, unless marked *documented* (the vendor says so) or *hypothesis*.
No production feed, cache, or tracking file was written.

Raw probe records: [source-probe-2026-11-02.json](source-probe-2026-11-02.json).
Yield measurement: `python docs/reverse-engineering/source_yield.py pages/searchtern.json`.

## 1. What SearchTern already ingests

Audited from `Listings/`. Do not add any of these twice.

| Adapter | Sources | Shape | Notes from code |
|---|---|---|---|
| `generate_listings.py` (jobhive) | `storage.stapply.ai/jobhive/v1/manifest.json` → parquet | bulk snapshot, DuckDB `read_parquet` | Primary source; 9,526 rows in the committed feed. Single manifest for all buckets, so no per-source validators/watermarks. |
| `markdown_sources.py` | GitHub README tables: SimplifyJobs (Summer 2027, New Grad, Off-Season), speedyapply (SWE/AI, intl), zapplyjobs (6 repos), SuryaHarikrishnan (6 tracks), GodlyDonuts/keryx, lilguy, zshah101, ApplyGuy, sndsh404, vanshb03, LorenzoLaCorte | rotated markdown/HTML tables | Dedup is on `(company, role, location)`; overlapping repos are dropped silently. |
| `breadth_sources.py` | Jobicy, Arbeitnow, Remotive, RemoteOK, Himalayas, Working Nomads, WeWorkRemotely RSS, HN "Who is hiring", The Muse | public JSON/RSS | Remote-first general boards; intern/new-grad yield is thin. |
| `sitemap_sources.py` | 20 host pairs (Walmart, Target, Kroger, Nucor, Union Pacific, Sysco, GM, Deloitte, Accenture, Capgemini, Cognizant, DXC, Publicis Sapient, Verizon, Lockheed, Northrop, Honeywell, Johnson Controls, GE HealthCare, ServiceNow, Workday) | sitemap → JSON-LD `JobPosting` | `robots.txt` is a hard gate via `protego`; 1.5 s/host, 4 host workers. |
| `ecr_source.py` | earlycareerradar.com sitemap | JSON-LD crawl | 0.25 s delay, ~20 min full crawl; `addressCountry` missing on ~43% of pages, country backstop from locality. |
| `runway_source.py` | app.joinrunway.io sitemap shards | JSON-LD crawl | Default 2 shards ≈ 7–8 h at 0.25 s; serialized by host lock. |
| `echojobs.py` | echojobs.io sitemap | JSON-LD detail fetch | `MAX_DETAIL_FETCHES = 100`, 1 s delay. |
| `jobspy_source.py` | jobspy library (Indeed/LinkedIn/Glassdoor class) | library scraping | Freshest source in the feed (median 11.7 d). |
| `generate_listings.py` (freehire) | freehire.me `/api/v1/jobs/search` | paginated API | Freshest non-scraping source (median 12.8 d). |
| ATS probes | Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Rippling, Pinpoint, Recruitee, Teamtailor, Breezy, Personio, Workday CXS | public board APIs | Workday site discovery via `robots.txt`; 12 h cooldown; Workday cap 200, SmartRecruiters cap 1,000. |
| `priority_boards.py` | 56 curated Greenhouse/Lever/Ashby boards | same ATS fetchers, no cooldown | Feeds `pages/priority.json` every 15 min. |
| `h1b_enrich.py` | H-1B disclosure data | enrichment | Not a source. |

## 2. Candidate sources, with probe evidence

Every row below is one GET from this host; a block is evidence about *this
fetcher*, not proof the source is unusable behind an authorized key.

| Candidate | Probe | Verdict |
|---|---|---|
| **Boardsweep** | 200, HTML, 9.6 KB, 790 ms. Page: "*Not a job board — we don't post ads*"; reads LinkedIn/Indeed ads to score a CV. | **Reject.** Not a listings source; its own copy says it publishes nothing. |
| **Jobhive / Stapply** | manifest 200, JSON, 40.6 KB, 275 ms; `data.stapply.ai` 200. | **Already primary.** No new integration. Its repo is what SearchTern already consumes. |
| **Simplify** | `/jobs` 200, HTML, 144 KB, 160 ms. Search UI works anonymously; `multi_search` documented in [README.md](README.md). GitHub job lists already ingested. | **Already covered** via GitHub READMEs. Do not scrape the search API. |
| **EchoJobs** | sitemap 200 on earlier runs, **429** today. | **Already integrated, needs backoff.** 429 means the current 1 s delay / 100-fetch cap is not conservative enough. |
| **Jobsekr** | `jobsekr.com` and `www.jobsekr.com` → DNS `NXDOMAIN`. | **Reject (unverifiable).** No site resolved. Re-check before spending effort. |
| **Wellfound** | `/jobs` 200, HTML, 200 KB, Turnstile challenge in the head; `robots.txt` disallows `/_jobs/`. | **Reject.** No public API; anti-bot + robots exclusion. |
| **Y Combinator** | `ycombinator.com/jobs` 200 HTML; `workatastartup.com/jobs` **406**. | **Blocked.** Work-at-a-Startup gates on login/API. YC's own jobs page is a thin marketing shell — no listing feed verified. |
| **Headcount Labs** | `sample-records.json` 200, JSON, 11.4 KB; `/docs/` 200. *Documented*: Bearer token, free 1,000 jobs/month, paid from $10/mo, every source rechecked ≥ daily, deduplicated records with direct apply links. | **Trial candidate.** Cheapest credible deduped feed; daily freshness is worse than the 15-min lane, so it belongs in medium/heavy, not priority. |
| **jobdataapi.com** | First anonymous query 200, JSON, 153 KB, `count: 131617`; second query **403** → anonymous limit. *Documented*: 53.4 M jobs, ATS-sourced, API key for paid plans. | **Trial candidate (paid).** ATS-sourced with `ext_id`/`application_url`, so it can be deduped by identity rather than title. Same daily-freshness caveat. |
| **USAJobs** | `data.usajobs.gov/api/search?Keyword=intern` **401**. *Documented*: free API key + `User-Agent` email required. | **Candidate (free key).** Official federal postings — different job population than the current feed, so overlap should be near zero. |
| **Adzuna** | **400** (needs `app_id`/`app_key`). | **Candidate (free tier with key).** Aggregator; overlap with existing sources unmeasured. |
| **Jooble / Careerjet** | 403 / DNS error without keys. | **Deprioritize.** Both are affiliate APIs with redistribution terms that must be read before caching. |
| **hiring.cafe** | `/api/search-jobs` **403**. | **Blocked.** Do not evade. |
| **jobright.ai** | Guessed `/api/v1/job/search` → **404**. | **Unverified.** No evidence the endpoint shape is real. |
| **Getro (VC portfolio boards)** | `api.getro.com/api/v2/collections/1/jobs` → 406 plain, 401 with `Accept: application/json`. | **Conditional.** Per-board collection token must be read from each public board page; high employer-direct yield for startups, but one config row per board. |
| **BambooHR** | Guessed tenant → 404. | **Needs tenant discovery.** `{tenant}.bamboohr.com/careers/list` is public once the tenant is known. |
| **JazzHR** | Board page 200; `/api/v1/board/demo` returned **HTML**, not JSON. | **Sitemap/JSON-LD only**, like `sitemap_sources`. |
| **Jobvite** | `/careers/` 200, HTML, 1.9 s. | **Sitemap/JSON-LD candidate.** Slow page, so it needs the existing per-host delay. |
| **WayUp, RippleMatch, Symplicity, CollegeRecruiter** | All 200 HTML. | **Unverified.** Robots and sitemap were not checked; university platforms usually require institutional login for listings. |
| **earlycareerradar / Runway robots** | ecr: `Allow: /`, `Disallow: /api/`. Runway: `Allow: /`, `Disallow: /api/`, `/explore/job/JOB_POSTING/`. | **Consistent with current adapters.** Both already avoid `/api/`. |

Also verified as already-integrated and healthy: Arbeitnow (200 JSON) and the
Jobhive manifest (200 JSON).

## 3. Measured value of what is already ingested

`source_yield.py` on the committed feed (2026-10-29, 20,901 rows, 20,844
normalized links, 0 rows missing a link, 57 duplicate links across sources):

| Source | Rows | Sole-source links | Median age | p90 age |
|---|---:|---:|---:|---:|
| jobhive | 9,526 | 9,482 | 35.7 d | 61.5 d |
| ecr | 3,206 | 3,206 | 40.7 d | 62.7 d |
| GodlyDonuts keryx Internships | 903 | 900 | 42.7 d | 63.7 d |
| SuryaHarikrishnan DS/AI | 774 | 769 | 38.7 d | 62.7 d |
| freehire | 749 | 745 | **12.8 d** | **16.5 d** |
| ats | 708 | 706 | 16.5 d | 38.0 d |
| jobspy | 681 | 681 | **11.7 d** | **14.7 d** |
| speedyapply SWE New Grad USA | 527 | 527 | 27.9 d | 59.9 d |
| priority | 23 | 23 | 13.7 d | 28.0 d |

**How to read this, and how not to.** "Sole" is near 100% for almost every
source because dedup already ran on `(company, role, location)` and each source
writes a distinct URL — so surviving rows are source-exclusive *by
construction*. It is an upper bound on uniqueness, not a measurement of unique
employers. The honest signal in this table is **freshness and volume**, not
novelty:

- The two API/live sources (`jobspy` 11.7 d, `freehire` 12.8 d) are two to three
  times fresher than the README-table sources (median 27–46 d, p90 60+ d).
  GitHub README lists keep old rows long after the posting is stale.
- `jobhive` is 46% of the feed by row count, which matches its role as the bulk
  backbone.
- 57 cross-source duplicate links survived dedup, so the `(company, role,
  location)` key is still not equivalent to URL identity.
- The feed is four days old at measurement time and carries no
  detection-to-publish timestamp per row, so **none of this is a latency
  measurement**.

## 4. What a real yield benchmark needs

The current artifacts cannot answer "which source adds jobs nobody else has".
Measuring that requires, per run and before dedup:

1. `source`, `fetched_at`, `source_item_id`, canonical apply URL, employer,
   title, location for every fetched row (not every surviving row).
2. Post-dedup outcome per row: `kept`, `merged_into=<id>`, `dropped_stale`,
   `dropped_filter` — so overlap is attributable, not silent.
3. Per-source outcome: HTTP status, pages/cursors consumed, truncation flag,
   retries, bytes, wall time.
4. A join on stable ATS identity `(ats, employer, requisition_id)` where the
   source exposes one; fall back to canonical URL, then to the fuzzy key.

With those four, "net-new unique employers per minute of ingest" becomes
computable. Without them, any source ranking is a guess.

## 5. Licensing and access boundaries

- jobdataapi, Headcount Labs, Adzuna, Jooble, Careerjet: **redistribution terms
  not reviewed** — read before caching beyond local use.
- LinkedIn, Indeed, ZipRecruiter, hiring.cafe, Wellfound, Work at a Startup:
  blocked or sign-in gated. Do not circumvent.
- Simplify, EchoJobs, Early Career Radar, Runway: public pages only; the
  exposed `/api/` paths are disallowed by robots and are not used.
- Jobhive/Stapply: already consumed as published; its repo is the upstream
  contract.

## 6. ATS coverage gap, counted from the same registry the adapter uses

`ATS_CSV_URL` (`Kayvan-Zahiri/state-of-ats-2026`) has 738 companies. Cross-
referencing its `ats_system` column against `ATS_ENDPOINTS` in
`generate_listings.py`:

| ATS | Companies | Supported today |
|---|---:|---|
| Workday | 289 | yes (CXS) |
| Greenhouse | 94 | yes |
| SuccessFactors | 68 | **no** |
| Oracle Cloud HCM | 51 | **no** |
| iCIMS | 40 | **no** |
| Internal ATS | 31 | n/a |
| Avature | 26 | **no** |
| Eightfold | 24 | **no** (robots.txt `Disallow: /`, already documented in `sitemap_sources`) |
| SmartRecruiters | 19 | yes |
| Taleo | 18 | **no** |
| Ashby | 15 | yes |
| USAJobs | 10 | **no** (free key available) |
| Kenexa BrassRing | 9 | **no** |
| Lever | 5 | yes |
| Jobvite | 5 | **no** |
| Phenom | 5 | **no** |

**314 of 738 companies (42.5%) sit on an ATS this repo cannot read.** The four
largest gaps — SuccessFactors (68), Oracle Cloud HCM (51), iCIMS (40), Taleo
(18) — are sitemap-visible in many cases, so the cheapest probe is the existing
robots-gated `sitemap_sources` path, not a new adapter per vendor. Avature (26)
and Phenom (5) are usually sitemap-visible too; Eightfold is excluded by robots
and must not be attempted.

Priority inside the gap, by company count and existing path:

1. SuccessFactors + Taleo — 86 companies, standard job sitemaps, JSON-LD on the
   posting page.
2. iCIMS — 40 companies, `jobs.icims.com` sitemaps, but per-tenant robots checks
   are mandatory before any fetch.
3. Oracle Cloud HCM — 51 companies, `*.oraclecloud.com/hcmUI/CandidateExperience`
   pages; no public JSON verified, so sitemap-only.
4. Avature/Phenom/BrassRing — 40 companies combined, lower priority.
