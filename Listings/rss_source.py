"""RSS/Atom job feeds as a source.

Many employers and niche boards publish a job feed at a stable URL and nothing
else, so a feed reader is the only authorized way to read them. Stdlib XML
parsing only -- a feed is attacker-shaped input, and ``xml.etree`` does not
resolve external entities or fetch DTDs, where a convenience parser might.

Feeds are configured, never discovered: ``SEARCHTERN_RSS_URLS`` is a
comma-separated list, empty by default, so this adapter imports and tests
without touching a single network call until someone opts in.
"""

import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import readme_utils

URLS_ENV = "SEARCHTERN_RSS_URLS"
_ATOM = "{http://www.w3.org/2005/Atom}"
_RSS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
# Aggregator titles carry the employer: "Bybit: Animator Intern".
_COMPANY_TITLE = re.compile(r"^(.{2,50}?):\s+(.{3,})$")


def configured():
    """Feed URLs from the environment, in the order given."""
    raw = os.environ.get(URLS_ENV, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _text(node, *names):
    for name in names:
        found = node.find(name)
        if found is not None and (found.text or "").strip():
            return " ".join((found.text or "").split())
    return ""


def _link(node):
    """Item URL: RSS puts it in <link> text, Atom in a <link href>."""
    for name in ("link", f"{_ATOM}link"):
        found = node.find(name)
        if found is None:
            continue
        href = (found.get("href") or "").strip()
        if href:
            return href
        if (found.text or "").strip():
            return found.text.strip()
    return ""


def _iso_date(raw):
    """RFC 822 (RSS) or ISO 8601 (Atom) date -> ``YYYY-MM-DD``, else "".

    The pipeline's windows compare against ``YYYY-MM-DD`` strings; a raw RSS
    ``pubDate`` is not parseable by ``pd.to_datetime`` in every pandas build.
    """
    raw = str(raw or "").strip()
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(raw).astimezone(UTC).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _company_role(title, feed_name):
    """Split an aggregator title into employer and role.

    Feed titles are mostly ``Employer: Role`` (We Work Remotely, Remotive). The
    feed host is not the employer, and the publisher blocks aggregator company
    names, so a feed that kept the host as company published nothing.
    """
    match = _COMPANY_TITLE.match(str(title or "").strip())
    if match and match.group(1).strip():
        return match.group(1).strip(), match.group(2).strip()
    return feed_name, str(title or "").strip()


def _row(title, link, description, date, feed_name):
    company, role = _company_role(title, feed_name)
    return {
        "company": company,
        "role": role,
        "location": "",
        "date": _iso_date(date),
        "link": link,
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": "",
        "description": description,
        "source": "rss",
    }


def parse(payload, feed_name=""):
    """Parse one RSS 2.0 or Atom document into listing rows.

    Returns [] for anything unparseable: a broken feed must never fail a run.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []

    items = root.findall(".//item")
    if not items:
        items = root.findall(f".//{_ATOM}entry")
    rows = []
    for item in items:
        title = _text(item, "title", f"{_ATOM}title")
        link = _link(item)
        if not title or not link:
            continue
        description = _text(
            item, "description", f"{_ATOM}summary", f"{_ATOM}content", _RSS_CONTENT
        )
        date = _text(item, "pubDate", f"{_ATOM}updated", f"{_ATOM}published")
        rows.append(_row(title, link, description, date, feed_name))
    return rows


def fetch(urls=None, workers=4):
    """Fetch every configured feed; returns (rows, per-feed stats)."""
    import concurrent.futures

    urls = configured() if urls is None else urls
    if not urls:
        return [], []

    def _one(url):
        response = readme_utils.http_get(url, timeout=25, retries=1)
        if response is None:
            return url, [], "failed"
        rows = parse(response.content, feed_name=_feed_name(url, response))
        return url, rows, "ok" if rows else "empty"

    rows, stats = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for url, feed_rows, status in pool.map(_one, urls):
            rows.extend(feed_rows)
            stats.append((url, len(feed_rows), status))
    return rows, stats


def _feed_name(url, response):
    """Human name for a feed: <title> when the document has one, else the host."""
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return url
    title = _text(root, "title", f"{_ATOM}title")
    if title:
        return title
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or url
