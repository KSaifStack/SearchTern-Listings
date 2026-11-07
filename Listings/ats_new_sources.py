"""New ATS adapters: Eightfold, Oracle Recruiting Cloud, iCIMS, ADP, BambooHR.

Each endpoint below was verified live on 2026-11-02 with read-only requests
(see docs/reverse-engineering/ats-expansion.md for the raw evidence):

* Eightfold  GET {host}/api/pcsx/search?domain={domain}&query=&location=&start=N
  10 rows/page, robots.txt allows /api/pcsx. Server-side query filter cuts
  paging (qualcomm: 2061 -> 115 for query=intern). Microsoft's host 429s
  cloud IPs on the first request, so it is excluded and any 429 stops the
  host for the run instead of retrying into it.
* Oracle    GET {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
  ?finder=findReqs;siteNumber={site},limit=200,offset=N with
  expand=requisitionList.secondaryLocations. (host, site) pairs come from
  feed links (/hcmUI/CandidateExperience/en/sites/{site}/job/{id}); the
  siteNumber is NOT derivable from a company name. Rows carry ISO PostedDate,
  Id, locations and description HTML inline -- no per-job calls.
* iCIMS     Portal search pages embed /jobs/{id}/{slug}/job links server-side;
  each job page carries schema.org JobPosting JSON-LD (title, datePosted,
  employmentType, jobLocation, description). Slugs contain the title, so
  intern-shaped slugs are fetched first under a per-portal cap.
* ADP       GET myjobs.adp.com/public/staffing/v1/career-site/{domain} gives
  the orgoid header the list requires; the list carries postingDate, reqId,
  description and locations inline. Job URL: myjobs.adp.com/{domain}/cx/job/{reqId}.
* BambooHR  GET {tenant}.bamboohr.com/careers/list is one response (id,
  jobOpeningName, location; no dates). Details ({host}/careers/{id}/detail)
  are fetched only for intern-shaped titles under a cap.
* WorkforceNow GET workforcenow.adp.com/mascsr/default/careercenter/public/
  events/staffing/v1/job-requisitions?cid={cid}&ccId={ccId}&lang=en_US&$top=100
  returns requisitionTitle, postDate, itemID and locations inline (verified
  2026-11-07). It caps at 20 requisitions per request whatever $top asks for
  ($top=100 still returned 20), so pages are walked with $skip and de-duped on
  itemID. The
  public posting URL is recruitment.html?cid=&ccId=&jobId={itemID}: itemID is
  exactly the jobId the board's own links carry, so rows deep-link rather than
  pointing at the board. cids are not derivable from a company name -- they
  come from feed links (wfn_boards_from_links) or the verified list below.

Conventions match breadth_sources.py: STD_COLS rows, per-source stats via
ingest_stats, polite delays, hard caps, truncated/rate_limited reporting.
"""

import json
import os
import re
import time
import urllib.parse

import pandas as pd
from readme_utils import http_get

STD_COLS = [
    "company", "role", "location", "date", "link", "is_remote",
    "salary_min", "salary_max", "salary_currency", "country_iso",
]

_UA = {"User-Agent": "SearchTern-Listings/1.0 (+https://github.com/KSaifStack/SearchTern-Listings)"}

EIGHTFOLD_MAX_PAGES = int(os.environ.get("SEARCHTERN_EIGHTFOLD_MAX_PAGES", "40"))
EIGHTFOLD_PAGE_DELAY = float(os.environ.get("SEARCHTERN_EIGHTFOLD_DELAY", "1.0"))
EIGHTFOLD_QUERIES = ("intern", "new grad")
ORACLE_MAX_JOBS = int(os.environ.get("SEARCHTERN_ORACLE_MAX_JOBS", "2000"))
ICIMS_MAX_JOBS = int(os.environ.get("SEARCHTERN_ICIMS_MAX_JOBS", "25"))
ICIMS_PAGE_DELAY = float(os.environ.get("SEARCHTERN_ICIMS_DELAY", "0.5"))
ADP_MAX_JOBS = int(os.environ.get("SEARCHTERN_ADP_MAX_JOBS", "1000"))
BAMBOO_MAX_DETAILS = int(os.environ.get("SEARCHTERN_BAMBOO_MAX_DETAILS", "30"))
WFN_CC_ID = os.environ.get("SEARCHTERN_WFN_CC_ID", "19000101_000001")
WFN_MAX_JOBS = int(os.environ.get("SEARCHTERN_WFN_MAX_JOBS", "100"))
WFN_PAGE = 20          # server cap per request, whatever $top asks for
WFN_MAX_PAGES = int(os.environ.get("SEARCHTERN_WFN_MAX_PAGES", "10"))
WFN_PAGE_DELAY = float(os.environ.get("SEARCHTERN_WFN_DELAY", "0.3"))

# (company, eightfold host, domain). Every entry answered HTTP 200 on 2026-11-02.
# microsoft.eightfold.ai is deliberately excluded: it 429s cloud IPs immediately.
EIGHTFOLD_BOARDS = [
    ("Qualcomm", "qualcomm.eightfold.ai", "qualcomm.com"),
    ("Starbucks", "starbucks.eightfold.ai", "starbucks.com"),
    ("CACI", "caci.eightfold.ai", "caci.com"),
    ("Applied Materials", "appliedmaterials.eightfold.ai", "appliedmaterials.com"),
    ("Arcadis", "arcadis.eightfold.ai", "arcadis.com"),
]

# (company, oraclecloud host, siteNumber). siteNumber comes from feed links,
# verified against recruitingCESites behaviour on 2026-11-02.
ORACLE_BOARDS = [
    ("WSP", "emit.fa.ca3.oraclecloud.com", "CX_2001"),
    ("Stantec", "hdhl.fa.us6.oraclecloud.com", "CX_1"),
]

# (company, icims portal host). Portals verified to embed job links server-side.
ICIMS_PORTALS = [
    ("General Dynamics Mission Systems", "careers-gdms.icims.com"),
    ("Kimley-Horn", "careers-kimley-horn.icims.com"),
    ("Dewberry", "careers-dewberry.icims.com"),
    ("Sargent & Lundy", "careers-sargentlundy.icims.com"),
    ("Peraton", "careers-peraton.icims.com"),
    ("Novelis", "careers-novelis.icims.com"),
]

# (company, myjobs.adp.com domain). orgoid resolves live per domain.
ADP_DOMAINS = [
    ("Pennoni", "pennoniexternalcareers"),
    ("Carolina Power", "carolinapowercareers"),
]

# (company, bamboohr tenant). The list carries no employer name, so tenants
# are curated by hand, never guessed.
BAMBOO_TENANTS = [
    ("Lunar Outpost", "lunaroutpost"),
]

# (company, workforcenow cid). Every entry answered HTTP 200 on 2026-11-07 with
# a non-zero meta.totalNumber. cids are opaque GUIDs, so they are curated or
# lifted from a feed link's ?cid=, never guessed.
WFN_BOARDS = [
    ("Intuitive Machines", "b0e24f83-6e4d-492d-9d6a-bc0fea197d6a"),
    ("North Wind", "f98fa7ab-a90c-4c3d-9d0f-acc72295894f"),
    ("Veterinary United", "53ad25cb-cc74-4ae1-87e3-8123a39f433c"),
    ("Community Care Plan", "5819e54c-5980-4670-adef-3258d4b05f16"),
    ("Drt Holdings", "deb3ce72-1fdb-482c-8628-1246bb38b0dc"),
    ("The Bernard Group", "1c6d33f4-8e70-4489-b6b7-2f1d9ca08e81"),
    ("Patriot Group International", "1e1c7010-328a-4fd5-8cab-54255cb16096"),
    ("Avalon", "47dbbe30-59f3-4be8-90d6-9934bef76b9f"),
    ("Pathstone", "89066319-3d2d-43f2-88e4-ea45aa701c73"),
    ("Steinberg Diagnostic Medical Imaging",
     "0f4ed7c3-5fe1-4305-b131-0fdbd9c753a7"),
    ("Meazure Learning", "80476592-d857-4207-83c6-fe66444a7e3b"),
    ("Curious Plot", "1c21058f-86b9-4409-a52d-cc79be9f6952"),
    ("Arcadia", "ccd74f4a-a9f5-457b-9738-346574383d58"),
    ("Canaccord Genuity", "cf46812f-b021-426f-844b-c83523c426e6"),
    ("Carlos Rosario Public Charter School",
     "33b607f4-564d-4335-b098-98dd2b218da4"),
]

_INTERN_SLUG_RE = re.compile(
    r"(intern|internship|co-?op|new-?grad|entry-?level|junior|trainee|"
    r"apprentice|early-?career|fellow|student|campus)", re.I,
)
# Job links carry a query string (/job?in_iframe=1), so the tail must allow ?...&....
_JOB_LINK_RE = re.compile(r'href="([^"]*?/jobs/(\d+)/([^"/?]*?)/job[^"]*)"', re.I)
_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_ORACLE_LINK_RE = re.compile(
    r"https?://([a-z0-9.\-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
    r"[a-z]{2}/sites/([A-Za-z0-9_\-]+)/job/(\d+)", re.I,
)
_WFN_CID_RE = re.compile(r"[?&]cid=([0-9a-fA-F\-]{36})")
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
# Corporate suffixes break cross-source dedup ("Acme, Inc." vs "Acme").
# Stripped here, in the new adapters only; the shared clean_company_name is
# untouched until the global suffix pass in FUTURE.md lands.
_SUFFIX_RE = re.compile(
    r"[\s,\.]+(inc|l\.l\.c|llc|corp|corporation|ltd|limited|co|company|plc|"
    r"pbc|gmbh)(\.|\s|$)", re.I)


def _strip_html(text):
    return _STRIP_TAGS_RE.sub(" ", str(text or "")).replace("&nbsp;", " ").strip()


def _from_unix(value):
    try:
        return str(pd.Timestamp(float(value), unit="s", tz="UTC"))
    except (TypeError, ValueError):
        return ""


def _from_date(value):
    if not value:
        return ""
    try:
        return str(pd.Timestamp(value))
    except (TypeError, ValueError):
        return ""


def _clean_employer(name):
    name = (name or "").strip().rstrip(",.")
    name = _SUFFIX_RE.sub("", name).strip().rstrip(",.")
    return " ".join(name.split())


def _row(company, role, location, date, link, is_remote,
         salary_min=None, salary_max=None, salary_currency=None,
         description=""):
    row = {
        "company": _clean_employer(company),
        "role": (role or "").strip(),
        "location": (location or "").strip(),
        "date": date or "",
        "link": (link or "").strip(),
        "is_remote": "true" if is_remote else "false",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "country_iso": "",
    }
    if description:
        row["description"] = description
    return row


# ── Eightfold ────────────────────────────────────────────────────────────────

def _eightfold_search(host, domain, query, start):
    params = urllib.parse.urlencode(
        {"domain": domain, "query": query, "location": "", "start": start})
    resp = http_get(f"https://{host}/api/pcsx/search?{params}",
                    timeout=25, retries=1, headers=_UA)
    return resp


def _eightfold_row(company, host, position):
    locs = position.get("locations") or []
    link = position.get("positionUrl") or ""
    return _row(
        company, position.get("name"), locs[0] if locs else "",
        _from_unix(position.get("postedTs")),
        f"https://{host}{link}" if link.startswith("/") else link,
        (position.get("workLocationOption") or "").lower() == "remote",
    )


def _fetch_eightfold_board(company, host, domain):
    """(rows, truncated, rate_limited). Sequential pages, polite delay."""
    rows, seen = [], set()
    truncated = False
    rate_limited = False
    for query in EIGHTFOLD_QUERIES:
        start, pages = 0, 0
        while pages < EIGHTFOLD_MAX_PAGES:
            resp = _eightfold_search(host, domain, query, start)
            if resp is None:
                break
            if resp.status_code == 429:
                rate_limited = True
                break
            if resp.status_code != 200:
                break
            try:
                data = (resp.json().get("data") or {})
            except ValueError:
                break
            positions = data.get("positions") or []
            if not positions:
                break
            fresh = [p for p in positions
                     if str(p.get("id") or "") not in seen]
            if not fresh:
                break
            for p in fresh:
                seen.add(str(p.get("id") or ""))
                rows.append(_eightfold_row(company, host, p))
            total = data.get("count") or 0
            start += len(positions)
            pages += 1
            if start >= total or len(positions) < 10:
                break
            if len(seen) >= EIGHTFOLD_MAX_PAGES * 10:
                truncated = True
                break
            time.sleep(EIGHTFOLD_PAGE_DELAY)
        if rate_limited:
            break
        time.sleep(EIGHTFOLD_PAGE_DELAY)
    return rows, truncated, rate_limited


# ── Oracle Recruiting Cloud ──────────────────────────────────────────────────

def oracle_boards_from_links(links):
    """Derive (company, host, site) candidates from feed job URLs.

    Returns {host: (site, count)} for Oracle Candidate Experience job links;
    the caller maps hosts to employer names. Pure parsing, no network.
    """
    out = {}
    for link in links or ():
        m = _ORACLE_LINK_RE.search(str(link or ""))
        if m:
            host, site = m.group(1).lower(), m.group(2)
            prev = out.get(host)
            out[host] = (site, (prev[1] if prev else 0) + 1)
    return out


def _fetch_oracle_board(company, host, site):
    rows, offset = [], 0
    total, truncated = 0, False
    while len(rows) < ORACLE_MAX_JOBS and offset < 9800:
        finder = (f"findReqs;siteNumber={site},limit=200,offset={offset},"
                  f"sortBy=POSTING_DATES_DESC")
        url = (f"https://{host}/hcmRestApi/resources/latest/"
               f"recruitingCEJobRequisitions?onlyData=true&"
               f"expand=requisitionList.secondaryLocations&finder="
               + urllib.parse.quote(finder, safe=";,=_"))
        resp = http_get(url, timeout=30, retries=2, headers=_UA)
        if resp is None or resp.status_code != 200:
            return rows, True, resp.status_code if resp else None
        try:
            items = resp.json().get("items") or []
        except ValueError:
            return rows, True, "bad-json"
        if not items:
            break
        item = items[0]
        total = item.get("TotalJobsCount") or total
        reqs = item.get("requisitionList") or []
        if not reqs:
            break
        for r in reqs:
            desc = _strip_html(
                (r.get("ShortDescriptionStr") or "") or
                ((r.get("ExternalResponsibilitiesStr") or "") + "\n" +
                 (r.get("ExternalQualificationsStr") or "")))
            rows.append(_row(
                company, r.get("Title"),
                r.get("PrimaryLocation") or "",
                _from_date(r.get("PostedDate")),
                f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                f"{site}/job/{r.get('Id')}",
                "remote" in str(r.get("WorkplaceType") or "").lower(),
                description=desc,
            ))
        if len(reqs) < 200:
            break
        offset += 200
    if total and total > len(rows):
        truncated = True
    return rows, truncated, None


# ── iCIMS ────────────────────────────────────────────────────────────────────

def _icims_job_links(portal):
    resp = http_get(f"https://{portal}/jobs/search?pr=0&in_iframe=1",
                    timeout=30, retries=2, headers=_UA)
    if resp is None or resp.status_code != 200:
        return None
    links = []
    for href, job_id, slug in _JOB_LINK_RE.findall(resp.text):
        url = href if href.startswith("http") else f"https://{portal}{href}"
        links.append((job_id, slug, url))
    # De-dupe on id, intern-shaped slugs first so the cap keeps the best rows.
    best = {}
    for job_id, slug, url in links:
        if job_id not in best or _INTERN_SLUG_RE.search(slug):
            if job_id in best and not _INTERN_SLUG_RE.search(slug):
                continue
            best[job_id] = (slug, url)
    ordered = sorted(best.items(),
                     key=lambda kv: (0 if _INTERN_SLUG_RE.search(kv[1][0]) else 1,
                                     kv[0]))
    return [(jid, slug, url) for jid, (slug, url) in ordered]


def _icims_row(company, url):
    resp = http_get(url, timeout=25, retries=1, headers=_UA)
    if resp is None or resp.status_code != 200:
        return None
    for block in _LD_RE.findall(resp.text):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(data, list):
            data = next((d for d in data
                         if isinstance(d, dict)
                         and d.get("@type") == "JobPosting"), None)
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        loc = ""
        job_loc = data.get("jobLocation")
        addr = None
        if isinstance(job_loc, list) and job_loc:
            addr = job_loc[0].get("address")
        elif isinstance(job_loc, dict):
            addr = job_loc.get("address")
        if isinstance(addr, dict):
            loc = ", ".join(filter(None, [
                addr.get("addressLocality"), addr.get("addressRegion"),
                addr.get("addressCountry")]))
        return _row(
            (data.get("hiringOrganization") or {}).get("name") or company,
            data.get("title"), loc, _from_date(data.get("datePosted")),
            data.get("url") or url,
            "remote" in loc.lower(),
            description=_strip_html(data.get("description")),
        )
    return None


def _fetch_icims_portal(company, portal):
    found = _icims_job_links(portal)
    if found is None:
        return [], False, "unreachable"
    rows = []
    for _jid, _slug, url in found[:ICIMS_MAX_JOBS]:
        time.sleep(ICIMS_PAGE_DELAY)
        row = _icims_row(company, url)
        if row and row["role"]:
            rows.append(row)
    return rows, len(found) > ICIMS_MAX_JOBS, None


# ── ADP ──────────────────────────────────────────────────────────────────────

def _fetch_adp_domain(company, domain):
    site = http_get(f"https://myjobs.adp.com/public/staffing/v1/career-site/{domain}",
                    timeout=25, retries=2, headers=_UA)
    if site is None or site.status_code != 200:
        return [], False, "no-orgoid"
    try:
        meta = site.json()
    except ValueError:
        return [], False, "bad-json"
    orgoid = meta.get("orgoid")
    employer = meta.get("clientName") or company
    if not orgoid:
        return [], False, "no-orgoid"
    headers = {**_UA, "orgoid": orgoid}
    rows, skip, total = [], 0, 0
    while len(rows) < ADP_MAX_JOBS:
        url = (f"https://myjobs.adp.com/public/staffing/v1/job-requisitions"
               f"?careerSiteDomain={domain}&$top=100&$skip={skip}&$select="
               f"jobTitle,reqId,postingDate,jobDescription,requisitionLocations")
        resp = http_get(url, timeout=30, retries=2, headers=headers)
        if resp is None or resp.status_code != 200:
            return rows, True, "list-failed"
        try:
            data = resp.json()
        except ValueError:
            return rows, True, "bad-json"
        total = data.get("count") or total
        reqs = data.get("jobRequisitions") or []
        if not reqs:
            break
        for r in reqs:
            locs = r.get("requisitionLocations") or []
            loc = ""
            if locs:
                first = locs[0] or {}
                loc = ", ".join(filter(None, [
                    first.get("city"), first.get("state"),
                    first.get("country")]))
            rows.append(_row(
                employer, r.get("jobTitle"), loc,
                _from_date(r.get("postingDate")),
                f"https://myjobs.adp.com/{domain}/cx/job/{r.get('reqId')}",
                "remote" in loc.lower(),
                description=_strip_html(r.get("jobDescription")),
            ))
        if len(reqs) < 100:
            break
        skip += 100
    return rows, bool(total and total > len(rows)), None


# ── BambooHR ─────────────────────────────────────────────────────────────────

def _fetch_bamboo_tenant(company, tenant):
    resp = http_get(f"https://{tenant}.bamboohr.com/careers/list",
                    timeout=25, retries=2, headers=_UA)
    if resp is None or resp.status_code != 200:
        return [], False, "unreachable"
    try:
        result = resp.json().get("result") or []
    except ValueError:
        content = (resp.text or "").lstrip()[:1]
        if content != "{":
            return [], False, "not-json-tenant-moved"
        return [], False, "bad-json"
    wanted = [r for r in result
              if _INTERN_SLUG_RE.search(str(r.get("jobOpeningName") or ""))]
    rows = []
    for r in wanted[:BAMBOO_MAX_DETAILS]:
        detail = http_get(
            f"https://{tenant}.bamboohr.com/careers/{r.get('id')}/detail",
            timeout=25, retries=1, headers=_UA)
        if detail is None or detail.status_code != 200:
            continue
        try:
            job = (detail.json().get("result") or {}).get("jobOpening") or {}
        except ValueError:
            continue
        loc = r.get("location") or {}
        rows.append(_row(
            company, job.get("jobOpeningName") or r.get("jobOpeningName"),
            ", ".join(filter(None, [loc.get("city"), loc.get("state")])),
            _from_date(job.get("datePosted")),
            f"https://{tenant}.bamboohr.com/careers/{r.get('id')}",
            bool(r.get("isRemote")),
            salary_currency=None,
            description=_strip_html(job.get("description")),
        ))
        time.sleep(0.5)
    return rows, len(wanted) > BAMBOO_MAX_DETAILS, None


# ── ADP WorkforceNow ────────────────────────────────────────────────────────

def wfn_boards_from_links(links):
    """Derive WorkforceNow cids from feed job URLs. Pure parsing, no network.

    Returns {cid: count} for links carrying ?cid=. The employer name has to come
    from the caller, since the board API returns no company field.
    """
    out = {}
    for link in links or ():
        m = _WFN_CID_RE.search(str(link or ""))
        if m:
            cid = m.group(1).lower()
            out[cid] = out.get(cid, 0) + 1
    return out


def _wfn_row(company, cid, cc_id, req):
    loc = ""
    locs = req.get("requisitionLocations") or []
    if locs:
        addr = (locs[0] or {}).get("address") or {}
        sub = addr.get("countrySubdivisionLevel1") or {}
        loc = ", ".join(filter(None, [
            addr.get("cityName"),
            sub.get("codeValue") if isinstance(sub, dict) else "",
        ]))
    # itemID is the id the board's own posting links carry as jobId.
    return _row(
        company, req.get("requisitionTitle"), loc,
        _from_date(req.get("postDate")),
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
        f"recruitment.html?cid={cid}&ccId={cc_id}&jobId={req.get('itemID')}"
        "&lang=en_US",
        "remote" in loc.lower(),
    )


def _fetch_wfn_board(company, cid, cc_id=None):
    """(rows, truncated, error) for one WorkforceNow career centre.

    Pages on $skip and de-duplicates on itemID, so a board that ignores $skip
    stops after its first page instead of repeating rows.
    """
    cc_id = cc_id or WFN_CC_ID
    rows, seen, skip, total, pages = [], set(), 0, 0, 0
    while len(rows) < WFN_MAX_JOBS and pages < WFN_MAX_PAGES:
        url = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/"
               f"events/staffing/v1/job-requisitions?cid={cid}&ccId={cc_id}"
               f"&lang=en_US&$top={WFN_PAGE}&$skip={skip}")
        resp = http_get(url, timeout=30, retries=2, headers=_UA)
        if resp is None:
            return rows, False, "unreachable"
        if resp.status_code != 200:
            return rows, False, f"http-{resp.status_code}"
        try:
            data = resp.json()
        except ValueError:
            return rows, False, "bad-json"
        reqs = data.get("jobRequisitions") or []
        total = (data.get("meta") or {}).get("totalNumber") or total
        fresh = 0
        for r in reqs:
            key = str(r.get("itemID") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(_wfn_row(company, cid, cc_id, r))
            fresh += 1
        pages += 1
        # A page can come back short (19 of 20 seen live 2026-11-07), so stop on
        # "nothing new" or on reaching meta.totalNumber, never on page length.
        if not fresh or (total and len(seen) >= total):
            break
        skip += WFN_PAGE
        time.sleep(WFN_PAGE_DELAY)
    kept = min(len(rows), WFN_MAX_JOBS)
    return rows[:kept], bool(total and total > kept), None


# ── driver ───────────────────────────────────────────────────────────────────

_SOURCES = [
    ("eightfold", [(c, h, d) for c, h, d in EIGHTFOLD_BOARDS]),
    ("oracle", [(c, h, s) for c, h, s in ORACLE_BOARDS]),
    ("icims", [(c, p) for c, p in ICIMS_PORTALS]),
    ("adp", [(c, d) for c, d in ADP_DOMAINS]),
    ("bamboohr", [(c, t) for c, t in BAMBOO_TENANTS]),
    ("wfn", [(c, cid) for c, cid in WFN_BOARDS]),
]


def fetch_and_parse(infer_country=None, workers=4):
    """Fetch all boards. Returns (df, stats) like breadth_sources."""
    import ingest_stats

    rows, stats, extra = [], [], {}

    def _run_eightfold(args):
        company, host, domain = args
        t0 = time.monotonic()
        try:
            got, trunc, limited = _fetch_eightfold_board(company, host, domain)
        except Exception as e:  # noqa: BLE001 - one board must not kill the run
            return (f"eightfold:{company}", [], 0.0, f"error: {e}", False)
        return (f"eightfold:{company}", got, time.monotonic() - t0,
                "rate_limited" if limited else ("ok" if not trunc else "truncated"),
                trunc)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        ef = list(ex.map(_run_eightfold, EIGHTFOLD_BOARDS))
    for name, got, secs, status, trunc in ef:
        stats.append((name, len(got)))
        rows.extend(got)
        extra[name] = (secs, status, trunc)

    def _run_rest(kind, fetch, args):
        t0 = time.monotonic()
        try:
            got, trunc, err = fetch(*args)
        except Exception as e:  # noqa: BLE001
            return (f"{kind}:{args[0]}", [], 0.0, f"error: {e}", False)
        if err and not got:
            return (f"{kind}:{args[0]}", [], time.monotonic() - t0, err, False)
        return (f"{kind}:{args[0]}", got, time.monotonic() - t0,
                ("truncated" if trunc else "ok"), trunc)

    rest = ([("oracle", _fetch_oracle_board, (c, h, s)) for c, h, s in ORACLE_BOARDS]
            + [("icims", _fetch_icims_portal, (c, p)) for c, p in ICIMS_PORTALS]
            + [("adp", _fetch_adp_domain, (c, d)) for c, d in ADP_DOMAINS]
            + [("bamboohr", _fetch_bamboo_tenant, (c, t)) for c, t in BAMBOO_TENANTS]
            + [("wfn", _fetch_wfn_board, (c, cid)) for c, cid in WFN_BOARDS])
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rest_done = list(ex.map(lambda item: _run_rest(*item), rest))
    for name, got, secs, status, trunc in rest_done:
        stats.append((name, len(got)))
        rows.extend(got)
        extra[name] = (secs, status, trunc)

    counts = dict(stats)
    for name, (secs, status, trunc) in extra.items():
        ingest_stats.record(name, discovered=counts.get(name, 0),
                            valid=counts.get(name, 0), seconds=secs,
                            status=status,
                            truncated_boards=1 if trunc else 0)

    df = pd.DataFrame(rows, columns=STD_COLS + (["description"] if any(
        "description" in r for r in rows) else []))
    if not df.empty and infer_country is not None:
        df["country_iso"] = df["location"].map(lambda v: infer_country(str(v or "")))
    return df, stats


if __name__ == "__main__":
    df, stats = fetch_and_parse()
    print(stats)
    assert not df.empty, "no jobs parsed — feed regression"
    assert set(STD_COLS) <= set(df.columns), "missing standard columns"
    assert df["link"].str.startswith("http").all(), "malformed links"
    print(f"self-check ok: {len(df):,} rows from {sum(1 for _n, c in stats if c)} sources")
