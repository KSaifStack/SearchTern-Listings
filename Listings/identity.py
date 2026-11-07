"""Stable ATS identity for one listing: ``(ats_type, ats_id)``.

Closure votes, dedup and the published feed all key on a requisition id, but
only some sources emit one. jobhive carries ``ats_id``; the probe, breadth and
markdown adapters carry a URL. A URL is an address, not an identity, so this
module reads the id the ATS itself put in its path -- the same value jobhive
stores in ``ats_id`` -- and never guesses for a host it does not know.

Two rules keep a guess from merging two distinct requisitions:

* **Shape.** A path segment is not automatically an id. ``jobs.ashbyhq.com/
  acme/application`` and ``jobs.lever.co/acme/apply`` are the *form routes*
  every posting shares, and reading them as ids collapsed 535 published rows
  onto two fake ids ("application", "apply") in the 2026-11-07 audit. Ashby and
  Lever ids are UUIDs, so anything else is rejected.
* **Scope.** Some ids are only unique *inside one employer* -- iCIMS, BambooHR
  and Workday number requisitions per tenant, so ``icims/10863`` belonged to
  four different companies. Those ids are stored tenant-scoped
  (``stifel/10863``), which can miss a cross-source match but can never merge
  two employers.

Unknown hosts are left blank and keep their URL identity: a wrong id does not
just miss a dedup, it can delete or merge the wrong posting.
"""

import re
import urllib.parse

# Workday hides the requisition inside the title: /job/Austin/Tech_Lead_R12345
_WORKDAY_REQ = re.compile(r"_([A-Za-z]{1,4}-?\d{3,}(?:-\d+)?)$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Route words that appear where an id would be, and are not ids.
_ROUTE = frozenset({
    "application", "apply", "jobs", "job", "careers", "career", "embed",
    "login", "api", "oauth", "search", "external", "postings", "posting",
    "openings", "talent", "index", "new", "all",
})
_BLANK = {"", "nan", "none", "null", "nat"}


def _segments(path):
    return [part for part in str(path or "").split("/") if part]


def _valid(value):
    """A candidate id, or "" if it is a route/blank rather than an id."""
    value = str(value or "").strip().strip("/")
    if not value or value.lower() in _ROUTE or value.lower() in _BLANK:
        return ""
    return value


def _tenant(host):
    """The employer namespace of a shared ATS host: acme.wd1.myworkdayjobs.com.

    iCIMS front-ends often put the board in a prefixed subdomain
    (``careers-stifel.icims.com``), so the prefix is dropped: the tenant is the
    employer, not the product.
    """
    tenant = host.split(".")[0]
    return re.sub(r"^(careers|career|jobs|job|apply|recruiting|talent)[-]", "", tenant)


def infer_ats(link):
    """Return ``(ats_type, ats_id)`` from a posting URL, or ``("", "")``.

    Only hostnames whose URL shape is known and id-bearing qualify. Every
    pattern here was checked against a live board URL and against the published
    feed for false matches (2026-11-07 identity audit).
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
        # boards/job-boards.greenhouse.io/<slug>/jobs/<id>. Ids are global.
        if "jobs" in segs:
            pos = segs.index("jobs")
            if pos + 1 < len(segs):
                candidate = _valid(segs[pos + 1])
                if candidate.isdigit() and len(candidate) >= 5:
                    return "greenhouse", candidate
        token = _valid((query.get("token") or [""])[0])
        if token.isdigit() and len(token) >= 5:
            return "greenhouse", token
        return "", ""

    if host.endswith("lever.co") and len(segs) >= 2:
        # jobs.lever.co/<slug>/<uuid>; /apply is a route, not a requisition.
        candidate = _valid(segs[-1])
        return ("lever", candidate) if _UUID.match(candidate) else ("", "")

    if host.endswith("ashbyhq.com") and len(segs) >= 2:
        # jobs.ashbyhq.com/<slug>/<uuid>; /application is a route.
        candidate = _valid(segs[-1])
        return ("ashby", candidate) if _UUID.match(candidate) else ("", "")

    if host.endswith("workable.com"):
        # apply.workable.com/<slug>/j/<code> | <slug>.workable.com/j/<code>
        if "j" in segs:
            pos = segs.index("j")
            if pos + 1 < len(segs):
                candidate = _valid(segs[pos + 1])
                if len(candidate) >= 6 and candidate.isalnum():
                    return "workable", candidate
        return "", ""

    if host.endswith(("myworkdayjobs.com", "workday.com")) and segs:
        match = _WORKDAY_REQ.search(_valid(segs[-1]))
        if match:
            # Requisition numbers restart per employer, so keep the tenant.
            return "workday", f"{_tenant(host)}/{match.group(1)}"
        return "", ""

    if host.endswith("icims.com"):
        # <tenant>.icims.com/jobs/<id>/<slug>/job
        if "jobs" in segs:
            pos = segs.index("jobs")
            if pos + 1 < len(segs) and segs[pos + 1].isdigit():
                return "icims", f"{_tenant(host)}/{segs[pos + 1]}"
        return "", ""

    if host.endswith("smartrecruiters.com") and len(segs) >= 2:
        # jobs.smartrecruiters.com/<Company>/<numeric id>-<title slug>
        match = re.match(r"(\d{6,})", _valid(segs[-1]))
        return ("smartrecruiters", match.group(1)) if match else ("", "")

    if host.endswith("bamboohr.com"):
        # <tenant>.bamboohr.com/careers/<id>, numbered per tenant.
        if "careers" in segs:
            pos = segs.index("careers")
            candidate = _valid(segs[pos + 1]) if pos + 1 < len(segs) else ""
            if candidate:
                return "bamboohr", f"{_tenant(host)}/{candidate}"
        return "", ""

    if host.endswith("jobvite.com"):
        # jobs.jobvite.com/<slug>/job/<id>; ids are opaque and path-scoped.
        if "job" in segs:
            pos = segs.index("job")
            candidate = _valid(segs[pos + 1]) if pos + 1 < len(segs) else ""
            if len(candidate) >= 6 and candidate.isalnum():
                return "jobvite", candidate
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
