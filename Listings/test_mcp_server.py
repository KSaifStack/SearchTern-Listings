"""Self-check for searchtern_mcp.py — drives the real server over stdio.

Run: python test_mcp_server.py  (stdlib only, no network)
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "searchtern_mcp.py")


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


FIXTURE_DATES = (_days_ago(2), _days_ago(3), _days_ago(4))

FIXTURE = [
    {"company": "Acme", "role": "Software Engineer Intern", "location": "New York, NY",
     "date": FIXTURE_DATES[0], "link": "https://example.com/a",
     "category": "Software Engineering", "job_type": "internship",
     "is_remote": "true", "country_iso": "US", "source": "priority",
     "description": "Build things.", "visa_sponsor": "true"},
    {"company": "Acme", "role": "Data Science Intern", "location": "Remote",
     "date": FIXTURE_DATES[1], "link": "https://example.com/b",
     "category": "Data Science", "job_type": "internship",
     "is_remote": "true", "country_iso": "US", "source": "freehire"},
    {"company": "Globex", "role": "Backend Engineer", "location": "Austin, TX",
     "date": FIXTURE_DATES[2], "link": "https://example.com/c",
     "category": "Software Engineering", "job_type": "new_grad",
     "is_remote": "false", "country_iso": "US", "source": "ats"},
]


def exchange(messages, data_path):
    env = dict(os.environ, SEARCHTERN_DATA=data_path)
    proc = subprocess.run(
        [sys.executable, SERVER],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, f"server exited {proc.returncode}: {proc.stderr}"
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def call(messages, data_path, want_id):
    for response in exchange(messages, data_path):
        if response.get("id") == want_id:
            if "error" in response:
                raise AssertionError(f"JSON-RPC error for id {want_id}: {response['error']}")
            return response["result"]
    raise AssertionError(f"no response with id {want_id}")


def tool_payload(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        data_path = os.path.join(tmp, "searchtern.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(FIXTURE, f)

        init = call([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05"}}], data_path, 1)
        assert init["protocolVersion"] == "2024-11-05", init
        assert init["capabilities"]["tools"] == {}, init
        assert init["serverInfo"]["name"] == "searchtern", init

        tools = call([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], data_path, 2)
        names = {t["name"] for t in tools["tools"]}
        assert names == {"search_jobs", "recent_jobs", "company_jobs", "stats"}, names
        for tool in tools["tools"]:
            assert tool["inputSchema"]["type"] == "object", tool

        search = tool_payload(call([{
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_jobs", "arguments": {"query": "intern"}},
        }], data_path, 3))
        assert search["count"] == 2, search
        assert search["results"][0]["date"] >= search["results"][1]["date"], "newest first"
        assert search["results"][0]["salary"] if "salary" in search["results"][0] else True

        by_category = tool_payload(call([{
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "search_jobs",
                       "arguments": {"category": "Software Engineering", "limit": 1}},
        }], data_path, 4))
        assert by_category["count"] == 2 and len(by_category["results"]) == 1, by_category

        remote = tool_payload(call([{
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "search_jobs", "arguments": {"remote_only": True}},
        }], data_path, 5))
        assert remote["count"] == 2, remote

        company = tool_payload(call([{
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "company_jobs", "arguments": {"company": "acme"}},
        }], data_path, 6))
        assert company["count"] == 2, company

        stats = tool_payload(call([{
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "stats", "arguments": {}},
        }], data_path, 7))
        assert stats["total"] == 3, stats
        assert stats["newest"] == FIXTURE_DATES[0][:10], stats
        assert stats["oldest"] == FIXTURE_DATES[2][:10], stats
        assert stats["remote_share"] == 0.667, stats

        recent_ok = tool_payload(call([{
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "recent_jobs", "arguments": {"days": 7}},
        }], data_path, 8))
        assert recent_ok["count"] == 3, recent_ok

        recent_none = tool_payload(call([{
            "jsonrpc": "2.0", "id": 81, "method": "tools/call",
            "params": {"name": "recent_jobs", "arguments": {"days": 0}},
        }], data_path, 81))
        assert recent_none["count"] == 0, recent_none

        ping = call([{"jsonrpc": "2.0", "id": 9, "method": "ping"}], data_path, 9)
        assert ping == {}, ping

        responses = exchange([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 10, "method": "bogus/method"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
        ], data_path)
        by_id = {r.get("id"): r for r in responses}
        assert 10 in by_id and by_id[10]["error"]["code"] == -32601, responses
        assert 11 in by_id and by_id[11]["error"]["code"] == -32602, responses
        assert all(r.get("id") is not None for r in responses), "notifications must not respond"

        bad_path = os.path.join(tmp, "missing.json")
        failures = exchange([{"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                              "params": {"name": "stats", "arguments": {}}}], bad_path)
        assert failures and failures[0]["result"].get("isError"), failures

    print(f"  test_mcp_server: all checks passed ({len(FIXTURE)} fixture rows)")


if __name__ == "__main__":
    main()
