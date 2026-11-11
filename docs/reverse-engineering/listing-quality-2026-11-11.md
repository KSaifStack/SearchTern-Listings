# Listing quality: employer names, titles, logos, duplicates — 2026-11-11

Scope: the published feed `pages/searchtern.json` (13,177 rows, 2,786 distinct
employers, 1,496 link hosts) and the code paths that produce it. The web
frontend lives in a different repository (`KSaifStack/SearchTern`), so rendering
fixes here are limited to the JSON contract and the README tables this repo
generates.

## What was measured

| Symptom | Count |
|---|---:|
| Employers with no whitespace and more than 11 characters | 566 / 2,786 |
| `Andurilindustries` | 58 rows |
| `Sharkninjaoperatingllc` | 41 rows |
| `Columbiauniversity` | 5 rows |
| `Morganmorganjobsapplynow` | 5 rows |
| Same employer key, multiple spellings | 0 |
| Canonical URLs spanning >1 `(company, role, location)` | 0 |
| `(ats_type, ats_id)` spanning >1 `(company, role, location)` | 0 |
| Titles all-caps with 3+ words | 108 / 13,177 |
| Titles longer than 90 chars | 211 (17 over 120, max 253) |
| Titles with doubled spaces | 147 |
| Titles with dangling separators | 51 |
| Titles containing HTML tags/entities | 0 |

Two conclusions:

1. **The visible damage is naming, not duplication.** No two published
   employers shared an alphanumeric key, and no URL or requisition id covered
   two distinct jobs. The feed was not full of duplicates; it was full of
   unrecognizable spellings of the same employers.
2. **Dedup was not applying identity consistently.** `dedup.dedup_keys` keyed the
   fallback branch on the raw company string, so the same job arriving as
   `Andurilindustries` and `Anduril Industries` could not collapse. At write
   time, only the canonical URL was consulted, ignoring the requisition id.

## Employer names

New module `Listings/company_names.py` returns `(display, key, confidence,
reason, domain)` under three rules:

* **Verified aliases win.** 31 measured spellings map to a canonical name and,
  where known, the official domain (`Anduril Industries`/`anduril.com`,
  `SharkNinja`/`sharkninja.com`, `Morgan & Morgan`/`forthepeople.com`). Canonical
  spellings are fixed points, so a shouting brand (`ABOUT YOU`) is not recased.
* **Separator-free slugs split only on a closed vocabulary** of corporate
  descriptors and locations (`industries`, `university`, `healthcare`,
  `management`, `canada`, `usa`, ...). Unknown concatenations keep their
  published spelling with `confidence="unchanged"`.
* **Case is repaired, never reworded.** Only all-lower or all-caps names are
  recased; mixed-case names (`iOS`, `McKinsey & Company`) are untouched.

Effect on the audited feed: **331 of 2,786 employers normalized** — 31 verified
aliases, 208 descriptor splits, 92 board/legal trims — with 2,454 left exactly as
published (most already human-readable). No display gained a doubled space.

One bug was caught during measurement and pinned by a test: stripping two-letter
legal tails turned `Apogeeusa` into `Apogeeu`. Only tails of three characters or
more are removed now (`BGE Inc` → `BGE` still works).

Records gained `company_key` (stable across aliases), `company_domain` (verified
domain or null) and `company_raw` (the published value, present only when the
display changed).

## Titles

`readme_utils.clean_role` unescapes entities, strips markup and zero-width
characters, collapses whitespace, removes dangling separators, and repairs case
**only when the whole title is uppercase**. No token is added, removed or
reordered, so internship / co-op / level / location distinctions survive.

The first version used a majority-caps rule; it rewrote
`URO R&D Co-op (MAY- DEC) 2027` as `Uro R&d Co-op (may- Dec) 2027`. That was
replaced with the all-caps rule, and the fixture now pins it. Measured: 335 of
13,177 titles cleaned, 0 mixed-case titles altered, longest title still 253
characters (data is never truncated).

README tables use `role_cell`, which keeps the existing wrap CSS and adds a
`title` tooltip carrying the full title when the 48-character column clip
applies. The JSON feed always carries the full title, so a detail view can show
it without truncation.

## Logos

The feed carried no logo field at all, so any frontend logo lookup had to key on
the raw company string — which is exactly the string that was broken. This repo
cannot verify logo assets or render the frontend. What it now provides:

* `company_key` — one value per employer across every alias, suitable as the
  logo/company-page lookup key.
* `company_domain` — the official domain, only for verified aliases. Never
  derived from an ATS or aggregator link, because a logo fetched from
  `greenhouse.io` would be the wrong brand.

A clean fallback (initials tile) and alias-to-branding resolution still need to
be implemented in the frontend repo; the pipeline deliberately emits no logo URL
it cannot verify.

## Duplicates

* `dedup.dedup_keys` now keys the fallback branch on `company_names.key`, so
  spelling variants of one employer are one identity.
* Requisition id still outranks URL, and is now tenant-scoped exactly as
  `identity.py` defines it.
* At write time a row is a duplicate if **either** its id or its canonical URL
  was already seen. The URL check is retained because a mirrored posting can
  carry conflicting ids between sources; the id check catches the same
  requisition behind different tracker links.
* Genuinely different jobs are preserved: separate locations, separate
  requisitions behind one tracker URL, and near-identical employers
  (`General Dynamics` vs `General Dynamics Mission Systems`) all stay distinct.

## Verification

* `pytest -q` → **257 passed** (new: `test_display_quality.py`, plus dedup
  fixtures in `test_dedup.py`; updated shadow fixture reason counting).
* `ruff check` on every changed file → clean.
* `git diff --check` → clean.
* Real-data check over the published feed: 331 employers normalized, 335 titles
  cleaned, 0 mixed-case titles mangled, no display with doubled spaces.

## Limits

* Republishing was not run; the new fields and names take effect on the next
  generation. No staging audit was repeated for this change.
* 19 normalized displays still contain a long concatenated first token (for
  example `Agnesirwin School`); the vocabulary does not know every brand
  boundary, and guessing one is worse than leaving the raw value.
* Majority-caps titles that are not wholly uppercase are intentionally left
  alone.
* Title wrapping, detail views and logo fallbacks are frontend work; this repo
  only guarantees the JSON contract and README tooltips.
