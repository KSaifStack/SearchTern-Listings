#!/usr/bin/env python3
"""SearchTern MCP server (stdio, stdlib only).

Exposes the published SearchTern job feed to any MCP client as four read-only
tools: search_jobs, recent_jobs, company_jobs, stats. Reads pages/searchtern.json
from this repo by default; SEARCHTERN_DATA (file path) or SEARCHTERN_DATA_URL
(http URL) override the source.

Run:  python searchtern_mcp.py
Wire into a client, e.g. Claude Code / opencode:
  {"mcpServers": {"searchtern": {"command": "python",
     "args": ["/path/to/SearchTern-Listings/Listings/searchtern_mcp.py"]}}}
"""
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "searchtern"
SERVER_VERSION = "1.0.0"
DEFAULT_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "pages", "searchtern.json")
DESCRIPTION_CHARS = 400

TOOLS = [
    {
        "name": "search_jobs",
        "description": "Search SearchTern listings by keyword, location, or "
                       "category. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Substring matched against role, company, and location."},
                "location": {"type": "string",
                             "description": "Substring matched against the location field."},
                "company": {"type": "string",
                            "description": "Substring matched against the company name."},
                "category": {"type": "string",
                             "description": "Exact role category, e.g. Software Engineering."},
                "remote_only": {"type": "boolean",
                                "description": "Keep only listings flagged remote."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "description": "Maximum results (default 25)."},
            },
        },
    },
    {
        "name": "recent_jobs",
        "description": "List the newest SearchTern postings from the last N days.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 0, "maximum": 90,
                         "description": "Lookback window in days (default 7)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                          "description": "Maximum results (default 25)."},
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
        "name": "stats",
        "description": "Feed totals: counts by category and source, date range, remote share.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_DATA = None


def _load_data():
    url = os.environ.get("SEARCHTERN_DATA_URL")
    path = os.environ.get("SEARCHTERN_DATA") or DEFAULT_DATA
    if url:
        with urllib.request.urlopen(url, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    else:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    records = json.loads(raw)
    return [r for r in records if isinstance(r, dict)]


def _data():
    global _DATA
    if _DATA is None:
        _DATA = _load_data()
    return _DATA


def _posted(record):
    try:
        value = datetime.fromisoformat(str(record.get("date", "")).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _salary(record):
    low, high, currency = (record.get("salary_min"), record.get("salary_max"),
                           record.get("salary_currency"))
    if low is None and high is None:
        return None
    span = f"{low or '?'}-{high or '?'}"
    return f"{span} {currency}".strip() if currency else span


def _brief(record):
    out = {
        "company": record.get("company"),
        "role": record.get("role"),
        "location": record.get("location"),
        "date": str(record.get("date") or "")[:10],
        "link": record.get("link"),
        "category": record.get("category"),
        "job_type": record.get("job_type"),
        "is_remote": str(record.get("is_remote")).lower() == "true",
        "country_iso": record.get("country_iso"),
        "source": record.get("source"),
    }
    salary = _salary(record)
    if salary:
        out["salary"] = salary
    if "visa_sponsor" in record:
        out["visa_sponsor"] = record.get("visa_sponsor")
    description = str(record.get("description") or "").strip()
    if description:
        out["description"] = description[:DESCRIPTION_CHARS]
    return out


def _matches(record, query):
    text = " ".join(str(record.get(k) or "") for k in ("role", "company", "location")).lower()
    return query.lower() in text


def _clamp(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _search_jobs(args):
    rows = _data()
    if args.get("query"):
        rows = [r for r in rows if _matches(r, str(args["query"]))]
    for field in ("location", "company"):
        if args.get(field):
            needle = str(args[field]).lower()
            rows = [r for r in rows if needle in str(r.get(field) or "").lower()]
    if args.get("category"):
        want = str(args["category"]).lower()
        rows = [r for r in rows if str(r.get("category") or "").lower() == want]
    if args.get("remote_only"):
        rows = [r for r in rows if str(r.get("is_remote")).lower() == "true"]
    rows.sort(key=_posted, reverse=True)
    limit = _clamp(args.get("limit"), 25, 1, 100)
    return {"count": len(rows), "results": [_brief(r) for r in rows[:limit]]}


def _recent_jobs(args):
    days = _clamp(args.get("days"), 7, 0, 90)
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    rows = [r for r in _data() if _posted(r).timestamp() >= cutoff]
    rows.sort(key=_posted, reverse=True)
    limit = _clamp(args.get("limit"), 25, 1, 100)
    return {"days": days, "count": len(rows), "results": [_brief(r) for r in rows[:limit]]}


def _company_jobs(args):
    needle = str(args.get("company") or "").strip().lower()
    if not needle:
        return {"error": "company is required"}
    rows = [r for r in _data() if needle in str(r.get("company") or "").lower()]
    rows.sort(key=_posted, reverse=True)
    limit = _clamp(args.get("limit"), 50, 1, 200)
    return {"company": args["company"], "count": len(rows),
            "results": [_brief(r) for r in rows[:limit]]}


def _stats(_args):
    rows = _data()
    dates = [d for d in (_posted(r) for r in rows) if d.year > 1]
    remote = sum(1 for r in rows if str(r.get("is_remote")).lower() == "true")
    return {
        "total": len(rows),
        "remote_share": round(remote / len(rows), 3) if rows else 0,
        "newest": max(dates).date().isoformat() if dates else None,
        "oldest": min(dates).date().isoformat() if dates else None,
        "by_category": dict(Counter(str(r.get("category") or "Unknown") for r in rows).most_common(15)),
        "by_source": dict(Counter(str(r.get("source") or "unknown") for r in rows).most_common(10)),
    }


HANDLERS = {
    "search_jobs": _search_jobs,
    "recent_jobs": _recent_jobs,
    "company_jobs": _company_jobs,
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
