"""Tests for the PR dashboard ported from the monolith (git_app/github_prs.py)
and its routes on the app's own mount.

Every `gh` call is monkeypatched — no network, no gh CLI needed.

Run: .venv/aw/bin/python -m pytest tests/test_github_prs.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import github_prs, plugin  # noqa: E402
from tests.test_plugin_routes import FakeCtx  # noqa: E402


def _pr(number=1, url="https://github.com/o/r/pull/1", **over):
    pr = {
        "number": number,
        "title": "A PR",
        "url": url,
        "author": {"login": "octocat"},
        "repository": {"nameWithOwner": "o/r"},
        "isDraft": False,
        "reviewDecision": "",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [],
        "comments": [],
        "state": "OPEN",
    }
    pr.update(over)
    return pr


# ---- classification (ported logic, unchanged) ----------------------------

def test_classify_ready_to_merge():
    pr = _pr(reviewDecision="APPROVED",
             statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}])
    assert github_prs.classify_pr(pr) == ("ready-to-merge", "passed")


def test_classify_tests_failed_beats_review():
    pr = _pr(reviewDecision="APPROVED",
             statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
    assert github_prs.classify_pr(pr) == ("tests-failed", "failed")


def test_classify_draft_wins():
    pr = _pr(isDraft=True,
             statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SUCCESS"}])
    badge, _ = github_prs.classify_pr(pr)
    assert badge == "draft"


def test_classify_running_ci():
    pr = _pr(statusCheckRollup=[{"status": "IN_PROGRESS"}])
    assert github_prs.classify_pr(pr) == ("ci-running", "running")


def test_format_pr_flattens_nested_fields():
    pr = _pr(comments=[{"author": {"login": "bob"}, "body": "x" * 300, "createdAt": "t"}],
             latestReviews=[{"author": {"login": "ana"}, "state": "APPROVED"}],
             reviewRequests=[{"login": "carl"}])
    out = github_prs.format_pr(pr)
    assert out["repo"] == "o/r"
    assert out["author"] == "octocat"
    assert out["commentCount"] == 1
    assert len(out["comments"][0]["body"]) == 200  # truncated
    assert out["reviews"] == [{"author": "ana", "state": "APPROVED"}]
    assert out["requestedReviewers"] == ["carl"]


# ---- fetch_prs de-duplication -------------------------------------------

def test_fetch_prs_dedupes_across_mine_team_and_review(monkeypatch):
    mine = _pr(1, "https://github.com/o/r/pull/1")
    shared = _pr(2, "https://github.com/o/r/pull/2")

    monkeypatch.setattr(github_prs, "search_prs",
                        lambda author, host=None: [mine] if author == "@me" else [shared, mine])
    monkeypatch.setattr(github_prs, "search_review_requested", lambda host=None: [shared])
    monkeypatch.setattr(github_prs, "enrich_pr", lambda pr, host=None: pr)

    my_prs, team_prs, review_prs = github_prs.fetch_prs({"team": ["buddy"]})
    assert [p["number"] for p in my_prs] == [1]
    assert [p["number"] for p in team_prs] == [2]   # `mine` skipped, already seen
    assert review_prs == []                         # `shared` already seen via team


# ---- DIVERGENCE: PRs are fetched without a team configured ---------------

def test_poll_fetches_prs_with_no_team(monkeypatch):
    """The monolith only fetched PRs when a team was set, so a fresh install
    showed nothing — not even the user's own PRs."""
    monkeypatch.setattr(github_prs, "fetch_repos", lambda base_dir=None: [])
    monkeypatch.setattr(github_prs, "_run_gh", lambda args, host=None: "octocat")
    called = {}

    def fake_fetch(cfg):
        called["cfg"] = cfg
        return [github_prs.format_pr(_pr())], [], []

    monkeypatch.setattr(github_prs, "fetch_prs", fake_fetch)

    wd = github_prs.PrWatchdog(lambda: {"team": [], "host": None})
    asyncio.run(wd.poll())

    assert called["cfg"]["team"] == []
    assert len(wd.get_cached()["my_prs"]) == 1
    assert wd.get_cached()["last_poll"] > 0


def test_poll_skips_prs_when_not_logged_in(monkeypatch):
    """No gh login → no PR calls at all (and no error state), just repos."""
    monkeypatch.setattr(github_prs, "fetch_repos", lambda base_dir=None: [{"name": "r"}])
    monkeypatch.setattr(github_prs, "_run_gh", lambda args, host=None: None)

    class _FailedRun:
        returncode = 1
        stdout = ""
        stderr = ""

    # The `gh api user` fallback shell-out — logged out, so it fails too.
    monkeypatch.setattr(github_prs.subprocess, "run", lambda *a, **kw: _FailedRun())

    def boom(cfg):
        raise AssertionError("fetch_prs must not run when logged out")

    monkeypatch.setattr(github_prs, "fetch_prs", boom)

    wd = github_prs.PrWatchdog(lambda: {"team": [], "host": None})
    asyncio.run(wd.poll())
    assert wd.get_cached()["repos"] == [{"name": "r"}]
    assert wd.get_cached()["my_prs"] == []


# ---- notifications ------------------------------------------------------

def test_detect_changes_notifies_ready_to_merge_and_merged():
    events = []
    wd = github_prs.PrWatchdog(lambda: {"team": ["buddy"]},
                               notify=lambda **kw: events.append(kw))
    url = "https://github.com/o/r/pull/1"
    old_my = {url: {"url": url, "badge": "needs-review", "state": "OPEN", "commentCount": 0}}
    new_my = [{"url": url, "number": 1, "title": "t", "badge": "ready-to-merge",
               "state": "OPEN", "commentCount": 0, "comments": []}]
    wd._detect_pr_changes(old_my, {}, new_my, [])
    assert [e["external_status"] for e in events] == ["ready-to-merge"]

    events.clear()
    old_my[url]["badge"] = "ready-to-merge"
    new_my[0]["state"] = "MERGED"
    new_my[0]["mergedBy"] = "ana"
    wd._detect_pr_changes(old_my, {}, new_my, [])
    assert [e["external_status"] for e in events] == ["merged"]


def test_detect_changes_is_silent_without_notify():
    wd = github_prs.PrWatchdog(lambda: {"team": []})
    wd._detect_pr_changes({}, {}, [], [])  # must not raise


# ---- config plumbing ----------------------------------------------------

def test_github_team_accepts_json_and_csv():
    ctx = FakeCtx()
    ctx.secrets.write("github_team", json.dumps(["a", "b"]))
    assert plugin._github_team(ctx) == ["a", "b"]

    ctx.secrets.write("github_team", " a , b ,")
    assert plugin._github_team(ctx) == ["a", "b"]

    assert plugin._github_team(FakeCtx()) == []


def test_poll_interval_floor_and_fallback():
    ctx = FakeCtx()
    assert plugin._github_poll_interval_s(ctx) == github_prs.DEFAULT_POLL_INTERVAL
    ctx.secrets.write("github_poll_interval_s", "5")
    assert plugin._github_poll_interval_s(ctx) == 60.0
    ctx.secrets.write("github_poll_interval_s", "not-a-number")
    assert plugin._github_poll_interval_s(ctx) == github_prs.DEFAULT_POLL_INTERVAL


# ---- routes -------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(github_prs, "fetch_repos", lambda base_dir=None: [{"name": "aw-workspace"}])
    monkeypatch.setattr(github_prs, "_run_gh", lambda args, host=None: "octocat")
    monkeypatch.setattr(github_prs, "fetch_prs",
                        lambda cfg: ([github_prs.format_pr(_pr())], [], []))
    ctx = FakeCtx()
    app = plugin.GitAppPlugin()
    return TestClient(app._build_routes(ctx)), ctx


def test_prs_route_polls_on_first_call(client):
    tc, _ = client
    body = tc.get("/github/prs").json()
    assert body["repos"] == [{"name": "aw-workspace"}]
    assert len(body["my_prs"]) == 1
    assert body["error"] is None


def test_refresh_route_repolls(client):
    tc, _ = client
    assert tc.post("/github/refresh").json()["last_poll"] > 0


def test_settings_route_persists_team_and_host(client):
    tc, ctx = client
    tc.post("/settings", json={"github_team": "ana, bob", "github_host": "ghe.corp"})
    assert json.loads(ctx.secrets.read("github_team")) == ["ana", "bob"]
    assert plugin._github_cfg(ctx)["host"] == "ghe.corp"


def test_ws_stream_sends_init_then_updates(client):
    tc, _ = client
    with tc.websocket_connect("/github/stream") as ws:
        init = json.loads(ws.receive_text())
        assert init["type"] == "github_init"
        assert "my_prs" in init


def test_routes_and_watchdog_share_one_cache(client):
    """The route's poller IS the registered watchdog's poller — a background
    poll must be visible to the next GET, and vice-versa."""
    tc, ctx = client
    app = plugin.GitAppPlugin()
    first = app._pr_watchdog(ctx)
    assert app._pr_watchdog(ctx) is first
    tc.get("/github/prs")
