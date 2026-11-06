# Runway (joinrunway.io) — how their job data works

Measured 2026-11-06, ~22:30–22:40 UTC, from one unauthenticated host. All numbers
below are observed, not quoted from marketing copy.

## Surface map

| Host | What it is | Evidence |
|---|---|---|
| `www.joinrunway.io` | Webflow marketing site behind Cloudflare | `cf-cache-status: HIT`, `age: 3044134` (~35 days stale), `server: cloudflare` |
| `app.joinrunway.io` | Next.js 15 App Router product on Vercel | `x-powered-by: Next.js`, `server: Vercel`, `/_next/static/chunks/*` |
| `cdn.joinrunway.io` | Company logo cache | `/logo/bmr.ca.jpg`, `cache-control: public, max-age=31536000, immutable` |
| `api` | No separate host. Same Next.js app, tRPC at `/api/trpc` | `x-matched-path: /api/trpc/[trpc]` |

Marketing and product are fully separate deploys. Search contention lives on the
Vercel app; the marketing site is a static CDN hit either way.

## How the data gets in: ATS-first crawling

Every job row carries `applyUrl` pointing at the employer's ATS, and the host
distribution over 4,000 consecutive rows is the giveaway:

```
workforcenow.adp.com          491      jobs.workable.com        129
careers.hrblock.com           317      recruiting2.ultipro.com  128
recruiting.paylocity.com      277      careers.hireology.com    111
myjobs.adp.com                219      egud.fa.us2.oraclecloud.com 78
jobs.smartrecruiters.com      203      eluq.fa.us2.oraclecloud.com 64
job-boards.greenhouse.io      153      (466 distinct hosts total)
recruiting.ultipro.com        133
```

466 distinct ATS hosts in one 4,000-row sample. That is not a per-company board
crawl like ours — it is per-ATS adapter work (ADP WorkforceNow, ADP MyJobs,
Paylocity, SmartRecruiters, Greenhouse, UltiPro, Workable, Hireology, Oracle
Cloud, TriNet, Zoho, HR Block), each pulling every customer's board through one
integration. One adapter buys hundreds of employers.

Rows also carry `brandFetchId` / `brandFetchDomain` and a cached logo, so branding
is resolved per employer domain at ingest, not at render.

## The query path is a public tRPC API

`robots.txt` disallows `/api/`, but the procedures answer without auth:

```
GET /api/trpc/jobPosting.getJobs?input={"json":{}}
GET /api/trpc/flightBoard.getJobStats?input={"json":{}}
GET /api/trpc/flightBoard.getStats?input={"json":{}}
```

`getJobs` returns `{jobs: [...40], nextCursor: {lastPostedAt, lastId}}`, ordered
`postedAt` desc. The cursor must be re-sent superjson-encoded
(`meta.values["cursor.lastPostedAt"] = ["Date"]`) or the server silently returns
page 1 again — a 60-page walk that ignores the meta block yields the same 40 rows
60 times, which is exactly the trap that produces fake "corpus size" numbers.

Other procedures visible in the bundles: `flightBoard.getLocationDetails`,
`searchCompanies`, `searchLocations`, `jobPosting.getSavedJobs`,
`getAppliedJobs`, `jobAnalysis.*`, `networking.getSearch`, `career.getProgress`.

The client calls `getJobs.useInfiniteQuery` — infinite scroll, one page at a time.
Filtering is **not** server-side: `searchQuery: "zzzzqqqq"` returns the identical
first page (same 40 ids) as an empty query, and `employmentType` / `dateRange` do
not change results on either `getJobs` or `getJobStats`. The browser holds the
fetched pages and filters them locally.

## Latency

| Call | p50 | p90 | body |
|---|---|---|---|
| `getJobs` (40 rows) | **418 ms** | 611 ms | 33 KB |
| `getJobStats` | 110–175 ms | — | tiny |
| `flightBoard.getStats` | ~120 ms | — | tiny |

`cache-control: public, max-age=0, must-revalidate` and `x-vercel-cache: MISS` on
every call: no CDN caching, each page is a live DB query. There is no
"one fetch then filter" path like SearchTern's `/recent` — the first screen costs a
round trip, and every scroll costs another 418 ms.

## Freshness

4,000 consecutive rows, ages relative to request time:

- p10 **3.3 h**, p50 **5.1 h**, p90 7.1 h, max 7.8 h
- only **2 of 4,000** rows under 1 h
- 2,712 of 4,000 under 6 h

So the newest real listing was ~3 h old at 22:40 UTC, and 4,000 rows only reach
back 7.8 h — roughly 500–600 new rows/hour, consistent with their own counters
(`addedToday` 14,243–14,993).

Two caveats. The head of the feed is polluted by future-dated rows
(`postedAt: 2026-11-23` on a "Conseiller / conseillère" row sorting first), so
"newest" has to be read past those. And `postedAt` is a self-reported ATS date,
same category of evidence as ours — comparable, not authoritative.

## Scale claims, and where they disagree

| Source | Number |
|---|---|
| `flightBoard.getStats` | `totalRoles: 3,691,500`, `rolesAddedToday: 14,243` |
| `flightBoard.getJobStats` | `totalMatches: 428,000`, `addedToday: 14,993` |
| `sitemap.xml` | 16 sitemaps × 50,000 URLs = up to **800,000** job pages |

Three internal numbers two orders of magnitude apart: 3.7 M is a lifetime crawl
counter, 428 K is what the matcher will serve, 800 K is the SEO surface. Worth
knowing before treating any of them as "jobs live right now."

## SEO surface

Every job page is server-rendered with schema.org `JobPosting` JSON-LD:

```json
{"@type":"JobPosting","datePosted":"2026-11-06T00:00:00.000Z",
 "validThrough":"2027-01-06T00:00:00.000Z","employmentType":"INTERN"}
```

`validThrough` = `datePosted` + 60 days. `robots.txt` allows everything except
`/api/`, saved/applied/interview views, and admin/profile. Up to 800k indexable
pages is the actual growth engine here, not the app.

## Compared to SearchTern

| | SearchTern (measured) | Runway (measured) |
|---|---|---|
| Delivery | one 1.6 MB gzip `/recent` (28,121 rows), cached hourly | paged API, 40 rows @ 418 ms |
| Filter | client-side, 7.8 ms p50 | client-side, over fetched pages |
| First results | ~253 ms perceived (200 ms debounce + filter) | first page ~418 ms + render |
| Newest listing | ~3 h typical, p50 pickup 13 h | **~3.3 h**, 600/h intake |
| Corpus | 26.6k classified rows | 428k matched / 800k pages / 3.7M lifetime |
| Source model | per-company board list + ATS adapters | per-ATS adapters, 466 hosts |

Read honestly: they win on freshness and breadth, we win on the cost of the first
result (one cached fetch + 7.8 ms filter vs a 418 ms live query) and on load
weight per result. Their freshness comes from adapter breadth and continuous
intake, not from a faster pipeline trick — there is no incremental feed, no CDN
caching, and no server-side search to copy.

## Confounds

- 4,000 rows, one host, one network, one time of day; 100 pages walked of an
  open-ended cursor.
- `postedAt` is ATS-reported and includes future-dated junk.
- Filter behaviour tested with three inputs against page 1 only.
- Their counters are batch-computed: `addedToday` and `totalMatches` did not move
  across 75 s.
- No authenticated view, so personalization/match ordering is unmeasured.
