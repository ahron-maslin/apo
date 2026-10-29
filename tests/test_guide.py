"""The user guide lives in docs/ for reading on GitHub and in the package for the
dashboard's /guide page; the two copies must not drift apart."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_packaged_guide_matches_the_docs():
    assert (ROOT / "src" / "apo" / "resources" / "guide.md").read_text() == (ROOT / "docs" / "user-guide.md").read_text()
