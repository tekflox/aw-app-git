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


def test_whoami_returns_user_on_success(monkeypatch):
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = '{"login": "octocat", "id": 1}'
            stderr = ""
        return R()

    monkeypatch.setattr(gh_auth.subprocess, "run", fake_run)
    assert gh_auth.whoami() == {"login": "octocat", "id": 1}


def test_whoami_returns_none_when_not_authenticated(monkeypatch):
    """Regression: `gh auth status` was observed exiting 0 with a "not logged
    in" message in one deployed container, which the old text-parsing check
    misread as logged in. `gh api user` can't have that failure mode — a
    non-zero exit (or unparsable body) is the only "not logged in" shape."""
    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "gh: To use GitHub CLI in a GitHub Actions workflow..."
        return R()

    monkeypatch.setattr(gh_auth.subprocess, "run", fake_run)
    assert gh_auth.whoami() is None


def test_configure_git_identity_from_account_fills_from_gh(monkeypatch):
    """One-button login seeds git user.name/email from the GitHub account when
    git has none set yet; private email falls back to the noreply address."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if cmd[:3] == ["git", "config", "--global"] and "--get" in cmd:
            r.stdout = ""  # nothing configured yet
        elif cmd[:3] == ["gh", "api", "user"] or cmd == ["gh", "api", "user"]:
            r.stdout = '{"login": "octocat", "name": "The Octocat", "id": 583231, "email": null}'
        return r

    monkeypatch.setattr(gh_auth.subprocess, "run", fake_run)
    out = gh_auth.configure_git_identity_from_account()
    assert out["user.name"] == "The Octocat"
    assert out["user.email"] == "583231+octocat@users.noreply.github.com"
    # It actually wrote both fields (get returned empty → set).
    assert ["git", "config", "--global", "user.name", "The Octocat"] in calls
    assert ["git", "config", "--global", "user.email", "583231+octocat@users.noreply.github.com"] in calls


def test_configure_git_identity_does_not_clobber_existing(monkeypatch):
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if cmd[:3] == ["git", "config", "--global"] and "--get" in cmd:
            r.stdout = "Existing Name" if cmd[-1] == "user.name" else "me@existing.dev"
        elif cmd[:2] == ["gh", "api"]:
            r.stdout = '{"login": "octocat", "name": "The Octocat", "id": 1, "email": "pub@x.com"}'
        return r

    sets = []
    orig = gh_auth.subprocess.run
    def spy(cmd, **kw):
        if cmd[:3] == ["git", "config", "--global"] and "--get" not in cmd:
            sets.append(cmd)
        return fake_run(cmd, **kw)
    monkeypatch.setattr(gh_auth.subprocess, "run", spy)
    out = gh_auth.configure_git_identity_from_account()
    assert out == {"user.name": "Existing Name", "user.email": "me@existing.dev"}
    assert sets == []  # nothing overwritten
