"""Runnable self-check for sitemap_sources robots gating and JSON-LD parsing.

No framework, no network. Run:  python test_sitemap_sources.py
"""

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import sitemap_sources as S

_REAL_HTTP_GET = S.http_get

EFFOLDT_ROBOTS = """
User-agent: *
Disallow: /
Allow: /$
Allow: /careers
Allow: /api/apply
Allow: /api/career_hub
Allow: /careerhub/explore/jobs
Allow: /gen

User-agent: IndeedJobBot
Disallow:
"""

HCL_ROBOTS = """
User-agent: *
Disallow: /applybutton/
Disallow: /talentcommunity/
Allow: /
"""

JOB_LD = """<html><head><title>Ignored</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Software Engineer Intern",
 "datePosted":"2026-08-01",
 "employmentType":"INTERN",
 "hiringOrganization":{"@type":"Organization","name":"Acme Labs"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
   "value":{"@type":"QuantitativeValue","minValue":32,"maxValue":40}},
 "description":"<p>Build &amp; ship things.</p>"}
</script></head><body></body></html>"""

GRAPH_LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"ignored"},
 {"@type":"JobPosting","title":"New Grad Engineer","datePosted":"2026-07-15",
  "hiringOrganization":{"name":"Globex"},"jobLocation":{"address":[
   {"addressLocality":"Remote","addressCountry":"US"}]}}]}
</script></head><body></body></html>"""

TITLE_ONLY_HTML = (
    '<html><head><title>Data Science Intern - Zeta | Careers</title>'
    '<meta name="description" content="Join our team."></head><body></body></html>'
)


class _Gate:
    """Mirrors robots_allows() over a fixed robots.txt body, no network.

    protego owns the rule evaluation; _blanket_disallow is the extra policy
    layer on top that keeps us off blanket-'Disallow: /' hosts. Both are
    exercised here so the gate is tested end to end rather than piecewise.
    """

    def __init__(self, text):
        self._text = text
        self._blanket = S._blanket_disallow(text, S.ROBOT_AGENT)

    def allows(self, url):
        if self._blanket:
            return False
        return bool(S.Protego.parse(self._text).can_fetch(url, S.ROBOT_AGENT))


def _robots(text):
    return _Gate(text)


def check_robots():
    print("robots gating (protego + blanket-disallow policy)")
    ef = _robots(EFFOLDT_ROBOTS)
    cases = [
        ("Eightfold blanket detected", S._blanket_disallow(EFFOLDT_ROBOTS, S.ROBOT_AGENT), True),
        ("Eightfold denies job pages", ef.allows("https://h/careers/job/1"), False),
        ("Eightfold denies sitemap.xml", ef.allows("https://h/careers/sitemap.xml"), False),
        ("Eightfold denies /jobs/x", ef.allows("https://h/jobs/x"), False),
        ("Eightfold root also denied (blanket wins)", ef.allows("https://h/"), False),
    ]
    hcl = _robots(HCL_ROBOTS)
    cases += [
        ("HCL not blanket", S._blanket_disallow(HCL_ROBOTS, S.ROBOT_AGENT), False),
        ("HCL allows job pages", hcl.allows("https://h/en/job/1"), True),
        ("HCL denies applybutton", hcl.allows("https://h/applybutton/x"), False),
        ("HCL denies talentcommunity", hcl.allows("https://h/talentcommunity/"), False),
    ]
    groups = _robots("User-agent: *\nDisallow: /\nUser-agent: IndeedJobBot\nDisallow:\n")
    cases.append(
        ("star group blanket applies", groups.allows("https://h/jobs/1"), False)
    )
    anchor = _robots("User-agent: *\nDisallow: /*.pdf$\nAllow: /files/\n")
    cases += [
        ("dollar anchor denies .pdf", anchor.allows("https://h/a.pdf"), False),
        ("anchor ignores .pdf.html", anchor.allows("https://h/a.pdf.html"), True),
    ]
    empty = _robots("User-agent: *\nDisallow:\n")
    cases.append(("empty Disallow allows all", empty.allows("https://h/x"), True))
    none = _robots("User-agent: *\nDisallow: /admin/\n")
    cases.append(("unmatched path allowed", none.allows("https://h/jobs/9"), True))
    tie = _robots("User-agent: *\nDisallow: /x\nAllow: /x\n")
    cases.append(("tie resolves to allow", tie.allows("https://h/x"), True))
    # Regression: per-bot blanket stanzas must not taint a permissive * group.
    scoped = (
        "User-agent: *\nAllow: /\n\n"
        "User-agent: TrackIf\nDisallow: /\n\n"
        "User-agent: coccocbot\nDisallow: /\n"
    )
    scoped_gate = _robots(scoped)
    cases += [
        ("per-bot blanket ignored", S._blanket_disallow(scoped, S.ROBOT_AGENT), False),
        ("per-bot stanza still permits us", scoped_gate.allows("https://h/jobs/1"), True),
    ]
    specific = (
        "User-agent: *\nAllow: /\n\nUser-agent: SearchTern\nDisallow: /\n"
    )
    cases.append(
        ("our own token beats wildcard", S._blanket_disallow(specific, S.ROBOT_AGENT), True)
    )
    return cases


def check_ld():
    print("JSON-LD JobPosting extraction")
    row = S.parse_job_page("Acme", "https://h/job/1", JOB_LD)
    cases = [
        ("role parsed", row["role"], "Software Engineer Intern"),
        ("company from org", row["company"], "Acme Labs"),
        ("date truncated", row["date"], "2026-08-01"),
        ("employment type", row["employment_type"], "INTERN"),
        ("locality", row["location"].split(",")[0], "Austin"),
        ("country code", row["country_iso"], "US"),
        ("salary min", row["salary_min"], 32),
        ("salary max", row["salary_max"], 40),
        ("currency", row["salary_currency"], "USD"),
        ("link preserved", row["link"], "https://h/job/1"),
    ]
    graph = S.parse_job_page("Globex", "https://h/job/2", GRAPH_LD)
    cases += [
        ("@graph node found", graph["role"], "New Grad Engineer"),
        ("list address handled", graph["location"].split(",")[0], "Remote"),
        ("remote detected", graph["is_remote"], "true"),
        ("missing salary is None", graph["salary_min"], None),
    ]
    return cases


def check_fallback():
    print("<title> fallback when no JSON-LD")
    row = S.parse_job_page("Zeta", "https://h/job/3", TITLE_ONLY_HTML)
    cases = [
        ("role from title", row["role"], "Data Science Intern"),
        ("fallback still returns link", row["link"], "https://h/job/3"),
        ("fallback not remote", row["is_remote"], "false"),
    ]
    placeholder = (
        '<html><head><title>Job Details</title></head><body></body></html>'
    )
    bare = '<html><head><title>Careers</title></head><body></body></html>'
    trailing = (
        '<html><head><title>QA Automation Intern (with German) Job Details'
        "</title></head><body></body></html>"
    )
    cases += [
        ("generic title rejected", S.parse_job_page("H", "https://h/j/5", placeholder), None),
        ("careers title rejected", S.parse_job_page("H", "https://h/j/6", bare), None),
        ("trailing 'Job Details' stripped",
         S.parse_job_page("C", "https://h/j/7", trailing)["role"],
         "QA Automation Intern (with German)"),
        ("company suffix stripped before tail",
         S._clean_fallback_role(
             "R2R Finance and Accounting Junior Manager Job Details | Capgemini",
             "Capgemini",
         ),
         "R2R Finance and Accounting Junior Manager"),
        ("company suffix alone stripped",
         S._clean_fallback_role("Data Science Intern - Zeta", "Zeta"),
         "Data Science Intern"),
        ("legitimate hyphen kept",
         S._clean_fallback_role("Full-Time Software Engineer Intern - Acme", "Acme"),
         "Full-Time Software Engineer Intern"),
        ("blank page yields nothing",
         S.parse_job_page("Z", "https://h/j/4", "<html></html>"), None),
    ]
    return cases


def check_country():
    print("country code only when confident")
    cases = [
        ("ISO2 passthrough", S._country_code({"addressCountry": "US"}), "US"),
        ("full name mapped", S._country_code({"addressCountry": "United States"}), "US"),
        ("Canada mapped", S._country_code({"addressCountry": "Canada"}), "CA"),
        ("UK mapped", S._country_code({"addressCountry": "United Kingdom"}), "GB"),
        ("comma list", S._country_code({"addressCountry": "Germany, Germany"}), "DE"),
        ("nested dict", S._country_code({"addressCountry": {"name": "India"}}), "IN"),
        ("unknown returns empty", S._country_code({"addressCountry": "Atlantis"}), ""),
        ("absent returns empty", S._country_code({}), ""),
    ]
    return cases


def check_filters():
    print("URL filtering")
    job = "https://h/careers/job/123-intern"
    cases = [
        ("job path kept", bool(S._JOB_PATH_RE.search(job)), True),
        ("apply path skipped", bool(S._SKIP_PATH_RE.search("https://h/careers/apply/9")), True),
        ("search path skipped", bool(S._SKIP_PATH_RE.search("https://h/jobs/search")), True),
        ("pdf skipped", bool(S._SKIP_PATH_RE.search("https://h/jobs/a.pdf")), True),
        ("intern slug matched", bool(S._INTERN_SLUG_RE.search(job)), True),
        ("newgrad slug matched", bool(S._INTERN_SLUG_RE.search("https://h/jobs/new-grad-swe")), True),
        ("senior slug ignored", bool(S._INTERN_SLUG_RE.search("https://h/jobs/senior-engineer")), False),
        ("non-https refused", S.robots_allows("http://h/careers/job/1"), False),
    ]
    return cases


def check_robots_unreadable():
    print("robots fetch failures deny; only 404/410 is unrestricted")

    class _Resp:
        def __init__(self, code, text=""):
            self.status_code = code
            self.text = text

    def probe(code, text=""):
        S._robots_cache.clear()
        S.http_get = lambda *a, **k: _Resp(code, text)
        return S.robots_allows("https://gated.example/careers/job/1")

    try:
        cases = [
            ("403 robots.txt denies", probe(403), False),
            ("401 robots.txt denies", probe(401), False),
            ("500 robots.txt denies", probe(500), False),
            ("429 robots.txt denies", probe(429), False),
            # RFC 9309: an empty robots.txt imposes no rules, so allow-all.
            ("empty 200 body allows", probe(200, "   "), True),
            ("404 means unrestricted", probe(404), True),
            ("410 means unrestricted", probe(410), True),
            ("200 with rules governs", probe(200, "User-agent: *\nDisallow: /careers/"), False),
            ("200 permitting rules", probe(200, "User-agent: *\nDisallow: /admin/\n"), True),
        ]
    finally:
        S.http_get = _REAL_HTTP_GET
    return cases


def check_not_modified_cache():
    """End-to-end against a real origin: 304 must replay the body.

    This used to be the bug that silently dropped rows -- a 304 came back as an
    empty string, indistinguishable from a deleted page. requests-cache owns
    conditional GET now, so the assertion is made against a live HTTP server
    that actually answers 304, not a mock.
    """
    print("conditional GET replays the cached body on 304")
    hits = {"n": 0}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits["n"] += 1
            inm = self.headers.get("If-None-Match")
            if inm == '"v1"':
                self.send_response(304)
                self.send_header("ETag", '"v1"')
                self.end_headers()
                return
            body = b"<html>JobPosting payload</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port

    real_dir, real_delay, real_http = S.CACHE_DIR, S.PER_HOST_DELAY_SECS, S._HTTP
    S.CACHE_DIR = f"/tmp/sitemap_cache_test_{os.getpid()}"
    S.PER_HOST_DELAY_SECS = 0
    S._HTTP = None
    try:
        S._session().cache.clear()
        url = f"http://127.0.0.1:{port}/jobs/job/1"
        first = S._polite_get(url, "127.0.0.1")
        second = S._polite_get(url, "127.0.0.1")
        third = S._polite_get(url, "127.0.0.1")

        # Simulate a fresh process: drop the in-memory session, keep the store.
        S._HTTP = None
        after_restart = S._polite_get(url, "127.0.0.1")

        cases = [
            ("first fetch gets the body", first, "<html>JobPosting payload</html>"),
            ("repeat fetch replays the body", second, "<html>JobPosting payload</html>"),
            ("third fetch still replays", third, "<html>JobPosting payload</html>"),
            ("body survives a session restart", after_restart, "<html>JobPosting payload</html>"),
            ("304 never yields empty string", second != "" and third != "", True),
            ("origin hit at most twice", hits["n"] <= 2, True),
        ]
    finally:
        srv.shutdown()
        S.CACHE_DIR, S.PER_HOST_DELAY_SECS, S._HTTP = real_dir, real_delay, real_http
    return cases


def main():
    failures = 0
    groups = (
        check_robots,
        check_ld,
        check_fallback,
        check_country,
        check_filters,
        check_robots_unreadable,
        check_not_modified_cache,
    )
    total = 0
    for group in groups:
        for name, got, want in group():
            total += 1
            if got == want:
                print(f"  PASS  {name}")
            else:
                failures += 1
                print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        print()
    print(
        f"all {total} checks passed"
        if not failures
        else f"{failures} of {total} check(s) failed"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())