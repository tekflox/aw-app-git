"""
gh login flow driven by the app's settings panel (config_schema.auth_method /
config_schema.github_token in aw-app.json). The token itself is NEVER read
from plain config — in the real framework it's resolved by ctx.secrets from
the zero-knowledge store (feature:user-zero-knowledge-secret-storage) and
handed to login_with_token() as a plain string only for the duration of the
`gh auth login` subprocess call; it is never written to disk here.

Once logged in, `gh` itself has ALREADY written the real credential
(`~/.config/gh/hosts.yml`) and `configure_git_identity_from_account()` may
have touched `~/.gitconfig` — `_sync_creds_to_data_dir()` mirrors those two
gh/git-owned files into this app's own sanctioned data dir
(`<AW_WORKSPACE_HOME>/data/git/`, gated by the `fs:workspace-data`
permission this app already declares) so OTHER processes on the same
workspace (a spawned agent-CLI container whose Agent Config has the
"GitHub / Git" permission on) can pick them up too. This is the same shape
the pre-decoupling `agents-platform` host-path mount
(`{AW_BASE_DIR}/data/home/.gitconfig` / `.config/gh`, see
`executor.py`'s `_perm_volumes["github"]`) already relied on — that copy
used to be done by hand; this makes it automatic, still just relocating
gh's own already-written files, never inventing a new plaintext secret.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


class GhAuthError(RuntimeError):
    pass


def _data_dir() -> Path:
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.expanduser("~/.aw-workspace")
    return Path(home) / "data" / "git"


def _sync_creds_to_data_dir() -> None:
    """Best-effort mirror of `~/.config/gh` + `~/.gitconfig` into this app's
    data dir. Never raises — a failure here must not fail the login/logout
    call it's attached to; the settings panel already confirmed gh's own
    auth succeeded, that's the source of truth for "are we logged in"."""
    try:
        dst_root = _data_dir()
        dst_root.mkdir(parents=True, exist_ok=True)

        src_gh = Path.home() / ".config" / "gh"
        if src_gh.is_dir():
            dst_gh = dst_root / "config-gh"
            shutil.rmtree(dst_gh, ignore_errors=True)
            shutil.copytree(src_gh, dst_gh)

        src_gitconfig = Path.home() / ".gitconfig"
        if src_gitconfig.is_file():
            shutil.copyfile(src_gitconfig, dst_root / "gitconfig")
    except Exception:
        pass


def _clear_creds_from_data_dir() -> None:
    """Reverses `_sync_creds_to_data_dir()` — called on logout so a revoked
    login doesn't leave a stale, still-working copy sitting in the data dir
    for spawned agent containers to keep picking up."""
    try:
        dst_root = _data_dir()
        shutil.rmtree(dst_root / "config-gh", ignore_errors=True)
        (dst_root / "gitconfig").unlink(missing_ok=True)
    except Exception:
        pass


_USERNAME_RE = re.compile(r"Logged in to [^\s]+ account (\S+)")


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` that turns a missing ``gh``/``git`` binary into an
    ordinary failed result (exit 127) instead of an unhandled
    ``FileNotFoundError`` — the CLI can vanish out from under a running app
    (see the F4 runtime's system-CLI healer, ``src/apps/commands.py``)
    between two calls here; every caller already handles a non-zero
    returncode, so this needs no special-casing at the call sites."""
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def login_with_token(token: str) -> str:
    """Runs `gh auth login --with-token`, piping the token on stdin (never argv/env)."""
    if not token:
        raise GhAuthError("no token provided")
    result = _run(["gh", "auth", "login", "--with-token"], input=token)
    if result.returncode != 0:
        raise GhAuthError(f"gh auth login failed: {result.stderr.strip()}")
    # Wires plain `git` (clone/push/pull over HTTPS) to authenticate through
    # `gh`'s own credential store too — writes a `credential."https://
    # github.com".helper = !gh auth git-credential` block into ~/.gitconfig.
    # Without this, only `gh` subcommands would work after login; git itself
    # would still prompt/fail. Best-effort: `gh auth login` above already
    # succeeded, a failure here shouldn't fail the whole login.
    _run(["gh", "auth", "setup-git"])
    _sync_creds_to_data_dir()
    return status()


def _git_config_get(key: str) -> str:
    r = _run(["git", "config", "--global", "--get", key])
    return r.stdout.strip() if r.returncode == 0 else ""


def _gh_api_json(path: str):
    r = _run(["gh", "api", path])
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
        _run(["git", "config", "--global", "user.name", name])
        cur_name = name

    cur_email = _git_config_get("user.email")
    if not cur_email and email:
        _run(["git", "config", "--global", "user.email", email])
        cur_email = email

    _sync_creds_to_data_dir()
    return {"user.name": cur_name, "user.email": cur_email}


def status() -> str:
    """Returns `gh auth status` output. Raises GhAuthError if not logged in.

    `gh auth status` writes its human-readable report to STDERR (not stdout) in
    most gh versions, so combine both streams — otherwise the "Logged in to …
    account <user>" line the username parser needs is invisible and the status
    panel shows a logged-in state with no username."""
    result = _run(["gh", "auth", "status"])
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


# Scopes the workspace itself relies on beyond a plain `repo` login. The
# device flow can NEVER grant these: an OAuth App's device-flow grant is
# limited to the scopes that App requests, and aw-app-git's public OAuth App
# asks only for repo/read:org/gist/workflow. Publishing an app image to GHCR
# or managing an org runner group therefore needs a PAT, and the settings
# panel should say so rather than leaving the user to discover it from a
# 403 much later.
RECOMMENDED_SCOPES = ("repo", "read:org", "workflow", "read:packages")

# GitHub scopes nest: granting a broad one implies the narrower ones, and
# the API only ever reports the broad one. A plain set-membership check
# therefore reports `read:org` "missing" on a token that holds `admin:org`
# — a false warning, which is worse than no warning because it teaches the
# user to ignore the panel. Only the implications this app checks for.
_SCOPE_IMPLIES = {
    "admin:org": ("write:org", "read:org"),
    "write:org": ("read:org",),
    "write:packages": ("read:packages",),
    "delete:packages": ("read:packages",),
    "admin:repo_hook": ("write:repo_hook", "read:repo_hook"),
    "admin:public_key": ("write:public_key", "read:public_key"),
    "repo": ("public_repo", "repo:status", "repo_deployment", "repo:invite",
             "security_events"),
    "user": ("read:user", "user:email", "user:follow"),
}


def _effective_scopes(granted: list[str]) -> set[str]:
    """Granted scopes plus everything they imply."""
    out = set(granted)
    for scope in granted:
        out.update(_SCOPE_IMPLIES.get(scope, ()))
    return out


def token_info() -> dict:
    """Masked token + granted scopes, for display in the settings panel.

    The raw token is NEVER returned — only enough of it to recognise which
    credential is in place (prefix + last 4). That's the whole point: a
    secret field that renders blank is indistinguishable from "nothing
    saved", which is exactly the confusion this reports away.

    Scopes come from GitHub's ``X-OAuth-Scopes`` response header, so they
    reflect what the credential can actually do, not what someone intended
    when creating it.
    """
    out: dict = {"masked": None, "scopes": [], "missing_scopes": [], "kind": None}
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                text=True, timeout=15)
        token = (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        token = ""
    if not token:
        return out

    out["kind"] = "oauth (device flow)" if token.startswith("gho_") else "personal access token"
    out["masked"] = f"{token[:4]}{'•' * 8}{token[-4:]}" if len(token) > 12 else "••••"

    # `gh api -i` prepends the response headers to the body.
    try:
        result = subprocess.run(["gh", "api", "-i", "user"], capture_output=True,
                                text=True, timeout=20)
        for line in (result.stdout or "").splitlines():
            if line.lower().startswith("x-oauth-scopes:"):
                raw = line.split(":", 1)[1]
                out["scopes"] = [s.strip() for s in raw.split(",") if s.strip()]
                break
    except (OSError, subprocess.SubprocessError):
        pass

    effective = _effective_scopes(out["scopes"])
    out["missing_scopes"] = [s for s in RECOMMENDED_SCOPES if s not in effective]
    return out


def logout() -> None:
    """Runs `gh auth logout` for github.com. GH_PROMPT_DISABLED skips the
    interactive confirmation prompt (there's no non-interactive flag)."""
    result = _run(
        ["gh", "auth", "logout", "--hostname", "github.com"],
        env={**os.environ, "GH_PROMPT_DISABLED": "1"},
    )
    if result.returncode != 0:
        raise GhAuthError(f"gh auth logout failed: {result.stderr.strip()}")
    _clear_creds_from_data_dir()
