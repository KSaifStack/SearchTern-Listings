"""RSS/Atom adapter: parses real feed shapes, never raises on garbage."""

import rss_source

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Acme Jobs</title>
<item><title>SWE Intern</title><link>https://acme.example/jobs/1</link>
<description>Build things.</description><pubDate>Tue, 04 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>No link</title><description>skipped</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Beta Careers</title>
<entry><title>Data Intern</title><link href="https://beta.example/jobs/9"/>
<summary>Analyse things.</summary><updated>2026-11-05T10:00:00Z</updated></entry>
</feed>"""


def test_rss_items_become_rows():
    rows = rss_source.parse(RSS, feed_name="Acme Jobs")
    assert len(rows) == 1                      # item without a link is skipped
    row = rows[0]
    assert row["role"] == "SWE Intern"
    assert row["link"] == "https://acme.example/jobs/1"
    assert row["source"] == "rss"
    assert row["description"] == "Build things."


def test_atom_entries_become_rows():
    rows = rss_source.parse(ATOM, feed_name="Beta Careers")
    assert len(rows) == 1
    assert rows[0]["link"] == "https://beta.example/jobs/9"
    assert rows[0]["role"] == "Data Intern"


def test_broken_feed_returns_nothing():
    assert rss_source.parse(b"<rss><channel>", feed_name="x") == []
    assert rss_source.parse(b"", feed_name="x") == []


def test_no_urls_configured_means_no_network(monkeypatch=None):
    import os

    os.environ.pop(rss_source.URLS_ENV, None)
    assert rss_source.configured() == []
    assert rss_source.fetch() == ([], [])
    os.environ[rss_source.URLS_ENV] = "https://a.example/feed.xml, https://b.example/feed.xml"
    try:
        assert rss_source.configured() == [
            "https://a.example/feed.xml", "https://b.example/feed.xml"]
    finally:
        os.environ.pop(rss_source.URLS_ENV, None)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
