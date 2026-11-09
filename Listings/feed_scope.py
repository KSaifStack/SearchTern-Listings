"""The one definition of which located rows a feed may publish.

Extracted from the nested `_feed_scope_filter` in generate_listings.py so the
shadow publisher compares against the same rule instead of a second copy. The
regex and the logic are verbatim.
"""
import re

import pandas as pd
from readme_utils import canonical_url

US_LOC_RE = re.compile(
    r"\b(?:US|USA|U\.S\.A\.|United States|California|Texas|New York|Washington|"
    r"Seattle|San Francisco|SF|NYC|Austin|Chicago|Boston|Mountain View|Palo Alto|"
    r"Sunnyvale|Los Angeles|Irvine|San Diego|Santa Clara|Cupertino|Menlo Park|"
    r"Redmond|Kirkland|Bellevue|Arlington|McLean|Reston|Atlanta|Denver|Portland|"
    r"Phoenix|Philadelphia|Pittsburgh|Minneapolis|Ann Arbor|Detroit|Miami|Orlando|"
    r"Tampa|Dallas|Houston|Raleigh|Durham|Charlotte|Nashville|Salt Lake City|"
    r"St Louis|Kansas City|Columbus|Indianapolis|Milwaukee|Baltimore)\b",
    re.IGNORECASE,
)


def scope(df):
    """Keep located US, international, and remote roles; deduplicate by URL."""
    df = df.fillna({"country_iso": ""})
    remote = df["location"].str.contains(r"\bremote\b", case=False, na=False)
    if "is_remote" in df.columns:
        remote = remote | df["is_remote"].astype(str).str.lower().isin(["true", "1"])
    df = df[
        (df["country_iso"] != "")
        | df["location"].str.contains(US_LOC_RE, na=False)
        | remote
    ]
    df = df[df["location"].notna() & (df["location"] != "")]
    # Cross-source URL-canonical dedup (same posting from two sources collapses).
    if not df.empty and "link" in df.columns:
        _canon = df["link"].map(canonical_url)
        _no_canon = _canon == ""
        _with_canon = df[~_no_canon].copy()
        _with_canon["_canon"] = _canon[~_no_canon]
        _with_canon = _with_canon.drop_duplicates(subset="_canon", keep="first").drop(columns="_canon")
        df = pd.concat([df[_no_canon], _with_canon], ignore_index=True)
    return df
