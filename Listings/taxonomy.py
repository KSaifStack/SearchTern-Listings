"""Career taxonomy: what an opportunity is, kept apart from whether we publish it.

Two decisions that used to be tangled into one title filter:

* **Eligibility** (:func:`opportunity_eligibility`) answers "is this an
  internship / co-op / new-grad / early-career role we take at all?" It knows
  nothing about industries. A finance internship and a software internship pass
  the same test, and so do nursing, marketing, and mechanical-engineering
  interns. Only genuinely non-early-career titles are refused (a senior or
  executive role with no intern/new-grad marker), plus the pipeline's
  false-positive guards ("internal only", "internal audit").
* **Classification** (:func:`classify`) answers "which career family and
  industry is this?" It never rejects a row. An unrecognised but eligible
  title lands in ``Other Professional`` with ``confidence="low"`` and a reason,
  instead of being dropped because no technology keyword matched.

The families mirror the product requirement (all industries, not just SWE).
``classify.role_category`` remains the compatibility subcategory and is reused
here as the specific bucket inside each family, so existing consumers of
``category`` keep working.
"""

import re

import classify as _classify

# The published families. Order is display order, not precedence.
CAREER_FAMILIES = (
    "Software Engineering & Technology",
    "Engineering (Mechanical, Electrical, Civil, Industrial, Aerospace)",
    "Finance, Accounting & Banking",
    "Business Analysis, Consulting & Administration",
    "Operations & Project Management",
    "Marketing, Communications & PR",
    "Supply Chain, Procurement & Logistics",
    "Human Resources & Recruiting",
    "Healthcare, Clinical & Public Health",
    "Scientific Research, Lab & Biotech",
    "Design, UX/UI & Creative",
    "Education & Teaching",
    "Government & Public Service",
    "Legal & Compliance",
    "Sales & Customer Success",
    "Other Professional",
)

FALLBACK_FAMILY = "Other Professional"

# Subcategory (_classify.ROLE_CATEGORIES key) -> (family, industry).
_SUBCATEGORY_MAP = {
    "Software Engineering": ("Software Engineering & Technology", "Technology"),
    "Data/ML/AI": ("Software Engineering & Technology", "Technology"),
    "Cybersecurity": ("Software Engineering & Technology", "Technology"),
    "DevOps/SRE": ("Software Engineering & Technology", "Technology"),
    "Hardware/Embedded": (
        "Engineering (Mechanical, Electrical, Civil, Industrial, Aerospace)",
        "Engineering",
    ),
    "Construction/Trades": (
        "Engineering (Mechanical, Electrical, Civil, Industrial, Aerospace)",
        "Construction & Trades",
    ),
    "Finance/Quant": ("Finance, Accounting & Banking", "Financial Services"),
    "Accounting": ("Finance, Accounting & Banking", "Financial Services"),
    "Legal/Compliance": ("Legal & Compliance", "Legal Services"),
    "Healthcare/Clinical": ("Healthcare, Clinical & Public Health", "Healthcare"),
    "Human Resources": ("Human Resources & Recruiting", "Human Resources"),
    "Sales/Business Dev": ("Sales & Customer Success", "Sales"),
    "Customer/Retail": ("Sales & Customer Success", "Retail & Hospitality"),
    "Marketing/Comms": ("Marketing, Communications & PR", "Marketing & Media"),
    "Education": ("Education & Teaching", "Education"),
    "Research/Science": ("Scientific Research, Lab & Biotech", "Research & Science"),
    "Operations/Supply": (
        "Supply Chain, Procurement & Logistics",
        "Logistics & Manufacturing",
    ),
    "Business/Analytics": (
        "Business Analysis, Consulting & Administration",
        "Business Services",
    ),
    "Product/Design": ("Design, UX/UI & Creative", "Design & Creative"),
    "Other": (FALLBACK_FAMILY, "Unclassified"),
}

# A second family can be real ("Sales Engineer", "Marketing Data Analyst"), so
# these phrases add a non-primary family when they match on their own. They are
# deliberately specific: a bare "engineer" must not pull every analyst into
# Software Engineering.
_CROSS_FAMILY = (
    ("Sales & Customer Success", (
        r"\bsales\b", r"\baccount executive\b", r"\bcustomer success\b",
        r"\bbusiness development\b",
    )),
    ("Software Engineering & Technology", (
        r"\bsoftware\b", r"\bdeveloper\b", r"\bengineer(?:ing)?\b",
        r"\bdata\b", r"\bsecurity\b", r"\bit\b",
    )),
    ("Engineering (Mechanical, Electrical, Civil, Industrial, Aerospace)", (
        r"\bmechanical\b", r"\belectrical\b", r"\bcivil\b", r"\bindustrial\b",
        r"\baerospace\b", r"\bmanufactur\w*",
    )),
    ("Finance, Accounting & Banking", (
        r"\bfinance\b", r"\bfinancial\b", r"\baccounting\b", r"\bbanking\b",
        r"\baudit\w*", r"\btax\b",
    )),
    ("Marketing, Communications & PR", (
        r"\bmarketing\b", r"\bbrand\b", r"\bcommunications?\b",
        r"\bpublic relations\b",
    )),
    ("Healthcare, Clinical & Public Health", (
        r"\bhealth\w*\b", r"\bclinical\b", r"\bnurs\w*", r"\bpublic health\b",
    )),
    ("Government & Public Service", (
        r"\bgovernment\b", r"\bpublic sector\b", r"\bcivil service\b",
        r"\bmunicipal\w*", r"\bfederal\b", r"\bcounty\b", r"\bcity of\b",
    )),
)

# Government has no subcategory of its own yet; this detects it ahead of the
# generic classifier so "Policy Intern, City of Austin" is not filed as Legal.
_GOVERNMENT_RE = re.compile(
    r"\b(?:government|public sector|civil service|municipal\w*|federal|"
    r"county|state of|city of|mayor|governor|senator|congress\w*|"
    r"public administration|public policy|policy analyst)\b",
    re.IGNORECASE,
)

# Non-career / false-positive guards. These are title artefacts, not industries.
# Project/program management and general operations are their own family; the
# shared subcategory vocabulary only had one "Operations/Supply" bucket.
_OPS_RE = re.compile(
    r"\bproject\s+manage\w*|\bprogram\s+manage\w*|\boperations?\b|\bops\b"
    r"|\bprogram\s+coordinator\b",
    re.IGNORECASE,
)

NON_CAREER_RE = re.compile(
    r"\binternal\s+(?:only|audit|medicine|security|candidate)\b"
    r"|\bunpaid\s+internship\b",
    re.IGNORECASE,
)

# Seniority markers that disqualify a title only when it carries no early-career
# marker of its own ("Senior Software Engineer Intern" stays eligible).
SENIORITY_RE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|head\s+of|director|vp|"
    r"vice\s+president|chief|executive|manager)\b",
    re.IGNORECASE,
)

_EARLY_CAREER_RE = re.compile(
    f"(?:{_classify.LISTINGS_INTERN_RE})|(?:{_classify.LISTINGS_NEWGRAD_RE})"
    r"|\bapprentice\w*|\bco-?op\b|\bgraduate\s+(?:scheme|program\w*)\b",
    re.IGNORECASE,
)

_ELIGIBLE_JOB_TYPES = frozenset({
    "internship", "intern", "co_op", "co-op", "coop", "new_grad", "newgrad",
    "new_graduate", "entry_level", "apprenticeship", "rotational", "graduate",
})

# Which employer/industry the row belongs to, for the coverage report.
_EXTRA_INDUSTRIES = ("Public Sector", "Operations", "Unclassified")
INDUSTRIES = tuple(sorted({industry for _family, industry in _SUBCATEGORY_MAP.values()}
                          | set(_EXTRA_INDUSTRIES)))


def opportunity_eligibility(role, job_type=""):
    """Return ``(eligible, reason)`` for one title.

    Independent of industry: the only refusals are non-career title artefacts
    and senior/executive titles with no early-career marker. Callers that
    already restricted to intern/new-grad titles get ``eligible=True`` for every
    legitimate profession.
    """
    text = str(role or "").strip()
    if not text:
        return False, "empty_title"
    if NON_CAREER_RE.search(text):
        return False, "non_career_title"
    kind = str(job_type or "").strip().lower()
    has_marker = bool(_EARLY_CAREER_RE.search(text))
    if kind in _ELIGIBLE_JOB_TYPES:
        if SENIORITY_RE.search(text) and not has_marker:
            return False, "seniority_mismatch"
        return True, "early_career_title"
    if has_marker:
        if SENIORITY_RE.search(text) and not _EARLY_CAREER_RE.search(text):
            return False, "seniority_mismatch"
        return True, "early_career_title"
    return False, "not_early_career"


def _match_terms(text):
    """Return the subcategory keywords that matched, most specific first."""
    matched = []
    for name, keywords in _classify.ROLE_CATEGORIES.items():
        if name == "Other":
            continue
        hits = [kw for kw in keywords if _classify._role_keyword_re(kw).search(text)]
        if hits:
            matched.append((name, hits))
    return matched


def classify(role):
    """Classify one title into a career family.

    Returns a dict with ``career_family``, ``industry``, ``subcategory``,
    ``career_families`` (primary first, at most two), ``confidence`` and
    ``reason``. Never raises and never returns a missing family: an unknown
    title is ``Other Professional`` with ``confidence="low"`` and an explicit
    reason.
    """
    text = str(role or "").strip().lower()
    if not text:
        return {
            "career_family": FALLBACK_FAMILY,
            "industry": "Unclassified",
            "subcategory": "Other",
            "career_families": [FALLBACK_FAMILY],
            "confidence": "low",
            "reason": "empty_title",
        }

    government = bool(_GOVERNMENT_RE.search(text))
    operations = bool(_OPS_RE.search(text))
    matches = _match_terms(text)
    if government:
        family, industry, subcategory = (
            "Government & Public Service", "Public Sector", "Government/Public Service",
        )
        confidence = "high"
        reason = "government:" + _GOVERNMENT_RE.search(text).group(0).strip()
    elif operations and not any(
            name in ("Operations/Supply",) for name, _hits in matches):
        family, industry, subcategory = (
            "Operations & Project Management", "Operations", "Operations/Supply",
        )
        confidence = "medium"
        reason = "operations:" + _OPS_RE.search(text).group(0).strip()
    elif operations:
        family, industry, subcategory = (
            "Operations & Project Management", "Operations", "Operations/Supply",
        )
        confidence = "high"
        reason = "operations:" + _OPS_RE.search(text).group(0).strip()
    elif matches:
        subcategory, hits = matches[0]
        family, industry = _SUBCATEGORY_MAP.get(subcategory, (FALLBACK_FAMILY, "Unclassified"))
        confidence = "high" if len(matches) > 1 or len(hits) > 1 else "medium"
        reason = f"{subcategory}:{', '.join(hits[:3])}"
    else:
        return {
            "career_family": FALLBACK_FAMILY,
            "industry": "Unclassified",
            "subcategory": "Other",
            "career_families": [FALLBACK_FAMILY],
            "confidence": "low",
            "reason": "no_keyword_match",
        }

    families = [family]
    for other_family, patterns in _CROSS_FAMILY:
        if other_family == family or other_family in families:
            continue
        for pattern in patterns:
            if re.search(pattern, text):
                families.append(other_family)
                break
        if len(families) >= 2:
            break

    return {
        "career_family": family,
        "industry": industry,
        "subcategory": subcategory,
        "career_families": families,
        "confidence": confidence,
        "reason": reason,
    }
