"""Uncommitted-changes watchdog (F6 phase 1) — periodically scans the
workspace's repos for uncommitted changes (`git status --porcelain`) and
surfaces a persistent notification via ``ctx.notify`` when a repo goes
dirty. Registered through ``ctx.watchdog.register`` (``watchdog:tasks``).

Repo discovery mirrors the old monolith's ``_fetch_repos()``
(`agentic-workspace/src/api/routes/github.py`): the workspace root itself
(if it's a git repo) plus every immediate child of `<root>/repos/` that has
a `.git` dir. The workspace root defaults to the process cwd — aw-workspace
runs with `WORKDIR /opt/agentic-workspace` (bind-mounted to the host
workspace dir), so `os.getcwd()` at call time is the same directory the
monolith called `BASE_DIR`. ``AW_APP_GIT_WATCHDOG_ROOT`` overrides it (tests,
or a non-default workspace layout).
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("aw_apps.git.watchdog")

DEFAULT_INTERVAL_S = 300.0
MIN_INTERVAL_S = 30.0


def workspace_root() -> str:
    return os.environ.get("AW_APP_GIT_WATCHDOG_ROOT") or os.getcwd()


def discover_repos(base_dir: str | None = None) -> list[tuple[str, str]]:
    """Returns ``[(name, path), ...]`` for every git repo under the workspace."""
    base_dir = base_dir or workspace_root()
    repos: list[tuple[str, str]] = []
    if os.path.isdir(os.path.join(base_dir, ".git")):
        repos.append((os.path.basename(os.path.normpath(base_dir)), base_dir))
    repos_dir = os.path.join(base_dir, "repos")
    if os.path.isdir(repos_dir):
        for name in sorted(os.listdir(repos_dir)):
            repo_path = os.path.join(repos_dir, name)
            if os.path.isdir(os.path.join(repo_path, ".git")):
                repos.append((name, repo_path))
    return repos


def uncommitted_files(repo_path: str) -> list[str] | None:
    """Runs `git status --porcelain` for ``repo_path``. Returns the changed-file
    lines (empty list = clean), or None if the check itself failed (git
    missing, not a repo, permission error) — callers should skip, not alarm."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("watchdog: git status failed for %s: %s", repo_path, e)
        return None
    if result.returncode != 0:
        log.warning("watchdog: git status failed for %s: %s", repo_path, result.stderr.strip())
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def scan_uncommitted(base_dir: str | None = None) -> dict[str, list[str]]:
    """Returns ``{repo_name: [changed-file lines]}`` for every dirty repo found."""
    dirty: dict[str, list[str]] = {}
    for name, path in discover_repos(base_dir):
        changes = uncommitted_files(path)
        if changes:
            dirty[name] = changes
    return dirty


class UncommittedWatchdog:
    """Stateful tick: notifies (via ``notify_fn``) only when the set of dirty
    repos, or which files are dirty in them, actually changes — so a repo
    left uncommitted doesn't re-notify every cycle, but new/changed
    uncommitted work does."""

    def __init__(self, notify_fn, base_dir: str | None = None) -> None:
        self._notify_fn = notify_fn
        self._base_dir = base_dir
        self._last_dirty: dict[str, tuple[str, ...]] = {}

    async def tick(self) -> None:
        dirty = scan_uncommitted(self._base_dir)
        current = {name: tuple(changes) for name, changes in dirty.items()}
        if current == self._last_dirty:
            return
        newly_dirty = {
            name: changes for name, changes in current.items()
            if self._last_dirty.get(name) != changes
        }
        self._last_dirty = current
        if not newly_dirty:
            return
        names = ", ".join(sorted(newly_dirty))
        total_files = sum(len(c) for c in newly_dirty.values())
        message = (
            f"Uncommitted changes in {names} ({total_files} file"
            f"{'s' if total_files != 1 else ''})"
        )
        result = self._notify_fn(message, level="warning", title="Uncommitted changes")
        if result is not None and hasattr(result, "__await__"):
            await result
