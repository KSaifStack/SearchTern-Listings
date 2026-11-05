"""Runnable self-check for readme_utils.http_get retry/backoff logic.

No framework. Run:  python test_http_backoff.py
"""

import concurrent.futures
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import readme_utils


class _Resp:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


def _patch(sequence):
    getter = mock.patch.object(
        readme_utils.SESSION, "get", side_effect=sequence
    )
    sleeper = mock.patch("readme_utils.time.sleep")
    # Per-provider pacing has its own clock/sleep binding (tested separately in
    # run_pacing_checks); neutralise it here so it cannot swallow the retry
    # sleeps asserted below.
    pacer = mock.patch("readme_utils._pace_sleep")
    return getter, sleeper, pacer


def run_checks():
    # 200 returns immediately, one request, no sleep.
    getter, sleeper, pacer = _patch([_Resp()])
    with getter as g, sleeper as s, pacer:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 200
    assert g.call_count == 1
    assert s.call_count == 0

    # One 429 then success: sleeps (2^0 + jitter) and retries.
    getter, sleeper, pacer = _patch([_Resp(429), _Resp()])
    with getter as g, sleeper as s, pacer:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 200
    assert g.call_count == 2
    assert s.call_count == 1
    wait = s.call_args[0][0]
    assert 1 <= wait <= 2  # 2^0=1 + uniform(0,1) -> [1,2)

    # Retry-After is honored as a floor for the wait.
    getter, sleeper, pacer = _patch([_Resp(503, {"Retry-After": "10"}), _Resp()])
    with getter as g, sleeper as s, pacer:
        readme_utils.http_get("http://u", timeout=5)
    assert g.call_count == 2
    assert s.call_args[0][0] >= 10

    # Retry-After > 60s gives up immediately (returns the 429).
    getter, sleeper, pacer = _patch([_Resp(429, {"Retry-After": "120"})])
    with getter as g, sleeper as s, pacer:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 429
    assert g.call_count == 1
    assert s.call_count == 0

    # Persistent 429s exhaust retries -> None, sleep backs off each attempt.
    getter, sleeper, pacer = _patch([_Resp(429), _Resp(429), _Resp(429)])
    with getter as g, sleeper as s, pacer:
        r = readme_utils.http_get("http://u", timeout=5, retries=3)
    assert r is None
    assert g.call_count == 3
    assert s.call_count == 2

    # Permanent authorization failures must not be retried.
    getter, sleeper, pacer = _patch([_Resp(403)])
    with getter as g, sleeper as s, pacer:
        assert readme_utils.http_get("http://u").status_code == 403
    assert g.call_count == 1 and s.call_count == 0

    # A streamed retry response must release its pooled connection.
    rate_limited = _Resp(429)
    getter, sleeper, pacer = _patch([rate_limited, _Resp()])
    with getter, sleeper, pacer:
        readme_utils.http_get("http://u", stream=True)
    assert rate_limited.closed

    # Network error then success is recovered.
    getter, sleeper, pacer = _patch(
        [readme_utils.requests.RequestException("timeout"), _Resp()]
    )
    with getter as g, sleeper as s, pacer:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 200

    # Exercise the shared pool under bounded concurrent callers without network access.
    lock = threading.Lock()
    active = 0
    peak = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            with lock:
                active -= 1

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(readme_utils.http_get, [url] * 6))
        assert all(response.status_code == 200 for response in responses)
        assert 1 < peak <= 3, peak
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    print("  test_http_backoff: all checks passed")


def run_pacing_checks():
    # Shared API hosts collapse into one throttle bucket; per-tenant hosts don't.
    assert readme_utils._provider_key("https://acme.recruitee.com/api/offers/") == "recruitee.com"
    assert readme_utils._provider_key("https://api.lever.co/v0/postings/x") == "lever.co"
    assert readme_utils._provider_key(
        "https://boards-api.greenhouse.io/v1/boards/x/jobs") == "greenhouse.io"
    # Workday tenants each own a pod — 1,000 boards must not serialise.
    assert readme_utils._provider_key(
        "https://micron.wd1.myworkdayjobs.com/wday/cxs/micron/x/jobs"
    ) == "micron.wd1.myworkdayjobs.com"
    assert readme_utils._provider_key("https://careers.example.com/jobs") == "careers.example.com"

    # Concurrency per bucket is capped even when callers pile on.
    lock = threading.Lock()
    active = peak = 0

    def hold():
        nonlocal active, peak
        with readme_utils._host_slot("https://acme.recruitee.com/api/offers/"):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: hold(), range(8)))
    assert 1 < peak <= readme_utils.HOST_CONCURRENCY, peak

    # A 429 on one tenant parks the provider's other workers, and only theirs.
    readme_utils._HOST_COOLDOWN.clear()
    with mock.patch("readme_utils._pace_sleep") as pace:
        readme_utils._cool_down("https://acme.recruitee.com/api/offers/", 5)
        with readme_utils._host_slot("https://other.recruitee.com/api/offers/"):
            pass
        assert pace.call_count == 1, "sibling tenant must inherit the cooldown"
        pace.reset_mock()
        with readme_utils._host_slot("https://api.lever.co/v0/postings/x"):
            pass
        assert pace.call_count == 0, "cooldown must not leak across providers"
    readme_utils._HOST_COOLDOWN.clear()

    print("  test_http_backoff: pacing checks passed")


if __name__ == "__main__":
    run_checks()
    run_pacing_checks()
    sys.exit(0)
