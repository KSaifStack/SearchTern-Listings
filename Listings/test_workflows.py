"""Every publishing workflow must run tests under a bounded timeout."""
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
PUBLISHERS = (
    "update-light.yml",
    "update-medium.yml",
    "update-heavy.yml",
    "update-all.yml",
    "update-priority.yml",
)


def test_every_publisher_runs_pytest_before_the_scraper():
    for name in PUBLISHERS:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "python -m pytest -q" in text, name
        assert "timeout-minutes:" in text, name
        assert text.index("python -m pytest -q") < text.index("generate_listings.py"), name
