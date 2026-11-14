"""Regression test for card 4185bf3b-9510-8132: GET /github/maintenance
?ping=true hung (~10s connection timeout) right after a deploy.

Root cause (confirmed live): MaintenanceWatchdog's cache is per-worker-
process, and GitAppPlugin.activate() re-runs on EVERY worker on every
deploy/reconcile, resetting every worker's cache to cold. With the
"github_maintenance" watchdog registered ``run_immediately=False``, none of
those cold caches warm themselves until either the task's own 30-minute
first-tick delay elapses, or an unlucky inbound request lands on a cold
worker and eats the full scan inline (measured ~38s against this
workspace's ~90-repo universe) — exactly what the github-maintenance dag
Workflow's http gate node (a ~10s client timeout) hit twice in a row.

This test only has to prove the call site passes ``run_immediately=True``
— WatchdogSupervisor's own scheduling semantics (src/apps/watchdog.py) are
that framework's concern, not this app's.

Run: .venv/aw/bin/python -m pytest tests/test_plugin_activate_watchdogs.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import plugin  # noqa: E402


class _FakeSecrets:
    def read(self, key):
        return None


class _FakeCommands:
    def install_system_cli(self, *args, **kwargs):
        pass


class _FakeRoutes:
    def register(self, app):
        pass


class _FakeWatchdog:
    def __init__(self):
        self.calls: dict[str, dict] = {}

    def register(self, task_id, fn, interval_s, run_immediately):
        self.calls[task_id] = {
            "fn": fn, "interval_s": interval_s, "run_immediately": run_immediately,
        }


class _FakeCtx:
    def __init__(self, *, capabilities: set[str]):
        self.package_dir = str(ROOT)
        self.config = {}
        self.secrets = _FakeSecrets()
        self.commands = _FakeCommands()
        self.routes = _FakeRoutes()
        self.watchdog = _FakeWatchdog()
        self.notify = lambda **kwargs: None
        self._capabilities = capabilities

    def has(self, capability):
        return capability in self._capabilities


def test_github_maintenance_watchdog_is_registered_run_immediately():
    """The whole fix: without this, every worker's MaintenanceWatchdog cache
    goes cold on every deploy and stays cold for up to 30 minutes (or until
    an inbound request eats a ~38s scan inline) — see the module docstring."""
    ctx = _FakeCtx(capabilities={"watchdog:tasks", "notifications:send"})
    app = plugin.GitAppPlugin()

    asyncio.run(app.activate(ctx))

    assert "github_maintenance" in ctx.watchdog.calls
    assert ctx.watchdog.calls["github_maintenance"]["run_immediately"] is True


def test_github_maintenance_watchdog_skipped_without_capability():
    """No watchdog:tasks capability — no registration at all, same as the
    other two watchdogs. Not the bug this card is about, just the other
    branch of the same ``if``."""
    ctx = _FakeCtx(capabilities=set())
    app = plugin.GitAppPlugin()

    asyncio.run(app.activate(ctx))

    assert "github_maintenance" not in ctx.watchdog.calls
