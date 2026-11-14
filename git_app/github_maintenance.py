"""``github_maintenance`` — the scan layer for the GitHub Maintenance Plan
(Architect design, Kanban card 4185bf3b-9510-81fc, this module's own slice
4185bf3b-9510-8121).

Two sweeps over the SAME repo universe, cached via ``ctx.watchdog`` like
:class:`github_prs.PrWatchdog`, but discovered differently:

* **PR attention sweep** — owner-scoped two-phase ``gh`` sweep, the same
  shape ``aw-app-agents-platform-runners/agents_platform_runners_app/
  pr_scan.py`` already proved (Phase A: one ``gh search prs --owner <o>
  --state open`` per distinct owner; Phase B: ``gh pr view`` enrichment only
  for non-draft, non-backed-off candidates). Logic is **ported, not
  imported** — this app cannot depend on a sibling repo's package — and
  ``classify_pr`` (:mod:`github_prs`) is reused as-is for badges.
* **Failing-workflow sweep** — per repo, the latest workflow runs on the
  *default* branch; a workflow is flagged when its most recent COMPLETED
  run concluded ``failure``. This is the genuinely new capability (no open
  PR to anchor a failure on, e.g. a broken scheduled/release workflow).

**Why owner-scoped discovery, not ``PrWatchdog``'s ``@me``-authored
search:** ``PrWatchdog`` only ever sees PRs authored by, or review-requested
of, the signed-in account — exactly right for a personal dashboard, wrong
for maintenance (misses Dependabot/external contributor PRs). The repo
universe here is :func:`uncommitted_watchdog.discover_repos` — which checks
the root aw-workspace checkout FIRST, then every ``repos/*`` child — fixing
by construction the blind spot ``pr_scan.py`` has for its own repo (it only
ever scans ``/opt/aw-workspace/repos/*``, so a PR on ``fredericowu/
aw-workspace`` itself is invisible to it).

**Per-PR surfaced-stamp back-off**, a stricter sibling of ``pr_scan.py``'s
``backoff_state``: once a PR is included in the attention list ("surfaced"),
it is not surfaced again until either its ``updatedAt`` moves, or a 48h
ceiling passes — ``pr_scan``'s own back-off has no ceiling (a PR can stay
backed off indefinitely once viewed), which is wrong here: this sweep feeds
a periodic Manager tick that would otherwise never re-learn about a PR that
deteriorated without a fresh ``updatedAt`` (e.g. a branch-protection change).
Accepted gap, same as the design doc: a PR that deteriorates with no
``updatedAt`` move waits out the full 48h.

**Error/truncation reporting is explicit, never a silent empty** — the same
rule ``pr_scan.py``'s own
``test_scan_cloned_repos_carries_a_missing_repos_dir_as_an_error_not_a_clean_empty``
enforces: an unreadable ``repos/`` dir, a failed ``gh`` call, or a repo whose
default branch can't be resolved all land in this module's own ``errors``
list rather than disappearing into a clean-looking zero.

This module is scan + cache ONLY — no REST route, no Telegram pings. The
next slice reads :meth:`MaintenanceWatchdog.get_cached` to build the gate.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Awaitable, Callable

from . import github_prs, uncommitted_watchdog

log = logging.getLogger("aw_apps.git.github_maintenance")

GH_TIMEOUT_S = 30
GH_MAX_CONCURRENCY = 8

DEFAULT_STALL_HOURS = 24.0
SURFACE_BACKOFF_CEILING_HOURS = 48.0
DEFAULT_POLL_INTERVAL_S = 1800.0  # ~30min

ATTENTION_BADGES = {"tests-failed", "ready-to-merge", "needs-review"}

SEARCH_FIELDS = "number,repository,title,url,updatedAt,isDraft,createdAt,author"
VIEW_FIELDS = "statusCheckRollup,reviewDecision,mergeable,isDraft,updatedAt"

# Same reasoning as pr_scan.py's own SEARCH_LIMIT (code-review finding, PR
# #41): `gh search prs` defaults to 30 best-match results, not updatedAt —
# sorting oldest-updated-first + a high limit means a cut-off PR is, by
# construction, one of the LEAST stale ones in the owner's queue.
SEARCH_LIMIT = 1000

RUNS_PER_PAGE = 10

STORE_REL = "data/git/github_maintenance_surfaced.json"

_SSH_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_SSH_URL_RE = re.compile(r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_HTTPS_RE = re.compile(r"^https://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


GhRunner = Callable[[list], Awaitable["tuple[int, str, str]"]]
"""``async (argv_after_gh) -> (returncode, stdout, stderr)``. Never raises —
a missing binary or a timeout comes back as a non-zero code with the detail
in ``stderr``. Injectable so tests never shell out to a real ``gh``."""


async def default_gh_runner(args: list) -> "tuple[int, str, str]":
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return 127, "", "gh: command not found"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"gh {' '.join(args)} timed out after {GH_TIMEOUT_S}s"
    return (proc.returncode or 0), stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _parse_iso_datetime(value: "str | None") -> "float | None":
    """Epoch seconds, or ``None`` for anything that doesn't parse — a
    date-only value (``fromisoformat`` happily accepts one as midnight) must
    never be mistaken for a fresh timestamp, so a missing time component is
    rejected rather than silently defaulted. Copied from
    ``agents_platform_runners_app/deploy_reconcile.py`` — same reasoning,
    different repo, no cross-repo import."""
    if not value:
        return None
    text = value.strip()
    if "T" not in text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _git_remote_url(repo_path: str) -> "str | None":
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path,
                             capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


async def _bounded_gather(items: list, worker, *, concurrency: int) -> list:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(item):
        async with sem:
            return await worker(item)

    return await asyncio.gather(*(_one(item) for item in items))


# ── repo discovery (owner/repo mapping over discover_repos()) ───────────

def parse_github_remote(url: str) -> "tuple[str, str] | None":
    """``(owner, repo)`` for a github.com ssh or https remote, ``None`` for
    anything else. Ported from pr_scan.py's function of the same name."""
    text = (url or "").strip()
    if not text:
        return None
    for pattern in (_SSH_RE, _SSH_URL_RE, _HTTPS_RE):
        m = pattern.match(text)
        if m:
            return m.group("owner"), m.group("repo")
    return None


def map_cloned_repos(remotes: dict) -> dict:
    """Pure. ``remotes`` is ``{dirname: origin_url_or_None}``. Dedups by
    ``(owner, repo)``; anything that didn't map to a GitHub ``owner/repo``
    is reported in ``unmapped`` rather than silently dropped. Ported from
    pr_scan.py's function of the same name."""
    repos: list = []
    seen: set = set()
    unmapped: list = []
    for dirname in sorted(remotes):
        url = remotes[dirname]
        parsed = parse_github_remote(url) if url else None
        if parsed is None:
            unmapped.append(dirname)
            continue
        owner, repo = parsed
        if (owner, repo) in seen:
            continue
        seen.add((owner, repo))
        repos.append({"owner": owner, "repo": repo, "path": dirname})
    owners = sorted({r["owner"] for r in repos})
    return {"repos": repos, "owners": owners, "unmapped": unmapped}


def discover_github_repos(*, base_dir: "str | None" = None,
                          git_remote_url: "Callable[[str], str | None]" = _git_remote_url) -> dict:
    """Maps :func:`uncommitted_watchdog.discover_repos`'s repo list — the
    root aw-workspace checkout first, then every ``repos/*`` child — to
    GitHub ``owner/repo`` via each repo's ``origin`` remote. Mapped entries
    carry their local checkout path as ``local_path`` (needed to resolve the
    default branch for the failing-workflow sweep).

    A ``repos/`` dir that can't be listed (missing or unreadable) is carried
    forward as ``error`` rather than collapsing into a clean-looking empty
    scan — the same precedent pr_scan.py's
    ``test_scan_cloned_repos_carries_a_missing_repos_dir_as_an_error_not_a_clean_empty``
    enforces for its own repo-enumeration step.
    """
    base_dir = base_dir or uncommitted_watchdog.workspace_root()
    repos_dir = os.path.join(base_dir, "repos")
    error = None
    try:
        os.listdir(repos_dir)
    except OSError as exc:
        error = f"{repos_dir} unreadable: {exc}"

    entries = uncommitted_watchdog.discover_repos(base_dir)
    remotes: dict = {}
    paths: dict = {}
    for name, path in entries:
        remotes[name] = git_remote_url(path)
        paths[name] = path

    mapped = map_cloned_repos(remotes)
    for repo in mapped["repos"]:
        repo["local_path"] = paths.get(repo["path"])
    mapped["scanned"] = len(entries)
    if error:
        mapped["error"] = error
    return mapped


def filter_prs_to_cloned_repos(prs: list, cloned_full_names: "set[str]") -> list:
    """Pure. Keeps only PRs whose ``repository.nameWithOwner`` is in the
    discovered repo set — the Phase A result is one owner-wide search, so
    this narrows it back down to repos actually checked out here."""
    out = []
    for pr in prs:
        full_name = ((pr.get("repository") or {}).get("nameWithOwner") or "")
        if full_name in cloned_full_names:
            out.append(pr)
    return out


# ── gh calls (PR sweep) ──────────────────────────────────────────────────

async def search_open_prs_for_owner(owner: str, gh_runner: GhRunner) -> "tuple[list | None, str | None]":
    code, out, err = await gh_runner(["search", "prs", "--owner", owner, "--state", "open",
                                      "--sort", "updated", "--order", "asc",
                                      "--limit", str(SEARCH_LIMIT), "--json", SEARCH_FIELDS])
    if code != 0:
        return None, (err or out or f"gh search prs --owner {owner} exited {code}").strip()
    try:
        rows = json.loads(out or "[]")
    except ValueError as exc:
        return None, f"malformed gh search prs output: {exc}"
    return (rows if isinstance(rows, list) else []), None


async def view_pr(url: str, gh_runner: GhRunner) -> "tuple[dict | None, str | None]":
    code, out, err = await gh_runner(["pr", "view", url, "--json", VIEW_FIELDS])
    if code != 0:
        return None, (err or out or f"gh pr view {url} exited {code}").strip()
    try:
        row = json.loads(out or "{}")
    except ValueError as exc:
        return None, f"malformed gh pr view output: {exc}"
    return (row if isinstance(row, dict) else {}), None


# ── surfaced-stamp back-off store ────────────────────────────────────────

def default_store_path() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.expanduser("~/.aw-workspace")
    return os.path.join(home, STORE_REL)


def load_surfaced_store(path: str) -> dict:
    """Fails OPEN on a missing or corrupt file — one wasted re-surface beats
    a PR permanently stranded by a read that can't be trusted."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_surfaced_store(path: str, store: dict) -> None:
    """Atomic write (tmp file in the same dir + ``os.replace``) so a crash
    mid-write never leaves a half-written file for the next read to choke on."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".github_maintenance_surfaced-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def prune_surfaced_store(store: dict, open_pr_keys: "set[str]", *,
                         searched_owners: "set[str] | None" = None) -> dict:
    """Keys no longer in the open-PR set are dropped, keeping the file
    bounded. A key is only eligible for pruning when its OWN owner was
    actually searched this pass (same code-review finding as pr_scan.py,
    PR #41): pruning unconditionally would delete a back-off stamp the
    moment its owner's search call merely fails or rate-limits."""
    if searched_owners is None:
        return {k: v for k, v in store.items() if k in open_pr_keys}
    return {k: v for k, v in store.items()
           if k in open_pr_keys or k.split("/", 1)[0] not in searched_owners}


def surfaced_backoff_state(current_updated_at: "str | None", stamp: "dict | None", now: float, *,
                           ceiling_hours: float = SURFACE_BACKOFF_CEILING_HOURS) -> str:
    """``"eligible"`` | ``"backed_off"``. Stricter than pr_scan.py's
    ``backoff_state``: eligible again not only when the PR has moved
    (``updatedAt`` strictly newer than the stamp) but also once
    ``ceiling_hours`` has passed since it was surfaced — otherwise a
    periodic maintenance tick would never re-learn about a PR that
    deteriorated without a fresh ``updatedAt``. Fails OPEN on an
    unparseable timestamp on either side, or a stamp missing its own
    ``surfaced_at``."""
    if not stamp:
        return "eligible"
    stamped_updated = stamp.get("updated_at")
    surfaced_at = stamp.get("surfaced_at")
    if not stamped_updated or not current_updated_at or surfaced_at is None:
        return "eligible"
    cur = _parse_iso_datetime(current_updated_at)
    stamped = _parse_iso_datetime(stamped_updated)
    if cur is None or stamped is None:
        return "eligible"
    if cur > stamped:
        return "eligible"
    if (now - surfaced_at) / 3600.0 >= ceiling_hours:
        return "eligible"
    return "backed_off"


def _pr_key(full_name: str, number: Any) -> str:
    return f"{full_name}#{number}"


# ── PR attention sweep ───────────────────────────────────────────────────

async def scan_pr_attention(*, now: float, gh_runner: GhRunner, cloned_full_names: "set[str]",
                            owners: list, store: dict, stall_hours: float = DEFAULT_STALL_HOURS,
                            concurrency: int = GH_MAX_CONCURRENCY) -> dict:
    """Two-phase sweep: Phase A one owner-scoped ``gh search prs`` call per
    distinct owner, Phase B ``gh pr view`` enrichment only for non-draft,
    not-backed-off survivors. Mutates ``store`` in place (adds a surfaced
    stamp for every PR newly added to ``attention``); the caller decides
    whether to persist it (``dry_run`` is the caller's concern, not this
    function's)."""
    errors: list = []
    searched_owners: set = set()
    truncated_owners: set = set()
    all_prs: list = []

    async def _search(owner: str) -> None:
        rows, error = await search_open_prs_for_owner(owner, gh_runner)
        if error is not None:
            errors.append({"owner": owner, "error": error})
            return
        searched_owners.add(owner)
        if len(rows) >= SEARCH_LIMIT:
            truncated_owners.add(owner)
        all_prs.extend(rows)

    await _bounded_gather(owners, _search, concurrency=concurrency)
    all_prs = filter_prs_to_cloned_repos(all_prs, cloned_full_names)

    open_pr_keys: set = set()
    probe_batch: list = []

    for pr in all_prs:
        full_name = (pr.get("repository") or {}).get("nameWithOwner") or ""
        key = _pr_key(full_name, pr.get("number"))
        open_pr_keys.add(key)
        if pr.get("isDraft"):
            continue
        if surfaced_backoff_state(pr.get("updatedAt"), store.get(key), now) == "backed_off":
            continue
        probe_batch.append((key, full_name, pr))

    attention: list = []

    async def _probe(item) -> None:
        key, full_name, pr = item
        view, error = await view_pr(pr.get("url"), gh_runner)
        if error is not None:
            errors.append({"owner": full_name.split("/", 1)[0] if "/" in full_name else None,
                           "error": f"gh pr view {pr.get('url')} failed: {error}"})
            return
        if view.get("isDraft"):
            return
        badge, _ci = github_prs.classify_pr(view)
        updated_at = view.get("updatedAt") or pr.get("updatedAt")
        age_hours = None
        at = _parse_iso_datetime(updated_at)
        if at is not None:
            age_hours = round((now - at) / 3600.0, 1)
        stalled = age_hours is not None and age_hours >= stall_hours

        reasons = []
        if badge in ATTENTION_BADGES:
            reasons.append(f"badge:{badge}")
        if stalled:
            reasons.append(f"stalled {age_hours}h (>{stall_hours}h)")
        if not reasons:
            return

        attention.append({
            "repo": full_name, "number": pr.get("number"), "url": pr.get("url"),
            "title": pr.get("title"), "author": (pr.get("author") or {}).get("login"),
            "badge": badge, "updated_at": updated_at, "age_hours": age_hours,
            "attention_reason": "; ".join(reasons),
        })
        store[key] = {"surfaced_at": now, "updated_at": updated_at}

    await _bounded_gather(probe_batch, _probe, concurrency=concurrency)

    return {
        "attention": attention,
        "errors": errors,
        "searched_owners": searched_owners,
        "open_pr_keys": open_pr_keys,
        "counts": {
            "owners": len(owners), "candidates_considered": len(all_prs),
            "probed": len(probe_batch), "attention": len(attention),
            "truncated_owners": sorted(truncated_owners),
        },
    }


# ── failing-workflow sweep ───────────────────────────────────────────────

def _latest_completed_run(runs_newest_first: list) -> "dict | None":
    for run in runs_newest_first:
        if (run.get("status") or "").lower() == "completed":
            return run
    return None


def _consecutive_failures(runs_newest_first: list) -> int:
    count = 0
    for run in runs_newest_first:
        if (run.get("status") or "").lower() != "completed":
            continue
        if (run.get("conclusion") or "").lower() == "failure":
            count += 1
        else:
            break
    return count


def analyze_workflow_runs(runs: list) -> dict:
    """Pure. ``runs`` is the raw ``workflow_runs`` list from one ``gh api
    repos/{owner}/{repo}/actions/runs?branch=<default>`` call — may span
    several workflows, grouped here by ``workflow_id``. A workflow is
    failing when its LATEST completed run on this branch concluded
    ``failure``; a workflow with no completed run yet on this branch (every
    run still in progress) is skipped rather than guessed at. Returns
    ``{workflow_id: {workflow, conclusion, latest_run_url,
    consecutive_failures, is_failing}}``."""
    groups: dict = {}
    for run in runs:
        key = run.get("workflow_id") or run.get("name")
        groups.setdefault(key, []).append(run)

    results: dict = {}
    for key, group_runs in groups.items():
        ordered = sorted(group_runs, key=lambda r: r.get("run_number") or 0, reverse=True)
        latest = _latest_completed_run(ordered)
        if latest is None:
            continue
        conclusion = (latest.get("conclusion") or "").lower()
        results[key] = {
            "workflow": latest.get("name") or str(key),
            "conclusion": conclusion,
            "latest_run_url": latest.get("html_url"),
            "consecutive_failures": _consecutive_failures(ordered),
            "is_failing": conclusion == "failure",
        }
    return results


async def scan_repo_failing_workflows(repo: dict, gh_runner: GhRunner) -> dict:
    """One repo's slice of the failing-workflow sweep. ``repo`` is one of
    :func:`discover_github_repos`'s mapped entries (``owner``/``repo``/
    ``local_path``). Default branch is discovered from the LOCAL checkout
    (``github_prs.discover_default_branch``, a ``git`` call, not ``gh``) —
    unresolved is reported as an error, never silently skipped."""
    owner, repo_name, local_path = repo["owner"], repo["repo"], repo.get("local_path")
    full_name = f"{owner}/{repo_name}"
    default_ref = await asyncio.to_thread(github_prs.discover_default_branch, local_path or "")
    if not default_ref:
        return {"repo": full_name, "error": "could not resolve a default branch", "entries": []}
    branch = default_ref.split("/", 1)[1] if "/" in default_ref else default_ref

    code, out, err = await gh_runner(
        ["api", f"repos/{owner}/{repo_name}/actions/runs?branch={branch}&per_page={RUNS_PER_PAGE}"])
    if code != 0:
        return {"repo": full_name, "error": (err or out or f"gh api exited {code}").strip(), "entries": []}
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        return {"repo": full_name, "error": f"malformed gh api output: {exc}", "entries": []}

    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    analyzed = analyze_workflow_runs(runs or [])
    entries = [
        {"repo": full_name, "workflow": v["workflow"], "default_branch": branch,
         "latest_run_url": v["latest_run_url"], "conclusion": v["conclusion"],
         "consecutive_failures": v["consecutive_failures"]}
        for v in analyzed.values() if v["is_failing"]
    ]
    return {"repo": full_name, "error": None, "entries": entries}


# ── the full sweep ────────────────────────────────────────────────────────

async def scan_maintenance(*, now: float, gh_runner: GhRunner = default_gh_runner,
                           base_dir: "str | None" = None,
                           git_remote_url: "Callable[[str], str | None]" = _git_remote_url,
                           stall_hours: float = DEFAULT_STALL_HOURS,
                           concurrency: int = GH_MAX_CONCURRENCY,
                           store_path: "str | None" = None, dry_run: bool = False) -> dict:
    """The full scan: repo discovery, the PR attention sweep, the
    failing-workflow sweep, and the surfaced-stamp store write (skipped on
    ``dry_run``). Returns ``{prs, failing_flows, counts, errors,
    repos_unmapped}`` — the shape the gate route (next slice) and
    :class:`MaintenanceWatchdog` both read as-is."""
    store_path = store_path or default_store_path()
    discovered = await asyncio.to_thread(discover_github_repos, base_dir=base_dir,
                                         git_remote_url=git_remote_url)
    repos_by_full_name = {f"{r['owner']}/{r['repo']}": r for r in discovered["repos"]}

    errors: list = []
    if discovered.get("error"):
        errors.append({"owner": None, "error": discovered["error"]})

    store = load_surfaced_store(store_path)
    pr_result = await scan_pr_attention(
        now=now, gh_runner=gh_runner, cloned_full_names=set(repos_by_full_name),
        owners=discovered["owners"], store=store, stall_hours=stall_hours, concurrency=concurrency)
    errors.extend(pr_result["errors"])

    if not dry_run:
        pruned = prune_surfaced_store(store, pr_result["open_pr_keys"],
                                      searched_owners=pr_result["searched_owners"])
        save_surfaced_store(store_path, pruned)

    workflow_results = await _bounded_gather(
        list(repos_by_full_name.values()),
        lambda repo: scan_repo_failing_workflows(repo, gh_runner),
        concurrency=concurrency)

    failing_flows: list = []
    for res in workflow_results:
        if res.get("error"):
            errors.append({"owner": res["repo"], "error": res["error"]})
            continue
        failing_flows.extend(res["entries"])

    return {
        "prs": pr_result["attention"],
        "failing_flows": failing_flows,
        "counts": {
            **pr_result["counts"],
            "scanned_repos": discovered.get("scanned", 0),
            "mapped_repos": len(repos_by_full_name),
            "failing_flows": len(failing_flows),
            "truncated": bool(pr_result["counts"]["truncated_owners"]),
        },
        "errors": errors,
        "repos_unmapped": discovered["unmapped"],
    }


class MaintenanceWatchdog:
    """Cached ``ctx.watchdog``-driven poller, PrWatchdog-style, but NOT a
    wrapper around :class:`github_prs.PrWatchdog` — its author-scoped
    discovery is wrong for this sweep (see the module docstring). The next
    slice's gate route reads :meth:`get_cached` and only triggers an inline
    :meth:`poll` when the cache is cold (``last_poll == 0``); a ~90-repo
    sweep cannot answer inside a synchronous HTTP request."""

    def __init__(self, *, base_dir: "str | None" = None, store_path: "str | None" = None,
                 gh_runner: GhRunner = default_gh_runner,
                 git_remote_url: "Callable[[str], str | None]" = _git_remote_url,
                 stall_hours: float = DEFAULT_STALL_HOURS, concurrency: int = GH_MAX_CONCURRENCY):
        self._base_dir = base_dir
        self._store_path = store_path
        self._gh_runner = gh_runner
        self._git_remote_url = git_remote_url
        self._stall_hours = stall_hours
        self._concurrency = concurrency
        self._cache: dict = {
            "prs": [], "failing_flows": [], "counts": {}, "errors": [],
            "last_poll": 0, "error": None,
        }

    def get_cached(self) -> dict:
        return dict(self._cache)

    async def poll(self, now: "float | None" = None) -> None:
        now = now if now is not None else time.time()
        try:
            result = await scan_maintenance(
                now=now, gh_runner=self._gh_runner, base_dir=self._base_dir,
                git_remote_url=self._git_remote_url,
                stall_hours=self._stall_hours, concurrency=self._concurrency,
                store_path=self._store_path)
            self._cache["prs"] = result["prs"]
            self._cache["failing_flows"] = result["failing_flows"]
            self._cache["counts"] = result["counts"]
            self._cache["errors"] = result["errors"]
            self._cache["error"] = ("; ".join(e["error"] for e in result["errors"])
                                    if result["errors"] else None)
        except Exception as e:  # the watchdog task itself must never die
            log.warning("github maintenance scan failed: %s", e)
            self._cache["error"] = str(e)
        self._cache["last_poll"] = now


__all__ = [
    "GhRunner", "default_gh_runner", "parse_github_remote", "map_cloned_repos",
    "discover_github_repos", "filter_prs_to_cloned_repos", "search_open_prs_for_owner",
    "view_pr", "surfaced_backoff_state", "load_surfaced_store", "save_surfaced_store",
    "prune_surfaced_store", "default_store_path", "scan_pr_attention",
    "analyze_workflow_runs", "scan_repo_failing_workflows", "scan_maintenance",
    "MaintenanceWatchdog", "DEFAULT_STALL_HOURS", "SURFACE_BACKOFF_CEILING_HOURS",
    "DEFAULT_POLL_INTERVAL_S", "ATTENTION_BADGES",
]
