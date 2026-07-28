"""Unit tests for git_app/gh_auth.py's username parsing.

Run: .venv/aw/bin/python -m pytest tests/test_gh_auth.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import gh_auth  # noqa: E402


def test_logged_in_username_parses_account():
    text = (
        "github.com\n"
        "  ✓ Logged in to github.com account octocat (oauth_token)\n"
        "  - Active account: true\n"
    )
    assert gh_auth.logged_in_username(text) == "octocat"


def test_logged_in_username_returns_none_when_not_found():
    assert gh_auth.logged_in_username("some unrelated output") is None
