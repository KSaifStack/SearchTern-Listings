# ATS expansion — Eightfold, Oracle, iCIMS, ADP, BambooHR (2026-11-02)

All endpoints below were verified live with read-only requests before any
code was written. Raw numbers: [ats-expansion-2026-11-02.json](ats-expansion-2026-11-02.json).

## Staging measurement (bounded run)

`Listings/ats_new_sources.py::fetch_and_parse`, 16 boards, 4 workers:

| source | rows | notes |
|---|---|---|
| eightfold × 5 (Qualcomm, Starbucks, CACI, Applied Materials, Arcadis) | 351 | server-side `query=intern` + `new grad`; 10 rows/page; Qualcomm 429'd mid-run and the host stopped cleanly |
| oracle × 2 (WSP, Stantec) | 3,637 | full boards, ISO dates + inline descriptions, no per-job calls |
| icims × 6 (GDMS, Kimley-Horn, Dewberry, Sargent & Lundy, Peraton, Novelis) | 60 | search pages embed job links; intern slugs first; JSON-LD per job |
| adp × 2 (Pennoni, PPC Partners) | 540 | orgoid resolves live; ISO dates + descriptions inline |
| bamboohr × 1 (Lunar Outpost) | 10 | list is dateless, details fetched for intern-shaped titles only |

**Total: 4,598 rows in 55.7 s (~83 rows/s), 712 intern-shaped, 100% dated.**
Caps used: `EIGHTFOLD_MAX_PAGES=12, ICIMS_MAX_JOBS=10, BAMBOO_MAX_DETAILS=10`
(production defaults are higher; see module env tunables).

## Endpoint reference

- Eightfold: `GET {host}/api/pcsx/search?domain={d}&query=&location=&start=N`
  robots.txt allows `/api/pcsx`. Fixed 10 rows/page. Microsoft's host 429s
  cloud IPs on the first request — excluded; any 429 stops the host for the
  run (EchoJobs pattern) instead of retrying into it.
- Oracle: `GET {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions`
  `?finder=findReqs;siteNumber={site},limit=200,offset=N` with
  `expand=requisitionList.secondaryLocations`. (host, site) comes from feed
  links (`/hcmUI/CandidateExperience/en/sites/{site}/job/{id}`); the site is
  NOT derivable from a company name, so `oracle_boards_from_links()` parses
  it out. Unknown siteNumbers answer 200 with a wrong site's jobs — the
  module only uses verified pairs. Nothing past offset 9,800 of one query.
- iCIMS: `{portal}/jobs/search?pr=0&in_iframe=1` embeds `/jobs/{id}/{slug}/job`
  links (query string included — the first regex missed it, fixed with a
  regression test). Job pages carry schema.org JobPosting JSON-LD with
  `datePosted`, `employmentType`, `jobLocation`, `url`, `directApply`.
- ADP: `GET myjobs.adp.com/public/staffing/v1/career-site/{domain}` returns
  the orgoid header the list requires; job URL is
  `myjobs.adp.com/{domain}/cx/job/{reqId}` (confirmed against feed links).
- BambooHR: `GET {tenant}.bamboohr.com/careers/list` (whole board, no dates);
  `{host}/careers/{id}/detail` has `datePosted` + compensation. Details are
  fetched for intern-shaped titles only. A non-JSON answer means the tenant
  moved — reported, never retried.

## Fixes found while probing (all applied)

- Greenhouse: `?pay_transparency=true` adds `pay_input_ranges` (cents);
  `_greenhouse_pay()` collapses to min/max dollars + currency
  (Scale AI: 110/167 postings carry ranges).
- Greenhouse paging: the board can exceed one 100-row page; pages are
  followed with an id-overlap stop (some boards re-send page 1; observed
  live). Whole-board-in-one-response boards are untouched (1 request).
- Teamtailor: switched `jobs.rss` → `jobs.json` (JSON Feed with per-item
  schema.org `JobPosting`: `date_published`, locations, `content_html`).
  `next_url` followed, capped at `TEAMTAILOR_MAX_PAGES=5`.
- Workday `workday_req_id`: `externalPath` tail now outranks `bulletFields[0]`
  (bulletFields is a free-text label on some tenants, e.g. "Regular Employee").
- Workday 422 (tenant moved data centres): recorded per employer and the
  `WorkdaySlugs|` discovery cache for that employer is dropped, so the next
  run re-probes robots.txt instead of failing through the cooldown.
- iCIMS employer names: hiringOrganization carries suffixes ("..., Inc.");
  `_clean_employer()` strips them for cross-source dedup (local to the new
  module; the global suffix pass is still open in FUTURE.md).
- H1B: `visa_median_salary` kept as object dtype so missing stays `None`
  (pandas coerced it to NaN, failing the suite's intent).

## Deliberately not built

- Phenom (`careers.rtx.com/widgets` answers 200 here, 4,809 RTX jobs): often
  fronts Workday (dup risk) and 403s some cloud IPs per the source notes.
  Revisit with a cloud-run test before wiring.
- SuccessFactors: no public JSON endpoint (job-board-apis confirms); stays on
  the sitemap route.
- Taleo: HTML scrape + tokens per OpenPostings discussion; no cheap endpoint.
  Sitemap route instead.
- SmartRecruiters robots.txt allows only LinkedInBot — the existing adapter
  works technically but is one policy change from dead; do not expand it.
- jobdataapi / Headcount Labs: still key-blocked (403 after one query / token
  required). Nothing to integrate until keys exist.
- New ATS kinds are NOT in `closure.VERIFIABLE_ATS` yet: snapshots are
  recorded (harmless, `record_snapshot` returns 0) but cast no close votes
  until the jobhive-side id shape is verified per ATS.

## Wiring

- Medium tier (every 2 h), after breadth: fetch → `_classify_freehire` →
  `_stamp(..., "ats")` → cache `ats_new` → dedup/split/merge like other tiers.
- `discover_boards.parse_link` + `PROBEABLE` cover the five new ATS, and
  `_fetch_company_jobs` delegates them to `ats_new_sources`, so newly found
  boards join the heavy probe automatically.
- Health: `source_health.py` appends per-run per-source lines to
  `Listings/cache/source_health.jsonl` (capped), alerts after 2 consecutive
  non-ok runs, prints the trailing-7d new-postings ranking.
