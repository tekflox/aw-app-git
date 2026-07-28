"""Unit tests for git_app/uncommitted_watchdog.py.

Run: .venv/aw/bin/python -m pytest tests/test_uncommitted_watchdog.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import uncommitted_watchdog as wd  # noqa: E402


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello\n")
    _git("add", ".", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)


def test_discover_repos_finds_root_and_children(tmp_path):
    _init_repo(tmp_path)
    repos_dir = tmp_path / "repos"
    _init_repo(repos_dir / "alpha")
    (repos_dir / "not-a-repo").mkdir()  # no .git — must be skipped

    found = dict(wd.discover_repos(str(tmp_path)))
    assert tmp_path.name in found
    assert found[tmp_path.name] == str(tmp_path)
    assert "alpha" in found
    assert "not-a-repo" not in found


def test_discover_repos_root_not_a_repo(tmp_path):
    repos_dir = tmp_path / "repos"
    _init_repo(repos_dir / "alpha")
    found = dict(wd.discover_repos(str(tmp_path)))
    assert tmp_path.name not in found
    assert "alpha" in found


def test_uncommitted_files_clean_repo(tmp_path):
    _init_repo(tmp_path)
    assert wd.uncommitted_files(str(tmp_path)) == []


def test_uncommitted_files_dirty_repo(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "new.txt").write_text("scratch\n")
    changes = wd.uncommitted_files(str(tmp_path))
    assert changes is not None
    assert any("new.txt" in line for line in changes)


def test_uncommitted_files_not_a_repo_returns_none(tmp_path):
    assert wd.uncommitted_files(str(tmp_path)) is None


def test_scan_uncommitted_only_lists_dirty_repos(tmp_path):
    repos_dir = tmp_path / "repos"
    clean = repos_dir / "clean"
    dirty = repos_dir / "dirty"
    _init_repo(clean)
    _init_repo(dirty)
    (dirty / "wip.txt").write_text("wip\n")

    dirty_map = wd.scan_uncommitted(str(tmp_path))
    assert "dirty" in dirty_map
    assert "clean" not in dirty_map


def test_watchdog_notifies_on_new_dirty_repo(tmp_path):
    repos_dir = tmp_path / "repos"
    repo = repos_dir / "app"
    _init_repo(repo)

    calls = []

    def notify(message, **kw):
        calls.append((message, kw))

    watchdog = wd.UncommittedWatchdog(notify, base_dir=str(tmp_path))
    asyncio.run(watchdog.tick())
    assert calls == []  # clean — no notification

    (repo / "wip.txt").write_text("wip\n")
    asyncio.run(watchdog.tick())
    assert len(calls) == 1
    assert "app" in calls[0][0]
    assert calls[0][1]["level"] == "warning"


def test_watchdog_does_not_renotify_same_dirty_state(tmp_path):
    repo = tmp_path
    _init_repo(repo)
    (repo / "wip.txt").write_text("wip\n")

    calls = []
    watchdog = wd.UncommittedWatchdog(lambda m, **kw: calls.append(m), base_dir=str(tmp_path))
    asyncio.run(watchdog.tick())
    asyncio.run(watchdog.tick())
    assert len(calls) == 1  # same dirty set both times — only one notification


def test_watchdog_renotifies_when_dirty_set_changes(tmp_path):
    repo = tmp_path
    _init_repo(repo)

    calls = []
    watchdog = wd.UncommittedWatchdog(lambda m, **kw: calls.append(m), base_dir=str(tmp_path))

    (repo / "a.txt").write_text("a\n")
    asyncio.run(watchdog.tick())
    assert len(calls) == 1

    (repo / "b.txt").write_text("b\n")
    asyncio.run(watchdog.tick())
    assert len(calls) == 2
