"""Published coverage report: career families, verification, and source yield.

The feed can answer "how many rows" but not "what kind of work, in which
industry, verified how often, from which source". This builds that payload from
the exact records the publication is about to write, so the numbers cannot
describe a different generation than the feed beside them.

Deliberately no quotas and no goals: the report describes the supply that
exists. Gaps are visible as small family counts, not filled by inventing rows.
"""

import collections
from datetime import UTC, datetime

import taxonomy


def _entry(name, total, verified, all_rows):
    share = round(100.0 * total / all_rows, 2) if all_rows else 0.0
    return {
        "name": name,
        "total": total,
        "verified": verified,
        "share_pct": share,
        "verified_pct": round(100.0 * verified / total, 2) if total else 0.0,
    }


def build(manifest, mirror_records, listing_records=None, rejection_reasons=None):
    """Build the coverage payload for one publication.

    ``manifest`` supplies the generation id and the published counts; the
    record lists are the post-filter rows that were actually written.
    """
    records = list(mirror_records or [])
    listings = list(listing_records or [])
    total = len(records)

    families = collections.Counter()
    family_verified = collections.Counter()
    industries = collections.Counter()
    job_types = collections.Counter()
    sources = collections.Counter()
    source_verified = collections.Counter()
    verified = 0

    for record in records:
        family = str(record.get("career_family") or taxonomy.FALLBACK_FAMILY)
        family_members = record.get("career_families") or [family]
        families[family] += 1
        # A cross-listed role counts once in its primary family and once in the
        # secondary-family tally, so shares can exceed 100% only there.
        for extra in family_members[1:]:
            families[str(extra)] += 1
        industries[str(record.get("industry") or "Unclassified")] += 1
        job_types[str(record.get("job_type") or "unknown")] += 1
        source = str(record.get("source") or "unknown")
        sources[source] += 1
        if str(record.get("verification_status") or "") == "verified":
            verified += 1
            family_verified[family] += 1
            source_verified[source] += 1

    family_rows = [
        _entry(name, families[name], family_verified[name], total)
        for name in taxonomy.CAREER_FAMILIES
        if families.get(name)
    ]
    family_rows.sort(key=lambda row: (-row["total"], row["name"]))

    industry_rows = [
        _entry(name, count, 0, total)
        for name, count in industries.most_common()
    ]
    source_rows = [
        {"source": name, "rows": count, "share_pct": round(100.0 * count / total, 2),
         "verified": source_verified[name]}
        for name, count in sources.most_common()
    ]

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
            "verified": verified,
            "observed": total - verified,
            "career_families": len([row for row in family_rows
                                    if row["name"] != taxonomy.FALLBACK_FAMILY]),
            "unclassified": families.get(taxonomy.FALLBACK_FAMILY, 0),
        },
        "verification": {
            "verified": verified,
            "observed": total - verified,
            "verified_pct": round(100.0 * verified / total, 2) if total else 0.0,
            "policy": ("verified = the row's source snapshot was re-fetched inside "
                       "its TTL this run; observed = republished from a still-valid "
                       "snapshot without a fresh fetch. Neither is a claim of "
                       "employer-side liveness."),
        },
        "career_families": family_rows,
        "industries": industry_rows,
        "job_types": dict(job_types.most_common()),
        "sources": source_rows,
        "rejections": dict(rejection_reasons or {}),
        "inputs": {"snapshots": (manifest or {}).get("inputs", {}).get("snapshots", {})},
    }
    return payload
