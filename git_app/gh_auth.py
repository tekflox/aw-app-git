"""
gh login flow driven by the app's settings panel (config_schema.auth_method /
config_schema.github_token in aw-app.json). The token itself is NEVER read
from plain config — in the real framework it's resolved by ctx.secrets from
the zero-knowledge store (feature:user-zero-knowledge-secret-storage) and
handed to login_with_token() as a plain string only for the duration of the
`gh auth login` subprocess call; it is never written to disk here.
"""

from __future__ import annotations

import os
import re
import subprocess


class GhAuthError(RuntimeError):
    pass


_USERNAME_RE = re.compile(r"Logged in to [^\s]+ account (\S+)")


def login_with_token(token: str) -> str:
    """Runs `gh auth login --with-token`, piping the token on stdin (never argv/env)."""
    if not token:
        raise GhAuthError("no token provided")
    result = subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GhAuthError(f"gh auth login failed: {result.stderr.strip()}")
    return status()


def login_web() -> str:
    """
    Interactive device-code web flow (`gh auth login --web`). Only usable
    where the caller can relay the printed one-time code + URL to the user
    (e.g. surfaced in the settings panel once the framework's settings UI
    exists) — not usable headless. Returns gh's combined stdout/stderr.
    """
    result = subprocess.run(
        ["gh", "auth", "login", "--web", "--git-protocol", "https"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GhAuthError(f"gh auth login --web failed: {result.stderr.strip()}")
    return result.stdout.strip() or result.stderr.strip()


def status() -> str:
    """Returns `gh auth status` output. Raises GhAuthError if not logged in."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GhAuthError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def logged_in_username(status_text: str) -> str | None:
    """Parses the account name out of `gh auth status` output, e.g.
    'Logged in to github.com account octocat (oauth_token)' -> 'octocat'."""
    match = _USERNAME_RE.search(status_text)
    return match.group(1) if match else None


def logout() -> None:
    """Runs `gh auth logout` for github.com. GH_PROMPT_DISABLED skips the
    interactive confirmation prompt (there's no non-interactive flag)."""
    result = subprocess.run(
        ["gh", "auth", "logout", "--hostname", "github.com"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )
    if result.returncode != 0:
        raise GhAuthError(f"gh auth logout failed: {result.stderr.strip()}")
