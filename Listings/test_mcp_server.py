"""Self-check for searchtern_mcp.py — drives the real server over stdio.

Run: python test_mcp_server.py  (stdlib only, no network)
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "searchtern_mcp.py")


def _days_ago(n):
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


FIXTURE_DATES = (_days_ago(2), _days_ago(3), _days_ago(4))
FIXTURE_HOURS_AGO = (datetime.now(UTC) - timedelta(hours=3)).isoformat()

FIXTURE = [
    {"company": "Acme", "role": "Software Engineer Intern", "location": "New York, NY",
     "date": FIXTURE_DATES[0], "link": "https://example.com/a",
     "category": "Software Engineering", "job_type": "internship",
     "is_remote": "true", "country_iso": "US", "source": "priority",
     "description": "Build things. Python and data pipelines.",
     "visa_sponsor": "true", "career_family": "Software Engineering & Technology",
     "ats_type": "greenhouse", "ats_id": "12345", "direct_apply": "true",
     "salary_min": "30", "salary_max": "45", "salary_currency": "USD",
     "first_seen_at": FIXTURE_HOURS_AGO},
    {"company": "Acme", "role": "Data Science Intern", "location": "Remote",
     "date": FIXTURE_DATES[1], "link": "https://example.com/b",
     "category": "Data Science", "job_type": "internship",
     "is_remote": "true", "country_iso": "US", "source": "freehire",
     "career_family": "Software Engineering & Technology"},
    {"company": "Globex", "role": "Backend Engineer", "location": "Austin, TX",
     "date": FIXTURE_DATES[2], "link": "https://example.com/c",
     "category": "Software Engineering", "job_type": "new_grad",
     "is_remote": "false", "country_iso": "US", "source": "ats",
     "description": "Rust services and Kubernetes operators.",
     "career_family": "Software Engineering & Technology",
     "ats_type": "workday", "ats_id": "WD-1", "direct_apply": "false"},
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


def run_tool(data_path, name, arguments, msg_id):
    return tool_payload(call([{
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }], data_path, msg_id))


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
        assert names == {"search_jobs", "get_job", "recent_jobs", "company_jobs",
                         "company_profile", "stats"}, names
        for tool in tools["tools"]:
            assert tool["inputSchema"]["type"] == "object", tool

        search = run_tool(data_path, "search_jobs", {"query": "intern"}, 3)
        assert search["count"] == 2, search
        assert search["results"][0]["date"] >= search["results"][1]["date"], "newest first"
        assert search["results"][0].get("salary", True)
        assert search["results"][0]["id"] == "greenhouse:12345", search["results"][0]
        assert search["results"][0]["salary_min"] == 30.0, search["results"][0]

        by_category = run_tool(data_path, "search_jobs",
                               {"category": "Software Engineering", "limit": 1}, 4)
        assert by_category["count"] == 2 and len(by_category["results"]) == 1, by_category
        assert by_category["next_cursor"] == "1", by_category

        # Description is searchable: only the third row mentions Kubernetes.
        full_text = run_tool(data_path, "search_jobs", {"query": "kubernetes"}, 41)
        assert full_text["count"] == 1, full_text
        assert full_text["results"][0]["role"] == "Backend Engineer", full_text
        assert full_text["results"][0]["description"].startswith("Rust services"), full_text

        multiword = run_tool(data_path, "search_jobs", {"query": "rust kubernetes"}, 42)
        assert multiword["count"] == 1, multiword
        for query in ("rust python", "kubernetes python"):
            assert run_tool(data_path, "search_jobs", {"query": query}, 43)["count"] == 0, query

        remote = run_tool(data_path, "search_jobs", {"remote_only": True}, 5)
        assert remote["count"] == 2, remote

        company = run_tool(data_path, "company_jobs", {"company": "acme"}, 6)
        assert company["count"] == 2, company

        assert run_tool(data_path, "search_jobs", {"job_type": "new_grad"}, 44)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"job_type": "internship,new_grad"}, 45)["count"] == 3
        assert run_tool(data_path, "search_jobs", {"country": "US"}, 46)["count"] == 3
        assert run_tool(data_path, "search_jobs", {"country": "CA"}, 47)["count"] == 0
        assert run_tool(data_path, "search_jobs", {"ats_type": "workday"}, 48)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"visa_sponsor": True}, 49)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"direct_apply": True}, 50)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"salary_disclosed": True}, 51)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"min_salary": 40}, 52)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"min_salary": 50}, 53)["count"] == 0
        assert run_tool(data_path, "search_jobs", {"career_family": "healthcare"}, 54)["count"] == 0
        assert run_tool(data_path, "search_jobs", {"first_seen_within_hours": 6}, 55)["count"] == 1
        assert run_tool(data_path, "search_jobs", {"posted_within_days": 5}, 56)["count"] == 3
        assert run_tool(data_path, "search_jobs", {"posted_within_days": 0}, 561)["count"] == 0

        # Pagination walks the newest-first order without repeating a row.
        page1 = run_tool(data_path, "search_jobs", {"limit": 1}, 57)
        assert page1["count"] == 3 and page1["returned"] == 1, page1
        assert page1["next_cursor"] == "1", page1
        page2 = run_tool(data_path, "search_jobs", {"limit": 1, "cursor": "1"}, 58)
        assert page2["returned"] == 1 and page2["next_cursor"] == "2", page2
        assert page2["results"][0]["id"] != page1["results"][0]["id"], page2
        page3 = run_tool(data_path, "search_jobs", {"limit": 1, "cursor": "2"}, 59)
        assert page3["returned"] == 1 and page3["next_cursor"] is None, page3

        # Detail lookup by id and by link, plus the missing cases.
        detail = run_tool(data_path, "get_job", {"id": "greenhouse:12345"}, 60)
        assert detail["description"] == "Build things. Python and data pipelines.", detail
        assert detail["salary_max"] == 45.0 and detail["direct_apply"] is True, detail
        by_link = run_tool(data_path, "get_job", {"link": "https://example.com/c"}, 61)
        assert by_link["id"] == "workday:WD-1", by_link
        assert by_link["is_remote"] is False, by_link
        missing = run_tool(data_path, "get_job", {"id": "nope"}, 62)
        assert missing.get("error") == "job not found", missing
        assert run_tool(data_path, "get_job", {}, 63).get("error") == "id or link is required"

        profile = run_tool(data_path, "company_profile", {"company": "acme"}, 64)
        assert profile["count"] == 2, profile
        assert profile["by_job_type"] == {"internship": 2}, profile
        assert profile["remote_share"] == 1.0, profile
        assert profile["salary_disclosed"] == 1, profile
        assert profile["names"] == {"Acme": 2}, profile
        assert len(profile["sample_jobs"]) == 2, profile
        assert run_tool(data_path, "company_profile", {"company": "zzz"}, 65).get("error")

        stats = run_tool(data_path, "stats", {}, 7)
        assert stats["total"] == 3, stats
        assert stats["newest"] == FIXTURE_DATES[0][:10], stats
        assert stats["oldest"] == FIXTURE_DATES[2][:10], stats
        assert stats["remote_share"] == 0.667, stats
        assert stats["by_job_type"] == {"internship": 2, "new_grad": 1}, stats
        assert stats["by_country"]["US"] == 3, stats
        assert stats["salary_disclosed"] == 1, stats
        assert stats["salary_disclosed_share"] == 0.333, stats
        assert stats["description_coverage"] == 0.667, stats
        assert stats["first_seen_tracked"] == 1, stats
        assert stats["last_verified_newest"] is None, stats

        recent_ok = run_tool(data_path, "recent_jobs", {"days": 7}, 8)
        assert recent_ok["count"] == 3, recent_ok

        recent_none = run_tool(data_path, "recent_jobs", {"days": 0}, 81)
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
