"""
gh login flow driven by the app's settings panel (config_schema.auth_method /
config_schema.github_token in aw-app.json). The token itself is NEVER read
from plain config — in the real framework it's resolved by ctx.secrets from
the zero-knowledge store (feature:user-zero-knowledge-secret-storage) and
handed to login_with_token() as a plain string only for the duration of the
`gh auth login` subprocess call; it is never written to disk here.
"""

from __future__ import annotations

import json
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


def _git_config_get(key: str) -> str:
    r = subprocess.run(
        ["git", "config", "--global", "--get", key],
        capture_output=True, text=True, check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _gh_api_json(path: str):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def configure_git_identity_from_account() -> dict:
    """Populate git's global ``user.name`` / ``user.email`` from the signed-in
    GitHub account, so a single "Sign in with GitHub" also sets up git commits —
    no manual name/email entry needed.

    Non-destructive: only fills a field that isn't already configured (never
    clobbers an identity the user set on purpose). Email falls back to GitHub's
    ``<id>+<login>@users.noreply.github.com`` when the account email is private
    (that address still authors pushes correctly). Returns the effective
    ``{"user.name", "user.email"}`` after the call."""
    user = _gh_api_json("user") or {}
    login = str(user.get("login") or "")
    name = str(user.get("name") or "").strip() or login
    email = str(user.get("email") or "").strip()
    if not email and login:
        uid = user.get("id")
        email = (
            f"{uid}+{login}@users.noreply.github.com" if uid
            else f"{login}@users.noreply.github.com"
        )

    cur_name = _git_config_get("user.name")
    if not cur_name and name:
        subprocess.run(["git", "config", "--global", "user.name", name], check=False)
        cur_name = name

    cur_email = _git_config_get("user.email")
    if not cur_email and email:
        subprocess.run(["git", "config", "--global", "user.email", email], check=False)
        cur_email = email

    return {"user.name": cur_name, "user.email": cur_email}


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
    """Returns `gh auth status` output. Raises GhAuthError if not logged in.

    `gh auth status` writes its human-readable report to STDERR (not stdout) in
    most gh versions, so combine both streams — otherwise the "Logged in to …
    account <user>" line the username parser needs is invisible and the status
    panel shows a logged-in state with no username."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise GhAuthError(combined)
    return combined


def logged_in_username(status_text: str) -> str | None:
    """Parses the account name out of `gh auth status` output, e.g.
    'Logged in to github.com account octocat (oauth_token)' -> 'octocat'."""
    match = _USERNAME_RE.search(status_text)
    return match.group(1) if match else None


def whoami() -> dict | None:
    """Authoritative login check: calls `gh api user` (a real authenticated API
    request) instead of parsing `gh auth status`'s human-readable report.

    `gh auth status` proved unreliable as a logged-in signal: its message
    format and exit-code behavior vary across gh versions/environments, and
    at least one deployed container was observed returning exit code 0 with a
    "You are not logged into any GitHub hosts" message — the settings panel
    read that as logged_in:true. `gh api user` can't have that ambiguity: it
    either returns the account JSON (exit 0, authenticated) or fails (exit
    non-zero / no parseable JSON, not authenticated) — there's no "successful
    exit with a not-logged-in message" middle state.

    Returns the parsed user object ({"login", "name", ...}) or None."""
    return _gh_api_json("user")


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
