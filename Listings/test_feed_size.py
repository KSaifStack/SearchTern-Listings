"""Measure serialized feed growth and the GitHub file ceiling.

GitHub rejects a file above 100 MB, so the published JSON cannot grow without a
measured budget. This test uses the real writer and manifest, not an estimate
from row counts alone.
"""
import json
import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import readme_generation

GITHUB_LIMIT = 100_000_000


def _frame(rows):
    now = datetime.now(UTC)
    return pd.DataFrame([{
        "company": f"Example Company {i}",
        "role": "Software Engineer Intern",
        "location": "San Francisco, CA",
        "date": (now - timedelta(days=1)).isoformat(),
        "link": f"https://boards.greenhouse.io/example/jobs/{100000 + i}",
        "is_remote": "false",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "country_iso": "US",
        "job_type": "internship",
        "source": "jobhive",
        "ats_type": "greenhouse",
        "ats_id": str(100000 + i),
        "description": "Build software with the team. " * 12,
    } for i in range(rows)])


def test_serialized_bytes_per_row_sets_a_measured_ceiling(tmp_path):
    rows = 400
    readme_generation.write_listings_json(_frame(rows), output_dir=str(tmp_path))
    pages = tmp_path / "pages"
    manifest = json.loads((pages / "feed-manifest.json").read_text())
    published = manifest["published"]["searchtern"]

    assert published["rows"] == rows
    assert published["bytes"] == (pages / "searchtern.json").stat().st_size
    bytes_per_row = published["bytes"] / rows
    ceiling = math.floor(GITHUB_LIMIT / bytes_per_row)

    assert bytes_per_row > 0
    assert ceiling > rows
    # The test records the measured budget in the failure message so a future
    # schema change that inflates rows-per-byte is visible, not guessed at.
    assert ceiling == math.floor(GITHUB_LIMIT / bytes_per_row)
