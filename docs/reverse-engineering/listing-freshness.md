# Listing freshness: what the pipeline delivers (2026-11-06)

Answers one question: how fast does a newly posted role reach the feed? This is
the only evidence for any "faster at listings" claim. It is a first
measurement, not a settled number.

## Method

1. **Publish gaps** — timestamps of commits to `pages/priority.json` and
   `pages/searchtern.json` over 21 days (`git log --format=%cI`). The workflows
   commit only when content changed, so a gap is time between changes, not
   publish latency.
2. **Live board probe** — the 26 boards already in the feed with the most links
   (12 Greenhouse, 8 Ashby, 6 Lever). Fetched each board's public jobs API and
   kept postings with `first_published` / `publishedAt` / `createdAt` in the
   last 14 days.
3. **Presence** — exact URL match against `pages/searchtern.json`.
4. **First appearance** — earliest of the last 78 feed snapshots (9 days) that
   contains the link; lag = first snapshot time − ATS publication time.

## Results

| Measurement | Value |
|---|---|
| Priority-feed change gap | p50 101 min, p95 334 min, max 674 min; 9 of 185 gaps ≤ 15 min |
| Full-feed change gap | p50 82 min, p95 249 min |
| Board postings probed (14 days) | 7,169 across 26 boards |
| Early-career-looking postings | 569 (title regex: intern, co-op, new grad, graduate, entry level, campus, early career, junior) |
| Present in feed, 14 days | 317 / 569 = **56%** |
| Present, posted ≤ 24 h ago | 25 / 85 = **29%** |
| Present, posted ≤ 72 h ago | 126 / 275 = 46% |
| First-appearance lag (n = 88 matched) | **p50 13.0 h**, p90 133.8 h, 36% ≤ 1 h, 43% ≤ 6 h, 62% ≤ 24 h |
| Site republish | hourly at :00 (every `/recent` row carries the same `last_seen_at`) |

Read it as: a third of covered early-career postings are in the feed within an
hour, most within about half a day, and a long tail takes days. That is not
"minutes", and it does not beat an hourly board.

## Limits that must travel with these numbers

- **Greenhouse `updated_at` is an edit time, not a publication time.** Edited
  postings look fresher than they were, so Greenhouse lags are understated.
  Ashby `publishedAt` and Lever `createdAt` are true publish times.
- **Exact URL matching.** Alternate or redirected links count as missing, so
  56% is a lower bound on coverage.
- **The title regex is not the feed's classifier.** Some of the 43% "missing"
  are out of scope by design (not intern/new-grad), not late.
- **26 of 1,123 feed boards probed**, 78 snapshots, n = 88 for the lag. Small.
- **No competitor was measured.** Nothing here says how fast Simplify,
  LinkedIn, Indeed or Handshake surface the same posting.
- The `date` field in the feed is **not** a usable posting timestamp: 19 of 21
  sampled rows carried a `date` newer than the snapshot that first contained
  them, so it is refreshed or reseeded between runs. Do not compute freshness
  from it.

## Claims

Supported today, with the sample size attached:

- "Most intern and new-grad roles we cover appear within hours, about a third
  within the first hour (first-appearance median 13 h, n = 88)."
- "The public feed republishes hourly; the priority lane runs every 15 minutes
  and publishes when it finds something new."

Not supported:

- **"Faster than other job boards."** No head-to-head measurement exists.
  On these numbers a median 13 h first appearance is slower than an hourly
  crawl, not faster.
- **"Listings refresh every 15 minutes."** The cron runs every 15 minutes; the
  published feed changed every ~101 minutes (median) across 21 days because
  the workflow commits only on change.
- **"Real-time" or "instant" listings.** Only 36% were inside an hour.

## What would make the claim true

1. Publish or expose the feed independently of run cadence (the site already
   republishes hourly; the GitHub lane is the 15-minute path).
2. Investigate where the first-appearance lag accumulates: Actions queue time
   vs per-source fetch vs the 15-minute run's own duration. This measurement
   cannot separate them — it needs stage timestamps (`run started`,
   `source fetched`, `classified`, `feed written`, `committed`).
3. Re-run this probe against the same 26 boards weekly; n = 88 is thin, and
   Greenhouse's edit timestamps need the per-job `first_published` endpoint to
   tighten.
4. Only then compare against another board with the same postings and the same
   clock.

## Reproduce

The probe is a standalone read of public APIs plus this repo's feed; no writes.
Fetch a board, take `publishedAt`/`createdAt`, match the URL against
`pages/searchtern.json`, and take the earliest containing commit from
`git log --format='%H %cI' -- pages/searchtern.json`.
