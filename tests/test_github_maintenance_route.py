"""End-to-end tests for GET /github/maintenance (card 4185bf3b-9510-81c5,
slice 2 of the GitHub Maintenance Plan) through a real FastAPI TestClient.

The route is a thin wrapper over MaintenanceWatchdog's cache (slice 1) — no
scan logic is exercised here; the cache is poked directly on the plugin's
``_maintenance`` instance. The ping-gate state store is always a tmp_path
file, never the real AW_WORKSPACE_HOME location (monkeypatched via
``default_ping_state_path``).

Run: .venv/aw/bin/python -m pytest tests/test_github_maintenance_route.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import github_maintenance, plugin  # noqa: E402
from tests.test_plugin_routes import FakeCtx  # noqa: E402


def _flow(repo="o/r", workflow="ci", **over):
    flow = {"repo": repo, "workflow": workflow, "default_branch": "main",
            "latest_run_url": f"https://github.com/{repo}/actions/runs/1",
            "conclusion": "failure", "consecutive_failures": 1}
    flow.update(over)
    return flow


def _pr(repo="o/r", number=1, **over):
    pr = {"repo": repo, "number": number, "url": f"https://github.com/{repo}/pull/{number}",
          "title": "fix", "author": "octocat", "badge": "tests-failed",
          "updated_at": "2026-11-14T00:00:00Z", "age_hours": 1.0,
          "attention_reason": "badge:tests-failed"}
    pr.update(over)
    return pr


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Builds the real routes with a fake ctx, points the ping-gate state
    store at tmp_path, and hands back (TestClient, app, ctx, notify_calls)."""
    monkeypatch.setattr(github_maintenance, "default_ping_state_path",
                        lambda: str(tmp_path / "state.json"))
    ctx = FakeCtx()
    notify_calls = []
    ctx.has = lambda cap: True
    ctx.notify = lambda **kw: notify_calls.append(kw) or {"ok": True}
    app = plugin.GitAppPlugin()
    api = app._build_routes(ctx)
    return TestClient(api), app, ctx, notify_calls


def _warm(app, *, prs=None, failing_flows=None, scanned_repos=5, truncated=False, error=None,
         mode="all", watched_repos=5):
    """Directly set the lazily-created MaintenanceWatchdog's cache — the
    cache shape slice 1 (PR#6) already produces, so this route never needs
    to run a real scan to be tested."""
    cache = app._maintenance.get_cached()
    cache.update({
        "prs": prs or [], "failing_flows": failing_flows or [],
        "counts": {"scanned_repos": scanned_repos, "truncated": truncated,
                  "mode": mode, "watched_repos": watched_repos},
        "errors": [], "last_poll": 123.0, "error": error,
    })
    app._maintenance._cache = cache


# ---- response shape / has_work -----------------------------------------

def test_has_work_true_when_a_pr_needs_attention(setup):
    tc, app, _, _ = setup
    _warm(app, prs=[_pr()])
    resp = tc.get("/github/maintenance")
    body = resp.json()
    assert resp.status_code == 200
    assert body["has_work"] is True
    assert body["prs"] == [_pr()]
    assert body["failing_flows"] == []
    assert body["counts"] == {"prs": 1, "failing_flows": 0, "scanned_repos": 5, "truncated": False,
                              "mode": "all", "watched_repos": 5}


def test_has_work_true_when_a_flow_is_failing(setup):
    tc, app, _, _ = setup
    _warm(app, failing_flows=[_flow()])
    body = tc.get("/github/maintenance").json()
    assert body["has_work"] is True
    assert body["failing_flows"] == [_flow()]
    assert body["counts"]["failing_flows"] == 1


def test_has_work_false_on_a_warm_but_clean_cache(setup):
    tc, app, _, _ = setup
    _warm(app)  # last_poll set, nothing attention-worthy
    body = tc.get("/github/maintenance").json()
    assert body["has_work"] is False
    assert body["prs"] == [] and body["failing_flows"] == []
    assert body["counts"] == {"prs": 0, "failing_flows": 0, "scanned_repos": 5, "truncated": False,
                              "mode": "all", "watched_repos": 5}


def test_limit_truncates_the_lists_but_not_the_counts(setup):
    tc, app, _, _ = setup
    many_prs = [_pr(number=n) for n in range(1, 6)]
    _warm(app, prs=many_prs)
    body = tc.get("/github/maintenance?limit=2").json()
    assert len(body["prs"]) == 2
    assert body["counts"]["prs"] == 5


# ---- cold-cache fallback (the healthy-path-only trap) -------------------

def test_cold_cache_polls_inline_instead_of_answering_false_silently(setup, monkeypatch):
    tc, app, _, _ = setup
    assert app._maintenance.get_cached()["last_poll"] == 0  # genuinely cold

    async def fake_poll(now=None):
        app._maintenance._cache.update({
            "prs": [], "failing_flows": [_flow()],
            "counts": {"scanned_repos": 3, "truncated": False},
            "errors": [], "last_poll": 999.0, "error": None,
        })

    monkeypatch.setattr(app._maintenance, "poll", fake_poll)
    body = tc.get("/github/maintenance").json()
    assert body["has_work"] is True  # NOT the false-on-cold-silently trap
    assert body["failing_flows"] == [_flow()]


def test_cold_cache_slower_than_the_budget_answers_warming_up_not_a_hang(setup, monkeypatch):
    """Card 4185bf3b-9510-8132: a cold scan that outruns the client's own
    timeout must never be allowed to just hang here until it finishes — the
    route answers truthfully and fast instead, well inside the AP-MT dag
    Workflow's observed ~10s connection timeout. (The scan continuing in the
    background afterward — asyncio.shield's whole point — is verified
    directly on MaintenanceWatchdog in test_github_maintenance.py: TestClient
    tears down its own event loop at the end of each call, so a task
    scheduled during this request isn't observable surviving past it here.)
    """
    tc, app, _, _ = setup
    monkeypatch.setattr(plugin, "MAINTENANCE_COLD_POLL_BUDGET_S", 0.05)

    async def slow_poll(now=None):
        await asyncio.sleep(0.3)
        app._maintenance._cache.update({
            "prs": [], "failing_flows": [_flow()],
            "counts": {"scanned_repos": 3, "truncated": False},
            "errors": [], "last_poll": 999.0, "error": None,
        })

    monkeypatch.setattr(app._maintenance, "poll", slow_poll)

    resp = tc.get("/github/maintenance")
    assert resp.status_code == 503
    body = resp.json()
    assert body["warming_up"] is True
    assert body["has_work"] is False


def test_cold_cache_two_concurrent_requests_share_one_scan(setup, monkeypatch):
    """Card 4185bf3b-9510-8132: before poll_task()'s single-flight guard,
    two requests landing on the same cold worker each kicked off their own
    redundant scan. Drive both through the real single-flight entry point
    directly (TestClient itself is single-threaded, so this exercises
    MaintenanceWatchdog.poll_task() the way two concurrent event-loop
    callers would, without needing real request concurrency)."""
    tc, app, _, _ = setup
    scans = []

    async def counting_poll(now=None):
        scans.append(1)
        app._maintenance._cache.update({
            "prs": [], "failing_flows": [], "counts": {}, "errors": [],
            "last_poll": 999.0, "error": None,
        })

    monkeypatch.setattr(app._maintenance, "poll", counting_poll)

    async def two_concurrent_callers():
        task_a = app._maintenance.poll_task()
        task_b = app._maintenance.poll_task()
        assert task_a is task_b
        await task_a
        await task_b

    asyncio.run(two_concurrent_callers())
    assert scans == [1]


def test_warm_cache_does_not_poll_again(setup, monkeypatch):
    tc, app, _, _ = setup
    _warm(app)  # already warm
    called = []
    monkeypatch.setattr(app._maintenance, "poll", lambda now=None: called.append(1))
    tc.get("/github/maintenance")
    assert called == []


# ---- broken scan layer → non-2xx -----------------------------------------

def test_a_broken_scan_is_a_502_not_a_quiet_has_work_false(setup):
    tc, app, _, _ = setup
    _warm(app, error="discover_github_repos blew up")
    resp = tc.get("/github/maintenance")
    assert resp.status_code == 502
    assert resp.json()["has_work"] is False
    assert "blew up" in resp.json()["error"]


# ---- ping=true / ping=false invariant ------------------------------------

def test_plain_get_never_notifies_even_with_failing_flows(setup):
    tc, app, _, notify_calls = setup
    _warm(app, failing_flows=[_flow()])
    tc.get("/github/maintenance")
    assert notify_calls == []

    tc.get("/github/maintenance?ping=false")
    assert notify_calls == []


def test_ping_true_fires_instant_notify_on_a_new_failure(setup):
    tc, app, _, notify_calls = setup
    _warm(app, failing_flows=[_flow()])
    body = tc.get("/github/maintenance?ping=true").json()

    # First-ever tick with a failure: the instant SET AND the first daily
    # summary both fire — two independent notify() calls.
    assert len(notify_calls) == 2
    levels = sorted(c["level"] for c in notify_calls)
    assert levels == ["error", "warning"]
    assert "o/r:ci" in body["pings"]["instant_sent"]
    assert body["pings"]["daily_summary_sent"] is True


def test_ping_true_does_not_renotify_the_same_ongoing_failure(setup):
    tc, app, _, notify_calls = setup
    _warm(app, failing_flows=[_flow()])
    tc.get("/github/maintenance?ping=true")
    assert len(notify_calls) == 2  # instant SET + first-ever daily summary

    notify_calls.clear()
    body = tc.get("/github/maintenance?ping=true").json()
    assert body["pings"]["instant_sent"] == []
    assert notify_calls == []  # no instant (not new), no summary (within 24h)


def test_ping_true_clears_on_green_then_renotifies_on_refailure(setup, monkeypatch):
    """The exact three-leg lifecycle the card requires: SET on failure,
    CLEAR on green with no notification, re-SET (and re-notify) on the next
    failure of the SAME key."""
    tc, app, _, notify_calls = setup
    times = iter([1000.0, 1060.0, 1120.0])
    monkeypatch.setattr(plugin, "_now_s", lambda: next(times))

    _warm(app, failing_flows=[_flow()])
    tc.get("/github/maintenance?ping=true")
    assert len(notify_calls) == 2  # instant SET + first-ever daily summary

    notify_calls.clear()
    _warm(app, failing_flows=[])  # went green
    body = tc.get("/github/maintenance?ping=true").json()
    assert notify_calls == []  # CLEAR — no notification
    assert body["pings"]["instant_sent"] == []

    notify_calls.clear()
    _warm(app, failing_flows=[_flow()])  # fails again
    body = tc.get("/github/maintenance?ping=true").json()
    assert len(notify_calls) == 1  # re-SET — notifies again (no summary yet, <24h)
    assert "o/r:ci" in body["pings"]["instant_sent"]


def test_ping_true_daily_summary_fires_at_most_once_per_24h(setup, monkeypatch):
    tc, app, _, notify_calls = setup
    times = iter([1000.0, 1000.0 + 3600.0, 1000.0 + 25 * 3600.0])
    monkeypatch.setattr(plugin, "_now_s", lambda: next(times))

    _warm(app, failing_flows=[_flow()])
    body = tc.get("/github/maintenance?ping=true").json()
    assert body["pings"]["daily_summary_sent"] is True  # tick 1: first-ever

    body = tc.get("/github/maintenance?ping=true").json()
    assert body["pings"]["daily_summary_sent"] is False  # tick 2: +1h, too soon

    body = tc.get("/github/maintenance?ping=true").json()
    assert body["pings"]["daily_summary_sent"] is True  # tick 3: +25h, due again


# ---- code-review round 2 regression coverage -----------------------------
# A FakeCtx.notify is a bare list.append with no dedup semantics — these
# tests assert directly on what the real NotificationManager.add_notification
# dedups on (source, external_id, external_status), since the route's own
# test double can't reproduce that bug the way the real facade did.

def test_instant_notify_external_id_is_unique_per_set_occurrence_not_per_flow(setup, monkeypatch):
    """The real NotificationDB.has_notification dedups forever on the bare
    (source, external_id, external_status) triple (its only purge is a
    one-time 7-day cleanup at process boot) — a constant external_id per
    flow key would silently swallow every notify after the first SET,
    defeating the whole re-notify-on-refailure design this route ships."""
    tc, app, _, notify_calls = setup
    times = iter([1000.0, 1060.0, 1120.0])
    monkeypatch.setattr(plugin, "_now_s", lambda: next(times))

    _warm(app, failing_flows=[_flow()])
    tc.get("/github/maintenance?ping=true")  # SET #1
    _warm(app, failing_flows=[])              # CLEAR
    tc.get("/github/maintenance?ping=true")
    _warm(app, failing_flows=[_flow()])       # SET #2 (same flow key)
    tc.get("/github/maintenance?ping=true")

    instant_ids = [c["external_id"] for c in notify_calls if c.get("external_status") == "failing"]
    assert len(instant_ids) == 2
    assert instant_ids[0] != instant_ids[1]  # same flow, different occurrence → different id


def test_a_notify_failure_is_swallowed_not_a_500_and_not_reported_as_sent(setup, monkeypatch):
    tc, app, ctx, _ = setup
    _warm(app, failing_flows=[_flow()])

    def boom(**kw):
        raise RuntimeError("notification backend is down")

    ctx.notify = boom
    resp = tc.get("/github/maintenance?ping=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_work"] is True  # the primary read still succeeds
    # The state write already landed (prevents a double-decide race with any
    # concurrent caller), but a notify that raised must not be reported as
    # delivered — see the rollback tests below for the retry-on-failure half
    # of this contract (card 4185bf3b-9510-816d).
    assert body["pings"]["instant_sent"] == []
    assert body["pings"]["daily_summary_sent"] is False


def test_an_instant_notify_failure_rolls_back_so_the_next_tick_retries(setup, monkeypatch):
    """Card 4185bf3b-9510-816d, round 1 code-review finding: notify now runs
    AFTER the state-file lock is released (to stop a slow notify backend
    from serializing every concurrent ping=true caller behind the same
    fcntl.flock — see the lock-release test below). Committing the decided
    state BEFORE notify is attempted would otherwise mean a notify that
    *raises* gets permanently treated as delivered, with no retry. This
    proves the rollback: a failed instant notify untracks that flow so the
    NEXT tick decides it as new again instead of silently dropping it."""
    tc, app, ctx, notify_calls = setup
    _warm(app, failing_flows=[_flow()])

    def boom(**kw):
        raise RuntimeError("notification backend is down")

    ctx.notify = boom
    body = tc.get("/github/maintenance?ping=true").json()
    assert body["pings"]["instant_sent"] == []

    # Backend recovered — the same still-failing flow is retried as new.
    notify_calls.clear()
    ctx.notify = lambda **kw: notify_calls.append(kw) or {"ok": True}
    body2 = tc.get("/github/maintenance?ping=true").json()
    assert "o/r:ci" in body2["pings"]["instant_sent"]
    assert any(c.get("external_status") == "failing" for c in notify_calls)


def test_a_daily_summary_notify_failure_rolls_back_so_it_retries(setup, monkeypatch):
    """Same rollback contract as the instant-notify test above, for the
    OTHER notify this route can send: a failed daily-summary notify must
    not stamp ``last_summary_at``, or the retry would wait out the full 24h
    for a summary that was never actually delivered."""
    tc, app, ctx, notify_calls = setup
    _warm(app, failing_flows=[_flow()])

    def selective_boom(**kw):
        if kw.get("external_status") == "summary":
            raise RuntimeError("notification backend is down")
        notify_calls.append(kw)
        return {"ok": True}

    ctx.notify = selective_boom
    body = tc.get("/github/maintenance?ping=true").json()
    assert body["pings"]["instant_sent"] == ["o/r:ci"]  # instant delivered fine
    assert body["pings"]["daily_summary_sent"] is False  # summary failed

    # Same still-failing flow, backend recovered: instant doesn't re-fire
    # (already tracked), but the summary retries since last_summary_at was
    # rolled back instead of stamped on a delivery that never happened.
    notify_calls.clear()
    ctx.notify = lambda **kw: notify_calls.append(kw) or {"ok": True}
    body2 = tc.get("/github/maintenance?ping=true").json()
    assert body2["pings"]["instant_sent"] == []
    assert body2["pings"]["daily_summary_sent"] is True


def test_ping_true_releases_the_state_lock_before_calling_notify(setup):
    """Root regression for card 4185bf3b-9510-816d: the live route hung
    under repeated real calls while ping=false stayed fast. Confirmed by
    reading the code — notify() used to run INSIDE the same fcntl.flock
    guarding the ping state file, so a slow/blocking notify backend (a
    synchronous Postgres write, in the real NotificationManager) serialized
    every concurrent ping=true caller behind it until the HTTP client's own
    timeout tripped. Reproduces the hang mechanism directly: a fake notify
    that blocks until released, and a non-blocking probe on the SAME lock
    file while that notify is in flight."""
    import fcntl
    import threading

    tc, app, ctx, notify_calls = setup
    _warm(app, failing_flows=[_flow()])

    started = threading.Event()
    release = threading.Event()

    def blocking_notify(**kw):
        notify_calls.append(kw)
        started.set()
        release.wait(timeout=5.0)
        return {"ok": True}

    ctx.notify = blocking_notify
    result: dict = {}

    def _call():
        resp = tc.get("/github/maintenance?ping=true")
        result["status_code"] = resp.status_code

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=5.0), "notify was never called — ping=true didn't take the notify path"

        lock_path = github_maintenance.default_ping_state_path() + ".lock"
        with open(lock_path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_free = False
            else:
                lock_free = True
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        assert lock_free, (
            "the ping state lock is still held while notify() is in flight — "
            "notify is running inside the fcntl.flock critical section again"
        )
    finally:
        release.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "the /github/maintenance request never finished"
    assert result["status_code"] == 200


def test_a_state_file_write_failure_is_swallowed_not_a_500(setup, monkeypatch):
    tc, app, _, _ = setup
    _warm(app, failing_flows=[_flow()])
    monkeypatch.setattr(github_maintenance, "save_surfaced_store",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    resp = tc.get("/github/maintenance?ping=true")
    assert resp.status_code == 200
    assert "pings" not in resp.json()


def test_limit_below_zero_is_rejected_not_silently_misread(setup):
    tc, _, _, _ = setup
    resp = tc.get("/github/maintenance?limit=-1")
    assert resp.status_code == 422


def test_locked_store_serializes_two_sequential_read_modify_writes(tmp_path):
    """Not a true multi-process race test — confirms the lock is actually
    acquirable/releasable across two back-to-back critical sections (a
    deadlocked or mis-released flock would hang or raise here) and that
    each section sees the OTHER's committed write."""
    path = str(tmp_path / "state.json")

    with github_maintenance.locked_store(path):
        state = github_maintenance.load_surfaced_store(path)
        state["seen"] = ["a"]
        github_maintenance.save_surfaced_store(path, state)

    with github_maintenance.locked_store(path):
        state = github_maintenance.load_surfaced_store(path)
        assert state["seen"] == ["a"]
        state["seen"].append("b")
        github_maintenance.save_surfaced_store(path, state)

    assert github_maintenance.load_surfaced_store(path)["seen"] == ["a", "b"]
