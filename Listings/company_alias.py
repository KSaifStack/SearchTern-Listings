"""Employer-name normalization for joining a board to the postings on it.

``closure`` decides whether an ATS board still lists a requisition by joining a
board's employer to a published row's employer. The two sides spell the same
company differently -- jobhive publishes the ATS's own string ("Booz Allen"),
the board registry and the host slug use another ("bah", "Booz Allen
Hamilton") -- and an unmatched join is an unverified row, which keeps a dead
posting forever. Measured 2026-11-07: 1,696 published rows across 461 employer
pairs failed that join for spelling alone.

Two rules, both conservative:

* **Legal suffixes are not part of a name.** ``Northrop Grumman Corporation``
  and ``Northrop Grumman`` are the same employer. Only legal forms are stripped
  (inc, llc, ltd, corp, plc, gmbh, pty, pte); business words like "Systems",
  "Group" or "Technologies" are *not*, because "General Dynamics" and "General
  Dynamics Mission Systems" are different employers and merging them would let
  one board vote on the other's rows.
* **Initialisms are aliased, never guessed.** ``bah`` -> ``Booz Allen
  Hamilton`` cannot be derived from the letters, so it is listed explicitly.
  Only pairs observed failing in the published feed are in the table; a
  heuristic that maps any short key to any longer one will eventually merge two
  employers, and a wrong merge deletes a live posting.
"""

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Legal forms only -- never business descriptors.
_SUFFIXES = (
    "incorporated", "inc", "llc", "llp", "ltd", "limited", "corp",
    "corporation", "plc", "gmbh", "pty", "pte",
)

# Measured alias pairs (2026-11-07 feed audit). Keys and values are already
# alphanumeric-casefolded; every value is the canonical spelling.
_ALIASES = {
    "bah": "boozallenhamilton",
    "boozallen": "boozallenhamilton",
    "ngc": "northropgrumman",
    "amat": "appliedmaterials",
    "gdms": "generaldynamicsmissionsystems",
    "snc": "sierranevadacorporation",
    "aero": "aerospacecorporation",
    "alliance": "nissan",
    "ag": "airbus",
    "leonardocompany": "leonardo",
    "americascookmedical": "cookgroup",
    "carrieresmarvesting": "marvesting",
    "microntechnology": "micron",
    "llnl": "lawrencelivermorenationallaboratory",
    "talos": "talostrading",
}


def normalize(name):
    """Return the join key for an employer name (``""`` for no name)."""
    if name is None:
        return ""
    raw = _NON_ALNUM.sub("", str(name).casefold())
    if not raw:
        return ""
    key = raw
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix) + 2:
                key = key[: -len(suffix)]
                changed = True
    return _ALIASES.get(key) or _ALIASES.get(raw) or key
