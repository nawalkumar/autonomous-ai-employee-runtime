"""Package metadata stays synchronized for release packaging."""

import re
from pathlib import Path

from app import __version__


def test_package_version_matches_pyproject() -> None:
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match is not None
    assert match.group(1) == __version__
    assert __version__ == "0.3.0"
