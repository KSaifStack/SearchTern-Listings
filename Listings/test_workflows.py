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
FEED_PUBLISHERS = PUBLISHERS[:-1]


def test_every_publisher_runs_pytest_before_the_scraper():
    for name in PUBLISHERS:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "python -m pytest -q" in text, name
        assert "timeout-minutes:" in text, name
        assert text.index("python -m pytest -q") < text.index("generate_listings.py"), name


def test_feed_publishers_upload_assets_and_exclude_monoliths():
    for name in FEED_PUBLISHERS:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert 'SEARCHTERN_FEED_DELIVERY: "release"' in text, name
        assert "gh release upload" in text, name
        assert "gh release download" in text, name
        assert text.index("gh release upload") < text.index("Commit and push updates"), name
        assert "exclude)pages/searchtern.json" in text, name
        assert "exclude)pages/listings.json" in text, name
        assert "git checkout -- pages/searchtern.json pages/listings.json" in text, name
