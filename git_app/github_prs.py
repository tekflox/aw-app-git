"""GitHub PR dashboard — polls the gh CLI, caches results, streams via WebSocket.

Ported (not rewritten) from the monolith's
``agentic-workspace/src/api/routes/github.py`` / ``aw-backend`` twin, which
served ``/api/github/prs``, ``/api/github/refresh``, ``/api/github/find-buddies``
and ``/ws/github``. aw-workspace's core has no GitHub surface at all — those
paths 404 on the workspace host — so the feature lives here, in the app that
already owns ``gh`` (installs it, holds its token, logs it in).

What the port had to adapt to this architecture — everything else is the
monolith's logic unchanged:

* **Config** — the monolith read a ``github`` block out of the workspace config
  (``aw.json``, later Postgres). An app has no such store: settings arrive as a
  ``cfg`` dict the plugin assembles from ``ctx.secrets`` (the only app-writable
  store) with install-time ``ctx.config`` as fallback. Same keys: ``team``,
  ``host``, ``poll_interval``.
* **Polling** — the monolith ran its own ``while True`` loop task. Here the
  plugin registers :meth:`PrWatchdog.poll` through ``ctx.watchdog`` so the
  framework owns the cadence, cancellation, and uninstall.
* **Broadcast** — aw-backend fanned updates across uvicorn workers over Redis
  (``RedisBroadcaster``). aw-workspace runs single-worker by design (see
  ``AW_WORKSPACE_WORKERS``), so this keeps the ORIGINAL agentic-workspace
  behaviour: deliver straight to the connected WebSocket listeners.
* **Notifications** — ``notification_mgr.add_notification`` becomes the injected
  ``notify`` callable (``ctx.notify``, capability ``notifications:send``), which
  reaches the very same NotificationManager. Only ``source`` differs: the
  workspace stamps it with the app id (``git``) instead of ``"github"``.
* **Repo discovery** — ``_fetch_repos()`` duplicated a walk this app already
  has in :mod:`git_app.uncommitted_watchdog` (itself a copy of that same
  monolith function), so it reuses ``discover_repos()`` instead of a third copy.

Three deliberate divergences, each a monolith bug that would have shipped as-is
(marked ``DIVERGENCE`` at the site):

1. PRs were only ever fetched when a ``team`` was configured — a fresh install
   with no team saw an empty dashboard forever, including its own PRs.
2. ``find_buddies``' early returns handed back a 3-tuple while the success path
   returned a list, so the route's ``{"buddies": ...}`` changed shape on the
   "no collaborators" path.
3. ``find_buddies`` dropped every login without a ``-`` in it (an artefact of
   one org's username convention) — which silently discards normal GitHub
   logins like ``octocat``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any

from .uncommitted_watchdog import discover_repos

log = logging.getLogger("aw_apps.git.github")

DEFAULT_POLL_INTERVAL = 300


def _run_gh(args, host=None):
    """Run a gh CLI command, return parsed JSON or None."""
    env = os.environ.copy()
    if host:
        env["GH_HOST"] = host
    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        if result.returncode != 0:
            log.warning("gh %s failed: %s", " ".join(args[:3]), result.stderr.strip()[:200])
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except FileNotFoundError:
        log.warning("gh CLI not found")
        return None
    except subprocess.TimeoutExpired:
        log.warning("gh CLI timed out")
        return None
    except json.JSONDecodeError:
        return None


def classify_pr(pr):
    """Classify PR into a status badge based on CI + review state."""
    checks = pr.get("statusCheckRollup") or []
    review_decision = pr.get("reviewDecision") or ""
    mergeable = pr.get("mergeable") or ""
    is_draft = pr.get("isDraft", False)

    ci_states = set()
    for check in checks:
        state = (check.get("status") or check.get("state") or "").upper()
        conclusion = (check.get("conclusion") or "").upper()
        if state in ("IN_PROGRESS", "QUEUED", "PENDING"):
            ci_states.add("running")
        elif conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            ci_states.add("failed")
        elif conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            ci_states.add("passed")
        elif state == "COMPLETED":
            ci_states.add("passed")

    if "running" in ci_states:
        ci = "running"
    elif "failed" in ci_states:
        ci = "failed"
    elif "passed" in ci_states:
        ci = "passed"
    else:
        ci = "unknown"

    if is_draft:
        badge = "draft"
    elif ci == "running":
        badge = "ci-running"
    elif ci == "failed":
        badge = "tests-failed"
    elif mergeable == "MERGEABLE" and review_decision == "APPROVED" and ci == "passed":
        badge = "ready-to-merge"
    elif ci == "passed" and review_decision != "APPROVED":
        badge = "needs-review"
    else:
        badge = "open"

    return badge, ci


def format_pr(pr):
    """Extract relevant fields from a gh PR JSON object."""
    badge, ci = classify_pr(pr)
    repo_obj = pr.get("repository") or {}
    comments_raw = pr.get("comments", [])
    comments = []
    if isinstance(comments_raw, list):
        for c in comments_raw[-5:]:
            comments.append({
                "author": (c.get("author") or {}).get("login", ""),
                "body": (c.get("body") or "")[:200],
                "createdAt": c.get("createdAt", ""),
            })

    merged_by = ""
    if pr.get("mergedBy"):
        merged_by = (pr["mergedBy"] or {}).get("login", "")

    review_requests = pr.get("reviewRequests", []) or []
    requested_reviewers = []
    if isinstance(review_requests, list):
        for rr in review_requests:
            login = (rr.get("login") or (rr.get("name") if isinstance(rr, dict) else ""))
            if login:
                requested_reviewers.append(login)

    latest_reviews = pr.get("latestReviews", []) or []
    reviews = []
    if isinstance(latest_reviews, list):
        for r in latest_reviews:
            reviews.append({
                "author": (r.get("author") or {}).get("login", ""),
                "state": r.get("state", ""),
            })

    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "author": (pr.get("author") or {}).get("login", ""),
        "repo": repo_obj.get("nameWithOwner", ""),
        "badge": badge,
        "ci": ci,
        "isDraft": pr.get("isDraft", False),
        "reviewDecision": pr.get("reviewDecision", ""),
        "mergeable": pr.get("mergeable", ""),
        "state": pr.get("state", "OPEN"),
        "mergedBy": merged_by,
        "commentCount": len(comments_raw) if isinstance(comments_raw, list) else 0,
        "comments": comments,
        "requestedReviewers": requested_reviewers,
        "reviews": reviews,
        "createdAt": pr.get("createdAt", ""),
        "updatedAt": pr.get("updatedAt", ""),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
    }


def search_prs(author, host=None):
    """Search for open PRs by author across all repos via gh search prs."""
    raw = _run_gh(["search", "prs", "--author", author, "--state", "open",
                   "--json", "number,title,url,repository,isDraft,createdAt,updatedAt",
                   "--limit", "30"], host=host)
    return raw or []


def search_review_requested(host=None):
    """Search for open PRs where current user is a requested reviewer."""
    raw = _run_gh(["search", "prs", "--review-requested", "@me", "--state", "open",
                   "--json", "number,title,url,repository,isDraft,createdAt,updatedAt",
                   "--limit", "30"], host=host)
    return raw or []


def enrich_pr(pr, host=None):
    """Fetch full PR details (CI status, review decision, mergeable) via gh pr view."""
    repo = (pr.get("repository") or {}).get("nameWithOwner", "")
    number = pr.get("number")
    if not repo or not number:
        return pr

    detail_fields = (
        "number,title,url,author,isDraft,reviewDecision,mergeable,statusCheckRollup,"
        "additions,deletions,createdAt,updatedAt,state,mergedBy,comments,reviewRequests,latestReviews"
    )
    detail = _run_gh(["pr", "view", str(number), "--repo", repo,
                      "--json", detail_fields], host=host)
    if detail:
        detail["repository"] = pr.get("repository", {})
        return detail
    return pr


def fetch_prs(github_cfg):
    """Fetch PRs for current user, review requests, and team members.

    Strategy: gh search prs (discovers across all repos) → gh pr view (full details per PR).
    """
    host = github_cfg.get("host")
    team = github_cfg.get("team", []) or []

    my_search = search_prs("@me", host=host)
    my_prs = []
    seen_urls = set()
    for pr in my_search:
        enriched = enrich_pr(pr, host=host)
        formatted = format_pr(enriched)
        my_prs.append(formatted)
        seen_urls.add(formatted["url"])

    team_prs = []
    for member in team:
        member_search = search_prs(member, host=host)
        for pr in member_search:
            if pr.get("url") in seen_urls:
                continue
            enriched = enrich_pr(pr, host=host)
            formatted = format_pr(enriched)
            team_prs.append(formatted)
            seen_urls.add(formatted["url"])

    review_search = search_review_requested(host=host)
    review_prs = []
    for pr in review_search:
        if pr.get("url") in seen_urls:
            continue
        enriched = enrich_pr(pr, host=host)
        formatted = format_pr(enriched)
        review_prs.append(formatted)
        seen_urls.add(formatted["url"])

    return my_prs, team_prs, review_prs


def discover_default_branch(repo_path):
    """Discover the default branch for a repo via remote/HEAD or common names."""
    for remote in ["upstream", "origin"]:
        head_out = subprocess.run(
            ["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=3
        )
        if head_out.returncode == 0 and head_out.stdout.strip():
            return head_out.stdout.strip().replace("refs/remotes/", "")
    for candidate in ["upstream/develop", "upstream/main", "upstream/master",
                      "origin/develop", "origin/main", "origin/master"]:
        rc = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=repo_path, capture_output=True, timeout=3
        ).returncode
        if rc == 0:
            return candidate
    return ""


def git_status_for_repo(repo_path: str, name: str) -> dict:
    """Return the dict shape used by Workspace > Repos for a single repo."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=5
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True, timeout=5
        ).stdout.strip())
        diff_ref = discover_default_branch(repo_path)
        ahead = 0
        if diff_ref:
            ahead_out = subprocess.run(
                ["git", "rev-list", "--count", f"{diff_ref}..HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            try:
                ahead = int(ahead_out)
            except ValueError:
                pass  # unexpected git output — leave ahead at 0
        return {"name": name, "path": repo_path, "branch": branch, "dirty": dirty,
                "diff_ref": diff_ref, "ahead": ahead}
    except Exception:
        return {"name": name, "path": repo_path, "branch": "?", "dirty": False}


def fetch_repos(base_dir: str | None = None):
    """Workspace > Repos list — the workspace root (pinned first) plus every
    git repo directly under ``<root>/repos``.

    Discovery is ``uncommitted_watchdog.discover_repos`` (same walk, same order
    as the monolith's ``_fetch_repos``) so this app has ONE definition of "the
    workspace's repos"; this only adds the per-repo git status on top.
    """
    return [git_status_for_repo(path, name) for name, path in discover_repos(base_dir)]


def find_buddies(github_cfg):
    """Find frequent PR collaborators from the last 3 months.

    Strategy:
    1. Search my PRs from last 3 months → extract reviewers
    2. Search PRs I reviewed from last 3 months → extract authors
    3. Count interactions, return top quadrant (above median)
    """
    from collections import Counter
    from datetime import datetime, timedelta

    host = github_cfg.get("host")
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    me_raw = _run_gh(["api", "user", "--jq", ".login"], host=host)
    if isinstance(me_raw, str):
        me = me_raw.strip()
    elif isinstance(me_raw, dict):
        me = me_raw.get("login", "")
    else:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, **({"GH_HOST": host} if host else {})}
        )
        me = result.stdout.strip()

    if not me:
        raise RuntimeError("Could not determine GitHub username")

    log.info("Finding buddies for %s (since %s)", me, cutoff)
    interactions = Counter()

    my_prs = _run_gh([
        "search", "prs", "--author", "@me", "--merged",
        "--created", f">={cutoff}",
        "--json", "number,repository",
        "--limit", "100",
    ], host=host) or []

    for pr in my_prs:
        repo = (pr.get("repository") or {}).get("nameWithOwner", "")
        number = pr.get("number")
        if not repo or not number:
            continue
        reviews = _run_gh([
            "pr", "view", str(number), "--repo", repo,
            "--json", "reviews", "--jq", "[.reviews[].author.login]",
        ], host=host)
        if isinstance(reviews, list):
            for reviewer in reviews:
                if reviewer and reviewer != me:
                    interactions[reviewer] += 1

    reviewed = _run_gh([
        "search", "prs", "--reviewed-by", "@me", "--merged",
        "--created", f">={cutoff}",
        "--json", "author,number",
        "--limit", "100",
    ], host=host) or []

    for pr in reviewed:
        author = (pr.get("author") or {}).get("login", "")
        if author and author != me:
            interactions[author] += 1

    # DIVERGENCE 2: the monolith's early exits returned a 3-tuple ([], "", [])
    # while the success path returns a list, so the caller's {"buddies": ...}
    # changed shape depending on the outcome. A list either way.
    if not interactions:
        return []

    # DIVERGENCE 3: the monolith dropped every login without a "-" — a filter
    # tuned to one org's `first-last` username convention that throws away
    # ordinary GitHub logins (octocat). Keep it as a PREFERENCE (org-style
    # logins first when there are any) rather than a hard filter, so a
    # personal account still discovers its collaborators.
    org_style = Counter({k: v for k, v in interactions.items() if "-" in k})
    if org_style:
        interactions = org_style

    counts = list(interactions.values())
    n = len(counts)
    mean = sum(counts) / n
    variance = sum((c - mean) ** 2 for c in counts) / n
    stddev = variance ** 0.5
    q1_threshold = mean + 0.675 * stddev

    all_collabs = [
        {"name": name, "count": count, "q1": count >= q1_threshold}
        for name, count in interactions.most_common()
    ]

    log.info("Found %d collaborators, Q1 threshold=%.1f (mean=%.1f, stddev=%.1f): %s",
             len(interactions), q1_threshold, mean, stddev,
             [c["name"] for c in all_collabs if c["q1"]][:10])

    return all_collabs


class PrWatchdog:
    """Polls GitHub PRs + repos on an interval, caches results, broadcasts to
    WebSocket listeners.

    ``load_cfg`` returns the current github settings dict (the plugin reads
    them from ``ctx.secrets``); ``notify`` is ``ctx.notify`` (or None when the
    app lacks ``notifications:send``); ``save_team`` persists an auto-discovered
    team (or None to skip discovery).
    """

    def __init__(self, load_cfg, notify=None, save_team=None):
        self._cache: dict[str, Any] = {
            "my_prs": [], "team_prs": [], "review_prs": [], "repos": [],
            "last_poll": 0, "error": None,
        }
        self._listeners: set = set()
        self._load_cfg = load_cfg
        self._notify = notify
        self._save_team = save_team
        self._auto_discovery_done = False
        self._my_login: str | None = None

    def get_cached(self):
        return dict(self._cache)

    async def poll(self, cfg=None):
        """Execute one poll cycle — PRs + repos."""
        cfg = cfg if cfg is not None else self._load_cfg()
        loop = asyncio.get_running_loop()

        repos = []
        try:
            repos = await loop.run_in_executor(None, fetch_repos)
            self._cache["repos"] = repos
        except Exception as e:
            log.warning("Repos scan failed: %s", e)

        if not self._my_login:
            try:
                host = cfg.get("host") if cfg else None
                user_data = await loop.run_in_executor(
                    None, lambda: _run_gh(["api", "user", "--jq", ".login"], host=host))
                if isinstance(user_data, str):
                    self._my_login = user_data.strip()
                elif not user_data:
                    env = os.environ.copy()
                    if host:
                        env["GH_HOST"] = host
                    result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                            capture_output=True, text=True, timeout=10, env=env)
                    if result.returncode == 0 and result.stdout.strip():
                        self._my_login = result.stdout.strip()
                log.info("GitHub user: %s", self._my_login)
            except Exception as e:
                log.warning("Failed to get GitHub login: %s", e)

        # DIVERGENCE 1: the monolith guarded this whole block with
        # `if cfg.get("team")`, so a workspace with no team configured never
        # fetched ANY PRs — not even the user's own, which need no team at all.
        # Gate on being logged in instead; team members are simply extra
        # authors to search for.
        if self._my_login:
            try:
                old_my = {pr["url"]: pr for pr in self._cache.get("my_prs", [])}
                old_team = {pr["url"]: pr for pr in self._cache.get("team_prs", [])}
                old_review = {pr["url"]: pr for pr in self._cache.get("review_prs", [])}
                my_prs, team_prs, review_prs = await loop.run_in_executor(None, fetch_prs, cfg)

                if self._notify and self._cache["last_poll"]:
                    all_old_team = {**old_team, **old_review}
                    all_new_team = team_prs + review_prs
                    self._detect_pr_changes(old_my, all_old_team, my_prs, all_new_team, cfg)

                self._cache["my_prs"] = my_prs
                self._cache["team_prs"] = team_prs
                self._cache["review_prs"] = review_prs
                self._cache["error"] = None
                log.info("GitHub poll: %d my PRs, %d team PRs, %d review PRs, %d repos",
                         len(my_prs), len(team_prs), len(review_prs), len(repos))
            except Exception as e:
                self._cache["error"] = str(e)
                log.warning("GitHub poll failed: %s", e)

        self._cache["last_poll"] = time.time()
        await self._broadcast()

        if not self._auto_discovery_done:
            self._auto_discovery_done = True
            if self._save_team and not (cfg.get("team") or []):
                asyncio.create_task(self._auto_discover_team(cfg))

    async def _auto_discover_team(self, cfg):
        """One-time background auto-discovery of buddies, persisted via save_team."""
        loop = asyncio.get_running_loop()
        try:
            buddies = await loop.run_in_executor(None, find_buddies, cfg)
            if not buddies:
                return
            q1_buddies = [b["name"] for b in buddies if b["q1"]]
            if not q1_buddies:
                return
            self._save_team(q1_buddies)
            log.info("Auto-discovered %d team members: %s", len(q1_buddies), q1_buddies)
            await self.poll()
        except Exception as e:
            log.warning("Auto team discovery failed: %s", e)

    def _detect_pr_changes(self, old_my, old_team, new_my, new_team, cfg=None):
        """Compare old vs new PRs and emit ninja notifications for meaningful changes."""
        notify = self._notify
        if not notify:
            return

        cfg = cfg if cfg is not None else self._load_cfg()
        buddy_logins = set(m.lower() for m in (cfg.get("team") or []))

        for pr in new_team:
            if pr["url"] not in old_team:
                author = pr.get("author", "")
                if author.lower() in buddy_logins:
                    notify(
                        message=f"#{pr.get('number')} {pr.get('title', '')[:60]}",
                        level="info",
                        title=f"New PR from {author}",
                        url=pr.get("url", ""),
                        external_id=str(pr.get("number", "")),
                        external_status="new",
                    )

        for pr in new_my:
            old = old_my.get(pr["url"])
            if not old:
                continue
            pr_id = str(pr.get("number", ""))
            if old.get("badge") != "ready-to-merge" and pr.get("badge") == "ready-to-merge":
                notify(
                    message=f"#{pr.get('number')} {pr.get('title', '')[:60]}",
                    level="success",
                    title="PR ready to merge!",
                    url=pr.get("url", ""),
                    external_id=pr_id,
                    external_status="ready-to-merge",
                )
            elif old.get("badge") != "tests-failed" and pr.get("badge") == "tests-failed":
                notify(
                    message=f"#{pr.get('number')} {pr.get('title', '')[:60]}",
                    level="error",
                    title="CI failed",
                    url=pr.get("url", ""),
                    external_id=pr_id,
                    external_status="tests-failed",
                )

            old_count = old.get("commentCount", 0)
            new_count = pr.get("commentCount", 0)
            if new_count > old_count:
                new_comments = pr.get("comments", [])
                for comment in new_comments[-(new_count - old_count):]:
                    commenter = comment.get("author", "someone")
                    body = comment.get("body", "")[:80]
                    notify(
                        message=f"#{pr.get('number')} {pr.get('title', '')[:40]}\n\"{body}\"",
                        level="info",
                        title=f"Comment from {commenter}",
                        url=pr.get("url", ""),
                        external_id=f"{pr_id}-comment-{new_count}",
                        external_status="comment",
                        supersedes=False,
                    )

        my_login = self._my_login or ""
        my_login_lower = my_login.lower()

        for pr in new_team:
            old = old_team.get(pr["url"])
            pr_id = str(pr.get("number", ""))
            if old and my_login:
                old_count = old.get("commentCount", 0)
                new_count = pr.get("commentCount", 0)
                if new_count > old_count:
                    for comment in pr.get("comments", [])[-max(new_count - old_count, 0):]:
                        body = comment.get("body", "")
                        commenter = comment.get("author", "someone")
                        if f"@{my_login}" in body or f"@{my_login_lower}" in body.lower():
                            notify(
                                message=f"#{pr.get('number')} {pr.get('title', '')[:40]}\n\"{body[:80]}\"",
                                level="warning",
                                title=f"{commenter} mentioned you",
                                url=pr.get("url", ""),
                                external_id=f"{pr_id}-mention-{new_count}",
                                external_status="mentioned",
                                supersedes=False,
                            )

        all_old = {**old_my, **old_team}
        all_new = {}
        for pr in new_my + new_team:
            all_new[pr["url"]] = pr

        for url, old_pr in all_old.items():
            new_pr = all_new.get(url)
            if new_pr and new_pr.get("state") == "MERGED" and old_pr.get("state") != "MERGED":
                merged_by = new_pr.get("mergedBy", "someone")
                notify(
                    message=f"#{new_pr.get('number')} {new_pr.get('title', '')[:60]}\nMerged by {merged_by}",
                    level="success",
                    title="PR merged!",
                    url=new_pr.get("url", ""),
                    external_id=str(new_pr.get("number", "")),
                    external_status="merged",
                )

    async def _broadcast(self):
        """Send cached data to all connected WebSocket clients."""
        if not self._listeners:
            return
        msg = json.dumps({"type": "github_update", **self._cache})
        dead = []
        for ws in self._listeners:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._listeners.discard(ws)

    def add_listener(self, ws):
        self._listeners.add(ws)

    def remove_listener(self, ws):
        self._listeners.discard(ws)
