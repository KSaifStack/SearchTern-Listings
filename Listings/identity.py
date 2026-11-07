"""Stable ATS identity for one listing: ``(ats_type, ats_id)``.

Closure votes, dedup and the published feed all key on a requisition id, but
only some sources emit one. jobhive carries ``ats_id``; the probe, breadth and
markdown adapters carry a URL. A URL is an address, not an identity, so this
module reads the id the ATS itself put in its path -- the same value jobhive
stores in ``ats_id`` -- and never guesses for a host it does not know.

Why not "last path segment" for everything: ``jobs.lever.co/acme`` has no
posting in it, and an unknown host's tail could be a location slug. A wrong id
does not just miss a dedup, it can merge two distinct requisitions, so unknown
hosts are left blank and keep their URL identity.
"""

import re
import urllib.parse

# Workday hides the requisition inside the title: /job/Austin/Tech_Lead_R12345
_WORKDAY_REQ = re.compile(r"_([A-Za-z]{0,4}\d{3,}(?:-\d+)?)$")
_BLANK = {"", "nan", "none", "null", "nat"}


def _segments(path):
    return [part for part in str(path or "").split("/") if part]


def infer_ats(link):
    """Return ``(ats_type, ats_id)`` from a posting URL, or ``("", "")``.

    Only hostnames whose URL shape is known and id-bearing qualify. Every
    pattern here was checked against a live board URL from the registry.
    """
    try:
        parts = urllib.parse.urlsplit(str(link or "").strip())
    except ValueError:
        return "", ""
    host = (parts.hostname or "").lower()
    if not host:
        return "", ""
    segs = _segments(parts.path)
    query = urllib.parse.parse_qs(parts.query)

    if host.endswith("greenhouse.io"):
        # boards/job-boards.greenhouse.io/<slug>/jobs/<id>
        if "jobs" in segs:
            pos = segs.index("jobs")
            if pos + 1 < len(segs):
                return "greenhouse", segs[pos + 1]
        token = (query.get("token") or [""])[0].strip()
        return ("greenhouse", token) if token else ("", "")

    if host.endswith("lever.co") and len(segs) >= 2:
        # jobs.lever.co/<slug>/<uuid>
        return "lever", segs[-1]

    if host.endswith("ashbyhq.com") and len(segs) >= 2:
        # jobs.ashbyhq.com/<slug>/<uuid>
        return "ashby", segs[-1]

    if host.endswith("workable.com"):
        # apply.workable.com/<slug>/j/<code> | <slug>.workable.com/j/<code>
        if "j" in segs:
            pos = segs.index("j")
            if pos + 1 < len(segs):
                return "workable", segs[pos + 1]
        return "", ""

    if host.endswith(("myworkdayjobs.com", "workday.com")) and segs:
        match = _WORKDAY_REQ.search(segs[-1])
        if match:
            return "workday", match.group(1)
        return "", ""

    if host.endswith("icims.com"):
        # <tenant>.icims.com/jobs/<id>/<slug>/job
        if "jobs" in segs:
            pos = segs.index("jobs")
            if pos + 1 < len(segs) and segs[pos + 1].isdigit():
                return "icims", segs[pos + 1]
        return "", ""

    if host.endswith("smartrecruiters.com") and len(segs) >= 2:
        # jobs.smartrecruiters.com/<slug>/<numeric id>
        return "smartrecruiters", segs[-1]

    if host.endswith("bamboohr.com"):
        # <tenant>.bamboohr.com/careers/<id>
        if "careers" in segs:
            pos = segs.index("careers")
            if pos + 1 < len(segs):
                return "bamboohr", segs[pos + 1]
        return "", ""

    if host.endswith("jobvite.com"):
        # jobs.jobvite.com/<slug>/job/<id>
        if "job" in segs:
            pos = segs.index("job")
            if pos + 1 < len(segs):
                return "jobvite", segs[pos + 1]
        return "", ""

    return "", ""


def pairs(frame):
    """Return ``(ats_type, ats_id)`` Series, filled from ``link`` when blank.

    Values a source already published are never overwritten -- they come from
    the ATS response itself and outrank anything derived from a URL.
    """
    import pandas as pd

    blank = pd.Series([""] * len(frame), index=frame.index, dtype=object)
    if "ats_type" in frame.columns:
        ats = frame["ats_type"].astype(str).str.strip()
        ats = ats.where(~ats.str.lower().isin(_BLANK), "")
    else:
        ats = blank.copy()
    if "ats_id" in frame.columns:
        req = frame["ats_id"].astype(str).str.strip()
        req = req.where(~req.str.lower().isin(_BLANK), "")
    else:
        req = blank.copy()

    missing = (ats == "") | (req == "")
    if missing.any() and "link" in frame.columns:
        inferred = frame["link"].map(infer_ats)
        ats = ats.where(~missing, inferred.map(lambda pair: pair[0]))
        req = req.where(~missing, inferred.map(lambda pair: pair[1]))
    return ats, req


def coverage(frame):
    """Share of rows that carry an id after :func:`pairs`, 0.0 for an empty frame."""
    if frame is None or len(frame) == 0:
        return {"rows": 0, "identified": 0, "pct": 0.0}
    ats, req = pairs(frame)
    identified = int(((ats != "") & (req != "")).sum())
    return {
        "rows": int(len(frame)),
        "identified": identified,
        "pct": round(100.0 * identified / len(frame), 2),
    }


def fill(frame):
    """Return a copy with ``ats_type``/``ats_id`` filled in from the URL."""
    if frame is None or len(frame) == 0:
        return frame
    ats, req = pairs(frame)
    out = frame.copy()
    out["ats_type"] = ats
    out["ats_id"] = req
    return out
