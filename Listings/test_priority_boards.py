"""Self-check for the priority fast-lane board list.

Run: python test_priority_boards.py  (stdlib only — no network, no pandas)
"""
import sys

import priority_boards

VALID_ATS = {"Greenhouse", "Lever", "Ashby", "SmartRecruiters"}
BOARDS = priority_boards.boards()

checks = [
    ("list is non-empty", len(BOARDS) > 0),
    ("every entry is a 3-tuple",
     all(isinstance(b, tuple) and len(b) == 3 for b in BOARDS)),
    ("every ats type is supported",
     all(b[1] in VALID_ATS for b in BOARDS)),
    ("no empty company/slug",
     all(b[0].strip() and b[2].strip() for b in BOARDS)),
    ("no duplicate (ats, slug) pairs",
     len({(b[1], b[2]) for b in BOARDS}) == len(BOARDS)),
    ("no duplicate company names",
     len({b[0].lower() for b in BOARDS}) == len(BOARDS)),
    ("covers all three public ATS kinds",
     {"Greenhouse", "Lever", "Ashby"} <= {b[1] for b in BOARDS}),
    ("boards() returns a copy, not the module list",
     priority_boards.boards() is not priority_boards.PRIORITY_BOARDS),
]

failures = []
for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)

print(f"\n{len(BOARDS)} priority boards across "
      f"{len({b[1] for b in BOARDS})} ATS kinds")

if failures:
    print(f"\n{len(failures)} check(s) failed:", *failures, sep="\n  ")
    sys.exit(1)
print("\nAll checks passed.")
