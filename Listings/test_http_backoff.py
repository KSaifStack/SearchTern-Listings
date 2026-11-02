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
    return getter, sleeper


def run_checks():
    # 200 returns immediately, one request, no sleep.
    getter, sleeper = _patch([_Resp()])
    with getter as g, sleeper as s:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 200
    assert g.call_count == 1
    assert s.call_count == 0

    # One 429 then success: sleeps (2^0 + jitter) and retries.
    getter, sleeper = _patch([_Resp(429), _Resp()])
    with getter as g, sleeper as s:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 200
    assert g.call_count == 2
    assert s.call_count == 1
    wait = s.call_args[0][0]
    assert 1 <= wait <= 2  # 2^0=1 + uniform(0,1) -> [1,2)

    # Retry-After is honored as a floor for the wait.
    getter, sleeper = _patch([_Resp(503, {"Retry-After": "10"}), _Resp()])
    with getter as g, sleeper as s:
        readme_utils.http_get("http://u", timeout=5)
    assert g.call_count == 2
    assert s.call_args[0][0] >= 10

    # Retry-After > 60s gives up immediately (returns the 429).
    getter, sleeper = _patch([_Resp(429, {"Retry-After": "120"})])
    with getter as g, sleeper as s:
        r = readme_utils.http_get("http://u", timeout=5)
    assert r.status_code == 429
    assert g.call_count == 1
    assert s.call_count == 0

    # Persistent 429s exhaust retries -> None, sleep backs off each attempt.
    getter, sleeper = _patch([_Resp(429), _Resp(429), _Resp(429)])
    with getter as g, sleeper as s:
        r = readme_utils.http_get("http://u", timeout=5, retries=3)
    assert r is None
    assert g.call_count == 3
    assert s.call_count == 2

    # Permanent authorization failures must not be retried.
    getter, sleeper = _patch([_Resp(403)])
    with getter as g, sleeper as s:
        assert readme_utils.http_get("http://u").status_code == 403
    assert g.call_count == 1 and s.call_count == 0

    # A streamed retry response must release its pooled connection.
    rate_limited = _Resp(429)
    getter, sleeper = _patch([rate_limited, _Resp()])
    with getter, sleeper:
        readme_utils.http_get("http://u", stream=True)
    assert rate_limited.closed

    # Network error then success is recovered.
    getter, sleeper = _patch(
        [readme_utils.requests.RequestException("timeout"), _Resp()]
    )
    with getter as g, sleeper as s:
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


if __name__ == "__main__":
    run_checks()
    sys.exit(0)
