"""Tests for git_app/github_maintenance.py — the GitHub Maintenance Plan's
scan layer (owner-scoped PR attention sweep + default-branch
failing-workflow sweep + surfaced-stamp back-off).

Same split as test_pr_scan.py (its sibling in
aw-app-agents-platform-runners): pure decision logic plus a fake ``gh``
runner, no real subprocess/network calls. The surfaced-stamp store is
always touched via a pytest ``tmp_path``, never the real
``AW_WORKSPACE_HOME`` location.

Run: .venv/aw/bin/python -m pytest tests/test_github_maintenance.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from git_app import github_maintenance as gm  # noqa: E402
from git_app import github_prs  # noqa: E402

NOW = 1_800_000_000.0  # arbitrary fixed epoch — no wall-clock dependency


def _iso(hours_ago: float) -> str:
    ts = dt.datetime.fromtimestamp(NOW - hours_ago * 3600, tz=dt.timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


# ── remote parsing ───────────────────────────────────────────────────────

def test_parses_ssh_colon_form():
    assert gm.parse_github_remote("git@github.com:tekflox/aw-app-kb.git") == ("tekflox", "aw-app-kb")


def test_parses_ssh_url_form():
    assert gm.parse_github_remote("ssh://git@github.com/tekflox/aw-automation.git") == (
        "tekflox", "aw-automation")


def test_parses_https_form():
    assert gm.parse_github_remote("https://github.com/tekflox/aw-app-kb.git") == (
        "tekflox", "aw-app-kb")


def test_parses_https_form_with_embedded_credentials():
    assert gm.parse_github_remote("https://x-access-token:tok@github.com/tekflox/aw-app-kb.git") == (
        "tekflox", "aw-app-kb")


def test_rejects_a_non_github_remote():
    assert gm.parse_github_remote("https://gitlab.com/someone/somewhere.git") is None


def test_rejects_empty_or_none():
    assert gm.parse_github_remote("") is None
    assert gm.parse_github_remote(None) is None


# ── map_cloned_repos ─────────────────────────────────────────────────────

def test_map_cloned_repos_dedups_two_dirs_cloning_the_same_github_repo():
    result = gm.map_cloned_repos({
        "aw-app-kb": "git@github.com:tekflox/aw-app-kb.git",
        "aw-app-kb-copy": "https://github.com/tekflox/aw-app-kb.git",
    })
    assert len(result["repos"]) == 1
    assert result["owners"] == ["tekflox"]
    assert result["unmapped"] == []


def test_map_cloned_repos_reports_unmapped_dirs_never_drops_them():
    result = gm.map_cloned_repos({
        "has-remote": "git@github.com:tekflox/aw-app-kb.git",
        "no-remote": None,
        "non-github": "https://gitlab.com/x/y.git",
    })
    assert result["unmapped"] == ["no-remote", "non-github"]
    assert [r["path"] for r in result["repos"]] == ["has-remote"]


# ── discover_github_repos (root checkout first, then repos/*) ───────────

def _init_bare_git_dir(path: Path) -> None:
    (path / ".git").mkdir(parents=True)


def test_discover_github_repos_includes_root_checkout_first():
    """The entire point of this sweep vs. pr_scan.py's own blind spot: the
    root aw-workspace checkout must be in the universe, not just repos/*."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        root_name = tmp_path.name
        _init_bare_git_dir(tmp_path)
        _init_bare_git_dir(tmp_path / "repos" / "alpha")

        def fake_remote(path):
            if path.endswith("alpha"):
                return "git@github.com:tekflox/alpha.git"
            return "git@github.com:fredericowu/aw-workspace.git"

        result = gm.discover_github_repos(base_dir=str(tmp_path), git_remote_url=fake_remote)
        full_names = {f"{r['owner']}/{r['repo']}" for r in result["repos"]}
        assert "fredericowu/aw-workspace" in full_names
        assert "tekflox/alpha" in full_names
        assert result.get("error") is None


def test_discover_github_repos_treats_a_merely_missing_repos_dir_as_legitimately_empty(tmp_path):
    """discover_repos() itself guards with os.path.isdir() first — a
    workspace with no repos/ cloned yet (fresh install, root-only checkout)
    is a real, valid state, not a scan failure."""
    base_dir = tmp_path / "workspace-root"
    _init_bare_git_dir(base_dir)  # root is a repo, repos/ never created

    result = gm.discover_github_repos(
        base_dir=str(base_dir), git_remote_url=lambda p: "git@github.com:fredericowu/aw-workspace.git")
    assert result.get("error") is None
    assert any(r["owner"] == "fredericowu" for r in result["repos"])


def test_discover_github_repos_carries_an_unreadable_repos_dir_as_an_error_not_a_crash(tmp_path):
    """Code-review finding: discover_repos() does its own UNGUARDED
    os.listdir() once its isdir() check passes, so a repos/ dir that EXISTS
    but can't be listed (permission denied) used to raise PermissionError
    straight out of this function instead of landing in ``error`` — and
    scan_maintenance awaits this via asyncio.to_thread with nothing around
    it, so the whole scan (both sweeps) died, not just repo enumeration."""
    base_dir = tmp_path / "workspace-root"
    _init_bare_git_dir(base_dir)
    repos_dir = base_dir / "repos"
    repos_dir.mkdir()
    (repos_dir / "alpha" / ".git").mkdir(parents=True)
    repos_dir.chmod(0o000)
    try:
        result = gm.discover_github_repos(
            base_dir=str(base_dir),
            git_remote_url=lambda p: "git@github.com:fredericowu/aw-workspace.git")
    finally:
        repos_dir.chmod(0o755)  # restore before tmp_path teardown tries to clean up

    assert result["error"] is not None
    assert str(repos_dir) in result["error"]
    # the root repo is still reported — an error on repos/ must not drop it
    assert any(r["owner"] == "fredericowu" for r in result["repos"])


# ── filter_prs_to_cloned_repos ───────────────────────────────────────────

def test_filter_keeps_only_cloned_repos():
    prs = [
        {"number": 1, "repository": {"nameWithOwner": "tekflox/aw-app-kb"}},
        {"number": 2, "repository": {"nameWithOwner": "tekflox/some-other-repo"}},
    ]
    kept = gm.filter_prs_to_cloned_repos(prs, {"tekflox/aw-app-kb"})
    assert [p["number"] for p in kept] == [1]


# ── resolve_repo_scope — settings-page watchlist, card 4185bf3b-9510-8147 ─

def _discovered(*entries):
    """``entries`` are ``(owner, repo, local_path)`` tuples."""
    repos = [{"owner": o, "repo": r, "path": r, "local_path": p} for o, r, p in entries]
    owners = sorted({o for o, _, _ in entries})
    return {"repos": repos, "owners": owners, "unmapped": [], "scanned": len(entries)}


def test_resolve_scope_unset_mode_is_byte_identical_to_all():
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"),
                             ("fredericowu", "aw-workspace", "/x/aw-workspace"))
    unset = gm.resolve_repo_scope(None, discovered)
    explicit_all = gm.resolve_repo_scope({"mode": "all"}, discovered)
    assert unset == explicit_all
    assert unset["mode"] == "all"
    assert unset["owners"] == ["fredericowu", "tekflox"]
    assert unset["full_names"] == {"tekflox/aw-app-kb", "fredericowu/aw-workspace"}
    assert unset["repos"] == discovered["repos"]
    assert unset["error"] is None


def test_resolve_scope_unknown_mode_fails_open_to_all():
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"))
    resolved = gm.resolve_repo_scope({"mode": "everything"}, discovered)
    assert resolved["mode"] == "all"
    assert resolved["owners"] == ["tekflox"]


def test_resolve_scope_cloned_is_identical_to_all_today():
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"))
    all_scope = gm.resolve_repo_scope({"mode": "all"}, discovered)
    cloned_scope = gm.resolve_repo_scope({"mode": "cloned"}, discovered)
    assert cloned_scope["owners"] == all_scope["owners"]
    assert cloned_scope["full_names"] == all_scope["full_names"]
    assert cloned_scope["repos"] == all_scope["repos"]
    assert cloned_scope["mode"] == "cloned"  # label differs, coverage doesn't


def test_resolve_scope_list_mode_owners_come_from_the_list_not_the_clones():
    """The real rate-limit/junk win: a list containing only tekflox repos
    must stop querying fredericowu entirely, even though fredericowu has
    repos cloned in this workspace."""
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"),
                             ("fredericowu", "aw-workspace", "/x/aw-workspace"))
    resolved = gm.resolve_repo_scope({"mode": "list", "list": ["tekflox/aw-app-kb"]}, discovered)
    assert resolved["owners"] == ["tekflox"]
    assert resolved["full_names"] == {"tekflox/aw-app-kb"}
    assert resolved["error"] is None


def test_resolve_scope_list_mode_a_cloned_entry_keeps_its_local_path():
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"))
    resolved = gm.resolve_repo_scope({"mode": "list", "list": ["tekflox/aw-app-kb"]}, discovered)
    assert resolved["repos"] == [{"owner": "tekflox", "repo": "aw-app-kb",
                                 "path": "aw-app-kb", "local_path": "/x/aw-app-kb"}]


def test_resolve_scope_list_mode_a_non_cloned_entry_gets_local_path_none():
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"))
    resolved = gm.resolve_repo_scope(
        {"mode": "list", "list": ["tekflox/aw-app-kb", "someone-else/other-repo"]}, discovered)
    repos_by_name = {f"{r['owner']}/{r['repo']}": r for r in resolved["repos"]}
    assert repos_by_name["someone-else/other-repo"]["local_path"] is None
    assert resolved["owners"] == ["someone-else", "tekflox"]


def test_resolve_scope_list_mode_empty_list_is_an_in_band_error_not_a_crash():
    discovered = _discovered(("tekflox", "aw-app-kb", "/x/aw-app-kb"))
    resolved = gm.resolve_repo_scope({"mode": "list", "list": []}, discovered)
    assert resolved["mode"] == "list"
    assert resolved["repos"] == []
    assert resolved["owners"] == []
    assert resolved["error"] == "watchlist mode with an empty list"


def test_search_sorts_oldest_updated_first_with_a_high_limit():
    captured = {}

    async def runner(args):
        captured["args"] = args
        return 0, "[]", ""

    asyncio.run(gm.search_open_prs_for_owner("tekflox", runner))
    args = captured["args"]
    assert args[args.index("--sort") + 1] == "updated"
    assert args[args.index("--order") + 1] == "asc"
    assert int(args[args.index("--limit") + 1]) >= 1000


# ── surfaced_backoff_state — the 48h-ceiling sibling of pr_scan's backoff ─

def test_backoff_eligible_with_no_stamp_at_all():
    assert gm.surfaced_backoff_state(_iso(1), None, NOW) == "eligible"


def test_backoff_backed_off_when_pr_has_not_moved_and_within_ceiling():
    stamp = {"updated_at": _iso(1), "surfaced_at": NOW - 3600}
    assert gm.surfaced_backoff_state(_iso(1), stamp, NOW) == "backed_off"


def test_backoff_re_arms_when_current_updated_at_is_newer_than_the_stamp():
    stamp = {"updated_at": _iso(5), "surfaced_at": NOW - 3600}
    assert gm.surfaced_backoff_state(_iso(1), stamp, NOW) == "eligible"


def test_backoff_re_arms_once_the_48h_ceiling_passes_even_with_no_new_activity():
    stamp = {"updated_at": _iso(1), "surfaced_at": NOW - (49 * 3600)}
    assert gm.surfaced_backoff_state(_iso(1), stamp, NOW) == "eligible"


def test_backoff_still_backed_off_just_under_the_ceiling():
    stamp = {"updated_at": _iso(1), "surfaced_at": NOW - (47 * 3600)}
    assert gm.surfaced_backoff_state(_iso(1), stamp, NOW) == "backed_off"


def test_backoff_fails_open_on_an_unparseable_stamped_timestamp():
    stamp = {"updated_at": "not-a-date", "surfaced_at": NOW}
    assert gm.surfaced_backoff_state(_iso(1), stamp, NOW) == "eligible"


def test_backoff_fails_open_on_an_unparseable_current_timestamp():
    stamp = {"updated_at": _iso(1), "surfaced_at": NOW}
    assert gm.surfaced_backoff_state("not-a-date", stamp, NOW) == "eligible"


def test_backoff_fails_open_on_a_stamp_missing_surfaced_at():
    stamp = {"updated_at": _iso(1)}
    assert gm.surfaced_backoff_state(_iso(1), stamp, NOW) == "eligible"


# ── surfaced store: round-trip / prune / atomicity / fail-open ──────────

def test_store_round_trips(tmp_path):
    path = str(tmp_path / "surfaced.json")
    store = {"tekflox/aw-app-kb#7": {"surfaced_at": NOW, "updated_at": _iso(10)}}
    gm.save_surfaced_store(path, store)
    assert gm.load_surfaced_store(path) == store


def test_store_write_is_atomic_no_tmp_file_left_behind(tmp_path):
    path = str(tmp_path / "surfaced.json")
    gm.save_surfaced_store(path, {"k": {"v": 1}})
    leftovers = [p for p in tmp_path.iterdir() if p.name != "surfaced.json"]
    assert leftovers == []


def test_store_load_fails_open_on_a_missing_file(tmp_path):
    assert gm.load_surfaced_store(str(tmp_path / "nope.json")) == {}


def test_store_load_fails_open_on_a_corrupt_file(tmp_path):
    path = tmp_path / "surfaced.json"
    path.write_text("{not valid json")
    assert gm.load_surfaced_store(str(path)) == {}


def test_prune_only_drops_keys_whose_owner_was_actually_searched():
    store = {"a/x#1": {"updated_at": "t"}, "b/y#2": {"updated_at": "t"}}
    pruned = gm.prune_surfaced_store(store, set(), searched_owners={"a"})
    assert pruned == {"b/y#2": {"updated_at": "t"}}


# ── scan_pr_attention — the full PR sweep, fake gh_runner, no network ───

def _fake_runner(search_responses=None, view_responses=None, search_errors=None, api_responses=None):
    search_responses = search_responses or {}
    view_responses = view_responses or {}
    search_errors = search_errors or {}
    api_responses = api_responses or {}

    async def runner(args):
        if args[:2] == ["search", "prs"]:
            owner = args[args.index("--owner") + 1]
            if owner in search_errors:
                return 1, "", search_errors[owner]
            return 0, json.dumps(search_responses.get(owner, [])), ""
        if args[:2] == ["pr", "view"]:
            url = args[2]
            return 0, json.dumps(view_responses.get(url, {})), ""
        if args[0] == "api":
            default = {"workflows": []} if "/actions/workflows?" in args[1] else {"workflow_runs": []}
            return 0, json.dumps(api_responses.get(args[1], default)), ""
        raise AssertionError(f"unexpected gh invocation: {args}")

    return runner


def _search_pr(number, *, owner="tekflox", repo="aw-app-kb", updated_hours_ago=10,
              is_draft=False, title="some PR"):
    return {"number": number, "repository": {"name": repo, "nameWithOwner": f"{owner}/{repo}"},
            "title": title, "url": f"https://github.com/{owner}/{repo}/pull/{number}",
            "updatedAt": _iso(updated_hours_ago), "isDraft": is_draft,
            "createdAt": _iso(updated_hours_ago + 1), "author": {"login": "fredericowu"}}


def _sweep(search_responses, view_responses=None, search_errors=None, store=None, **kwargs):
    runner = _fake_runner(search_responses=search_responses, view_responses=view_responses,
                          search_errors=search_errors)
    store = store if store is not None else {}
    result = asyncio.run(gm.scan_pr_attention(
        now=NOW, gh_runner=runner, cloned_full_names={"tekflox/aw-app-kb", "someone-else/other-repo"},
        owners=list(search_responses), store=store, **kwargs))
    return result, store


def test_tests_failed_badge_and_stale_pr_both_surface_with_reasons():
    pr = _search_pr(7, updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    result, store = _sweep({"tekflox": [pr]}, view_responses={pr["url"]: view})
    assert len(result["attention"]) == 1
    item = result["attention"][0]
    assert item["badge"] == "tests-failed"
    assert "badge:tests-failed" in item["attention_reason"]
    assert "stalled" in item["attention_reason"]
    assert "tekflox/aw-app-kb#7" in store


def test_fresh_pr_with_no_ci_configured_never_surfaces_but_is_still_probed():
    """No CI rollup + an approved review classifies as badge "open" (not
    "ready-to-merge" — that needs a passed CI run too), and the PR is fresh
    — neither leg of the attention filter fires."""
    pr = _search_pr(8, updated_hours_ago=1)
    view = {"statusCheckRollup": [], "reviewDecision": "APPROVED", "mergeable": "MERGEABLE",
           "isDraft": False, "updatedAt": pr["updatedAt"]}
    result, _ = _sweep({"tekflox": [pr]}, view_responses={pr["url"]: view})
    assert result["attention"] == []
    assert result["counts"]["probed"] == 1


def test_ready_to_merge_badge_surfaces_even_though_fresh():
    """A PR green+approved+mergeable IS attention-worthy — it needs someone
    to actually merge it, regardless of staleness."""
    pr = _search_pr(81, updated_hours_ago=1)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
           "reviewDecision": "APPROVED", "mergeable": "MERGEABLE", "isDraft": False,
           "updatedAt": pr["updatedAt"]}
    result, _ = _sweep({"tekflox": [pr]}, view_responses={pr["url"]: view})
    assert result["attention"][0]["badge"] == "ready-to-merge"
    assert "stalled" not in result["attention"][0]["attention_reason"]


def test_a_draft_pr_is_never_probed_or_surfaced():
    pr = _search_pr(9, updated_hours_ago=72, is_draft=True)
    result, _ = _sweep({"tekflox": [pr]})
    assert result["attention"] == []
    assert result["counts"]["probed"] == 0


def test_needs_review_badge_surfaces():
    pr = _search_pr(10, updated_hours_ago=1)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    result, _ = _sweep({"tekflox": [pr]}, view_responses={pr["url"]: view})
    assert result["attention"][0]["badge"] == "needs-review"


def test_already_backed_off_pr_is_not_reprobed():
    pr = _search_pr(11, updated_hours_ago=30)
    store = {"tekflox/aw-app-kb#11": {"surfaced_at": NOW - 3600, "updated_at": pr["updatedAt"]}}
    result, _ = _sweep({"tekflox": [pr]}, view_responses={}, store=store)
    assert result["counts"]["probed"] == 0
    assert result["attention"] == []


def test_new_activity_since_the_stamp_re_arms_eligibility():
    pr = _search_pr(12, updated_hours_ago=1)
    older_stamp_time = _iso(100)
    store = {"tekflox/aw-app-kb#12": {"surfaced_at": NOW - 3600, "updated_at": older_stamp_time}}
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    result, _ = _sweep({"tekflox": [pr]}, view_responses={pr["url"]: view}, store=store)
    assert result["counts"]["probed"] == 1
    assert len(result["attention"]) == 1


def test_per_owner_search_failure_is_isolated_and_other_owners_continue():
    pr = _search_pr(17, owner="someone-else", repo="other-repo", updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    result, _ = _sweep({"someone-else": [pr], "tekflox": []},
                       view_responses={pr["url"]: view},
                       search_errors={"tekflox": "gh: rate limited"})
    assert result["errors"] == [{"owner": "tekflox", "error": "gh: rate limited"}]
    assert len(result["attention"]) == 1  # someone-else's owner search still succeeded


def test_filters_out_prs_from_repos_not_in_the_discovered_universe():
    pr_in = _search_pr(20, updated_hours_ago=30)
    pr_out = {"number": 21, "repository": {"nameWithOwner": "unrelated/repo"},
             "title": "x", "url": "https://github.com/unrelated/repo/pull/21",
             "updatedAt": _iso(30), "isDraft": False, "createdAt": _iso(31),
             "author": {"login": "someone"}}
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr_in["updatedAt"]}
    result, _ = _sweep({"tekflox": [pr_in, pr_out]}, view_responses={pr_in["url"]: view})
    assert result["counts"]["candidates_considered"] == 1


# ── analyze_workflow_runs — pure grouping/decision logic ─────────────────

def _run(run_number, *, status="completed", conclusion="success", workflow_id=1, name="CI"):
    return {"run_number": run_number, "status": status, "conclusion": conclusion,
           "workflow_id": workflow_id, "name": name,
           "html_url": f"https://github.com/o/r/actions/runs/{run_number}"}


def test_latest_failure_flags_the_workflow():
    runs = [_run(3, conclusion="failure"), _run(2, conclusion="success"), _run(1, conclusion="success")]
    result = gm.analyze_workflow_runs(runs)
    assert result[1]["is_failing"] is True
    assert result[1]["conclusion"] == "failure"


def test_latest_green_run_clears_an_earlier_failure():
    runs = [_run(3, conclusion="success"), _run(2, conclusion="failure"), _run(1, conclusion="failure")]
    result = gm.analyze_workflow_runs(runs)
    assert result[1]["is_failing"] is False


def test_consecutive_failures_counts_back_from_the_newest():
    runs = [_run(4, conclusion="failure"), _run(3, conclusion="failure"),
           _run(2, conclusion="success"), _run(1, conclusion="failure")]
    result = gm.analyze_workflow_runs(runs)
    assert result[1]["consecutive_failures"] == 2


def test_in_progress_latest_run_is_skipped_for_latest_completed_lookup():
    runs = [_run(2, status="in_progress", conclusion=None), _run(1, conclusion="failure")]
    result = gm.analyze_workflow_runs(runs)
    assert result[1]["is_failing"] is True  # falls back to the last COMPLETED run


def test_a_workflow_with_no_completed_run_yet_is_skipped_not_guessed():
    runs = [_run(1, status="in_progress", conclusion=None)]
    result = gm.analyze_workflow_runs(runs)
    assert result == {}


def test_multiple_workflows_grouped_independently():
    runs = [_run(1, workflow_id=1, name="CI", conclusion="failure"),
           _run(1, workflow_id=2, name="Deploy", conclusion="success")]
    result = gm.analyze_workflow_runs(runs)
    assert result[1]["is_failing"] is True
    assert result[2]["is_failing"] is False


# ── scan_repo_failing_workflows ───────────────────────────────────────────

def test_scan_repo_failing_workflows_reports_a_failing_entry(monkeypatch):
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")
    runner = _fake_runner(api_responses={
        "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
            "total_count": 1, "workflows": [{"id": 9, "name": "CI", "state": "active"}]},
        "repos/tekflox/aw-app-kb/actions/workflows/9/runs?branch=main&per_page=10": {
            "workflow_runs": [_run(2, workflow_id=9, conclusion="failure")]},
    })
    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": "/x"}, runner))
    assert result["error"] is None
    assert result["entries"] == [{
        "repo": "tekflox/aw-app-kb", "workflow": "CI", "default_branch": "main",
        "latest_run_url": "https://github.com/o/r/actions/runs/2",
        "conclusion": "failure", "consecutive_failures": 1,
    }]


def test_scan_repo_failing_workflows_a_low_traffic_workflow_still_surfaces(monkeypatch):
    """The exact code-review finding: a busy workflow and a rarely-triggered
    one no longer share one combined 10-run page — each workflow gets its
    own dedicated runs query, so the rarely-triggered one's failure can't be
    pushed out of the window by the busy one."""
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")
    busy_runs = {"workflow_runs": [_run(n, workflow_id=1, name="CI", conclusion="success")
                                   for n in range(50, 40, -1)]}  # 10 green runs
    runner = _fake_runner(api_responses={
        "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
            "total_count": 2, "workflows": [
                {"id": 1, "name": "CI", "state": "active"},
                {"id": 2, "name": "Weekly Release", "state": "active"},
            ]},
        "repos/tekflox/aw-app-kb/actions/workflows/1/runs?branch=main&per_page=10": busy_runs,
        "repos/tekflox/aw-app-kb/actions/workflows/2/runs?branch=main&per_page=10": {
            "workflow_runs": [_run(3, workflow_id=2, name="Weekly Release", conclusion="failure")]},
    })
    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": "/x"}, runner))
    assert result["error"] is None
    workflows_flagged = {e["workflow"] for e in result["entries"]}
    assert workflows_flagged == {"Weekly Release"}


def test_scan_repo_failing_workflows_skips_disabled_workflows(monkeypatch):
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")
    runner = _fake_runner(api_responses={
        "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
            "total_count": 1, "workflows": [{"id": 5, "name": "Old CI", "state": "disabled_manually"}]},
    })
    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": "/x"}, runner))
    assert result["entries"] == []
    assert result["error"] is None


def test_scan_repo_failing_workflows_isolates_a_per_workflow_fetch_error(monkeypatch):
    """One workflow's runs call failing must not drop the OTHER workflow's
    entry — same per-item error isolation the PR sweep already has."""
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    async def runner(args):
        if args[:2] == ["api", "repos/tekflox/aw-app-kb/actions/workflows?per_page=100"]:
            return 0, json.dumps({"total_count": 2, "workflows": [
                {"id": 1, "name": "CI", "state": "active"},
                {"id": 2, "name": "Deploy", "state": "active"},
            ]}), ""
        if args[1] == "repos/tekflox/aw-app-kb/actions/workflows/1/runs?branch=main&per_page=10":
            return 1, "", "gh: rate limited"
        if args[1] == "repos/tekflox/aw-app-kb/actions/workflows/2/runs?branch=main&per_page=10":
            return 0, json.dumps({"workflow_runs": [_run(1, workflow_id=2, name="Deploy",
                                                          conclusion="failure")]}), ""
        raise AssertionError(args)

    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": "/x"}, runner))
    assert "rate limited" in result["error"]
    assert [e["workflow"] for e in result["entries"]] == ["Deploy"]


def test_scan_repo_failing_workflows_unresolved_default_branch_is_an_error(monkeypatch):
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "")
    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": "/x"}, _fake_runner()))
    assert result["entries"] == []
    assert "default branch" in result["error"]


def test_scan_repo_failing_workflows_gh_api_failure_is_an_error_not_an_empty(monkeypatch):
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    async def failing_runner(args):
        return 1, "", "gh: rate limited"

    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": "/x"}, failing_runner))
    assert result["entries"] == []
    assert "rate limited" in result["error"]


# ── scan_repo_failing_workflows — non-cloned (list mode) remote fallback ──
# A local_path of None (a list-mode watchlist entry that isn't cloned here)
# must never reach discover_default_branch — the default branch comes from
# `gh api repos/{owner}/{repo}` instead.

def test_scan_repo_failing_workflows_non_cloned_entry_resolves_branch_remotely():
    runner = _fake_runner(api_responses={
        "repos/tekflox/aw-app-kb": {"default_branch": "main"},
        "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
            "total_count": 1, "workflows": [{"id": 9, "name": "CI", "state": "active"}]},
        "repos/tekflox/aw-app-kb/actions/workflows/9/runs?branch=main&per_page=10": {
            "workflow_runs": [_run(2, workflow_id=9, conclusion="failure")]},
    })
    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "aw-app-kb", "local_path": None}, runner))
    assert result["error"] is None
    assert result["entries"][0]["default_branch"] == "main"


def test_scan_repo_failing_workflows_non_cloned_entry_404_surfaces_as_an_error():
    """The detection path for a watchlist entry that drifted out from under
    its stored name (renamed/deleted on GitHub) — never a silent skip."""
    async def not_found_runner(args):
        return 1, "", "gh: HTTP 404: Not Found"

    result = asyncio.run(gm.scan_repo_failing_workflows(
        {"owner": "tekflox", "repo": "renamed-away", "local_path": None}, not_found_runner))
    assert result["entries"] == []
    assert "404" in result["error"]


def test_remote_default_branch_rejects_a_response_with_no_default_branch_field():
    async def runner(args):
        return 0, "{}", ""

    branch, error = asyncio.run(gm._remote_default_branch("tekflox", "aw-app-kb", runner))
    assert branch is None
    assert "no default_branch" in error


# ── scan_maintenance — full integration, fake gh_runner, no network ─────

def test_scan_maintenance_reports_both_prs_and_failing_flows(tmp_path, monkeypatch):
    base = tmp_path / "ws"
    _init_bare_git_dir(base)
    _init_bare_git_dir(base / "repos" / "aw-app-kb")
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    def git_remote_url(path):
        if path.endswith("aw-app-kb"):
            return "git@github.com:tekflox/aw-app-kb.git"
        return "git@github.com:fredericowu/aw-workspace.git"

    pr = _search_pr(30, updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    runner = _fake_runner(
        search_responses={"tekflox": [pr], "fredericowu": []},
        view_responses={pr["url"]: view},
        api_responses={
            "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
                "total_count": 1, "workflows": [{"id": 1, "name": "CI", "state": "active"}]},
            "repos/tekflox/aw-app-kb/actions/workflows/1/runs?branch=main&per_page=10": {
                "workflow_runs": [_run(1, conclusion="failure")]},
            "repos/fredericowu/aw-workspace/actions/workflows?per_page=100": {
                "total_count": 1, "workflows": [{"id": 2, "name": "CI", "state": "active"}]},
            "repos/fredericowu/aw-workspace/actions/workflows/2/runs?branch=main&per_page=10": {
                "workflow_runs": [_run(1, workflow_id=2, conclusion="success")]},
        })
    store_path = str(tmp_path / "surfaced.json")
    result = asyncio.run(gm.scan_maintenance(
        now=NOW, gh_runner=runner, base_dir=str(base), git_remote_url=git_remote_url,
        store_path=store_path))

    assert len(result["prs"]) == 1
    assert result["prs"][0]["repo"] == "tekflox/aw-app-kb"
    assert len(result["failing_flows"]) == 1
    assert result["failing_flows"][0]["repo"] == "tekflox/aw-app-kb"
    assert result["counts"]["mapped_repos"] == 2
    assert result["errors"] == []
    assert gm.load_surfaced_store(store_path)  # the surfaced stamp was persisted


def test_scan_maintenance_surfaces_an_unreadable_repos_dir_as_an_error_not_a_clean_empty(tmp_path, monkeypatch):
    base = tmp_path / "ws"
    _init_bare_git_dir(base)  # root exists, still scanned, despite repos/ being unreadable below
    repos_dir = base / "repos"
    repos_dir.mkdir()
    (repos_dir / "alpha" / ".git").mkdir(parents=True)
    repos_dir.chmod(0o000)
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    try:
        result = asyncio.run(gm.scan_maintenance(
            now=NOW, gh_runner=_fake_runner(), base_dir=str(base),
            git_remote_url=lambda p: "git@github.com:fredericowu/aw-workspace.git",
            store_path=str(tmp_path / "surfaced.json")))
    finally:
        repos_dir.chmod(0o755)

    assert any(e["error"] and "unreadable" in e["error"] for e in result["errors"])
    assert result["counts"]["mapped_repos"] == 1


def test_scan_maintenance_dry_run_writes_nothing_to_the_store(tmp_path):
    base = tmp_path / "ws"
    _init_bare_git_dir(base)
    _init_bare_git_dir(base / "repos" / "aw-app-kb")
    store_path = str(tmp_path / "surfaced.json")

    pr = _search_pr(40, updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    runner = _fake_runner(search_responses={"tekflox": [pr]}, view_responses={pr["url"]: view})
    asyncio.run(gm.scan_maintenance(
        now=NOW, gh_runner=runner, base_dir=str(base),
        git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git",
        store_path=store_path, dry_run=True))
    assert not Path(store_path).exists()


# ── scan_maintenance — scope wiring (card 4185bf3b-9510-8147) ────────────

def _two_repo_workspace(tmp_path):
    base = tmp_path / "ws"
    _init_bare_git_dir(base)
    _init_bare_git_dir(base / "repos" / "aw-app-kb")

    def git_remote_url(path):
        if path.endswith("aw-app-kb"):
            return "git@github.com:tekflox/aw-app-kb.git"
        return "git@github.com:fredericowu/aw-workspace.git"

    return base, git_remote_url


def test_scan_maintenance_scope_none_is_byte_identical_to_the_pre_scope_pipeline(tmp_path, monkeypatch):
    """HARD backcompat (Architect design §5): a workspace that never
    configured the setting must scan exactly what it always has."""
    base, git_remote_url = _two_repo_workspace(tmp_path)
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    pr = _search_pr(30, updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}

    def _run_scan(scope):
        runner = _fake_runner(
            search_responses={"tekflox": [pr], "fredericowu": []},
            view_responses={pr["url"]: view},
            api_responses={
                "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
                    "total_count": 1, "workflows": [{"id": 1, "name": "CI", "state": "active"}]},
                "repos/tekflox/aw-app-kb/actions/workflows/1/runs?branch=main&per_page=10": {
                    "workflow_runs": [_run(1, conclusion="failure")]},
                "repos/fredericowu/aw-workspace/actions/workflows?per_page=100": {
                    "total_count": 1, "workflows": [{"id": 2, "name": "CI", "state": "active"}]},
                "repos/fredericowu/aw-workspace/actions/workflows/2/runs?branch=main&per_page=10": {
                    "workflow_runs": [_run(1, workflow_id=2, conclusion="success")]},
            })
        return asyncio.run(gm.scan_maintenance(
            now=NOW, gh_runner=runner, base_dir=str(base), git_remote_url=git_remote_url,
            store_path=str(tmp_path / f"surfaced-{scope}.json"), scope=scope))

    no_scope = _run_scan(None)
    all_scope = _run_scan({"mode": "all"})
    assert no_scope["prs"] == all_scope["prs"]
    assert no_scope["failing_flows"] == all_scope["failing_flows"]
    assert no_scope["counts"]["mode"] == "all"
    assert no_scope["counts"]["watched_repos"] == 2


def test_scan_maintenance_list_mode_covers_a_non_cloned_entry_too(tmp_path, monkeypatch):
    base, git_remote_url = _two_repo_workspace(tmp_path)
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    runner = _fake_runner(
        search_responses={"someone-else": []},
        api_responses={
            "repos/tekflox/aw-app-kb/actions/workflows?per_page=100": {
                "total_count": 0, "workflows": []},
            "repos/someone-else/other-repo": {"default_branch": "main"},
            "repos/someone-else/other-repo/actions/workflows?per_page=100": {
                "total_count": 1, "workflows": [{"id": 5, "name": "CI", "state": "active"}]},
            "repos/someone-else/other-repo/actions/workflows/5/runs?branch=main&per_page=10": {
                "workflow_runs": [_run(1, workflow_id=5, conclusion="failure")]},
        })
    result = asyncio.run(gm.scan_maintenance(
        now=NOW, gh_runner=runner, base_dir=str(base), git_remote_url=git_remote_url,
        store_path=str(tmp_path / "surfaced.json"),
        scope={"mode": "list", "list": ["tekflox/aw-app-kb", "someone-else/other-repo"]}))

    assert result["counts"]["mode"] == "list"
    assert result["counts"]["watched_repos"] == 2
    assert [f["repo"] for f in result["failing_flows"]] == ["someone-else/other-repo"]
    # fredericowu/aw-workspace is cloned here but NOT in the list — must not
    # be searched/swept at all.
    assert result["counts"]["owners"] == 2


def test_scan_maintenance_list_mode_empty_list_is_a_clean_zero_scan_with_an_error(tmp_path):
    base, git_remote_url = _two_repo_workspace(tmp_path)
    result = asyncio.run(gm.scan_maintenance(
        now=NOW, gh_runner=_fake_runner(), base_dir=str(base), git_remote_url=git_remote_url,
        store_path=str(tmp_path / "surfaced.json"), scope={"mode": "list", "list": []}))

    assert result["prs"] == [] and result["failing_flows"] == []
    assert result["counts"]["watched_repos"] == 0
    assert any("empty list" in e["error"] for e in result["errors"])


def test_scan_maintenance_narrowing_the_scope_prunes_the_now_unwatched_prs_surfaced_stamp(
        tmp_path, monkeypatch):
    """The surfaced store's SET/CLEAR interplay with a scope change
    (Architect design §5): narrowing scope away from a specific repo, while
    its OWNER is still searched (another of that owner's repos stays in
    the list), drops the now-unwatched repo's back-off stamp — the
    attention filter excludes it from ``open_pr_keys`` even though the
    search that would have kept its stamp alive still ran. Re-widening
    would re-surface it once rather than leaving it permanently backed off
    on stale state."""
    base, git_remote_url = _two_repo_workspace(tmp_path)
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")
    store_path = str(tmp_path / "surfaced.json")

    pr = _search_pr(99, owner="tekflox", repo="aw-app-kb", updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    runner = _fake_runner(search_responses={"tekflox": [pr], "fredericowu": []},
                          view_responses={pr["url"]: view})

    asyncio.run(gm.scan_maintenance(
        now=NOW, gh_runner=runner, base_dir=str(base), git_remote_url=git_remote_url,
        store_path=store_path, scope={"mode": "all"}))
    assert "tekflox/aw-app-kb#99" in gm.load_surfaced_store(store_path)

    # Narrow to a watchlist that still searches "tekflox" (another tekflox
    # repo stays listed) but no longer watches tekflox/aw-app-kb
    # specifically — the owner search still runs and returns the same PR,
    # but the attention filter now drops it before it reaches open_pr_keys.
    narrow_runner = _fake_runner(search_responses={"tekflox": [pr]}, view_responses={pr["url"]: view})
    asyncio.run(gm.scan_maintenance(
        now=NOW, gh_runner=narrow_runner, base_dir=str(base), git_remote_url=git_remote_url,
        store_path=store_path, scope={"mode": "list", "list": ["tekflox/other-repo"]}))
    assert "tekflox/aw-app-kb#99" not in gm.load_surfaced_store(store_path)


# ── MaintenanceWatchdog — cache shape, never dies on a scan failure ─────

def test_watchdog_poll_populates_the_cache_from_a_successful_scan(tmp_path, monkeypatch):
    base = tmp_path / "ws"
    _init_bare_git_dir(base)
    _init_bare_git_dir(base / "repos" / "aw-app-kb")
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    pr = _search_pr(50, updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    runner = _fake_runner(
        search_responses={"tekflox": [pr]}, view_responses={pr["url"]: view},
        api_responses={"repos/tekflox/aw-app-kb/actions/workflows?per_page=100":
                       {"total_count": 0, "workflows": []}})

    wd = gm.MaintenanceWatchdog(
        base_dir=str(base), store_path=str(tmp_path / "surfaced.json"), gh_runner=runner,
        git_remote_url=lambda p: "git@github.com:tekflox/aw-app-kb.git")

    asyncio.run(wd.poll(now=NOW))
    cached = wd.get_cached()
    assert cached["last_poll"] == NOW
    assert cached["error"] is None
    assert len(cached["prs"]) == 1


def test_watchdog_poll_never_raises_on_a_missing_root_and_reports_a_clean_empty(tmp_path):
    """A base_dir that doesn't exist at all (e.g. a misconfigured
    AW_APP_GIT_WATCHDOG_ROOT) is a legitimately empty scan, not a failure —
    the registered callable must not die either way, under the framework's
    own cadence loop."""
    wd = gm.MaintenanceWatchdog(base_dir=str(tmp_path / "does-not-exist-root"))
    asyncio.run(wd.poll(now=NOW))  # must not raise
    cached = wd.get_cached()
    assert cached["last_poll"] == NOW
    assert cached["error"] is None
    assert cached["prs"] == [] and cached["failing_flows"] == []


def test_watchdog_poll_never_raises_on_an_unreadable_repos_dir_and_records_the_error(tmp_path):
    base = tmp_path / "ws"
    _init_bare_git_dir(base)
    repos_dir = base / "repos"
    repos_dir.mkdir()
    repos_dir.chmod(0o000)
    try:
        wd = gm.MaintenanceWatchdog(
            base_dir=str(base), git_remote_url=lambda p: "git@github.com:fredericowu/aw-workspace.git")
        asyncio.run(wd.poll(now=NOW))  # must not raise — this is the exact bug the review found
    finally:
        repos_dir.chmod(0o755)

    cached = wd.get_cached()
    assert cached["last_poll"] == NOW
    assert cached["error"] is not None
    assert "unreadable" in cached["error"]


def test_watchdog_poll_reads_the_scope_fn_each_poll(tmp_path, monkeypatch):
    """A changed setting must take effect on the NEXT poll — the watchdog
    must call scope_fn() per poll, not cache a stale scope from construction
    time."""
    base = tmp_path / "ws"
    _init_bare_git_dir(base)
    _init_bare_git_dir(base / "repos" / "aw-app-kb")
    monkeypatch.setattr(github_prs, "discover_default_branch", lambda path: "origin/main")

    pr = _search_pr(60, owner="tekflox", repo="aw-app-kb", updated_hours_ago=30)
    view = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
           "reviewDecision": "", "mergeable": "MERGEABLE", "isDraft": False, "updatedAt": pr["updatedAt"]}
    runner = _fake_runner(search_responses={"tekflox": [pr], "fredericowu": []},
                          view_responses={pr["url"]: view})

    scope = {"mode": "all"}
    wd = gm.MaintenanceWatchdog(
        base_dir=str(base), store_path=str(tmp_path / "surfaced.json"), gh_runner=runner,
        git_remote_url=lambda p: ("git@github.com:tekflox/aw-app-kb.git" if p.endswith("aw-app-kb")
                                  else "git@github.com:fredericowu/aw-workspace.git"),
        scope_fn=lambda: scope)

    asyncio.run(wd.poll(now=NOW))
    assert len(wd.get_cached()["prs"]) == 1
    assert wd.get_cached()["counts"]["mode"] == "all"

    scope = {"mode": "list", "list": ["fredericowu/aw-workspace"]}
    asyncio.run(wd.poll(now=NOW + 1))
    cached = wd.get_cached()
    assert cached["counts"]["mode"] == "list"
    assert cached["prs"] == []  # tekflox/aw-app-kb is no longer watched


# ── ping-gate planning (pure — route-level slice) ───────────────────────

def _flow(repo="o/r", workflow="ci", **over):
    flow = {"repo": repo, "workflow": workflow, "default_branch": "main",
            "latest_run_url": f"https://github.com/{repo}/actions/runs/1",
            "conclusion": "failure", "consecutive_failures": 1}
    flow.update(over)
    return flow


def test_flow_key_is_repo_colon_workflow():
    assert gm.flow_key(_flow(repo="o/r", workflow="ci")) == "o/r:ci"


def test_plan_pings_a_brand_new_failure_is_reported_and_stamped():
    plan = gm.plan_maintenance_pings([_flow()], {}, NOW)
    assert [gm.flow_key(f) for f in plan["new_failures"]] == ["o/r:ci"]
    assert plan["state"]["failing"]["o/r:ci"]["first_seen_at"] == NOW


def test_plan_pings_an_already_surfaced_failure_does_not_renotify():
    state = {"failing": {"o/r:ci": {"first_seen_at": NOW - 3600}}}
    plan = gm.plan_maintenance_pings([_flow()], state, NOW)
    assert plan["new_failures"] == []
    # still present in the next state — an ongoing failure isn't cleared
    assert "o/r:ci" in plan["state"]["failing"]


def test_plan_pings_a_cleared_flow_is_dropped_from_state_with_no_notification():
    state = {"failing": {"o/r:ci": {"first_seen_at": NOW - 3600}}}
    plan = gm.plan_maintenance_pings([], state, NOW)  # flow went green
    assert plan["new_failures"] == []
    assert plan["state"]["failing"] == {}


def test_plan_pings_a_cleared_then_refailed_flow_renotifies():
    """The full SET → CLEAR → re-SET lifecycle the card requires tested
    explicitly: a flow that goes green and then fails again is news again,
    gated strictly on the store transition rather than on raw failing state."""
    state = {}
    plan1 = gm.plan_maintenance_pings([_flow()], state, NOW)
    assert [gm.flow_key(f) for f in plan1["new_failures"]] == ["o/r:ci"]

    plan2 = gm.plan_maintenance_pings([], plan1["state"], NOW + 60)  # went green
    assert plan2["new_failures"] == []
    assert plan2["state"]["failing"] == {}

    plan3 = gm.plan_maintenance_pings([_flow()], plan2["state"], NOW + 120)  # fails again
    assert [gm.flow_key(f) for f in plan3["new_failures"]] == ["o/r:ci"]


def test_plan_pings_daily_summary_skipped_when_no_failing_flows():
    plan = gm.plan_maintenance_pings([], {}, NOW)
    assert plan["summary_due"] is False
    assert plan["state"]["last_summary_at"] is None


def test_plan_pings_daily_summary_fires_once_then_waits_24h():
    plan1 = gm.plan_maintenance_pings([_flow()], {}, NOW)
    assert plan1["summary_due"] is True
    assert plan1["state"]["last_summary_at"] == NOW

    plan2 = gm.plan_maintenance_pings([_flow()], plan1["state"], NOW + 3600)  # 1h later
    assert plan2["summary_due"] is False
    assert plan2["state"]["last_summary_at"] == NOW  # unchanged

    plan3 = gm.plan_maintenance_pings([_flow()], plan2["state"], NOW + 25 * 3600)  # >24h later
    assert plan3["summary_due"] is True
    assert plan3["state"]["last_summary_at"] == NOW + 25 * 3600
