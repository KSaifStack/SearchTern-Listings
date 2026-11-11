"""Employer display names and a stable identity key.

ATS payloads and job boards publish the same employer in shapes no reader
recognizes: ``andurilindustries``, ``sharkninjaoperatingllc``,
``morganmorganjobsapplynow``. The frontend keys logos, company filters and brand
pages on the company string, so every spelling becomes a separate unbranded
employer even though it is one company.

Three rules, all conservative:

* **A verified alias wins.** Each entry in ``_VERIFIED`` was checked against the
  employer's own site (2026-11-11) and carries the canonical spelling plus the
  official domain when it is known. Nothing is derived from initials.
* **A separator-free name is split only on a closed vocabulary of corporate
  descriptors** (``industries``, ``university``, ``healthcare`` ...) and a short
  list of board/legal tails. This is not word segmentation: there is no
  dictionary and no guessing at brand boundaries. Anything that does not match
  keeps the published spelling.
* **Case is repaired, never reworded.** Names already in mixed case (``iOS``,
  ``McKinsey``) are left untouched, and no token is added, dropped or reordered.

The caller keeps the published value as ``company_raw`` when it changes, and
``key()`` gives every alias of one employer the same identity string for dedup,
logo lookup and company filters.
"""

import html
import re
from typing import NamedTuple

_ALNUM = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


class Employer(NamedTuple):
    """One employer name with everything a publication needs to carry."""

    display: str
    key: str
    confidence: str  # verified | derived | unchanged
    reason: str
    domain: str


# Keys are alphanumeric casefolded spellings observed in the published feed
# (2026-11-11); values are the employer's own spelling and official domain.
_VERIFIED = {
    "aboutyougmbh": ("ABOUT YOU", "aboutyou.com"),
    "acluinternships": ("ACLU", "aclu.org"),
    "andurilindustries": ("Anduril Industries", "anduril.com"),
    "cityofnewyork": ("City of New York", "nyc.gov"),
    "columbiauniversity": ("Columbia University", "columbia.edu"),
    "deliveryhero": ("Delivery Hero", "deliveryhero.com"),
    "deloittenetherlands": ("Deloitte Netherlands", "deloitte.com"),
    "fehrandpeers": ("Fehr & Peers", "fehrandpeers.com"),
    "gannettfleming": ("Gannett Fleming", "gannettfleming.com"),
    "gehealthcare": ("GE HealthCare", "gehealthcare.com"),
    "indiecampers": ("Indie Campers", "indiecampers.com"),
    "keenfootwear": ("KEEN Footwear", "keenfootwear.com"),
    "kensingtontours": ("Kensington Tours", "kensingtontours.com"),
    "kpffconsultingengineers": ("KPFF Consulting Engineers", "kpff.com"),
    "lilasciences": ("Lila Sciences", "lilasciences.com"),
    "marinerfinance": ("Mariner Finance", "marinerfinance.com"),
    "mineralstech": ("Minerals Technologies", "mineralstech.com"),
    "alixpartners": ("AlixPartners", "alixpartners.com"),
    "morganmorganjobsapplynow": ("Morgan & Morgan", "forthepeople.com"),
    "nbcuniversal": ("NBCUniversal", "nbcuniversal.com"),
    "paconsulting": ("PA Consulting", "paconsulting.com"),
    "primehealthcare": ("Prime Healthcare", "primehealthcare.com"),
    "rolandberger": ("Roland Berger", "rolandberger.com"),
    "sargentlundy": ("Sargent & Lundy", "sargentlundy.com"),
    "sharkninjaoperatingllc": ("SharkNinja", "sharkninja.com"),
    "simpsongumpertzheger": ("Simpson Gumpertz & Heger", "sgh.com"),
    "standtogether": ("Stand Together", "standtogether.org"),
    "syngentagroup": ("Syngenta Group", "syngenta.com"),
    "thermofisher": ("Thermo Fisher Scientific", "thermofisher.com"),
    "thenewyorktimes": ("The New York Times", "nytco.com"),
    "veoliaenvironnementsa": ("Veolia", "veolia.com"),
}

# Trailing words that name the corporate form or a location, not the employer.
# Longest first so "pharmaceuticals" beats "pharma" and "healthcare" beats
# "health". Every entry is a word that is a separate token in an employer name;
# nothing here is used to guess a brand boundary.
_DESCRIPTORS = tuple(sorted((
    "pharmaceuticals", "pharmaceutical", "laboratories", "technologies",
    "manufacturing", "distribution", "construction", "engineering",
    "semiconductors", "therapeutics", "entertainment", "universities",
    "infrastructure", "healthcare", "industries", "foundation",
    "association", "university", "laboratory", "management", "consulting",
    "engineers", "insurance", "financial", "logistics", "materials",
    "chemicals", "solutions", "services", "performance", "properties",
    "enterprises", "technology", "beverages", "education", "institute",
    "restaurant", "electrical", "sciences", "partners", "ventures",
    "airlines", "airways", "commerce", "community", "medicine", "research",
    "strategy", "systems", "software", "security", "analytics", "studios",
    "capital", "fashion", "transit", "electric", "building", "networks",
    "travel", "hotels", "resorts", "motors", "mining", "energy", "finance",
    "medical", "hospital", "pharmacy", "dental", "school", "college",
    "society", "council", "agency", "authority", "robotics", "federal",
    "animal", "global", "digital", "labor", "asset", "tours", "foods",
    "retail", "steel", "health", "group", "labs",
    "canada", "mexico", "brazil", "india", "germany", "france", "spain",
    "italy", "japan", "china", "singapore", "australia", "ireland",
    "israel", "netherlands", "poland", "romania", "philippines",
    "malaysia", "vietnam", "usa",
), key=len, reverse=True))

# A location token is the one descriptor whose correct casing is not Title Case.
_DESCRIPTOR_DISPLAY = {"usa": "USA"}

# Canonical spellings are fixed points: a verified brand may shout ("ABOUT
# YOU") and the case repair pass must not rewrite it.
_CANONICAL = {display.casefold(): (display, domain)
              for display, domain in _VERIFIED.values()}

# Page chrome that trails an employer slug, never part of its name.
_JUNK_TAILS = tuple(sorted((
    "jobsapplynow", "applynow", "internships", "internship", "recruitment",
    "opportunities", "recruiting", "vacancies", "employment", "openings",
    "careers", "hiring", "campus", "career", "jobs", "job",
), key=len, reverse=True))

# Legal forms are removed only from a separator-free slug, and only when a
# recognizable name of at least four characters remains.
_LEGAL_TAILS = tuple(sorted((
    "incorporated", "corporation", "limited", "gmbh", "corp", "llc", "ltd",
    "plc", "pte", "pty", "srl", "inc",
), key=len, reverse=True))

# Initialisms that must not be title-cased. Only used when repairing case.
_ACRONYMS = frozenset({
    "aclu", "ag", "ai", "aig", "api", "asu", "aws", "b2b", "b2c", "bcg",
    "bdo", "bge", "bmw", "bny", "bp", "cad", "cdc", "cia", "cna", "cnc",
    "cpa", "cs", "cuny", "dei", "dhs", "dod", "doe", "dsp", "eeo", "ehs",
    "emt", "epa", "eu", "ey", "faa", "fbi", "fpga", "gcp", "ge", "gis",
    "gsk", "hr", "hsbc", "hvac", "ibm", "ima", "imf", "irs", "it", "jpm",
    "kia", "kpff", "lp", "lpn", "mba", "mit", "ml", "nasa", "nhs", "nih",
    "nyu", "oecd", "osha", "phd", "plc", "pnc", "pr", "qa", "rf", "rn",
    "saas", "sec", "sql", "stem", "suny", "ubs", "ucla", "ui", "uk", "un",
    "unicef", "us", "usa", "usc", "usda", "usaa", "ux", "vw", "who", "wto",
})

_ROMAN = frozenset({"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"})

# Lowercase connectors inside a multi-word employer name.
_SMALL_WORDS = frozenset({
    "a", "an", "and", "at", "by", "de", "del", "der", "du", "for", "in",
    "la", "le", "of", "on", "or", "the", "to", "van", "von", "with",
})


def _key(text):
    return _ALNUM.sub("", str(text or "").casefold())


def _clean(name):
    text = html.unescape(str(name or ""))
    text = _TAG.sub(" ", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = _WS.sub(" ", text).strip()
    return text.strip(" \u201c\u201d\"-–—|")


def _title_parts(word):
    """Capitalize a hyphen/slash-joined word without changing its letters."""
    parts = re.split(r"([-/])", word)
    out = []
    for part in parts:
        if part in "-/":
            out.append(part)
        elif part.lower() in _ACRONYMS or part.lower() in _ROMAN:
            out.append(part.upper())
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return "".join(out)


def _case_word(word, first=False, last=False, from_upper=False):
    if not any(character.isalpha() for character in word):
        return word
    low = word.lower()
    if low in _ACRONYMS or low in _ROMAN:
        return word.upper()
    if low == "co-op":
        return "Co-op"
    if low in _SMALL_WORDS and not first and not last:
        return low
    if from_upper and word.isupper() and word.isalpha() and len(word) <= 4:
        # "BDO USA", "GE", "BP": short shouting tokens are usually initialisms.
        return word
    return _title_parts(word)


def _recase(words, from_upper):
    return " ".join(
        _case_word(word, first=index == 0, last=index == len(words) - 1,
                   from_upper=from_upper)
        for index, word in enumerate(words)
    )


def _split_concatenated(text):
    """Split a separator-free slug on known tails.

    Returns ``(words, reason)``; ``words`` is empty when nothing matched, which
    means the slug keeps its published spelling.
    """
    slug = text
    stripped_junk = False
    for suffix in _JUNK_TAILS:
        if slug.endswith(suffix) and len(slug) - len(suffix) >= 4:
            slug = slug[: -len(suffix)]
            stripped_junk = True
            break
    stripped_legal = False
    for suffix in _LEGAL_TAILS:
        # Three letters is enough for "BGE Inc" -> "BGE"; two-letter tails are
        # deliberately absent because "SA" also ends "USA" and "AG" ends many
        # ordinary words (2026-11-11: "Apogeeusa" became "Apogeeu").
        if slug.endswith(suffix) and len(slug) - len(suffix) >= 3:
            slug = slug[: -len(suffix)]
            stripped_legal = True
            break

    words = []
    while True:
        match = next(
            (d for d in _DESCRIPTORS
             if slug.endswith(d) and len(slug) - len(d) >= 4),
            None,
        )
        if not match:
            break
        words.insert(0, _DESCRIPTOR_DISPLAY.get(match, match))
        slug = slug[: -len(match)]

    if not words:
        if stripped_junk or stripped_legal:
            return [slug], "trimmed board/legal suffix"
        return [], "unrecognized concatenation"
    words.insert(0, slug)
    if stripped_junk or stripped_legal:
        return words, "trimmed board/legal suffix"
    return words, "split corporate descriptor"


def _employer(display, confidence, reason, domain=""):
    return Employer(display, _key(display), confidence, reason, domain)


def normalize(name):
    """Return the display spelling and identity of an employer name.

    Never invents an identity: an unrecognized separator-free slug is returned
    as published (only its case is repaired), and the caller can tell the two
    apart through ``confidence`` and ``reason``.
    """
    text = _clean(name)
    if not text:
        return Employer("", "", "unchanged", "empty", "")

    canonical = _CANONICAL.get(text.casefold())
    if canonical:
        return _employer(canonical[0], "verified", "verified alias", canonical[1])

    verified = _VERIFIED.get(_key(text))
    if verified:
        return _employer(verified[0], "verified", "verified alias", verified[1])

    if " " not in text:
        words, reason = _split_concatenated(text)
        if not words:
            return _employer(text, "unchanged", reason)
        display = _recase(words, from_upper=text.isupper())
        verified = _VERIFIED.get(_key(display))
        if verified:
            return _employer(verified[0], "verified", "verified alias", verified[1])
        return _employer(display, "derived", reason)

    from_upper = text.isupper()
    if text.islower() or from_upper:
        return _employer(_recase(text.split(" "), from_upper), "derived",
                         "repaired letter case")
    return _employer(text, "unchanged", "already displayable")


def key(name):
    """Stable identity for an employer name; every alias maps to one value."""
    return _key(normalize(name).display or str(name or ""))
