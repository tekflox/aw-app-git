"""Regression test for card 4185bf3b-9510-816d: the live ``GET
/github/maintenance?ping=true`` route hung/timed out under repeated real
calls, while ``?ping=false`` stayed reliably fast.

Root cause (confirmed by reading ``plugin.py``'s ``_ping_maintenance``):
``ctx.notify`` used to be called from *inside* the ``with
github_maintenance.locked_store(state_path):`` block guarding the ping
state file. A slow/blocking notify backend (e.g. a slow Telegram send,
or a slow Postgres write via ``NotificationManager.add_notification``)
held that cross-process ``fcntl.flock`` for the whole notify duration, so
every concurrent ``ping=true`` caller queued behind it until the HTTP
client's own ~10s timeout tripped.

This test reproduces the hang mechanism directly: a fake ``ctx.notify``
that blocks until released, and a probe that tries to acquire the SAME
lock file (non-blocking) while that fake notify is in flight. Pre-fix,
the probe fails (lock still held). Post-fix, the probe succeeds
immediately — the lock was already released before notify was ever
called.

Run: .venv/aw/bin/python -m pytest tests/test_maintenance_ping_lock.py
"""
from __future__ import annotations

import fcntl
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import github_maintenance, plugin  # noqa: E402


class _FakeSecrets:
    def read(self, key):
        return None

    def write(self, key, value):
        return {"key": key, "written": True}


class _BlockingNotifyCtx:
    """A ``ctx`` whose ``notify`` blocks until the test releases it, and
    whose ``has`` always reports the notifications capability as granted —
    the minimum needed to drive ``_ping_maintenance``'s real notify path."""

    def __init__(self, release_event: threading.Event, started_event: threading.Event):
        self.secrets = _FakeSecrets()
        self.config = {}
        self._release_event = release_event
        self._started_event = started_event
        self.calls: list[dict] = []

    def has(self, capability: str) -> bool:
        return True

    def notify(self, **kwargs):
        self.calls.append(kwargs)
        self._started_event.set()
        # Simulate a slow notify backend (network call, slow DB write, ...).
        # Bounded so a genuinely broken fix still fails the test instead of
        # hanging the suite forever.
        self._release_event.wait(timeout=5.0)
        return {"id": len(self.calls)}


def _failing_flow() -> dict:
    return {
        "repo": "tekflox/aw-app-git",
        "workflow": "ci.yml",
        "consecutive_failures": 3,
        "latest_run_url": "https://github.com/tekflox/aw-app-git/actions/runs/1",
    }


def _lock_is_free(lock_path: str) -> bool:
    """Non-blocking probe: can a FRESH file descriptor take the exclusive
    flock on ``lock_path`` right now? Mirrors exactly what a second
    concurrent ``ping=true`` call's own ``locked_store`` open+flock would
    attempt — fcntl.flock's lock is per open-file-description, so a second
    ``open()`` in the same process still conflicts with an fd already
    holding the lock, same as it would across two real worker threads."""
    with open(lock_path, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


def test_ping_true_releases_the_state_lock_before_calling_notify(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))

    release_event = threading.Event()
    started_event = threading.Event()
    ctx = _BlockingNotifyCtx(release_event, started_event)

    app = plugin.GitAppPlugin()
    api = app._build_routes(ctx)
    tc = TestClient(api)

    # Warm the maintenance cache directly (bypassing a real `gh`-shelling
    # poll) with one failing flow, so ping=true's notify path actually
    # fires instead of being a no-op on an empty cache.
    watchdog = app._maintenance_watchdog(ctx)
    watchdog._cache.update({
        "prs": [], "failing_flows": [_failing_flow()], "counts": {},
        "errors": [], "last_poll": time.time(), "error": None,
    })

    result: dict = {}

    def _call():
        resp = tc.get("/github/maintenance", params={"ping": "true"})
        result["status_code"] = resp.status_code
        result["json"] = resp.json()

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()

    assert started_event.wait(timeout=5.0), "notify was never called — ping=true didn't take the notify path"

    lock_path = github_maintenance.default_ping_state_path() + ".lock"
    try:
        # The regression: while the fake notify is blocked (simulating a
        # slow backend), the state-file lock must already be free — it was
        # only ever meant to guard load/decide/write, not the notify call.
        assert _lock_is_free(lock_path), (
            "the ping state lock is still held while notify() is in flight — "
            "notify is running inside the fcntl.flock critical section again"
        )
    finally:
        release_event.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "the /github/maintenance request never finished"
    assert result["status_code"] == 200
    assert result["json"]["pings"]["instant_sent"] == ["tekflox/aw-app-git:ci.yml"]
    # Brand-new state (no last_summary_at yet) also fires the daily summary —
    # the instant notify plus the summary notify, both issued after the lock
    # was released, is still just one blocking notify in flight at a time.
    assert len(ctx.calls) == 2
