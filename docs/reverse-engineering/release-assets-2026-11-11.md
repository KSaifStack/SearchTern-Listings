# Feed publication failure and release-asset migration — 2026-11-11

**Scope:** diagnose the failed `Update listings (light)` run and make the publisher stop committing the monolithic feed to git. No deploy, commit, push, or filter widening.

## Failure observed

- Run: [`Update listings (light)` #365](https://github.com/KSaifStack/SearchTern-Listings/actions/runs/43112267381), head `e096af8a`.
- Steps: tests passed in 10 seconds; the scraper passed in 9m56s; `Commit and push updates` failed after five retries, and the job exited 1. No source or test error occurred.
- The previous heavy run ([`#243`](https://github.com/KSaifStack/SearchTern-Listings/actions/runs/43074971263)) had the same split: tests and scraper passed, then the commit/push step failed after five retries.
- The repository’s `main` branch is not protected (`protected: false`), so this was not a branch-protection rejection.
- Published `pages/searchtern.json` is **88,951,778 bytes** for **26,765 rows**. The light and heavy runs rebuild the union before writing, so the next output can cross GitHub’s 100 MB file limit. The retry loop re-pushes the same oversized commit five times.
- The exact GitHub server message is not visible in the public job page without authentication. The step boundary, the identical heavy-run failure, the unprotected branch, and the feed size point to the feed commit rather than the ingestion gate.

## Change

The feed is now a GitHub Release artifact, while git holds the small publication contract.

- `SEARCHTERN_FEED_DELIVERY=release` makes the manifest describe two asset URLs under the rolling `feed-latest` release.
- `pages/feed-manifest.json` carries the URL, rows, bytes, and SHA-256 for `searchtern.json` and `listings.json`.
- A release-mode manifest is verified from its URL/hash metadata. A fresh checkout may still contain the previous compatibility JSON; that file is not treated as the current artifact and cannot block the next publish.
- The four full-feed workflows upload both assets, download them again, compare bytes and SHA-256 with the manifest, then commit only the manifest, README, country pages, RSS, cache, and diagnostics.
- `pages/searchtern.json` and `pages/listings.json` are excluded from the commit and restored to the checkout copy, so the push contains no 89 MB blob.
- The README generator links the release asset when release delivery is active.
- The bundled MCP server resolves `pages/feed-manifest.json` first and fetches the asset URL; `SEARCHTERN_DATA` and `SEARCHTERN_DATA_URL` still override it.

## Consumer note

The old raw `pages/searchtern.json` remains in git as a compatibility copy but is not updated by the new publisher. Consumers must use `published.searchtern.url` from `pages/feed-manifest.json`. The deployed backend lives outside this repository and still needs that one-line source change; the bundled MCP server is updated here.

The rolling `feed-latest` tag is overwritten on each publish. The local physical last-known-good directory remains, but durable per-generation release assets are still a follow-up for a true pointer rollback.

## Verification

```text
cd Listings && python3 -m pytest -q
262 passed

cd Listings && python3 -m ruff check feed_manifest.py readme_generation.py \
  readme_rendering.py searchtern_mcp.py test_release_delivery.py test_workflows.py
All checks passed!

python3 -m py_compile Listings/*.py
git diff --check
PyYAML parse of all update-*.yml: ok
```

A release-mode smoke publication wrote both asset URLs, replaced both local compatibility files with stale content, and still verified the manifest. `test_publication_stages.py` now runs the same check through the full publication audit.

## Remaining limits

- GitHub Release upload is not exercised here because no release token is available in this checkout. The workflow verifies the downloaded asset before committing.
- The backend consumer migration is outside this repository.
- The feed is still large and still takes a full download; shards remain opt-in and are not a substitute for the manifest contract.
- No deploy, commit, or push was performed.
