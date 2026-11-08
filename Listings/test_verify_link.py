"""Dead-link verification must be conservative: it deletes published rows.

`_verify_link` runs over stale postings before publication and drops the ones it
calls dead, so a false "dead" is a live job removed from the feed. Only 404 and
410 mean gone; rate limits, auth walls, server errors, timeouts and transport
exceptions all mean "could not tell", and the row stays.
"""

import generate_listings
import pytest
import requests


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _FakeRequests:
    """Stand-in for the `requests` module inside `_verify_link`."""

    def __init__(self, head=None, get=None):
        self._head = head
        self._get = get
        self.head_calls = []
        self.get_calls = []

    def _call(self, kind, spec, url, **kwargs):
        if kind == "head":
            self.head_calls.append((url, kwargs))
        else:
            self.get_calls.append((url, kwargs))
        if isinstance(spec, Exception):
            raise spec
        return _Resp(spec)


def _install(monkeypatch, head=None, get=None):
    fake = _FakeRequests()
    fake.head = lambda url, **kw: fake._call("head", head, url, **kw)
    fake.get = lambda url, **kw: fake._call("get", get, url, **kw)
    monkeypatch.setattr(generate_listings, "requests", fake)
    return fake


LINK = "https://boards.example.com/acme/jobs/123"


def test_200_is_alive_without_second_request(monkeypatch):
    fake = _install(monkeypatch, head=200)
    assert generate_listings._verify_link(LINK) is True
    assert fake.get_calls == []


@pytest.mark.parametrize("code", [404, 410])
def test_gone_status_is_dead_after_get_confirms(monkeypatch, code):
    """HEAD and the ranged GET must agree before a row is dropped."""
    fake = _install(monkeypatch, head=code, get=code)
    assert generate_listings._verify_link(LINK) is False
    assert len(fake.head_calls) == 1
    assert len(fake.get_calls) == 1
    assert fake.get_calls[0][1]["headers"]["Range"].startswith("bytes=")


@pytest.mark.parametrize(
    "code",
    [403, 429, 500, 502, 503, 405, 302],
)
def test_blocked_or_broken_head_keeps_the_row(monkeypatch, code):
    """Auth walls, rate limits, server errors and odd methods are not evidence."""
    fake = _install(monkeypatch, head=code, get=404)
    assert generate_listings._verify_link(LINK) is True
    assert fake.get_calls == []


def test_head_404_but_get_200_is_alive(monkeypatch):
    """Several ATS hosts 404 on HEAD and serve 200 on GET (GM among them)."""
    _install(monkeypatch, head=404, get=200)
    assert generate_listings._verify_link(LINK) is True


def test_head_404_but_get_204_is_alive(monkeypatch):
    _install(monkeypatch, head=404, get=204)
    assert generate_listings._verify_link(LINK) is True


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("slow"),
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.TooManyRedirects("loop"),
        RuntimeError("anything unexpected"),
    ],
)
def test_head_exception_keeps_the_row(monkeypatch, error):
    _install(monkeypatch, head=error)
    assert generate_listings._verify_link(LINK) is True


def test_get_exception_after_404_keeps_the_row(monkeypatch):
    """An inconclusive follow-up is not a confirmation of death."""
    _install(monkeypatch, head=404, get=requests.exceptions.ReadTimeout("slow"))
    assert generate_listings._verify_link(LINK) is True


def test_probe_uses_timeout_and_redirects(monkeypatch):
    fake = _install(monkeypatch, head=200)
    generate_listings._verify_link(LINK)
    url, kwargs = fake.head_calls[0]
    assert url == LINK
    assert kwargs["timeout"] == 10
    assert kwargs["allow_redirects"] is True
