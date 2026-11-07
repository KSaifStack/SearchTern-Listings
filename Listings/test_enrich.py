"""Phase-2 enrichment: only rows that need a description get fetched."""

import enrich
import pandas as pd


def _frame(rows):
    return pd.DataFrame(rows, columns=[
        "company", "role", "date", "link", "description", "job_type"])


def test_plan_skips_rows_that_already_have_text():
    frame = _frame([
        ["A", "SWE Intern", "2026-11-01", "https://a.example/1", "already here", "internship"],
        ["B", "SWE Intern", "2026-11-02", "https://b.example/1", "", "internship"],
    ])
    assert enrich.plan(frame) == [1]


def test_plan_respects_cap_types_and_new_only():
    frame = _frame([
        ["A", "Intern", "2026-11-01", "https://a.example/1", "", "internship"],
        ["B", "Intern", "2026-11-03", "https://b.example/1", "", "internship"],
        ["C", "Intern", "2026-11-02", "https://c.example/1", "", "marketing"],
    ])
    assert enrich.plan(frame, cap=2) == [1, 0]          # newest first
    assert enrich.plan(frame, new_only=lambda row: row.name == 0) == [0]
    assert enrich.plan(frame, types=()) == [1, 2, 0]    # no type filter


def test_extract_description_prefers_og_meta():
    page = """<html><head>
      <meta name="description" content="short">
      <meta property="og:description" content="Build intern tools at Acme for the summer.">
    </head><body>noise</body></html>"""
    assert enrich.extract_description(page) == "Build intern tools at Acme for the summer."
    assert enrich.extract_description("<html><p>no meta</p></html>") == ""


def test_apply_plan_fills_and_counts_failures():
    frame = _frame([
        ["A", "Intern", "2026-11-01", "https://a.example/1", "", "internship"],
        ["B", "Intern", "2026-11-02", "https://b.example/1", "", "internship"],
    ])

    def fetch(url):
        if "b.example" in url:
            raise RuntimeError("boom")
        return ('<meta property="og:description" content="'
                'Ship the intern dashboard with the platform team.">')

    stats = enrich.apply_plan(frame, [0, 1], fetch=fetch, workers=2)
    assert stats["planned"] == 2 and stats["filled"] == 1 and stats["failed"] == 1
    assert frame.at[0, "description"].startswith("Ship the intern dashboard")
    assert frame.at[1, "description"] == ""


def test_empty_plan_is_a_noop():
    frame = _frame([["A", "Intern", "2026-11-01", "https://a.example/1", "", "internship"]])
    assert enrich.plan(None) == []
    assert enrich.apply_plan(frame, [])["planned"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
