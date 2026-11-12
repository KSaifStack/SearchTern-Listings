import collections
import contextlib
import glob
import hashlib
import html
import json
import math
import os
from datetime import UTC, datetime
from email.utils import format_datetime

import atomic_fs
import company_names
import coverage as coverage_report
import feed_manifest
import identity
import readme_rendering
import taxonomy
from readme_rendering import build_header, generate_country_pages
from readme_utils import (
    APPLY_BUTTON,
    GITHUB_FILE_SIZE_LIMIT,
    INACTIVE_THRESHOLD_DAYS,
    MAX_COMPANY_LEN,
    NORMALIZED_BLOCKED_COMPANIES,
    SIZE_BUFFER,
    build_table,
    canonical_url,
    clean_company_name,
    clean_link,
    clean_location,
    clean_role,
    days_display,
    format_company,
    format_location,
    is_english,
    normalize_company,
    role_cell,
    truncate,
    visa_cell,
)

_atomic_write_pairs = atomic_fs.write_pairs
_atomic_write_text = atomic_fs.write_text


def _json_safe(value):
    """NaN and NaT compare unequal to themselves; emit them as null so the
    output is valid JSON instead of the non-standard `NaN`/`NaT` tokens."""
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        pass
    return value


def _build_json_records(dataframe, reasons=None, max_age_days=INACTIVE_THRESHOLD_DAYS):
    """Apply the shared final filters and return the JSON-ready records.

    `reasons` is an optional counter: when given, every refused row bumps the
    predicate that refused it. Production passes nothing. The shadow publisher
    does, because "27,366 rows dropped" is not an explanation and "21,235 of them
    had no apply link" is.

    `max_age_days=None` keeps every row still present in an eligible snapshot:
    the general feed must not delete an old-but-active posting just because it
    passed a fixed age. The technology subset keeps its documented 60-day
    window for compatibility with existing consumers.
    """
    clean_records = []
    seen_urls = set()
    seen_ids = set()
    identities = list(zip(*identity.pairs(dataframe), strict=False)) if len(dataframe) else []

    def drop(cause):
        if reasons is not None:
            reasons[cause] = reasons.get(cause, 0) + 1

    for position, (_, row) in enumerate(dataframe.iterrows()):
        company = str(row["company"]).strip()
        source_role = str(row["role"]).strip()
        role = clean_role(source_role)
        date = str(row["date"]).strip()
        link = clean_link(row.get("link"))

        if not link.startswith(("http://", "https://")):
            drop("no_apply_link")
            continue

        row = row.copy()
        row["link"] = link

        cleaned_company = clean_company_name(company)
        if not cleaned_company:
            drop("empty_company")
            continue
        if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
            drop("blocked_company")
            continue
        if not is_english(role) or not is_english(cleaned_company):
            drop("non_english")
            continue

        if max_age_days is not None:
            try:
                posted = datetime.fromisoformat(date.replace("Z", "+00:00"))
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=UTC)
                if (datetime.now(UTC) - posted).days > max_age_days:
                    drop("inactive")
                    continue
            except Exception:
                pass

        record = row.to_dict()
        # Keep JSON strict: NaN/Inf → null, or strict parsers (browser
        # fetch().json()) hard-fail on the whole feed.
        for k, v in record.items():
            if isinstance(v, float) and not math.isfinite(v):
                record[k] = None
        employer = company_names.normalize(cleaned_company)
        record["company"] = employer.display
        record["company_key"] = employer.key
        record["company_domain"] = employer.domain or None
        if employer.display != cleaned_company:
            record["company_raw"] = company
        record["role"] = role
        if role != source_role:
            record["role_raw"] = source_role
        # Industry is classified after eligibility, never instead of it. The
        # old code used one technology title filter for both decisions, so a
        # nursing or finance internship could only enter by looking technical.
        tax = taxonomy.classify(role)
        record["category"] = tax["subcategory"]
        record["career_family"] = tax["career_family"]
        record["industry"] = tax["industry"]
        record["career_families"] = tax["career_families"]
        record["career_confidence"] = tax["confidence"]
        record["career_reason"] = tax["reason"]
        record["location"] = clean_location(record.get("location", ""))
        record["source_posted_at"] = str(record.get("date") or "")
        record["closed_at"] = ""
        record["last_verified_at"] = str(record.get("verified_at") or "")
        record["verification_status"] = (
            "verified" if record["last_verified_at"].strip() else "observed"
        )
        record = {k: _json_safe(v) for k, v in record.items()}

        # Identity first, address second: the same requisition reaches us through
        # a tracker and the employer's ATS, and one address can serve several
        # locations of one requisition. A link whose canonical form is empty is
        # not a duplicate of every other such link (7,920 markdown rows collapsed
        # onto one row otherwise, 2026-11-07).
        ats_type, ats_id = identities[position] if position < len(identities) else ("", "")
        ats_type = str(ats_type or "").strip().lower()
        ats_id = str(ats_id or "").strip().lower()
        # Either signal wins: one requisition reached through two sources shares
        # an id even when the tracker URL differs, and a mirrored row keeps the
        # same apply URL even when the sources publish conflicting ids (the
        # shadow fixture asserts that second case).
        id_key = f"id|{ats_type}|{ats_id}" if ats_type and ats_id else ""
        url_key = canonical_url(link)
        if (id_key and id_key in seen_ids) or (url_key and url_key in seen_urls):
            drop("duplicate_id" if id_key and id_key in seen_ids else "duplicate_url")
            continue
        if id_key:
            seen_ids.add(id_key)
        if url_key:
            seen_urls.add(url_key)
        clean_records.append(record)

    return clean_records


class PublicationRejected(RuntimeError):
    """The feed failed its pre-write gate; the previous artifacts are untouched."""


def _read_manifest(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _json_text(records):
    return json.dumps(records, indent=2, ensure_ascii=False, default=str, allow_nan=False)


def prepare_listings_json(dataframe, output_dir=".", mirror_dataframe=None,
                          snapshots_report=None, required_sources=(), minimum_rows=1,
                          mirror_max_age_days=None):
    """Build the feed and manifest text and gate them, without writing anything.

    Everything a publication needs is decided here: the final row filters, the
    required-source/empty gates, the previous generation preserved for recovery,
    and the generation id that README and the legacy artifacts must carry.
    """
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    listing_reasons = collections.Counter()
    mirror_reasons = collections.Counter()
    clean_records = _build_json_records(dataframe, reasons=listing_reasons)

    export_path = os.path.join(pages_dir, "listings.json")

    # The deployed backend scrapes pages/searchtern.json (not listings.json),
    # so write it in the same run instead of syncing by hand (a manual sync
    # once shipped an uncategorized copy and broke the app's role-category
    # filter). When a `mirror_dataframe` is supplied, searchtern.json is the
    # broader feed — every role type, not just the CS/tech subset.
    mirror_records = (
        clean_records if mirror_dataframe is None
        else _build_json_records(mirror_dataframe, reasons=mirror_reasons,
                                 max_age_days=mirror_max_age_days)
    )
    mirrored_path = os.path.join(pages_dir, "searchtern.json")
    manifest_path = os.path.join(pages_dir, "feed-manifest.json")

    source_counts = collections.Counter(
        str(row.get("source") or "unknown") for row in mirror_records
    )
    # Fail before staging a single byte. The old feed and manifest remain the
    # last-known-good pair, which is the only safe answer to an empty/critical-
    # source publish.
    if len(mirror_records) < int(minimum_rows):
        raise PublicationRejected(
            f"refusing to publish {len(mirror_records)} rows (minimum {minimum_rows})"
        )
    missing_required = [name for name in required_sources
                        if source_counts.get(name, 0) <= 0]
    if missing_required:
        raise PublicationRejected(
            "refusing to publish without required source(s): "
            + ", ".join(missing_required)
        )

    listing_text = _json_text(clean_records)
    mirror_text = _json_text(mirror_records)
    snapshot_summary = {
        "generated_at": (snapshots_report or {}).get("generated_at"),
        "count": int((snapshots_report or {}).get("count", 0)),
        "expected": list((snapshots_report or {}).get("expected", []) or []),
        "fresh": list((snapshots_report or {}).get("fresh", []) or []),
        "expired": list((snapshots_report or {}).get("expired", []) or []),
        "empty": list((snapshots_report or {}).get("empty", []) or []),
        "missing": list((snapshots_report or {}).get("missing", []) or []),
        "rejected": list((snapshots_report or {}).get("rejected", []) or []),
        "invalid": list((snapshots_report or {}).get("invalid", []) or []),
        "seeded": list((snapshots_report or {}).get("seeded", []) or []),
    }
    degraded = bool(snapshot_summary["missing"] or snapshot_summary["expired"]
                    or snapshot_summary["rejected"] or snapshot_summary["invalid"]
                    or snapshot_summary["empty"])
    # Preserve the verified previous generation before replacing it. If the
    # previous feed is torn, prepare() restores its physical LKG first.
    feed_manifest.prepare(pages_dir)
    previous = _read_manifest(manifest_path)
    previous_published = previous.get("published") if isinstance(
        previous.get("published"), dict) else {}
    last_known_good = (
        {"generated_at": previous.get("generated_at"),
         "run_id": previous.get("run_id"),
         "published": previous_published,
         "path": f"pages/{feed_manifest.LKG_DIR}",
         "manifest": f"pages/{feed_manifest.LKG_DIR}/{feed_manifest.LKG_CURRENT}"}
        if previous_published else None
    )

    # A partial or failed run must never replace a larger valid publication with
    # a smaller one: the snapshot ledger can be empty on a fresh checkout, in
    # which case every cached source is uneligible and the run only knows what it
    # fetched live. Refusing the write keeps the previous generation intact and
    # reports the reason, instead of publishing a 9k-row feed over a 30k-row one
    # (the 2026-11-07..09 swing). SEARCHTERN_MIN_RETAINED=0 disables the gate.
    previous_rows = int((previous_published.get("searchtern") or {}).get("rows") or 0)
    floor = float(os.environ.get("SEARCHTERN_MIN_RETAINED", "0.6") or 0.6)
    if floor > 0 and previous_rows >= 500 and len(mirror_records) < previous_rows * floor:
        raise PublicationRejected(
            f"refusing to replace {previous_rows:,} published rows with "
            f"{len(mirror_records):,} (retention floor {floor:.0%}); previous "
            "generation left intact"
        )

    generated_at = datetime.now(UTC)
    mirror_digest = hashlib.sha256(mirror_text.encode("utf-8")).hexdigest()
    run_id = feed_manifest.make_run_id(generated_at, mirror_digest)
    delivery = feed_manifest.delivery_descriptor()
    release = delivery["mode"] == "release"
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": generated_at.isoformat(),
        "status": "degraded" if degraded else "ok",
        "publication_status": "published",
        "delivery": delivery,
        "published": {
            "listings": {
                "path": "pages/listings.json",
                "rows": len(clean_records),
                "bytes": len(listing_text.encode("utf-8")),
                "sha256": hashlib.sha256(listing_text.encode("utf-8")).hexdigest(),
                "url": (feed_manifest.asset_url("listings.json")
                        if release else None),
            },
            "searchtern": {
                "path": "pages/searchtern.json",
                "rows": len(mirror_records),
                "bytes": len(mirror_text.encode("utf-8")),
                "sha256": mirror_digest,
                "url": (feed_manifest.asset_url("searchtern.json")
                        if release else None),
            },
        },
        "required_sources": list(required_sources),
        "last_known_good": last_known_good,
        "recovery": {
            "path": f"pages/{feed_manifest.LKG_DIR}",
            "manifest": f"pages/{feed_manifest.LKG_DIR}/{feed_manifest.LKG_CURRENT}",
        },
        "source_counts": dict(sorted(source_counts.items())),
        "inputs": {"snapshots": snapshot_summary},
        "coverage": {"path": "pages/coverage.json", "scheme": "career-family-v1"},
    }
    # Every published generation carries its own industry/verification report,
    # built from the records written beside it — never from a separate fetch.
    coverage_payload = coverage_report.build(
        manifest, mirror_records, clean_records,
        rejection_reasons={
            **{f"searchtern:{k}": v for k, v in mirror_reasons.items()},
            **{f"listings:{k}": v for k, v in listing_reasons.items()},
        },
    )
    manifest["career_families"] = {
        row["name"]: row["total"] for row in coverage_payload["career_families"]
    }

    return {
        "pairs": [
            (export_path, listing_text),
            (mirrored_path, mirror_text),
            (manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"),
            (os.path.join(pages_dir, "coverage.json"),
             json.dumps(coverage_payload, indent=2, ensure_ascii=False) + "\n"),
        ],
        "manifest": manifest,
        "counts": {"listings": len(clean_records), "searchtern": len(mirror_records)},
        "mirror_records": mirror_records,
        "coverage": coverage_payload,
        "pages_dir": pages_dir,
    }


def write_listings_json(dataframe, output_dir=".", mirror_dataframe=None,
                        snapshots_report=None, required_sources=(), minimum_rows=1,
                        mirror_max_age_days=None):
    """Write the feed pair and manifest in one atomic transaction.

    Kept as the feed-only entry point used by tests and the shadow tooling. A
    production run uses `publish_generation`, which puts the README, country
    pages and reconciled count artifacts in the same transaction.
    """
    prepared = prepare_listings_json(
        dataframe, output_dir=output_dir, mirror_dataframe=mirror_dataframe,
        snapshots_report=snapshots_report, required_sources=required_sources,
        minimum_rows=minimum_rows, mirror_max_age_days=mirror_max_age_days,
    )
    _atomic_write_pairs(prepared["pairs"])

    pages_dir = prepared["pages_dir"]
    print(f"Wrote {os.path.join(pages_dir, 'listings.json')} "
          f"— {prepared['counts']['listings']:,} listings")
    print(f"Wrote {os.path.join(pages_dir, 'searchtern.json')} "
          f"— {prepared['counts']['searchtern']:,} listings")
    print(f"Wrote {os.path.join(pages_dir, 'feed-manifest.json')} "
          f"— {prepared['counts']['searchtern']:,} published rows")
    return prepared["counts"]


def render_readme_pages(dataframe, output_dir=".", generation=None):
    """Render every README page without touching disk.

    Returns ``(pending, stale, stats)``: `pending` is ``[(path, content)]``,
    `stale` is the old page paths this generation does not include, and `stats`
    carries the counters `generate_readme` prints.
    """
    pages_dir = os.path.join(output_dir, "pages")
    old_pages = (glob.glob(os.path.join(pages_dir, "README-*.md"))
                 if os.path.exists(pages_dir) else [])

    all_rows = []
    prev_company = None
    prev_age = None
    skipped_blocked = 0
    skipped_inactive = 0
    skipped_foreign = 0
    skipped_oracle = 0

    for _, row in dataframe.iterrows():
        company = str(row["company"]).strip()
        role = clean_role(str(row["role"]).strip())
        location = str(row["location"]).strip()
        date = str(row["date"]).strip()
        link = clean_link(row.get("link"))

        cleaned_company = clean_company_name(company)
        if not cleaned_company:
            skipped_oracle += 1
            continue

        if not link:
            skipped_oracle += 1
            continue

        if normalize_company(cleaned_company) in NORMALIZED_BLOCKED_COMPANIES:
            skipped_blocked += 1
            continue

        if not is_english(role) or not is_english(cleaned_company):
            skipped_foreign += 1
            continue

        try:
            posted = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=UTC)
            if (datetime.now(UTC) - posted).days > INACTIVE_THRESHOLD_DAYS:
                skipped_inactive += 1
                continue
        except Exception:
            pass

        employer = company_names.normalize(cleaned_company)
        company = truncate(employer.display, MAX_COMPANY_LEN)
        full_company = employer.display if company != employer.display else ""
        age = days_display(date)
        company_cell = format_company(company, prev_company, age, prev_age, link,
                                      title=full_company)
        prev_company = company
        prev_age = age

        all_rows.append(
            "<tr>\n"
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{company_cell}</td>\n'
            f'{role_cell(role)}\n'
            f'<td style="word-break:break-word; overflow-wrap:anywhere;">{format_location(location)}</td>\n'
            f'<td align="center" style="white-space:nowrap; overflow-wrap:anywhere; width:8%;"><a href="{html.escape(link, quote=True)}" style="display:inline-block; max-width:100%;"><img src="{APPLY_BUTTON}" width="60" alt="Apply" style="display:block; max-width:100%; height:auto; margin:0 auto;"></a></td>\n'
            f'<td style="white-space:nowrap;">{age}</td>\n'
            f'<td style="text-align:center; white-space:nowrap;">{visa_cell(row)}</td>\n'
            "</tr>"
        )

    total_rows = len(all_rows)

    pages = []
    current_page_rows = []
    current_size = 0

    for row_html in all_rows:
        row_size = len(row_html.encode("utf-8"))
        if current_size + row_size > (GITHUB_FILE_SIZE_LIMIT - SIZE_BUFFER) and current_page_rows:
            pages.append(current_page_rows)
            current_page_rows = [row_html]
            current_size = row_size
        else:
            current_page_rows.append(row_html)
            current_size += row_size

    if current_page_rows:
        pages.append(current_page_rows)

    total_pages = len(pages)
    marker = feed_manifest.marker(generation)
    pending = []
    written = []

    for i, page_rows in enumerate(pages):
        page_num = i + 1
        filepath = (
            os.path.join(output_dir, "README.md")
            if page_num == 1
            else os.path.join(pages_dir, f"README-{page_num}.md")
        )

        nav = build_header(page_num, total_pages, total_rows, dataframe if page_num == 1 else None)
        content = (nav + build_table(page_rows)
                   + f"\n\n{__import__('readme_rendering').build_nav(page_num, total_pages)}\n"
                   + marker + "\n")
        pending.append((filepath, content))
        written.append(filepath)
        size_kb = len(content.encode("utf-8")) / 1024
        print(f"Staged {filepath} — {len(page_rows):,} rows, {size_kb:.1f} KB")

    written_set = {os.path.abspath(path) for path in written}
    stale = [path for path in old_pages if os.path.abspath(path) not in written_set]
    stats = {
        "total_rows": total_rows,
        "total_pages": total_pages,
        "skipped_oracle": skipped_oracle,
        "skipped_blocked": skipped_blocked,
        "skipped_inactive": skipped_inactive,
        "skipped_foreign": skipped_foreign,
    }
    return pending, stale, stats


def generate_readme(dataframe, output_dir=".", generation=None):
    """
    Writes job listings into README files split by GitHub's render limit.
    Page 1 → README.md (root)
    Page 2+ → pages/README-2.md, pages/README-3.md, etc.
    """
    pending, stale, stats = render_readme_pages(dataframe, output_dir, generation)

    # Replace every README page together so a failure cannot leave a mix of old
    # and new tables. Only after the new pages are live do stale pages vanish.
    _atomic_write_pairs(pending)
    for old_file in stale:
        with contextlib.suppress(OSError):
            os.remove(old_file)

    generate_country_pages(dataframe, output_dir, generation)

    print(f"\nTotal rows written  : {stats['total_rows']:,}")
    print(f"Pages created       : {stats['total_pages']}")
    print(f"Skipped (Oracle URLs): {stats['skipped_oracle']}")
    print(f"Skipped (blocked)   : {stats['skipped_blocked']}")
    print(f"Skipped (inactive)  : {stats['skipped_inactive']}")
    print(f"Skipped (foreign)   : {stats['skipped_foreign']}")

    return [path for path, _content in pending]


def publish_generation(listings_frame, mirror_frame=None, readme_frame=None,
                       output_dir=".", snapshots_report=None, required_sources=(),
                       minimum_rows=1, include_feed_xml=True,
                       mirror_max_age_days=None):
    """Publish one coherent generation: feed, manifest, README, health, RSS.

    Every file that describes the publication is staged and replaced in one
    transaction. A crash during any stage leaves the previous generation
    byte-identical; a failure after the commit restores the previous generation
    from the physical last-known-good copy. `audit()` then proves README,
    manifest, health, and feed all carry the same generation id.
    """
    prepared = prepare_listings_json(
        listings_frame, output_dir=output_dir, mirror_dataframe=mirror_frame,
        snapshots_report=snapshots_report, required_sources=required_sources,
        minimum_rows=minimum_rows, mirror_max_age_days=mirror_max_age_days,
    )
    pages_dir = prepared["pages_dir"]
    generation = prepared["manifest"]["run_id"]
    readme_source = readme_frame if readme_frame is not None else listings_frame

    readme_pairs, readme_stale, readme_stats = render_readme_pages(
        readme_source, output_dir, generation)
    country_pairs, country_stale, _country_written = readme_rendering.render_country_pages(
        readme_source, output_dir, generation)

    # The manifest also describes the README surface it was published beside,
    # so a reader can tell "the README is a different window" from "the README
    # was not regenerated".
    manifest = prepared["manifest"]
    manifest["published"]["readme"] = {
        "path": "README.md",
        "window_days": 60,
        "rows": readme_stats["total_rows"],
        "pages": readme_stats["total_pages"],
        "generation": generation,
    }
    feed_pairs = [
        (path, text) for path, text in prepared["pairs"]
        if not path.endswith(feed_manifest.MANIFEST_NAME)
    ]
    feed_pairs.append((
        os.path.join(pages_dir, feed_manifest.MANIFEST_NAME),
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    ))

    extra = list(readme_pairs) + list(country_pairs)
    if include_feed_xml:
        extra.append((os.path.join(pages_dir, "feed.xml"),
                      render_feed_xml(prepared["mirror_records"], generation=generation)))
    legacy = feed_manifest.build_reconciled(pages_dir, manifest)
    extra.extend(feed_manifest.json_pairs(legacy))

    _atomic_write_pairs(feed_pairs + extra)
    for stale in (*readme_stale, *country_stale):
        with contextlib.suppress(OSError):
            os.remove(stale)

    try:
        feed_manifest.audit(output_dir)
    except Exception:
        if feed_manifest.has_last_known_good(pages_dir):
            feed_manifest.recover(pages_dir)
        raise

    print(f"Wrote {os.path.join(pages_dir, 'listings.json')} "
          f"— {prepared['counts']['listings']:,} listings")
    print(f"Wrote {os.path.join(pages_dir, 'searchtern.json')} "
          f"— {prepared['counts']['searchtern']:,} listings")
    print(f"Wrote {os.path.join(pages_dir, 'feed-manifest.json')} "
          f"— {prepared['counts']['searchtern']:,} published rows")
    families = prepared["coverage"]["totals"]
    print(f"Coverage: {families['career_families']} career families, "
          f"{families['unclassified']:,} unclassified, "
          f"{families['verified']:,}/{families['published']:,} verified this run")
    print(f"README: {readme_stats['total_rows']:,} rows over "
          f"{readme_stats['total_pages']} page(s), generation {generation}")
    return prepared["manifest"]


def write_priority_json(dataframe, output_dir="."):
    """Write the small, high-frequency "fast lane" feed.

    Only the curated priority boards' recent postings live here, so a workflow
    can refresh it every few minutes without committing the 38MB searchtern.json.
    Output is sorted and stripped of volatile fields (observed_at, which is
    restamped every run) so the file only changes when a posting actually does —
    otherwise the fast-lane workflow would commit churn on every cycle.
    """
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    if dataframe is None or dataframe.empty:
        records = []
    else:
        records = _build_json_records(dataframe)
    for record in records:
        record.pop("observed_at", None)

    records.sort(key=lambda r: (
        str(r.get("company", "")).lower(),
        str(r.get("role", "")).lower(),
        str(r.get("location", "")).lower(),
        str(r.get("link", "")),
    ))

    path = os.path.join(pages_dir, "priority.json")
    _atomic_write_text(
        path, json.dumps(records, indent=2, ensure_ascii=False,
                         default=str, allow_nan=False)
    )

    print(f"Wrote {path} — {len(records):,} priority listings")


def _feed_time(record):
    try:
        value = datetime.fromisoformat(str(record.get("date", "")).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value
    except ValueError:
        return datetime.now(UTC)


def render_feed_xml(records, limit=200, generation=None):
    """Render a newest-first RSS 2.0 body from already-filtered records."""
    records = sorted(records, key=_feed_time, reverse=True)[:limit]

    items = []
    for record in records:
        title = f"{record['role']} at {record['company']}"
        bits = [b for b in (record.get("location"), record.get("category"),
                            record.get("job_type")) if b]
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(title)}</title>\n"
            f"      <link>{html.escape(record['link'])}</link>\n"
            f"      <guid isPermaLink=\"true\">{html.escape(record['link'])}</guid>\n"
            f"      <pubDate>{format_datetime(_feed_time(record))}</pubDate>\n"
            f"      <description>{html.escape(' | '.join(str(b) for b in bits))}</description>\n"
            "    </item>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        f"  <!-- searchtern:generation {generation or 'unknown'} -->\n"
        "  <channel>\n"
        "    <title>SearchTern — new internships and new-grad roles</title>\n"
        "    <link>https://searchtern.net</link>\n"
        "    <description>Fresh software engineering internships and entry-level roles, "
        "posting by posting.</description>\n"
        "    <language>en-us</language>\n"
        f"    <lastBuildDate>{format_datetime(datetime.now(UTC))}</lastBuildDate>\n"
        '    <atom:link href="https://raw.githubusercontent.com/KSaifStack/SearchTern-Listings/main/pages/feed.xml" rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items) + ("\n" if items else "")
        + "  </channel>\n"
        "</rss>\n"
    )


def write_feed_xml(dataframe, output_dir=".", limit=200):
    """Write a newest-first RSS 2.0 feed of the live listings.

    Feed readers and importer tools (IFTTT, Zapier, Discord bots) can subscribe
    here instead of polling the multi-MB JSON, and each item links straight to
    the application URL. Rows without a date are stamped with the run time so
    the feed never contains silent gaps.
    """
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    if dataframe is None or dataframe.empty:
        records = []
    else:
        records = _build_json_records(dataframe)
    body = render_feed_xml(records, limit)
    items = len(records[:limit])

    path = os.path.join(pages_dir, "feed.xml")
    _atomic_write_text(path, body)

    print(f"Wrote {path} — {items:,} feed items")
