# Publication migration: sharded feed instead of one 68 MB file

Measured 2026-11-07 on the live `pages/searchtern.json` (29,282 rows).

| Artifact | Size | Fetch cost |
|---|---|---|
| `pages/searchtern.json` (monolithic) | 67.9 MB raw | whole file, whole parse, every consumer |
| `pages/shards/listings-000NN.json.gz` | 14.53 MB gz in 2 parts | one part per page of results |
| `pages/manifest.json` | ~2 KB | counts, coverage, timestamps, part list |

Compression ratio 4.7x, 4.56 s to write both parts. Generation is not the
problem — distribution is. A frontend that needs 20 rows today downloads 68 MB.

## Why shards are opt-in, not default

The pipeline still commits the monolithic JSON. Emitting the shards as well
would add 14.5 MB per run on top of it. With `update-heavy` and `update-all`
each running every few hours, that is roughly 170 MB/day of new git objects in a
repository whose `.git` is already 2.9 GB — gzip does not delta, so every
revision is stored whole. `SEARCHTERN_SHARDS=1` turns shards on; the default is
off until the big files stop being committed.

## Order of work (each step is reversible)

1. **Swap the reader first.** Point the site at `manifest.json` +
   `shards/listings-000NN.json.gz`, falling back to `searchtern.json` when the
   manifest is absent. Nothing is removed yet; both paths work.
2. **Publish both for one week.** Run with `SEARCHTERN_SHARDS=1` and compare
   row counts, byte size and load time per page against the old feed.
3. **Move shards out of git.** Upload the same files as GitHub Release assets
   (or a CDN bucket) from the workflow, and commit only the manifest with
   absolute part URLs. The manifest is small enough to commit every run; this
   is the step that ends repository growth.
4. **Stop committing the monolith.** Change the workflows' `git add pages/` to
   exclude `pages/searchtern.json` and `pages/listings.json`, keep writing them
   as build artifacts for the fast path.
5. **Delete the fallback** once no consumer reads the monolith.

Rollback at any step is "stop publishing shards, serve the JSON you still
commit" — steps 1–2 change nothing that is deleted, and step 4 is one line in
five workflow files.

## Frontend consumption contract

- `manifest.json`: `generated_at`, `rows`, `shards[]` (`part`, `rows`, `bytes`,
  `sha16`), `field_coverage[]`. Poll this; it is small and cheap.
- `listings-000NN.json.gz`: newline-delimited JSON, one listing per line, keys
  sorted. Fetch only the parts whose `sha16` changed since the last poll.
- Rows carry `first_seen_at`, `last_seen_at`, `verified_at`, `ats_type`,
  `ats_id`; a client can sort by first-seen and show verification age without
  recomputing anything from `date`.

Adding a part is additive; parts shrink when the feed shrinks and stale parts
are deleted by the publisher, so a client must treat the manifest's part list as
authoritative rather than caching part names forever.

## Authorized webhook research (not implemented)

Webhooks need an inbound HTTP endpoint, and this pipeline runs on scheduled
GitHub Actions jobs, which have none. The three routes that stay within the
existing hosting:

1. **GitHub `repository_dispatch`** — a provider that supports GitHub webhooks
   (many ATS partner programs do not) can dispatch the existing workflow
   directly. No new infrastructure, no payload parsing beyond the event name.
2. **Cloudflare Worker / small function** as receiver, committing or dispatching
   on arrival. This is new infrastructure and a new secret; it is the honest
   option only once an employer actually offers a signed webhook.
3. **Scheduled pull of a webhook-fed file** — a provider that writes its events
   to a URL we can GET. That is just another RSS/JSON source and needs no
   endpoint at all.

Recommendation: do not build a receiver speculatively. Wire route 1 for a
specific employer that offers it; the cost is one workflow file and no secrets.

## RSS

`Listings/rss_source.py` parses RSS 2.0 and Atom with `xml.etree` (no DTDs, no
external entities) and is configured by `SEARCHTERN_RSS_URLS`, empty by default.
It is tested and deliberately unwired: with no URLs set, wiring it would add an
untested code path that does nothing. Adding it to the light tier is a one-line
merge once real feeds are chosen.
