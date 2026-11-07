# Positioning and claims (2026-11-06)

What competitors put on their homepages, what we can say back, and what we must
never say. Every SearchTern number here is measured and re-derivable; every
competitor number is quoted from their own page on the date shown.

The short version: **we cannot win on breadth or on freshness claims, so we
should not try. We can win on the three things nobody else on this list has —
H-1B sponsorship data, a measured filter, and numbers anyone can check.**

## 1. What they claim, and how checkable it is

| Site | Their headline claims | Checkable? |
|---|---|---|
| **Simplify** | "Fresh roles, added hourly"; "Helping 2 million job seekers"; "index postings directly from 50+ job boards and company career pages daily"; "get hired 3x faster"; "apply to 50 jobs with just 5 minutes" | Mostly no. The daily counters ("New jobs added today—") render as empty placeholders without JS; 2 M users and "3x faster" have no stated denominator. The one falsifiable claim is "50+ boards", which is modest. |
| **Boardsweep** | "4,694 company job boards read"; "3 min median, posting to found, top companies"; "44 min median for every other board"; "403 new roles in the last 24 hours"; every 3 min for top companies, hourly for the rest; $39 season pass | Best-in-class on specificity, and it is checkable — but **their own site does not agree with itself**: 3,763 / 4,427 / 4,438 / 4,614 / 4,684 / 4,694 boards across their own pages and LinkedIn posts. The 3 min / 44 min medians and the "found in N minutes" table are the strongest honest speed claim among the four. |
| **Huntr** | "Join 500,000+ job seekers"; "landed jobs at 1000s of companies"; "1.1k reviews, 4.9 stars"; autofill on "1000s of websites"; "10x faster" | User count and review score are checkable in principle; "10x" is not. Note Huntr **has no job corpus at all** — it is a tracker plus autofill. Not a listings competitor. |
| **Runway** | "Trusted by 175k+ students"; "750+ campuses"; "monitor 175,000+ company career sites in real time"; "updates job listings in real time"; "1 in 10 applications lands an interview" | Partly. Measured against their own API: 428,000 matched roles, 3,691,500 lifetime roles, 466 distinct ATS hosts in a 4,000-row sample, newest listing ~3.3 h old, 418 ms per 40-row page. "Real time" and "175,000 career sites" do not survive contact with those numbers. See [runway.md](../reverse-engineering/runway.md). |

The exploitable pattern: **every competitor's headline is a number nobody can
recompute.** Boardsweep's own board count drifts by 900+ between their own
pages, and Runway's three internal totals disagree by two orders of magnitude.
That is the gap in this market.

## 2. Where we actually stand (measured)

Feed as committed (`pages/searchtern.json`, built 2026-11-05T00:41Z):

| Fact | Value |
|---|---|
| Rows | 26,643 |
| Distinct companies | 5,748 |
| Internships / new-grad roles | 19,493 / 5,649 |
| Countries | 72 (US 15,748, CA 1,614, FR 1,220, GB 1,148, DE 726) |
| Sources | 49 feeds, 35 distinct source labels, 167 priority ATS boards |
| H-1B flags | 4,603 rows marked sponsor, 791 with a matched sponsored role title |
| H-1B reference data | 82,393 employers, USCIS FY2021–2023, DOL LCA role data 2024–2025 |
| With salary | 1,060 rows |
| With description | 14,458 rows |
| Query compute | p50 7.8 ms, p95 9.5 ms over 28,121 rows |
| Input-to-results | p50 253 ms (200 ms of it is our own debounce) |
| Payload | 12.3 MB raw / 1.6 MB gzip; cold LCP 2.6–2.8 s |
| First appearance | p50 **13.0 h**, 36% ≤ 1 h, 62% ≤ 24 h (n = 88) |
| Coverage | 56% of early-career postings on covered boards |

So: smaller than all of them, slower than Boardsweep and Runway on freshness,
faster than all of them on nothing we can prove except filter compute.

## 3. Claims to ship now

Each of these is true today, has a number attached, and a reader can check it.
That last part is the point — it is the only thing on this page a competitor
cannot copy by editing a counter.

**Sponsorship (nobody else has this)**
- "Every listing checked against 82,393 H-1B-sponsoring employers (USCIS
  FY2021–2023) and DOL role data. 4,603 listings currently carry a sponsor flag."
- "Filter to roles where sponsorship is already on record — 791 match a sponsored
  job title directly."

**Speed we measured ourselves**
- "Search 26,000+ listings in your browser in 7.8 ms (p95 9.5 ms). Results land
  about 0.25 s after you stop typing."
- "No per-search scraping and no server round-trip. The index ships with the
  page; the query never leaves your machine."

**Auditability — the positioning**
- "Every number we publish can be recomputed from our public feed. The feed
  carries its own `observed_at`; the pipeline is open source."
- "Check us: `api.searchtern.net/recent` returns what the site searches, with
  timestamps. No counter that only we can see."

**Access**
- "Free, no account to browse. The whole feed is public JSON."
- "49 sources across 72 countries — not just US big tech."

**Scope we can defend honestly**
- "Internships and new-grad roles only. No senior roles padding the count."
  (19,493 internships and 5,649 new-grad rows; 6,212 other titles are already
  classified out of those two buckets, so state it as a focus, not a filter.)

## 4. Claims that unlock after the freshness fix

The measured 13.0 h p50 is queue starvation, not crawl speed: all five
workflows share `concurrency: group: job-listings`, so the `*/15` priority lane
queues behind 158–595 s tier runs and GitHub cancels the pending tick. Only 9
of 185 feed-change gaps were under 15 minutes.

Fix that (separate concurrency group for priority, staggered crons), re-run the
26-board probe, and then — and only then — these become sayable:

- "New intern roles on tracked boards usually appear within the hour."
- "The hot-company lane polls every 15 minutes for real."

Ship them only with the re-measured p50 in hand, and publish the p50. A
checkable "p50 47 min" beats a competitor's unverifiable "3 min".

## 5. Never say

| Don't say | Why |
|---|---|
| "Faster than Simplify / Boardsweep / LinkedIn" | No same-query, same-machine trial exists. Boardsweep's own numbers (3 min median) would beat our measured 13 h. |
| "Real-time listings" | 36% inside an hour. Runway says this and their own feed contradicts them; do not join them. |
| "Listings refresh every 15 minutes" | The site republishes hourly; the 15-minute lane writes the GitHub feed and is currently starved. |
| "Lightweight", "instant load", "faster page" | 1.6 MB gzip, 2.6–2.8 s cold LCP. |
| "More jobs than X", any 175k-style breadth number | 26,643 rows vs 428,000 matched on Runway and 4,694 boards on Boardsweep. |
| "Every listing links to the employer directly" | 1,501 rows are marked `direct_apply`; several sources are aggregators. Boardsweep can say this, we cannot yet. |
| "Millions of openings" (our 5.1 M upstream) | That is Jobhive's corpus, not ours, and 39–70% of sampled buckets are 90+ days old. |

## 6. Ready-to-use copy

Homepage subhead:
> **26,000+ internships and new-grad roles, with H-1B sponsorship checked
> against 82,393 employers — and a search that answers in 7.8 ms.**

Proof strip (each line links to the feed or the repo):
> 5,748 companies · 72 countries · 4,603 sponsor-flagged roles · 7.8 ms filter
> (p95 9.5 ms) · feed republished hourly · open source

Differentiator block:
> **Most job boards tell you how many jobs they have. We show you the file.**
> The feed behind this site is public JSON with its own timestamp. Recompute any
> number on this page yourself. If our freshness is worse than someone's, you
> will be able to see it here rather than take our word for it.

Anti-claim disclaimer (footer, one line) — this is a trust asset, not a weakness:
> We do not claim to be the fastest board. We publish what we measured:
> 7.8 ms filter, hourly republish, and a median 13 h from an ATS posting to our
> feed. We are working on the last one in public.

## 7. What to measure next to unlock stronger claims

1. Re-run the 26-board pickup probe after the concurrency fix — the single
   number that decides whether we can talk about freshness at all.
2. Ship a `/stats` line on the site (rows, companies, `observed_at`, last run
   duration) so every claim in section 3 has a live source.
3. Count how many of the 26,643 rows carry a sponsor flag *and* a matched role
   title per company, so the H-1B claim can be stated per employer rather than
   as a total.
4. Instrument the pipeline stages (run started / source fetched / classified /
   feed written / committed) — the 13.0 h cannot currently be attributed to a
   stage, which is why the fix is a guess until it is measured.

## Sources and limits

- SearchTern figures: `pages/searchtern.json`, `pages/h1b.json`,
  `docs/reverse-engineering/search-speed.md`, `listing-freshness.md`,
  `runway.md`, and the five workflows in `.github/workflows/`.
- Competitor figures: simplify.jobs, boardsweep.io (+ `/how-it-works`,
  `/compare`, `/preview`), huntr.co, joinrunway.io — read 2026-11-06.
- Competitor numbers are quoted, not verified; where their own pages disagree
  it is noted. Their "median minutes" figures are unaudited and should be
  treated as their claim, not a fact.
- Our freshness numbers are n = 88 postings across 26 boards, one host, one
  day. Small. Re-measure before copying any of them into a headline.
