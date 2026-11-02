# jobdataapi and Headcount Labs — evaluation before integration

Both are keyed. No key was used, no account was created, and neither is
integrated. That is the correct order: value is demonstrated first.

Harness: [`pre_dedup_benchmark.py`](pre_dedup_benchmark.py). Both adapters are
written and skip unless `JOBDATAAPI_KEY` / `HEADCOUNT_LABS_API_KEY` is set.

## jobdataapi.com

**Observed (2026-11-02, this host, no key):**

- First anonymous query, `?title=intern`, returned **200** with
  `content-type: application/json`, 153,311 bytes read, and `"count": 131617`.
  That is the 131K-record endpoint named in the task.
- Every later anonymous request — `?title=software engineer intern&max_age=14`
  with `page=1..4` — returned **403** in ~350 ms each, with no `Retry-After`.
  Anonymous access is a one-shot allowance per client, not a paging allowance.
- **Pagination is therefore untested.** Page size, cursor/offset behaviour, and
  maximum depth are unknown from measurement. The response did expose `count`
  and a `next` URL, which is consistent with page-number paging, but that is all
  that can be said.
- Record shape from the one successful response (from the vendor's own example,
  which matched the live payload): `id`, `ext_id`, `company{name, website_url,
  linkedin_url}`, `title`, `location`, `types[]`, `regions[]`, `countries[]`,
  `states[]`, `cities[]`, `has_remote`, `published`, `description_md`,
  `experience_level`, `salary_min/max/currency`, `application_url`.
- `application_url` points at the employer's ATS (Greenhouse, SmartRecruiters in
  the samples), and `ext_id` is the ATS requisition id. **This is the useful
  property:** it feeds identity dedup directly, unlike jobhive's cumulative
  `(company,title,location)` rows.
- Vendor page (documented, not verified): 53,384,266 total jobs, 528,903 new in
  the last 7 days, 2,570,945 in 30 days; `Authorization: Api-Key`; public
  endpoints for limited testing; paid plans. `/pricing/` and `/api-docs/` both
  returned **404**, so **price and rate limits are unknown**.

**Freshness:** unmeasurable now. A `max_age` filter exists, so recency is at
least expressible; the distribution of `published` across the live catalog could
not be sampled.

**Incremental yield:** unmeasurable now. The 131,617 figure is a query-wide
count for one keyword, not a net-new count against SearchTern.

**Verdict:** do not integrate yet. Needs one key for roughly ten minutes of
work: run `JOBDATAAPI_KEY=... python pre_dedup_benchmark.py --limit 500`, then
page once to confirm the cursor convention. Cost is unknown until pricing is
read, so the spend cannot be justified before the yield number exists.

## Headcount Labs (api.headcountlabs.com)

**Observed:** public `sample-records.json` (11,415 bytes, 200) is a
**projection sample, not the API contract** — its own description says so, with
`extractedAt: 2026-09-30` and 3 records, all healthcare Workday postings, none
intern titles. So the sample cannot measure yield and must not be used as one.
The API itself requires a bearer token; no unauthenticated read of
`/v1/jobs` was attempted because the docs state 401 for it.

**Documented — pagination:**

- `GET /v1/jobs`: page size 1–250 (1,000 only when `fields=job_id`), default 50;
  `next_cursor` → `cursor`. Cursors are opaque HMAC-signed tokens bound to
  projection, filters, sort and page size; they do not expire.
- Default order is `updated`; a walk can **miss jobs added during the walk** and
  withdrawn jobs drop out. For reliable sync the docs point at
  `GET /v1/jobs/changes`: page 1–1,000, default 100, `resume_cursor` → `after`
  while `has_more`. Bulk export exists for up to 100,000 jobs.

**Documented — auth, cost, rate:**

- Bearer token. **100,000 records free per UTC month**, then $0.01 per 1,000
  records, at any volume. Every returned job counts as one record, including
  re-reads of records already fetched.
- 30 requests/60 s and 1,000 requests/UTC day on the free allowance; 60/60 s and
  no daily limit once paying. 429 carries `Retry-After`. Responses carry
  `X-Data-Records-Charged` and remaining-allowance headers.
- Note: the aggregator listing that describes this API says "1,000 jobs/month".
  The vendor's own docs say 100,000 records/month. **Trust the vendor docs; the
  third-party figure is wrong.**

**Documented — quality and lifecycle:**

- `postings[]` per job, each with source platform, `source_posted_at`,
  `first_seen_at`, `last_completed_fetch_at`, `latest_application_check`
  (`available` / `closed` / `broken` / `inconclusive`).
- `currentness` (`current` / `uncertain` / `not_current`), `posting_status`
  (`listed` / `no_longer_listed`), and a `stale` boolean.
- Merges redirect with **308** + `merged_into_job_id`; withdrawn jobs return
  **410** with `no_longer_listed_at`. That is a real closure signal — the thing
  jobhive does not provide (see [jobhive-staleness.md](jobhive-staleness.md)).
- 113 platform `source` values, covering SuccessFactors, iCIMS, Taleo, ADP,
  Paylocity, UKG, Phenom, Avature, Eightfold, JazzHR, BambooHR, Jobvite, Join
  and more — i.e. most of the ATS gap in this repo.

**Documented — freshness:** "re-checks every job daily" and explicitly
**no freshness SLA**; `last_completed_fetch_at` is the only honest signal.
Minimum latency is therefore ~24 h, which disqualifies it from the 15-minute
priority lane.

**Cost at realistic cadences** (250 records/page, paid tier only after the free
100k):

| Poll cadence | Records/month | Estimated cost |
|---|---:|---:|
| 1×/hour | 180,000 | $0.80 |
| 1×/6h | 30,000 | free |
| 1×/5min | 2,160,000 | ~$20.60 |

The 5-minute cadence buys nothing anyway, because the source itself is daily.
Recommended shape: hourly delta via `/v1/jobs/changes` (which returns only
events, not the whole catalog) inside the medium tier.

**Verdict:** best-evidenced candidate so far on lifecycle and ATS breadth, and
cheap at a sane cadence, but **freshness ceiling is 24 h** and its incremental
yield against SearchTern is unmeasured because it needs a key. Do not integrate
before that measurement.

## What would settle both

1. A key for each (Headcount free tier is enough; jobdataapi price unknown).
2. One harness run at `--limit 250`, which fits Headcount's page size.
3. For Headcount, one `/v1/jobs/changes` poll to confirm event volume per hour.
4. Decision rule, fixed in advance: integrate only if net-new unique
   `(company, title, location)` postings per 1,000 records exceed what the
   freshest current source (`jobspy`, 11.7 d median) delivers, at a cost below
   the monthly budget.
