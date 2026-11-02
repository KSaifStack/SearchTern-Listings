# Jobhive stale-listing problem — measured

Jobhive (Stapply) is 46% of SearchTern's published feed and the only source with
bulk coverage. This is what its snapshots actually contain.

Measured 2026-11-02 by reading the published per-ATS parquet files over HTTP with
DuckDB (`read_parquet` on `storage.stapply.ai/jobhive/v1/<ats>/jobs.parquet`).
Read-only; nothing downloaded to the repo.

## The corpus keeps years-old postings

| Bucket | Rows | ≤7 days | ≤30 days | ≥90 days | ≥90 d share | Oldest `posted_at` |
|---|---:|---:|---:|---:|---:|---|
| adp | 5,674 | 626 | 1,918 | 2,201 | 38.8% | 2014-04-28 |
| apple | 4,986 | 516 | 1,528 | 3,231 | 64.8% | 2021-04-07 |
| amazon | 32,929 | 2,273 | 10,004 | 23,125 | 70.2% | 2024-02-02 |
| ashby | 55,128 | 6,368 | 19,411 | 37,169 | 67.4% | 2019-10-08 |
| greenhouse | 178,743 | 14,265 | 52,875 | 107,245 | 60.0% | 2018-12-20 |

`all.parquet` is 5,090,813 rows (2.24 GB parquet, 16.6 GB csv). Between 39% and
70% of each sampled bucket is older than 90 days, and postings from 2014–2021
are still present today. A posting does not leave this corpus when it closes.

## There is no closure field to compensate

Schema of the per-ATS snapshots:

```
url, title, company, ats_type, ats_id, location, country_iso, region, language,
lat, lon, is_remote, salary_min, salary_max, salary_currency, salary_period,
salary_summary, employment_type, department, team, description, posted_at,
requisition_id, apply_url, commitment, raw
```

No `status`, no `closed_at`, no `last_seen`, no tombstone. `posted_at` is the
only time field, and it is 100% populated in every bucket sampled — so age is
the only available proxy for liveness.

`apply_url` is **null for all 5,674 adp rows** (0 populated), so the direct
apply target must be derived from `url`/`ats_id` for that ATS. Any dedup or
closure check keyed on `apply_url` silently loses that bucket.

## What this does to the feed

The pipeline is already limiting the damage: `build_job_query` uses a **60-day
lookback** for the search feed and **90 days** for the mirror feed
(`generate_listings.py`), which is why published jobhive rows show a 35.7 d
median and 61.5 d p90 instead of the corpus's tail.

That produces two errors at once:

- **False positives.** A role posted 20 days ago and filled 3 days ago is still
  inside the 60-day window and still shown as open. Nothing in the record says
  it closed.
- **False negatives.** A genuinely open rolling posting (internships are often
  open for months) is dropped at day 61 because age is treated as status.

And the DuckDB query partitions by `(company, title, location)` and picks the
newest row, so two distinct requisitions with those fields equal collapse before
URL identity is ever considered.

## Fix that does not need a new vendor

Jobhive rows carry `ats_type`, `ats_id` and `url` for 12 ATS platforms this repo
already polls directly (Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
Rippling, Pinpoint, Recruitee, Teamtailor, Breezy, Personio, Workday).
For those:

1. Build the set of live ids from the current ATS snapshot per employer.
2. Mark a jobhive row `dropped_absent` when its `(ats_type, employer, ats_id)`
   is missing from a **successfully completed** snapshot — never from a failed
   or truncated fetch.
3. Keep the row when the board fetch failed, and record `unknown_liveness`
   rather than guessing.

This turns a batch catalog into a verified one for the subset SearchTern can
check itself, with no new dependency and no extra requests. It does not help for
ADP, Amazon, Apple and the other buckets with no direct adapter; those still
need either age alone or a feed like Headcount Labs that publishes
`posting_status` / `application_check`.

## What to verify next

- Whether jobhive ever removes rows: diff two snapshots ≥ 24 h apart and count
  ids that disappear. If almost nothing disappears, absence is not a usable
  closure signal even in principle.
- How often `posted_at` changes for an unchanged `ats_id` (re-post updates).
- Whether `raw` contains a source closure marker that the flat schema discards —
  sample `raw` for one bucket before writing it off.
