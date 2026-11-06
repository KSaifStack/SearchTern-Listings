# Search speed: measured on the live site (2026-11-06)

One machine, one network, unauthenticated, headless Chromium 156 + Node on the
same host. Every figure below is a direct observation with the capture time
attached. None of it is a competitor comparison; see
[benchmarks.md](benchmarks.md) for why that comparison is still unavailable.

Targets: `https://searchtern.net/jobs` and `https://api.searchtern.net/recent`.

## What was measured

| Metric | Value | How |
|---|---|---|
| `/recent` payload (06:00 UTC) | 22,238 rows, 9,564,249 B raw, 1,287,895 B gzip | `curl`, decompressed size |
| `/recent` payload (16:20 UTC) | 28,121 rows, 12,285,677 B raw, 1,596,770 B gzip | `curl -H 'Accept-Encoding: gzip'` |
| `/recent` fetch | 0.78 s gzip / 1.14 s raw, TTFB ~0.53 s | 1 GET each, warm CDN, `cf-cache-status: DYNAMIC` |
| JSON.parse of the payload | 63–72 ms | Node `performance.now()` around one parse |
| Baseline index build (filter out rows with no company/link) | 7.0–7.5 ms | node, once per page load |
| Query compute (filter + date sort + slice 15) | **p50 7.8 ms, p95 9.5 ms** at 28,121 rows | 10 queries × 10 reps, node; p50 6.0 / p95 12.3 ms at 22,238 rows |
| Input-to-results in the real UI | **p50 253 ms**, range 235–268 ms | 32 keystroke-to-count-change trials via CDP |
| Fixed search debounce | **200 ms** | deployed `Jobs-*.js`: `setTimeout(()=>{ve(e),c(1)},200)` |
| Document | 4,098 B / 177 ms total (`/jobs`) | `curl` |
| FCP / TTFB | 236 ms / 126–334 ms | Chrome performance timeline |
| LCP warm | 1.66–1.74 s | repeated runs, warm cache |
| LCP / first result row, cold profile | 2.76 s / 2,623 ms | fresh browser profile |
| CLS | 0.12 | performance timeline |
| Public feed cadence | hourly at :00 | every row's `last_seen_at` = `2026-11-06 16:00:24Z` when read at 16:20 |

## Where the time actually goes

The search is client-side: the page fetches the whole feed once, then filters
the in-memory array on every keystroke. No query ever leaves the browser, and
nothing is scraped per search.

So perceived search cost splits as:

```
200 ms  fixed debounce in the bundle      <- dominates
 ~8 ms  filter + sort + slice (28k rows)
~45 ms  React render of the 15-row page   (253 observed - 200 - 8)
```

The debounce is ~80% of the wait and is pure UI policy, not a data cost. The
10× growth in the filtering set (22k → 28k rows) moved p50 by under 2 ms.

The real weight problem is upfront: 12.3 MB raw / 1.6 MB gzip fetched and parsed
before the first results, which is why cold LCP is 2.6–2.8 s while the query
itself is single-digit milliseconds.

## Claims these numbers support

Say these, with the cadence worded as cadence, not latency:

- "Search filters 28,000+ listings in your browser in under 10 ms (p95 9.5 ms);
  results appear about 0.25 s after you stop typing."
- "No per-search scraping and no server round-trip — the index ships with the
  page and the query never leaves the browser."
- "The listings feed is republished hourly." (Public API, verified from
  `last_seen_at` alignment at :00.)
- Listing freshness is measured separately in
  [listing-freshness.md](listing-freshness.md); as of 2026-11-06 the first
  appearance of a covered role was p50 13 h (n = 88), so do not attach a
  "faster than other boards" claim to it.
- "The priority intern feed on GitHub refreshes every 15 minutes." (Workflow
  cron `*/15`; this is intake cadence for the GitHub feed, **not** site
  freshness — the site republishes hourly.)

## Claims these numbers do not support

- **"Faster than Simplify / Boardsweep / LinkedIn."** No controlled
  same-query, same-machine trial exists. Earlier single-navigation observations
  are in [benchmarks.md](benchmarks.md) and are not comparable.
- **"Results refresh every 15 minutes."** False for the site; the public API is
  hourly. The 15-minute lane writes the GitHub priority feed.
- **"Sub-200 ms search."** Perceived p50 is 253 ms because of the debounce.
  Only the compute step is sub-10 ms.
- **"Lightweight / instant load."** 1.6 MB gzip and a 2.6 s cold LCP say
  otherwise; do not put page weight in a speed claim.
- **Real-user p75.** Everything here is one host and one network. Field data
  needs RUM on the site.

## Cheapest levers, in measured order

1. Cut or shorten the 200 ms debounce (or fire immediately and debounce only the
   render). Worth ~120–150 ms of the 253 ms p50 at zero data cost.
2. Ship descriptions separately from `/recent` so the search payload shrinks
   from 12.3 MB. That is the cold-LCP and mobile-data fix, not a query-latency
   fix.
3. Edge-cache `/recent` (`max-age` is set but Cloudflare reports `DYNAMIC`), so
   repeat visitors do not re-pay the payload.

All three are site/API-side and outside this repository.

## Reproduce

```sh
# payload + freshness
curl -s -H 'Accept-Encoding: gzip' -o /tmp/recent.json \
  -w '%{size_download} bytes, %{time_total}s\n' https://api.searchtern.net/recent

# compute-only latency (node, no browser)
node /tmp/bench.mjs        # parse_ms, base_filter_ms, query_p50, query_p95

# debounce evidence
curl -s https://searchtern.net/assets/Jobs-*.js | grep -o 'setTimeout([^)]*)'
```

Limit that must travel with the numbers: single host, single network, no CPU or
network throttling, no mobile device, sample sizes of 10–32 per figure. They
bound the current behaviour; they are not a p75 field measurement.
