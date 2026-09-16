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