"""Shared listing-classification rules.

Single source of truth for the word-boundary regexes used by both the README
and listings.json pipelines. The SQL conditions in generate_listings.py are
built from the same intern/new-grad term lists below so the Python and SQL
classifiers cannot drift apart again (the "Cooper St" and "Suite" leaks were
both unanchored substrings matching street names).

Rules:
- Short tokens (ui, ml, qa, it, ai) are whole-word only: "ui" must not match
  "s*u*ite" or "recru*u*iting", "ml" must not match "ht*ml*".
- Plurals are matched via \\w* so "student" catches "students".
"""

import re


def _bounded_re(term):
    """Bind a single word-ish term: \\bterm\\w* — matches exact + plurals."""
    return r"\b" + term + r"\w*"


def _terms_pattern(terms):
    return "|".join(_bounded_re(t) for t in terms)


def _sql_terms(terms):
    """Bounded-pattern body mirroring _terms_pattern, for Postgres regexp."""
    return "|".join(f"\\b{re.escape(t)}\\w*" for t in terms)


def _regexp(column, pattern):
    return f"regexp_matches({column}, '{pattern}', 'i')"


# ---------------------------------------------------------------------------
# Tech keywords (listings.json / README filter)
# ---------------------------------------------------------------------------
TECH_KEYWORDS = (
    # whole-word acronyms / short tokens — never leave these unanchored
    r"\b(?:swe|sde|mts|it|ai|ml|nlp|ux|qa|dba|hpc|sre|sdet|devops|fpga|asic|devrel)\b",
    # SWE core
    r"\bsoftware\b", r"\bdeveloper\b", r"\bprogrammer\b", r"\bcoder\b",
    r"\bengineer(?:ing)?\b", r"\bcomputer science\b",
    r"\bfull[ -]?stack\b", r"\bback[ -]?end\b", r"\bfront[ -]?end\b",
    # data / AI
    r"\bdata\b", r"\bdata science\b", r"\bdata scientist\w*",
    r"\bmachine learn(?:ing)?\b", r"\bdeep learn(?:ing)?\b",
    r"\bartificial intellig(?:ence)?\b", r"\bcomputer vision\b",
    r"\bllm\b", r"\bapplied scien\w*", r"\bscientific comput\w*",
    # infra / security
    r"\bcloud\b", r"\bsite reliab\w*", r"\bcybersecur\w*", r"\bsecurity\b",
    r"\bnetwork\w*", r"\bsysadmin\w*", r"\binfrastructure\b", r"\bplatform\b",
    r"\bsystems\b", r"\barchitect\w*", r"\bdistributed\b", r"\blinux\b", r"\bunix\b",
    r"\bdevops\b", r"\bautomation\b",
    # hardware / embedded
    r"\bhardware\b", r"\bfirmware\b", r"\bembedded\b", r"\bchip\w*",
    r"\belectrical\b", r"\belectronics\b", r"\bcontrols\b", r"\bsignal\b",
    r"\btelecom\w*", r"\brobotics\b", r"\bcompiler\w*",
    # quality / test
    r"\bquality assur\w*", r"\btest\w*", r"\bsdet\b",
    # ui / product / web / mobile
    r"\bui\b", r"\bux\b", r"\bdesign\w*", r"\bproduct\w*", r"\bweb\b",
    r"\bmobile\b", r"\bios\b", r"\bandroid\b",
    # tech-leaning roles / emerging
    r"\bquant\b", r"\banalyst\w*", r"\bresearch\w*", r"\bgame\w*",
    r"\bblockchain\b", r"\bweb3\b", r"\bcryptograph\w*", r"\btechnical\b",
    r"\btechnology\b",
)
TECH_KEYWORDS_RE = "|".join(TECH_KEYWORDS)

# ---------------------------------------------------------------------------
# Intern / new-grad / excluded title terms (shared Python + SQL)
# ---------------------------------------------------------------------------
INTERN_TITLE_TERMS = (
    "undergraduate", "undergrad", "student", "apprentice",
    "trainee", "fellowship", "praktikum", "werkstudent", "reu",
)
INTERN_SPECIAL_RE = r"\bintern(?:ship)?\b|\bco-?op\b|\bstage\b"

NEWGRAD_TITLE_TERMS = ("campus", "rotational", "junior")
NEWGRAD_SPECIAL_RE = (
    r"new[\s-]grad(?:uate)?\b|university[\s-]grad(?:uate)?\b"
    r"|\bentry[\s-]level\b|\bearly\s+career\b|\bfresh\s+grad\b"
)

LISTINGS_INTERN_RE = "|".join(
    (INTERN_SPECIAL_RE, _terms_pattern(INTERN_TITLE_TERMS))
)
LISTINGS_NEWGRAD_RE = "|".join(
    (NEWGRAD_SPECIAL_RE, _terms_pattern(NEWGRAD_TITLE_TERMS))
)

TITLE_EXCLUDE_RE = (
    r"\bpharmacist\w*|\bpharmacy\b|\bdental\b|\bnurse\w*|\bphysician\w*"
    r"|\bmedical\s+intern\b|\bclinical\s+intern\b|\binternal medicine\b"
    r"|\binternal audit\b|\binternal\s+only\b|\binternal\s+security\b"
    r"|\bsales\s+associate\w*|\bsales\s+representative\w*|\bveterinary\b"
    r"|\bpastor\w*|\bteacher\w*"
    r"|\bmarketing\s+intern\b|\bhr\s+intern\b|\bhuman\s+resources\b"
    r"|\bsenior\s+(?!.*(?:intern|co-op|apprentice|trainee))"
)

TITLE_EXCLUDE_TERMS = (
    "pharmacist", "pharmacy", "dental", "nurse", "nursing", "physician",
    "veterinary", "pastor", "teacher",
)

# SQL regexp_matches variants of the above, for the manifest queries.
def intern_title_cond(column="title"):
    """SQL cond matching a title like the intern regex."""
    return _regexp(column, INTERN_SPECIAL_RE + "|" + _sql_terms(INTERN_TITLE_TERMS))


def newgrad_title_cond(column="title"):
    """SQL cond matching a title like the new-grad regex."""
    return _regexp(column, NEWGRAD_SPECIAL_RE + "|" + _sql_terms(NEWGRAD_TITLE_TERMS))


def titles_tech_cond(column="title"):
    """SQL cond matching a title like the tech-keyword regex."""
    return _regexp(column, TECH_KEYWORDS_RE)


# ---------------------------------------------------------------------------
# Role category (drives the app's "role category" filter via searchtern.json)
# ---------------------------------------------------------------------------
# One ordered taxonomy: the first category with a keyword that matches the
# lower-cased role title wins. Order is most-specific → most-generic so, e.g.,
# "Data Engineer" lands in Data/ML/AI before Software Engineering's generic
# "engineer", and "Sales Engineer" lands in Sales before SE.
#
# Matching is word-aware: short tokens (<=3 chars: pm, ui, ai, ml, bi, it, hr,
# qa, swe, sre, ...) match whole words only; longer keywords match at a word
# start (so "engineer" catches "engineering"/"engineers"). This avoids the
# earlier substring bugs where "pm" matched "develo[pm]ent" and "api" matched
# "c[api]tal".
ROLE_CATEGORIES = {
    "Data/ML/AI": (
        "data scientist", "data science", "data engineer", "data analytics",
        "data analyst", "machine learning", "ml engineer", "ml",
        "artificial intelligence", "ai", "deep learning", "nlp",
        "natural language", "computer vision", "data mining", "big data",
        "business intelligence", "bi", "llm", "generative", "genai",
        "analytics engineer", "data annotation", "data", "analytics",
    ),
    "Cybersecurity": (
        "cybersecurity", "cyber", "security", "infosec",
        "information security", "penetration", "pentest", "appsec", "threat",
        "vulnerability", "malware", "red team", "blue team", "soc analyst",
    ),
    "Finance/Quant": (
        "quant", "quantitative", "trader", "trading", "finance", "financial",
        "investment", "private equity", "venture", "hedge fund", "risk",
        "actuarial", "banking", "fp&a", "underwriting", "insurance",
        "valuation", "portfolio", "credit", "wealth",
    ),
    "Accounting": (
        "accounting", "accountant", "audit", "tax", "bookkeep",
        "accounts payable", "accounts receivable", "payroll", "treasury",
        "controller", "cpa",
    ),
    "Legal/Compliance": (
        "legal", "law", "compliance", "paralegal", "attorney", "counsel",
        "regulatory", "contract", "patent", "intellectual property",
        "presuit", "pre-suit", "public affairs", "policy", "advocacy",
    ),
    "Healthcare/Clinical": (
        "clinical", "medical", "nurse", "nursing", "physician", "patient",
        "pharmacy", "pharmac", "dental", "hospital", "health", "therapist",
        "therapy", "physician assistant", "medical assistant", "patholog",
        "radiolog", "speech language", "dietitian", "phlebotom",
        "physical therapy", "veterinar", "cna", "bna",
    ),
    "Hardware/Embedded": (
        "hardware", "embedded", "firmware", "electrical", "electronics",
        "mechanical", "mechatronics", "chip", "asic", "fpga", "pcb",
        "robotics", "semiconductor", "controls engineer", "signal",
        "aerospace", "automotive engineer", "mechatronic", "metallurg",
        "materials", "optics", "chemical engineer", "biomedical",
        "industrial engineer", "process engineer",
    ),
    "Construction/Trades": (
        "construction", "civil", "structural", "electrician", "plumber",
        "welding", "welder", "carpenter", "hvac", "mechanic", "maintenance",
        "superintendent", "installer", "machinist", "cnc", "fabrication",
        "surveyor", "architect", "landscape", "laborer", "diesel",
        "automotive", "nd t", "ndt", "arborist", "sprinkler", "fitter",
        "pipefitter", "inspector", "boilermaker", "gis", "survey",
        "estimating", "inspection", "commissioning", "building", "envelope",
        "fire alarm", "arboricult", "plumb", "site development",
    ),
    "DevOps/SRE": (
        "devops", "sre", "site reliability", "platform engineer",
        "infrastructure", "cloud engineer", "kubernetes", "k8s", "terraform",
        "ci/cd", "sysadmin", "systems administrator", "network engineer",
        "network administrator", "cloud", "aws", "azure", "sdet",
        "networking", "network", "wireless", "service desk", "help desk",
        "it support", "desktop support",
    ),
    "Product/Design": (
        "product manager", "product management", "product owner",
        "product analyst", "product intern", "product design",
        "product designer", "product", "ux", "ui", "user experience",
        "user interface", "interaction design", "industrial design",
        "graphic design", "visual design", "brand design", "design", "pm",
    ),
    "Human Resources": (
        "human resources", "hr", "recruiting", "recruiter", "talent",
        "people operations", "benefits", "compensation",
    ),
    "Sales/Business Dev": (
        "sales", "account executive", "account manager",
        "business development", "bdr", "sdr", "partnership",
        "brand ambassador", "account representative", "territory",
        "customer success",
    ),
    "Marketing/Comms": (
        "marketing", "brand", "seo", "sem", "content", "social media",
        "communications", "public relations", "pr", "growth",
        "advertising", "copywriter", "demand generation", "gtm",
        "go-to-market",
    ),
    "Customer/Retail": (
        "customer service", "customer experience", "customer support",
        "customer care", "call center", "reservation", "booking", "hotel",
        "hospitality", "retail", "cashier", "store associate",
        "guest services", "barista", "front desk", "store manager",
        "branch manager", "management trainee", "manager trainee", "teller",
    ),
    "Education": (
        "teacher", "tutor", "education", "instructor", "teaching",
        "professor", "academic", "curriculum", "resident advisor",
        "student success", "student assistant", "student supervision",
        "coach",
    ),
    "Research/Science": (
        "research", "researcher", "scientist", "laboratory", "lab", "biolog",
        "chemistry", "chemist", "physics", "physicist", "geolog",
        "environmental", "postdoc", "biochem", "neuroscience",
        "sustainability", "toxicolog", "sensory", "crop", "agri", "soil",
        "horticult",
    ),
    "Operations/Supply": (
        "operations", "ops", "supply chain", "logistics", "procurement",
        "warehouse", "fulfillment", "inventory", "production", "manufactur",
        "quality", "plant", "distribution", "sourcing", "demand planning",
        "buyer", "safety", "transportation", "driver", "cdl", "scheduler",
        "scheduling", "dispatch", "fleet", "planning", "facilit", "ehs",
        "environmental health",
    ),
    "Business/Analytics": (
        "business analyst", "business analytics", "analyst", "strategy",
        "consulting", "consultant", "business", "management",
        "project manage", "program manage",
    ),
    "Software Engineering": (
        "software engineer", "software develop", "swe", "sde", "backend",
        "back-end", "frontend", "front-end", "fullstack", "full-stack",
        "full stack", "web develop", "developer", "programmer", "engineer",
        "engineering", "ios", "android", "mobile develop", "coder",
        "computer science", "it", "technology", "technical", "qa",
        "quality assurance", "software", "application develop",
    ),
    "Other": (),
}

_ROLE_PATTERN_CACHE = {}


def _role_keyword_re(keyword):
    """Compile a role keyword to a word-aware regex.

    Short tokens are matched whole-word; longer keywords match at a word start
    so plurals/variants still hit ("engineer" → "engineering").
    """
    pattern = _ROLE_PATTERN_CACHE.get(keyword)
    if pattern is None:
        escaped = re.escape(keyword)
        if len(keyword) <= 3 and " " not in keyword:
            body = r"\b" + escaped + r"\b"
        else:
            body = r"\b" + escaped
        pattern = re.compile(body)
        _ROLE_PATTERN_CACHE[keyword] = pattern
    return pattern


def role_category(role):
    """Bucket a role title into one of ROLE_CATEGORIES (or "Other")."""
    text = (role or "").lower()
    for name, keywords in ROLE_CATEGORIES.items():
        for keyword in keywords:
            if _role_keyword_re(keyword).search(text):
                return name
    return "Other"