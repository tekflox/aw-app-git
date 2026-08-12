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


def test_login_with_token_syncs_creds_to_data_dir(monkeypatch, tmp_path):
    """login_with_token() must mirror gh's own ~/.config/gh + ~/.gitconfig
    into <AW_WORKSPACE_HOME>/data/git/ so a spawned agent container (Agent
    Config "GitHub / Git" permission on) can pick them up — see the module
    docstring / _sync_creds_to_data_dir()."""
    fake_home = tmp_path / "home"
    (fake_home / ".config" / "gh").mkdir(parents=True)
    (fake_home / ".config" / "gh" / "hosts.yml").write_text("github.com:\n  oauth_token: abc123\n")
    (fake_home / ".gitconfig").write_text("[user]\n\tname = octocat\n")
    monkeypatch.setattr(gh_auth.Path, "home", lambda: fake_home)

    workspace_home = tmp_path / "workspace-home"
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(workspace_home))

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "Logged in to github.com account octocat (oauth_token)"
            stderr = ""
        return R()

    monkeypatch.setattr(gh_auth.subprocess, "run", fake_run)
    gh_auth.login_with_token("ghp_whatever")

    synced_hosts = workspace_home / "data" / "git" / "config-gh" / "hosts.yml"
    synced_gitconfig = workspace_home / "data" / "git" / "gitconfig"
    assert synced_hosts.read_text() == "github.com:\n  oauth_token: abc123\n"
    assert synced_gitconfig.read_text() == "[user]\n\tname = octocat\n"


def test_logout_clears_synced_creds(monkeypatch, tmp_path):
    workspace_home = tmp_path / "workspace-home"
    data_dir = workspace_home / "data" / "git"
    (data_dir / "config-gh").mkdir(parents=True)
    (data_dir / "config-gh" / "hosts.yml").write_text("stale")
    (data_dir / "gitconfig").write_text("stale")
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(workspace_home))

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(gh_auth.subprocess, "run", fake_run)
    gh_auth.logout()

    assert not (data_dir / "config-gh").exists()
    assert not (data_dir / "gitconfig").exists()


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


# --- scope reporting ---------------------------------------------------------


def test_broad_scopes_imply_the_narrow_ones():
    """A token with admin:org holds read:org — GitHub only reports the broad
    one, so a naive membership check would raise a false 'missing' warning."""
    from git_app.gh_auth import _effective_scopes
    eff = _effective_scopes(["admin:org", "write:packages", "repo"])
    assert "read:org" in eff
    assert "read:packages" in eff
    assert "public_repo" in eff


def test_nothing_reported_missing_for_a_full_admin_token(monkeypatch):
    from git_app import gh_auth
    monkeypatch.setattr(gh_auth, "_effective_scopes", gh_auth._effective_scopes)
    eff = gh_auth._effective_scopes(["repo", "admin:org", "workflow", "write:packages"])
    assert [s for s in gh_auth.RECOMMENDED_SCOPES if s not in eff] == []


def test_a_bare_repo_token_is_reported_as_missing_the_rest():
    from git_app import gh_auth
    eff = gh_auth._effective_scopes(["repo"])
    missing = [s for s in gh_auth.RECOMMENDED_SCOPES if s not in eff]
    assert "read:org" in missing and "read:packages" in missing
