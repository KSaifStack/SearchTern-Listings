"""Published coverage report: career families, field coverage, and source yield.

The feed can answer "how many rows" but not "what kind of work, in which
industry, from which source, with how many fields filled". This builds that
payload from the exact records the publication is about to write, so the numbers
cannot describe a different generation than the feed beside them.

Two words are kept strictly apart:

* **snapshot_fresh** — the row's source snapshot was re-fetched inside its TTL
  this run. It says nothing about whether the employer still lists the job.
* **liveness** — a URL was actually requested and answered (or did not). Only
  this block is evidence that a posting exists.

The old payload called snapshot freshness "verified" and reported 100%, which
read as a liveness claim the pipeline never made (audit 2026-11-13).

Deliberately no quotas and no goals: the report describes the supply that
exists. Gaps are visible as small family counts, not filled by inventing rows.
"""

import collections
from datetime import UTC, datetime

import taxonomy

# Fields the feed advertises. A consumer that filters or claims on one of these
# needs its fill rate next to the count, not a caveat discovered after the fact.
COVERAGE_FIELDS = (
    "id", "company", "company_key", "role", "location", "date", "link",
    "description", "salary_min", "first_seen_at", "country_iso", "job_type",
    "ats_id", "closed_at",
)

_EMPTY = {"", "nan", "none", "null", "nat", "unknown", "n/a", "na", "-"}

LIVENESS_POLICY = (
    "liveness = the posting URL was requested this run (new rows plus a rotating "
    "stale slice) and answered. checked=0 means no evidence was gathered this "
    "run, not that every row is alive."
)


def _filled(value):
    """True when a value carries information (not blank, NaN or a placeholder)."""
    text = str(value if value is not None else "").strip()
    return text.lower() not in _EMPTY


def _entry(name, total, verified, all_rows, fallback=False):
    share = round(100.0 * total / all_rows, 2) if all_rows else 0.0
    row = {
        "name": name,
        "total": total,
        "snapshot_fresh": verified,
        "share_pct": share,
        "snapshot_fresh_pct": round(100.0 * verified / total, 2) if total else 0.0,
    }
    if fallback:
        # "Other Professional" is a published family, not a failure bucket.
        # A reader must be able to tell it apart without guessing from its name.
        row["fallback"] = True
    return row


def _field_rows(records):
    total = len(records) or 1
    out = []
    for field in COVERAGE_FIELDS:
        filled = sum(1 for record in records if _filled(record.get(field)))
        out.append({"field": field, "filled": filled,
                    "pct": round(100.0 * filled / total, 2)})
    return out


def build(manifest, mirror_records, listing_records=None, rejection_reasons=None,
          liveness=None):
    """Build the coverage payload for one publication.

    ``manifest`` supplies the generation id and the published counts; the
    record lists are the post-filter rows that were actually written.
    ``liveness`` is ``{"checked": int, "dead": int}`` from the run's URL checks.
    """
    records = list(mirror_records or [])
    listings = list(listing_records or [])
    total = len(records)

    families = collections.Counter()
    family_verified = collections.Counter()
    industries = collections.Counter()
    industry_verified = collections.Counter()
    job_types = collections.Counter()
    sources = collections.Counter()
    source_verified = collections.Counter()
    low_confidence = 0
    verified = 0

    for record in records:
        family = str(record.get("career_family") or taxonomy.FALLBACK_FAMILY)
        family_members = record.get("career_families") or [family]
        families[family] += 1
        # A cross-listed role counts once in its primary family and once in the
        # secondary-family tally, so shares can exceed 100% only there.
        for extra in family_members[1:]:
            families[str(extra)] += 1
        industry = str(record.get("industry") or "Unclassified")
        industries[industry] += 1
        job_types[str(record.get("job_type") or "unknown")] += 1
        source = str(record.get("source") or "unknown")
        sources[source] += 1
        if str(record.get("career_confidence") or "") == "low":
            low_confidence += 1
        if str(record.get("verification_status") or "") == "verified":
            verified += 1
            family_verified[family] += 1
            industry_verified[industry] += 1
            source_verified[source] += 1

    family_rows = [
        _entry(name, families[name], family_verified[name], total,
               fallback=name == taxonomy.FALLBACK_FAMILY)
        for name in taxonomy.CAREER_FAMILIES
        if families.get(name)
    ]
    family_rows.sort(key=lambda row: (-row["total"], row["name"]))

    industry_rows = [
        _entry(name, count, industry_verified[name], total)
        for name, count in industries.most_common()
    ]
    source_rows = [
        {"source": name, "rows": count, "share_pct": round(100.0 * count / total, 2),
         "snapshot_fresh": source_verified[name]}
        for name, count in sources.most_common()
    ]

    liveness = liveness or {}
    checked = int(liveness.get("checked") or 0)
    dead = int(liveness.get("dead") or 0)

    published = (manifest or {}).get("published") or {}
    # Match the legacy count artifacts exactly (row counts for the two feeds),
    # so audit() can compare this file against run_meta/health/source_stats
    # without a second shape.
    published_counts = {
        name: int((published.get(name) or {}).get("rows") or 0)
        for name in ("listings", "searchtern") if isinstance(published.get(name), dict)
    }
    run_id = str((manifest or {}).get("run_id") or "")
    payload = {
        "scheme": "career-family-v1",
        "generation": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "run": {
            "generation": run_id,
            "published": published_counts,
            "publication_status": "published",
        },
        "totals": {
            "published": total,
            "technology_subset": len(listings),
            "snapshot_fresh": verified,
            "republished": total - verified,
            # The fallback family is a real family, so it is counted as one.
            "career_families": len(family_rows),
            "other_professional": families.get(taxonomy.FALLBACK_FAMILY, 0),
            "low_confidence": low_confidence,
            "liveness_checked": checked,
            "liveness_dead": dead,
        },
        "verification": {
            "snapshot_fresh": verified,
            "republished": total - verified,
            "snapshot_fresh_pct": round(100.0 * verified / total, 2) if total else 0.0,
            "liveness": {"checked": checked, "dead": dead,
                         "policy": LIVENESS_POLICY},
            "policy": ("snapshot_fresh = the row's source snapshot was re-fetched "
                       "inside its TTL this run; republished = served from a "
                       "still-valid snapshot without a fresh fetch. Neither is a "
                       "claim of employer-side liveness — see verification.liveness."),
        },
        "fields": _field_rows(records),
        "career_families": family_rows,
        "industries": industry_rows,
        "job_types": dict(job_types.most_common()),
        "sources": source_rows,
        "rejections": dict(rejection_reasons or {}),
        "inputs": {"snapshots": (manifest or {}).get("inputs", {}).get("snapshots", {})},
    }
    return payload
