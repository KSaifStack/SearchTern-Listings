#!/usr/bin/env python3
"""SearchTern MCP server (stdio, stdlib only).

Exposes the published SearchTern job feed to any MCP client as six read-only
tools: search_jobs, get_job, recent_jobs, company_jobs, company_profile, stats.
Resolves the feed from pages/feed-manifest.json (release asset) by default,
falling back to pages/searchtern.json; SEARCHTERN_DATA (file path),
SEARCHTERN_DATA_URL, or SEARCHTERN_MANIFEST_URL override the source.

The feed is re-checked every 30 seconds: a client that stays connected sees a
republished feed instead of the snapshot it happened to start with.

Run:  python searchtern_mcp.py
Wire into a client, e.g. Claude Code / opencode:
  {"mcpServers": {"searchtern": {"command": "python",
     "args": ["/path/to/SearchTern-Listings/Listings/searchtern_mcp.py"]}}}
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "searchtern"
SERVER_VERSION = "1.1.0"
PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pages")
DEFAULT_DATA = os.path.join(PAGES_DIR, "searchtern.json")
DEFAULT_MANIFEST = os.path.join(PAGES_DIR, "feed-manifest.json")
DESCRIPTION_CHARS = 400
KEY_TTL_SECONDS = 30.0

# Fields a free-text query is matched against. Description is included because
# it is the only place most stack and skill words appear: 19,157 of 26,765 rows
# carry one, and "python" matches 1,561 descriptions but only 32 roles.
TEXT_FIELDS = ("role", "company", "location", "description", "category",
               "career_family", "career_families", "industry", "ats_type")

TOOLS = [
    {
        "name": "search_jobs",
        "description": "Search SearchTern listings. A free-text query is matched "
                       "against role, company, location, description, category, "
                       "career family, industry and ATS type; every word in the "
                       "query must appear. Structured filters narrow the same "
                       "search. Results are newest-first and paginated: pass the "
                       "returned next_cursor back as cursor. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Free text matched against role, company, "
                                         "location, description, category, career "
                                         "family, industry and ATS type."},
                "location": {"type": "string",
                             "description": "Substring matched against the location field."},
                "company": {"type": "string",
                            "description": "Substring matched against the company name."},
                "category": {"type": "string",
                             "description": "Exact role category, e.g. Software Engineering."},
                "career_family": {"type": "string",
                                  "description": "Substring matched against the career "
                                                 "family, e.g. Healthcare."},
                "job_type": {"type": "string",
                             "description": "One or more of internship, new_grad "
                                            "(comma separated)."},
                "country": {"type": "string",
                            "description": "One or more ISO alpha-2 codes, e.g. US,CA."},
                "ats_type": {"type": "string",
                             "description": "One or more ATS names, e.g. greenhouse,workday."},
                "remote_only": {"type": "boolean",
                                "description": "Keep only listings flagged remote."},
                "visa_sponsor": {"type": "boolean",
                                 "description": "Keep only listings whose employer "
                                                "carries a sponsorship signal."},
                "direct_apply": {"type": "boolean",
                                 "description": "Keep only listings that apply on the "
                                                "employer's own ATS."},
                "salary_disclosed": {"type": "boolean",
                                     "description": "Keep only listings with a posted "
                                                    "salary (5.5% of the feed)."},
                "min_salary": {"type": "number",
                               "description": "Keep listings whose top of range is at "
                                              "least this value. Use with a currency "
                                              "filter or salary_disclosed."},
                "posted_within_days": {"type": "integer", "minimum": 0, "maximum": 365,
                                       "description": "Keep listings posted in the last N days."},
                "first_seen_within_hours": {"type": "integer", "minimum": 0,
                                            "description": "Keep listings first seen in "
                                                           "the last N hours (32% of the "
                                                           "feed has first_seen_at)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "description": "Page size (default 25)."},
                "cursor": {"type": "string",
                           "description": "next_cursor from a previous call."},
            },
        },
    },
    {
        "name": "get_job",
        "description": "Fetch one listing in full: complete description, salary "
                       "range, visa history, ATS identity, first/last verified "
                       "timestamps. Look it up by id (from any search result) or "
                       "by link. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": "Job id returned by search_jobs or company_jobs."},
                "link": {"type": "string",
                         "description": "The apply link, as an alternative to id."},
            },
        },
    },
    {
        "name": "recent_jobs",
        "description": "List the newest SearchTern postings from the last N days, "
                       "paginated newest-first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 0, "maximum": 365,
                         "description": "Lookback window in days (default 7)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "description": "Page size (default 25)."},
                "cursor": {"type": "string",
                           "description": "next_cursor from a previous call."},
            },
        },
    },
    {
        "name": "company_jobs",
        "description": "List every open SearchTern posting at one company.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name (substring)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "description": "Maximum results (default 50)."},
            },
            "required": ["company"],
        },
    },
    {
        "name": "company_profile",
        "description": "Employer-level summary for one company: open roles, role "
                       "mix, career families, countries, remote share, salary "
                       "disclosure, sponsorship signals, ATS platforms, and the "
                       "newest listings. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name (substring)."},
            },
            "required": ["company"],
        },
    },
    {
        "name": "stats",
        "description": "Feed totals: counts by category, source, job type, career "
                       "family, country and ATS, date range, remote share, salary "
                       "and description coverage, and how fresh the data is.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_DATA = None
_DATA_KEY = None
_DATA_CHECKED_AT = 0.0


def _read_manifest(source):
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    else:
        with open(source, encoding="utf-8") as f:
            raw = f.read()
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _load_data():
    explicit_path = os.environ.get("SEARCHTERN_DATA")
    explicit_url = os.environ.get("SEARCHTERN_DATA_URL")
    if explicit_path:
        with open(explicit_path, encoding="utf-8") as f:
            raw = f.read()
    elif explicit_url:
        with urllib.request.urlopen(explicit_url, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    else:
        manifest_source = os.environ.get("SEARCHTERN_MANIFEST_URL") or DEFAULT_MANIFEST
        if (os.path.exists(manifest_source)
                or manifest_source.startswith(("http://", "https://"))):
            manifest = _read_manifest(manifest_source)
        else:
            manifest = {}
        published = manifest.get("published") if isinstance(manifest, dict) else {}
        entry = published.get("searchtern") if isinstance(published, dict) else {}
        asset = entry.get("url") if isinstance(entry, dict) else None
        if str((manifest.get("delivery") or {}).get("mode") or "") == "release" and asset:
            with urllib.request.urlopen(asset, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        else:
            with open(DEFAULT_DATA, encoding="utf-8") as f:
                raw = f.read()
    records = json.loads(raw)
    return [r for r in records if isinstance(r, dict)]


def _data_key():
    """Cheap identity of the currently published feed.

    A local file is identified by mtime+size, the manifest by generated_at and
    sha256, so a republish is detected without re-parsing 89 MB of JSON. Returns
    None when the source cannot be probed (the cache then keeps its value).
    """
    path = os.environ.get("SEARCHTERN_DATA")
    if path:
        try:
            stat = os.stat(path)
            return ("file", stat.st_mtime_ns, stat.st_size)
        except OSError:
            return ("file", path, None)
    url = os.environ.get("SEARCHTERN_DATA_URL")
    if url:
        return ("url", url)
    source = os.environ.get("SEARCHTERN_MANIFEST_URL") or DEFAULT_MANIFEST
    try:
        manifest = _read_manifest(source)
    except Exception:  # noqa: BLE001 - an unreadable manifest is not a fatal call
        return None
    entry = (manifest.get("published") or {}).get("searchtern") or {}
    return ("manifest", manifest.get("generated_at"), entry.get("sha256"),
            entry.get("rows"))


def _data():
    global _DATA, _DATA_KEY, _DATA_CHECKED_AT
    now = time.monotonic()
    if _DATA is not None and now - _DATA_CHECKED_AT < KEY_TTL_SECONDS:
        return _DATA
    key = _data_key()
    _DATA_CHECKED_AT = now
    if _DATA is None or (key is not None and key != _DATA_KEY):
        _DATA = _load_data()
        _DATA_KEY = key
    return _DATA


def _parse_ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _posted(record):
    return _parse_ts(record.get("date")) or datetime.min.replace(tzinfo=UTC)


def _to_float(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _truth(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _salary(record):
    low, high, currency = (record.get("salary_min"), record.get("salary_max"),
                           record.get("salary_currency"))
    if low in (None, "") and high in (None, ""):
        return None
    span = f"{low or '?'}-{high or '?'}"
    return f"{span} {currency}".strip() if currency else span


def _salary_high(record):
    """Top of a posted range, used for salary thresholds."""
    return _to_float(record.get("salary_max")) or _to_float(record.get("salary_min"))


def _job_id(record):
    """Stable per-listing id.

    The published feed carries no id column, so it is derived here: the ATS
    identity when the listing has one (21,907 rows), otherwise a short hash of
    the apply link. Deterministic across runs, which is what pagination and
    get_job need.
    """
    ats_type = str(record.get("ats_type") or "").strip()
    ats_id = str(record.get("ats_id") or "").strip()
    if ats_type and ats_id:
        return f"{ats_type}:{ats_id}"
    link = str(record.get("link") or "").strip().lower()
    return "u:" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def _family_text(record):
    families = record.get("career_families")
    if isinstance(families, (list, tuple)):
        families = " ".join(str(f) for f in families)
    return f"{record.get('career_family') or ''} {families or ''}".lower()


def _brief(record):
    out = {
        "id": _job_id(record),
        "company": record.get("company"),
        "role": record.get("role"),
        "location": record.get("location"),
        "date": str(record.get("date") or "")[:10],
        "link": record.get("link"),
        "category": record.get("category"),
        "job_type": record.get("job_type"),
        "career_family": record.get("career_family"),
        "is_remote": _truth(record.get("is_remote")),
        "country_iso": record.get("country_iso"),
        "source": record.get("source"),
    }
    salary = _salary(record)
    if salary:
        out["salary"] = salary
    low, high = _to_float(record.get("salary_min")), _to_float(record.get("salary_max"))
    if low is not None:
        out["salary_min"] = low
    if high is not None:
        out["salary_max"] = high
    if record.get("salary_currency"):
        out["salary_currency"] = record.get("salary_currency")
    if _truth(record.get("visa_sponsor")):
        out["visa_sponsor"] = True
    if _truth(record.get("direct_apply")):
        out["direct_apply"] = True
    if record.get("ats_type"):
        out["ats_type"] = record.get("ats_type")
    first_seen = str(record.get("first_seen_at") or "")[:10]
    if first_seen:
        out["first_seen_at"] = first_seen
    description = str(record.get("description") or "").strip()
    if description:
        out["description"] = description[:DESCRIPTION_CHARS]
        out["description_chars"] = len(description)
        if len(description) > DESCRIPTION_CHARS:
            out["description_truncated"] = True
    return out


def _detail(record):
    out = {}
    for key in ("company", "role", "location", "date", "link", "source", "category",
                "job_type", "career_family", "industry", "career_reason", "seniority",
                "employment_type", "country_iso", "ats_type", "ats_id",
                "salary_currency", "source_posted_at", "verification_status",
                "first_seen_at", "last_seen_at", "last_verified_at", "verified_at",
                "observed_at", "closed_at"):
        value = record.get(key)
        if value not in (None, ""):
            out[key] = value
    out["id"] = _job_id(record)
    out["is_remote"] = _truth(record.get("is_remote"))
    out["direct_apply"] = _truth(record.get("direct_apply"))
    out["visa_sponsor"] = _truth(record.get("visa_sponsor"))
    for key in ("salary_min", "salary_max", "visa_median_salary"):
        number = _to_float(record.get(key))
        if number is not None:
            out[key] = number
    role_count = _to_float(record.get("visa_role_count"))
    if role_count is not None:
        out["visa_role_count"] = int(role_count)
    if record.get("visa_role"):
        out["visa_role"] = record.get("visa_role")
    if record.get("tier_hint"):
        out["tier_hint"] = record.get("tier_hint")
    if record.get("career_families"):
        out["career_families"] = record.get("career_families")
    description = str(record.get("description") or "").strip()
    out["description"] = description
    out["description_chars"] = len(description)
    return out


def _matches(record, query):
    tokens = [t for t in str(query).lower().split() if t]
    if not tokens:
        return True
    text = " ".join(str(record.get(k) or "") for k in TEXT_FIELDS).lower()
    return all(token in text for token in tokens)


def _clamp(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _parse_cursor(value):
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def _csv_lower(value):
    return {v.strip().lower() for v in str(value).split(",") if v.strip()}


def _csv_upper(value):
    return {v.strip().upper() for v in str(value).split(",") if v.strip()}


def _paginate(rows, args, default_limit=25):
    limit = _clamp(args.get("limit"), default_limit, 1, 200)
    offset = _parse_cursor(args.get("cursor"))
    page = rows[offset:offset + limit]
    remainder = offset + len(page)
    return (page, {
        "count": len(rows),
        "returned": len(page),
        "limit": limit,
        "cursor": args.get("cursor"),
        "next_cursor": str(remainder) if remainder < len(rows) else None,
    })


def _search_jobs(args):
    rows = _data()
    if args.get("query"):
        rows = [r for r in rows if _matches(r, args["query"])]
    for field in ("location", "company"):
        if args.get(field):
            needle = str(args[field]).lower()
            rows = [r for r in rows if needle in str(r.get(field) or "").lower()]
    if args.get("category"):
        want = str(args["category"]).lower()
        rows = [r for r in rows if str(r.get("category") or "").lower() == want]
    if args.get("career_family"):
        needle = str(args["career_family"]).lower()
        rows = [r for r in rows if needle in _family_text(r)]
    if args.get("job_type"):
        want = _csv_lower(args["job_type"])
        rows = [r for r in rows if str(r.get("job_type") or "").lower() in want]
    if args.get("country"):
        want = _csv_upper(args["country"])
        rows = [r for r in rows if str(r.get("country_iso") or "").upper() in want]
    if args.get("ats_type"):
        want = _csv_lower(args["ats_type"])
        rows = [r for r in rows if str(r.get("ats_type") or "").lower() in want]
    if args.get("remote_only"):
        rows = [r for r in rows if _truth(r.get("is_remote"))]
    if args.get("visa_sponsor"):
        rows = [r for r in rows if _truth(r.get("visa_sponsor"))]
    if args.get("direct_apply"):
        rows = [r for r in rows if _truth(r.get("direct_apply"))]
    if args.get("salary_disclosed"):
        rows = [r for r in rows if _salary_high(r) is not None]
    if args.get("min_salary") is not None:
        threshold = _to_float(args.get("min_salary"))
        if threshold is not None:
            rows = [r for r in rows
                    if _salary_high(r) is not None and _salary_high(r) >= threshold]
    if args.get("posted_within_days") is not None:
        days = _clamp(args.get("posted_within_days"), 7, 0, 365)
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        rows = [r for r in rows if _posted(r).timestamp() >= cutoff]
    if args.get("first_seen_within_hours") is not None:
        hours = _clamp(args.get("first_seen_within_hours"), 24, 0, 100000)
        cutoff = datetime.now(UTC).timestamp() - hours * 3600
        rows = [r for r in rows
                if (_parse_ts(r.get("first_seen_at")) or datetime.min.replace(tzinfo=UTC))
                .timestamp() >= cutoff]

    rows.sort(key=lambda r: (_posted(r), _job_id(r)), reverse=True)
    page, meta = _paginate(rows, args)
    return {**meta, "results": [_brief(r) for r in page]}


def _get_job(args):
    key = str(args.get("id") or "").strip()
    link = str(args.get("link") or "").strip()
    if not key and not link:
        return {"error": "id or link is required"}
    for record in _data():
        if key and _job_id(record) == key:
            return _detail(record)
        if link and str(record.get("link") or "").strip() == link:
            return _detail(record)
    return {"error": "job not found", "id": key or None, "link": link or None}


def _recent_jobs(args):
    days = _clamp(args.get("days"), 7, 0, 365)
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    rows = [r for r in _data() if _posted(r).timestamp() >= cutoff]
    rows.sort(key=lambda r: (_posted(r), _job_id(r)), reverse=True)
    page, meta = _paginate(rows, args)
    return {"days": days, **meta, "results": [_brief(r) for r in page]}


def _company_jobs(args):
    needle = str(args.get("company") or "").strip().lower()
    if not needle:
        return {"error": "company is required"}
    rows = [r for r in _data() if needle in str(r.get("company") or "").lower()]
    rows.sort(key=lambda r: (_posted(r), _job_id(r)), reverse=True)
    limit = _clamp(args.get("limit"), 50, 1, 200)
    return {"company": args["company"], "count": len(rows),
            "results": [_brief(r) for r in rows[:limit]]}


def _company_profile(args):
    needle = str(args.get("company") or "").strip().lower()
    if not needle:
        return {"error": "company is required"}
    rows = [r for r in _data() if needle in str(r.get("company") or "").lower()]
    if not rows:
        return {"company": args.get("company"), "count": 0,
                "error": "no listings found for that company"}
    total = len(rows)
    dates = [d for d in (_posted(r) for r in rows) if d.year > 1]
    first_seen = [d for d in (_parse_ts(r.get("first_seen_at")) for r in rows) if d]
    rows.sort(key=lambda r: (_posted(r), _job_id(r)), reverse=True)
    return {
        "company": args.get("company"),
        "count": total,
        "names": dict(Counter(str(r.get("company") or "") for r in rows).most_common(5)),
        "by_job_type": dict(Counter(str(r.get("job_type") or "unknown") for r in rows)),
        "by_career_family": dict(Counter(str(r.get("career_family") or "unknown")
                                         for r in rows).most_common(5)),
        "by_country": dict(Counter(str(r.get("country_iso") or "unknown")
                                   for r in rows).most_common(5)),
        "by_ats": dict(Counter(str(r.get("ats_type") or "unknown")
                               for r in rows).most_common(5)),
        "remote_share": round(sum(1 for r in rows if _truth(r.get("is_remote"))) / total, 3),
        "salary_disclosed": sum(1 for r in rows if _salary_high(r) is not None),
        "visa_sponsor": sum(1 for r in rows if _truth(r.get("visa_sponsor"))),
        "direct_apply": sum(1 for r in rows if _truth(r.get("direct_apply"))),
        "newest": max(dates).date().isoformat() if dates else None,
        "oldest": min(dates).date().isoformat() if dates else None,
        "first_seen_oldest": min(first_seen).date().isoformat() if first_seen else None,
        "sample_jobs": [_brief(r) for r in rows[:5]],
    }


def _stats(_args):
    rows = _data()
    total = len(rows)
    dates = [d for d in (_posted(r) for r in rows) if d.year > 1]
    remote = sum(1 for r in rows if _truth(r.get("is_remote")))
    salary_rows = sum(1 for r in rows if _salary_high(r) is not None)
    described = sum(1 for r in rows if str(r.get("description") or "").strip())
    verified = [d for d in (_parse_ts(r.get("last_verified_at")) for r in rows) if d]
    first_seen = sum(1 for r in rows if _parse_ts(r.get("first_seen_at")))
    return {
        "total": total,
        "remote_share": round(remote / total, 3) if total else 0,
        "newest": max(dates).date().isoformat() if dates else None,
        "oldest": min(dates).date().isoformat() if dates else None,
        "last_verified_newest": max(verified).isoformat() if verified else None,
        "by_category": dict(Counter(str(r.get("category") or "Unknown")
                                    for r in rows).most_common(15)),
        "by_source": dict(Counter(str(r.get("source") or "unknown")
                                  for r in rows).most_common(10)),
        "by_job_type": dict(Counter(str(r.get("job_type") or "unknown") for r in rows)),
        "by_career_family": dict(Counter(str(r.get("career_family") or "unknown")
                                         for r in rows).most_common(15)),
        "by_country": dict(Counter(str(r.get("country_iso") or "unknown")
                                   for r in rows).most_common(15)),
        "by_ats": dict(Counter(str(r.get("ats_type") or "unknown")
                               for r in rows).most_common(10)),
        "salary_disclosed": salary_rows,
        "salary_disclosed_share": round(salary_rows / total, 3) if total else 0,
        "visa_sponsor_count": sum(1 for r in rows if _truth(r.get("visa_sponsor"))),
        "direct_apply_count": sum(1 for r in rows if _truth(r.get("direct_apply"))),
        "description_coverage": round(described / total, 3) if total else 0,
        "first_seen_tracked": first_seen,
    }


HANDLERS = {
    "search_jobs": _search_jobs,
    "get_job": _get_job,
    "recent_jobs": _recent_jobs,
    "company_jobs": _company_jobs,
    "company_profile": _company_profile,
    "stats": _stats,
}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(message):
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(msg_id, -32602, f"unknown tool: {name}")
        try:
            payload = handler(params.get("arguments") or {})
        except Exception as exc:
            return _result(msg_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        return _result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        })
    if msg_id is None:
        return None
    return _error(msg_id, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print("searchtern_mcp: skipping non-JSON line", file=sys.stderr)
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
