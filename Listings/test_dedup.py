import ast
import os
import tempfile
from pathlib import Path

import pandas as pd
from dedup import dedup_across, dedup_keys, dedupe


def frame(rows):
    return pd.DataFrame(
        rows, columns=["company", "role", "location"]
    )


def test_employer_spellings_are_one_identity():
    postings = pd.DataFrame([
        {"company": "Andurilindustries", "role": "Software Engineer Intern",
         "location": "Seattle, WA"},
        {"company": "Anduril Industries", "role": "Software Engineer Intern",
         "location": "Seattle, WA"},
    ])
    assert len(dedupe(postings)) == 1


def test_distinct_employers_and_locations_survive():
    postings = pd.DataFrame([
        {"company": "General Dynamics", "role": "Engineering Intern",
         "location": "Falls Church, VA"},
        {"company": "General Dynamics Mission Systems", "role": "Engineering Intern",
         "location": "Falls Church, VA"},
        {"company": "Acme", "role": "Engineering Intern", "location": "Phoenix, AZ"},
        {"company": "Acme", "role": "Engineering Intern", "location": "Tempe, AZ"},
    ])
    assert len(dedupe(postings)) == 4


def test_requisition_id_outranks_a_shared_tracker_link():
    same_link = pd.DataFrame([
        {"company": "Acme", "role": "Intern", "location": "Remote",
         "ats_type": "gh", "ats_id": "1", "link": "https://tracker.test/abc"},
        {"company": "Acme", "role": "Intern", "location": "Remote",
         "ats_type": "gh", "ats_id": "2", "link": "https://tracker.test/abc"},
    ])
    # One tracker address fronts two requisitions: both are real jobs.
    assert len(dedupe(same_link)) == 2

    same_job = pd.DataFrame([
        {"company": "Acme", "role": "Intern", "location": "Remote",
         "ats_type": "gh", "ats_id": "1", "link": "https://acme.test/1"},
        {"company": "Acme", "role": "Intern", "location": "Remote",
         "ats_type": "gh", "ats_id": "1", "link": "https://tracker.test/abc"},
    ])
    assert len(dedupe(same_job)) == 1


def run_checks():
    df = frame(
        [
            ["Acme", "Engineer Intern", "Phoenix, AZ"],
            ["acme", "engineer  intern", " phoenix, az "],
            ["Acme", "Engineer Intern", "Tempe, AZ"],
        ]
    )
    out = dedupe(df)
    assert len(out) == 2, out
    assert list(out["location"]) == ["Phoenix, AZ", "Tempe, AZ"]

    base = frame([["Acme", "Engineer Intern", "Phoenix, AZ"]])
    extra = frame(
        [
            ["ACME", "Engineer Intern", "Phoenix, AZ"],
            ["Beta", "Engineer Intern", "Phoenix, AZ"],
        ]
    )
    rest = dedup_across(base, extra)
    assert len(rest) == 1, rest
    assert rest.iloc[0]["company"] == "Beta"

    assert dedup_keys(frame([["A  B", "R", " L "]])).iloc[0] == "details|a b|r|l"

    postings = pd.DataFrame([
        {"company": "Acme", "role": "Intern", "location": "Remote", "link": "https://example.com/jobs/1?utm_source=a"},
        {"company": "Acme", "role": "Intern", "location": "Remote", "link": "https://example.com/jobs/1?utm_source=b"},
        {"company": "Acme", "role": "Intern", "location": "Remote", "link": "https://example.com/jobs/2"},
        {"company": "Acme", "role": "Intern", "location": "Remote", "link": "https://example.com/job?id=1"},
        {"company": "Acme", "role": "Intern", "location": "Remote", "link": "https://example.com/job?id=2"},
    ])
    assert len(dedupe(postings)) == 4
    assert len(dedup_across(postings.iloc[:1], postings.iloc[1:])) == 3
    # Execute only the cache writer: importing the pipeline would run the scraper.
    tree = ast.parse(Path(__file__).with_name("generate_listings.py").read_text())
    writer = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_save_cache")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tier_test.parquet")
        env = {"os": os, "tempfile": tempfile, "CACHE_DIR": tmp, "_cache_path": lambda _: path}
        exec(compile(ast.Module(body=[writer], type_ignores=[]), "generate_listings.py", "exec"), env)
        frame([["Old", "Role", "Place"]]).to_parquet(path, index=False)
        class BrokenFrame:
            def to_parquet(self, *args, **kwargs):
                raise OSError("write failed")
        try:
            env["_save_cache"](BrokenFrame(), "test")
            raise AssertionError("write should fail")
        except OSError:
            pass
        assert pd.read_parquet(path).iloc[0]["company"] == "Old"
        assert os.listdir(tmp) == ["tier_test.parquet"]

    print("  test_dedup: all checks passed")


if __name__ == "__main__":
    run_checks()
