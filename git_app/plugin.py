"""
Entrypoint referenced by aw-app.json's runtime.entrypoint
("git_app.plugin:GitAppPlugin").

Plugs into the real F4 framework runtime and uses the gated ``ctx`` facades
rather than raw shell:

* ``ctx.commands`` (``commands:install``) — install git + gh THROUGH the facade
  (journaled; reverted on uninstall via scripts/uninstall.sh).
* ``ctx.secrets`` (``secrets:own``) — the gh token lives in the workspace-side
  secure store; the app reads it on activate to log gh in, and the settings
  route writes it (the settings *UI* is a later frontend phase).
* ``ctx.routes`` (``routes:register``) — a small settings sub-app to set the
  token + read gh auth status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time

# A local name, not ``time.time`` itself — monkeypatching the real stdlib
# ``time`` module (as a naive ``monkeypatch.setattr(plugin.time, "time",
# ...)`` would) mutates it process-wide, which also feeds httpx/cookiejar's
# OWN internal `time.time()` calls inside a TestClient request and starves
# a test's fake clock sequence. Patching this name only affects this module.
_now_s = time.time

# Module level, unlike the other fastapi imports (which stay local to
# _build_routes): this file uses `from __future__ import annotations`, so a
# handler's annotations are strings FastAPI resolves against the MODULE's
# globals. A WebSocket imported inside the function isn't there, and the socket
# param gets mistaken for a required query field (close 1008 on connect).
from fastapi import WebSocket, WebSocketDisconnect

from . import device_flow, gh_auth, github_maintenance, github_prs, uncommitted_watchdog

log = logging.getLogger("aw_apps.git")

# aw-app-git's own public OAuth App (Device Flow enabled, no client_secret
# needed) — must match config_schema.oauth_client_id's "default" in
# aw-app.json so "Sign in with GitHub" works with zero configuration. The
# framework doesn't merge config_schema defaults into ctx.config on install,
# so this fallback is what actually makes that true for a fresh install.
DEFAULT_OAUTH_CLIENT_ID = "Ov23liWR67ZgY2P6fYKh"


def _watchdog_enabled(ctx) -> bool:
    raw = ctx.secrets.read("watchdog_enabled")
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("false", "0", "")


def _watchdog_interval_s(ctx) -> float:
    raw = ctx.secrets.read("watchdog_interval_s")
    try:
        interval = float(raw) if raw else uncommitted_watchdog.DEFAULT_INTERVAL_S
    except (TypeError, ValueError):
        interval = uncommitted_watchdog.DEFAULT_INTERVAL_S
    return max(interval, uncommitted_watchdog.MIN_INTERVAL_S)


def _github_team(ctx) -> list[str]:
    """The PR-dashboard's team list (team logins whose PRs are also shown).

    Stored as JSON in ``ctx.secrets`` (app-writable) with install-time
    ``ctx.config`` as fallback, same precedence as :func:`_oauth_client_id`. A
    comma-separated string is accepted too — that's what a plain settings text
    field posts.
    """
    raw = ctx.secrets.read("github_team")
    if raw is None:
        raw = (getattr(ctx, "config", {}) or {}).get("github_team")
    if isinstance(raw, list):
        return [str(m).strip() for m in raw if str(m).strip()]
    if not raw:
        return []
    text = str(raw).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(m).strip() for m in parsed if str(m).strip()]
        except json.JSONDecodeError:
            log.warning("github_team is not valid JSON — falling back to comma-split")
    return [m.strip() for m in text.split(",") if m.strip()]


def _github_cfg(ctx) -> dict:
    """The ``github`` config block the ported dashboard expects — same keys the
    monolith read out of aw.json (``team``/``host``/``poll_interval``)."""
    host = (
        ctx.secrets.read("github_host")
        or (getattr(ctx, "config", {}) or {}).get("github_host")
        or ""
    ).strip()
    return {
        "team": _github_team(ctx),
        "host": host or None,
        "poll_interval": _github_poll_interval_s(ctx),
    }


def _github_poll_interval_s(ctx) -> float:
    raw = ctx.secrets.read("github_poll_interval_s")
    if raw is None:
        raw = (getattr(ctx, "config", {}) or {}).get("github_poll_interval_s")
    try:
        interval = float(raw) if raw else github_prs.DEFAULT_POLL_INTERVAL
    except (TypeError, ValueError):
        interval = github_prs.DEFAULT_POLL_INTERVAL
    return max(interval, 60.0)


def _oauth_client_id(ctx) -> str:
    """Public OAuth App client_id (device flow needs no client_secret).

    A custom id saved via the Advanced settings form is persisted through
    ``ctx.secrets`` (the only facade this app has for writing anything that
    survives a restart — ``ctx.config`` is set once at install time and isn't
    writable by app routes). That takes precedence over install-time config,
    then the env var, then the baked-in default — so a value saved via
    Advanced settings, or set for the whole container, still overrides the
    app's own OAuth App.
    """
    return (
        ctx.secrets.read("oauth_client_id")
        or (getattr(ctx, "config", {}) or {}).get("oauth_client_id")
        or os.environ.get("AW_APP_GIT_OAUTH_CLIENT_ID")
        or DEFAULT_OAUTH_CLIENT_ID
    ).strip()


def _resolve_repo_path(value: str) -> str | None:
    """Map any of the forms the Repos nav sends to an absolute git repo path.

    Mirrors aw-app-diff-tool's ``diff_app/repo_paths.py::resolve_repo_path``
    (itself ported from the monolith's ``git_ops.py``) — same normalization,
    pointed at this app's own ``uncommitted_watchdog.workspace_root()``.

    Accepts:
      - absolute path  -> used as-is if it's a git repo
      - bare workspace name ("aw-workspace") -> workspace root
      - bare child name ("vpn", "resume") -> <root>/repos/<name>
      - relative path -> resolved against the workspace root

    Returns the absolute path if it points to a git repo, otherwise None.
    """
    if not value:
        return None
    base_dir = uncommitted_watchdog.workspace_root()
    if os.path.isabs(value):
        return value if os.path.isdir(os.path.join(value, ".git")) else None
    if value == os.path.basename(os.path.normpath(base_dir)):
        candidate = base_dir
        if os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    if "/" not in value:
        candidate = os.path.join(base_dir, "repos", value)
        if os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
    candidate = os.path.normpath(os.path.join(base_dir, value))
    if os.path.isdir(os.path.join(candidate, ".git")):
        return candidate
    return None


def _run_git_steps(repo_path: str, steps: list[tuple[list[str], str]]) -> dict:
    """Runs ``steps`` (``[(argv, error_label), ...]``) in ``repo_path``,
    stopping at the first failure. Shared by ``/pull`` (fetch + pull) and
    ``/push`` (a single step) so both report the same shape."""
    output = ""
    for argv, label in steps:
        result = subprocess.run(
            argv, capture_output=True, text=True, cwd=repo_path, timeout=60,
        )
        if result.returncode != 0:
            return {"success": False, "error": f"{label}: {result.stderr.strip()}",
                    "output": result.stdout.strip()}
        output = result.stdout.strip()
    return {"success": True, "output": output or "Already up to date."}


class GitAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        for cli in manifest.get("contributes", {}).get("system_clis", []):
            # `verify` from the manifest decides what "installed" means for
            # this CLI. git needs one: a binary shipped without its remote
            # helpers is on PATH and prints a version while every https://
            # operation fails, which is how a broken git survived unnoticed
            # long enough to take nvm/node/yarn/pnpm down with it (2026-08-12).
            ctx.commands.install_system_cli(
                cli["name"], cli["installer"], uninstall="scripts/uninstall.sh",
                verify=cli.get("verify"),
            )
        log.info("aw-app-git activated: git + gh installed")

        # If a token is already stored (secrets:own), log gh in now — this also
        # runs on every reconcile pass after workspace recreation.
        token = ctx.secrets.read("github_token")
        if token:
            try:
                gh_auth.login_with_token(token)
                log.info("gh auth: logged in via stored token")
            except gh_auth.GhAuthError as e:
                log.warning("gh auth: stored token did not log in: %s", e)

        ctx.routes.register(self._build_routes(ctx))

        if ctx.has("watchdog:tasks") and ctx.has("notifications:send"):
            watchdog = uncommitted_watchdog.UncommittedWatchdog(ctx.notify)

            async def _tick() -> None:
                if _watchdog_enabled(ctx):
                    await watchdog.tick()

            ctx.watchdog.register(
                "uncommitted", _tick,
                lambda: _watchdog_interval_s(ctx),
                run_immediately=False,
            )
            log.info("aw-app-git: uncommitted-changes watchdog registered")

        if ctx.has("watchdog:tasks"):
            # The PR dashboard's poller. The monolith ran its own `while True`
            # loop; here the framework owns the cadence (and cancels it on
            # uninstall). Not immediate: activate() runs during boot reconcile,
            # and a first poll shells out to `gh` several times.
            ctx.watchdog.register(
                "github", self._pr_watchdog(ctx).poll,
                lambda: _github_poll_interval_s(ctx),
                run_immediately=False,
            )
            log.info("aw-app-git: GitHub PR watchdog registered")

        if ctx.has("watchdog:tasks"):
            # The GitHub Maintenance Plan's scan layer (card 4185bf3b-9510-8121)
            # — owner-scoped PR attention + default-branch failing-workflow
            # sweep, cached the same way as the "github" watchdog above. No
            # route reads this cache yet; that's the next slice.
            ctx.watchdog.register(
                "github_maintenance", self._maintenance_watchdog(ctx).poll,
                lambda: github_maintenance.DEFAULT_POLL_INTERVAL_S,
                run_immediately=False,
            )
            log.info("aw-app-git: GitHub maintenance watchdog registered")

    async def deactivate(self) -> None:
        # git + gh removal is driven by the framework's journal reverse-replay
        # (scripts/uninstall.sh); the secret namespace is purged by the runtime.
        log.info("aw-app-git deactivated")

    def _pr_watchdog(self, ctx):
        """The PR dashboard's poller — ONE instance shared by the routes and the
        registered watchdog task (they read/refresh the same cache). Built
        lazily so ``_build_routes`` also works standalone, without activate()."""
        existing = getattr(self, "_prs", None)
        if existing is not None:
            return existing
        try:
            notify = ctx.notify if ctx.has("notifications:send") else None
        except AttributeError:
            notify = None  # a ctx without the facade (tests) — no notifications

        self._prs = github_prs.PrWatchdog(lambda: _github_cfg(ctx), notify=notify)
        return self._prs

    def _maintenance_watchdog(self, ctx):
        """The GitHub Maintenance scan's poller — same lazy-singleton shape
        as :meth:`_pr_watchdog`, built fresh (not wrapping ``PrWatchdog``:
        its ``@me``-authored discovery is wrong for maintenance)."""
        existing = getattr(self, "_maintenance", None)
        if existing is not None:
            return existing
        self._maintenance = github_maintenance.MaintenanceWatchdog()
        return self._maintenance

    def _build_routes(self, ctx):
        from fastapi import Body, FastAPI, Query
        from fastapi.responses import JSONResponse

        api = FastAPI()

        # ---- Repo pull/push (Workspace > Repos nav) ----
        # Ported from the monolith's src/api/routes/git_ops.py::pull, plus a
        # new push, moved here (instead of aw-workspace core) so they hot-reload
        # with this app and need no core restart. Auth is whatever `git`/`gh`
        # already have configured globally: gh_auth.login_with_token() runs
        # `gh auth setup-git` on sign-in, which wires plain `git push`/`pull`
        # through gh's own credential helper — nothing new to configure here.

        async def _git_op(data: dict, steps: list[tuple[list[str], str]]):
            repo_path = _resolve_repo_path(data.get("repo", ""))
            if not repo_path:
                return JSONResponse(
                    {"success": False, "error": f"Not a git repo: {data.get('repo', '')}"},
                    status_code=400,
                )
            loop = asyncio.get_event_loop()
            try:
                return await loop.run_in_executor(None, _run_git_steps, repo_path, steps)
            except subprocess.TimeoutExpired:
                return JSONResponse({"success": False, "error": "Git operation timed out"}, status_code=504)
            except Exception as e:
                return JSONResponse({"success": False, "error": str(e)}, status_code=500)

        @api.post("/pull")
        async def git_pull(data: dict = Body(...)):
            return await _git_op(data, [
                (["git", "fetch", "--all", "--prune"], "git fetch failed"),
                (["git", "pull"], "git pull failed"),
            ])

        @api.post("/push")
        async def git_push(data: dict = Body(...)):
            return await _git_op(data, [(["git", "push"], "git push failed")])

        # ---- GitHub PR dashboard (ported from the monolith's /api/github/*) ----
        # Same payloads, same semantics; only the prefix moves, from the core's
        # /api/github/... to this app's own /api/apps/git/github/... mount.
        prs = self._pr_watchdog(ctx)

        @api.get("/github/prs")
        async def get_prs():
            """Return cached PR data."""
            cached = prs.get_cached()
            if not cached["last_poll"]:
                await prs.poll()
                cached = prs.get_cached()
            return cached

        @api.post("/github/refresh")
        async def refresh_prs():
            """Force a fresh poll."""
            await prs.poll()
            return prs.get_cached()

        # ---- GitHub Maintenance gate (card 4185bf3b-9510-81c5, slice 2 of
        # the GitHub Maintenance Plan) ----
        # Thin sync wrapper over MaintenanceWatchdog's cache (slice 1,
        # PR#6) — no new scan logic here. Precedent: aw-app-notion's
        # GET /kanban/cards-need-attention.
        maintenance = self._maintenance_watchdog(ctx)

        def _ping_maintenance(failing_flows: list) -> "dict | None":
            """The reping pattern: pure decision logic lives in
            ``github_maintenance.plan_maintenance_pings`` (testable without
            ``ctx`` or a filesystem); this does the I/O that function can't
            — notifying and persisting the SET/CLEAR + daily-summary state.
            Called ONLY when ``ping=true`` (see ``get_maintenance`` below)
            — a plain GET never reaches this function.

            Any failure here (the notify backend down, a state-file write
            error) is caught and reported as ``None`` rather than raised —
            code-review finding (PR #7 round 2): this is a best-effort
            side-channel on top of an already-successfully-computed
            has_work/prs/counts response, and must never turn that healthy
            read into a 500 the way the route's own deliberate 502 (``cached
            .get("error")``) is reserved for an actually broken scan.

            The lock guards ONLY load → decide → write (card
            4185bf3b-9510-816d): ``notify`` fires AFTER it's released, so a
            slow/blocking notify backend stalls just this one call instead
            of serializing every concurrent ``ping=true`` caller behind the
            same ``fcntl.flock``. This doesn't reopen the SET/CLEAR race the
            lock exists for — by the time any other caller can acquire the
            lock, this call's ``save_surfaced_store`` has already persisted
            the decided state, so a concurrent decide always sees it.

            Retry-on-notify-failure (code-review finding, this card, round
            1): committing the decided state BEFORE notify is attempted
            would otherwise mean a notify that *raises* (not just one
            that's slow — e.g. the backend's Postgres write failing
            outright) gets permanently treated as delivered, with no retry
            on the next poll. Each notify call below is caught
            individually and, if it raised, its flow key (or the summary
            stamp) is rolled back in a SECOND, short lock acquisition —
            no slow I/O inside it — so the next ``ping=true`` call decides
            that flow as "new" again instead of silently dropping it. The
            rollback itself can't reopen the SET/CLEAR race: any other
            caller that decided concurrently already saw this flow as
            tracked from this call's first save, so no one double-fires
            for it before the rollback runs."""
            state_path = github_maintenance.default_ping_state_path()
            try:
                with github_maintenance.locked_store(state_path):
                    state = github_maintenance.load_surfaced_store(state_path)
                    now_ts = _now_s()
                    plan = github_maintenance.plan_maintenance_pings(failing_flows, state, now_ts)
                    github_maintenance.save_surfaced_store(state_path, plan["state"])

                try:
                    notify = ctx.notify if ctx.has("notifications:send") else None
                except AttributeError:
                    notify = None  # a ctx without the facade (tests) — no notifications

                failed_keys = []
                summary_failed = False
                if notify is not None:
                    for flow in plan["new_failures"]:
                        flow_key = github_maintenance.flow_key(flow)
                        try:
                            # external_id folds in the SET's own timestamp —
                            # code-review finding (PR #7 round 1/2 HIGH): the
                            # real NotificationDB.has_notification dedups on
                            # the bare (source, external_id, external_status)
                            # triple FOREVER (its only purge is a one-time
                            # 7-day cleanup at process boot), so a constant
                            # id per flow key would silently swallow every
                            # notify after the first real occurrence —
                            # defeating the whole SET/CLEAR/re-notify design
                            # this route exists to ship.
                            notify(
                                message=f"{flow.get('workflow')} failing on {flow.get('repo')} "
                                        f"({flow.get('consecutive_failures', 0)} consecutive run(s))",
                                level="error",
                                title="GitHub workflow failing",
                                url=flow.get("latest_run_url") or "",
                                external_id=f"ci:{flow_key}:{int(now_ts)}",
                                external_status="failing",
                            )
                        except Exception:
                            log.exception("github maintenance ping: instant notify failed for %s", flow_key)
                            failed_keys.append(flow_key)
                    if plan["summary_due"]:
                        names = ", ".join(github_maintenance.flow_key(f) for f in failing_flows[:10])
                        try:
                            notify(
                                message=f"{len(failing_flows)} failing workflow(s): {names}",
                                level="warning",
                                title="GitHub Maintenance — daily summary",
                                external_status="summary",
                            )
                        except Exception:
                            log.exception("github maintenance ping: daily summary notify failed")
                            summary_failed = True

                if failed_keys or summary_failed:
                    with github_maintenance.locked_store(state_path):
                        current = github_maintenance.load_surfaced_store(state_path)
                        failing = dict(current.get("failing") or {})
                        for flow_key in failed_keys:
                            failing.pop(flow_key, None)
                        current["failing"] = failing
                        if summary_failed:
                            current["last_summary_at"] = state.get("last_summary_at")
                        github_maintenance.save_surfaced_store(state_path, current)
            except Exception:
                log.exception("github maintenance ping (notify/persist) failed")
                return None
            return {
                "instant_sent": [github_maintenance.flow_key(f) for f in plan["new_failures"]
                                  if github_maintenance.flow_key(f) not in failed_keys],
                "daily_summary_sent": bool(plan["summary_due"]) and not summary_failed,
            }

        @api.get("/github/maintenance")
        async def get_maintenance(limit: int = Query(20, ge=0), ping: bool = False):
            """Sync gate on the GitHub Maintenance scan cache. ``has_work``
            = any attention PR OR any failing flow. Non-2xx when the scan
            layer itself is broken (a top-level ``scan_maintenance``
            failure, not a per-owner/per-repo error already reported
            in-band via ``errors``/``counts``) — so the dag Workflow's http
            gate node can tell "no work" from "broken" instead of reading a
            failed scan as a healthy, quiet one.

            Cold-cache fallback: a fresh deploy/restart has never polled
            (``last_poll == 0``) — poll inline once rather than ever
            answering ``has_work=false`` before a single real scan ran.

            ``ping`` defaults to False, same invariant as cards-need-
            attention's own ``reping``: a plain GET stays a pure read. Only
            ``ping=true`` (the Workflow's gate node) fires the instant/daily
            notifications, synchronously, inside this handler.
            """
            cached = maintenance.get_cached()
            if not cached["last_poll"]:
                await maintenance.poll()
                cached = maintenance.get_cached()

            if cached.get("error"):
                return JSONResponse({"has_work": False, "error": cached["error"]}, status_code=502)

            prs_list = cached.get("prs") or []
            failing_flows = cached.get("failing_flows") or []
            counts = cached.get("counts") or {}

            result = {
                "has_work": bool(prs_list) or bool(failing_flows),
                "prs": prs_list[:limit],
                "failing_flows": failing_flows[:limit],
                "counts": {
                    "prs": len(prs_list),
                    "failing_flows": len(failing_flows),
                    "scanned_repos": counts.get("scanned_repos", 0),
                    "truncated": bool(counts.get("truncated")),
                },
            }
            if ping:
                pings = _ping_maintenance(failing_flows)
                if pings is not None:
                    result["pings"] = pings
            return result

        @api.websocket("/github/stream")
        async def github_stream(websocket: WebSocket):
            """WebSocket endpoint — sends cached data on connect, then live
            updates. The monolith served this at /ws/github; an app's sockets
            live under its own mount (identity-guarded there like every other
            app route — see AppRuntime's guard)."""
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "github_init", **prs.get_cached()}))
            prs.add_listener(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass  # client closed the github stream
            finally:
                prs.remove_listener(websocket)

        # In-memory state for the one device-flow login in flight at a time
        # (single-user workspace — no need to key this by session/user).
        pending: dict = {}

        @api.post("/device/start")
        async def device_start():
            """Kicks off the OAuth Device Flow: returns the user_code + verification
            URL for the settings UI to show, and stashes device_code server-side
            so the poll step below needs no client-supplied state."""
            client_id = _oauth_client_id(ctx)
            if not client_id:
                return {
                    "error": "not_configured",
                    "message": (
                        "Configure the OAuth App client_id (config oauth_client_id or "
                        "env AW_APP_GIT_OAUTH_CLIENT_ID) before using Sign in with GitHub."
                    ),
                }
            try:
                result = device_flow.start(client_id)
            except device_flow.DeviceFlowError as e:
                return {"error": "device_flow_error", "message": str(e)}
            pending.clear()
            pending.update(
                client_id=client_id,
                device_code=result["device_code"],
                interval=result.get("interval", 5),
            )
            return {
                "user_code": result["user_code"],
                "verification_uri": result.get("verification_uri")
                or result.get("verification_uri_complete"),
                "expires_in": result.get("expires_in"),
                "interval": pending["interval"],
            }

        @api.post("/device/poll")
        async def device_poll():
            """One poll attempt against the device_code from the last /device/start.
            On success: stores the token (secrets:own) and logs gh in, same as the
            token-paste path."""
            if not pending.get("device_code"):
                return {
                    "status": "error",
                    "message": "no device-code login in progress — click Sign in with GitHub first",
                }
            try:
                result = device_flow.poll(pending["client_id"], pending["device_code"])
            except device_flow.DeviceFlowError as e:
                pending.clear()
                return {"status": "error", "message": str(e)}

            status = result["status"]
            if status == "slow_down":
                pending["interval"] = result.get("interval", pending["interval"] + 5)
                return {"status": "slow_down", "interval": pending["interval"]}
            if status == "authorization_pending":
                return {"status": "authorization_pending", "interval": pending["interval"]}
            if status in ("expired_token", "access_denied"):
                pending.clear()
                return {"status": status}
            if status == "success":
                token = result["access_token"]
                pending.clear()
                ctx.secrets.write("github_token", token)
                try:
                    gh_auth.login_with_token(token)
                    # One button does it all: also seed git's commit identity
                    # (user.name/user.email) from the account just signed in.
                    identity = gh_auth.configure_git_identity_from_account()
                    return {"status": "success", "logged_in": True, "git_identity": identity}
                except gh_auth.GhAuthError as e:
                    return {"status": "success", "logged_in": False, "error": str(e)}
            return {"status": status}

        @api.post("/settings/token")
        async def set_token(data: dict = Body(...)):
            """Store the gh token (secrets:own) and log gh in with it."""
            token = data.get("github_token", "")
            if not token:
                return {"error": "github_token is required"}
            ctx.secrets.write("github_token", token)
            try:
                gh_auth.login_with_token(token)
                identity = gh_auth.configure_git_identity_from_account()
                return {"ok": True, "logged_in": True, "git_identity": identity}
            except gh_auth.GhAuthError as e:
                return {"ok": True, "logged_in": False, "error": str(e)}

        @api.post("/settings")
        async def save_settings(data: dict = Body(...)):
            """Generic config-window submit (the framework's Apps view, and the
            windows/main.json Advanced form, post here). Routes the
            ``x-secret`` token to the secret store, applies git identity, logs
            gh in, and persists a custom ``oauth_client_id`` if given. Fields
            are all optional so a partial save (e.g. only the token) works."""
            result: dict = {"ok": True}
            if "watchdog_enabled" in data:
                ctx.secrets.write("watchdog_enabled", str(bool(data["watchdog_enabled"])))
                result["watchdog_enabled"] = bool(data["watchdog_enabled"])
            if data.get("watchdog_interval_s"):
                try:
                    ctx.secrets.write("watchdog_interval_s", str(float(data["watchdog_interval_s"])))
                    result["watchdog_interval_s"] = float(data["watchdog_interval_s"])
                except (TypeError, ValueError):
                    result["watchdog_interval_s_error"] = "must be a number"
            if "github_team" in data:
                team = data["github_team"]
                if not isinstance(team, list):
                    team = [m.strip() for m in str(team).split(",") if m.strip()]
                ctx.secrets.write("github_team", json.dumps(team))
                result["github_team"] = team
            if "github_host" in data:
                ctx.secrets.write("github_host", (data.get("github_host") or "").strip())
                result["github_host"] = (data.get("github_host") or "").strip()
            if data.get("github_poll_interval_s"):
                try:
                    ctx.secrets.write(
                        "github_poll_interval_s", str(float(data["github_poll_interval_s"])))
                    result["github_poll_interval_s"] = float(data["github_poll_interval_s"])
                except (TypeError, ValueError):
                    result["github_poll_interval_s_error"] = "must be a number"
            oauth_client_id = (data.get("oauth_client_id") or "").strip()
            if oauth_client_id:
                ctx.secrets.write("oauth_client_id", oauth_client_id)
                result["oauth_client_id"] = "saved"
            token = data.get("github_token", "")
            if token:
                ctx.secrets.write("github_token", token)
                try:
                    gh_auth.login_with_token(token)
                    result["logged_in"] = True
                    # Parity with the device-flow path (/device/poll): either
                    # way of signing in should leave a usable git identity, so
                    # the user never has to know which one they used. Only
                    # fills fields that are empty — an identity set on purpose
                    # is never clobbered.
                    result["git_identity"] = gh_auth.configure_git_identity_from_account()
                except gh_auth.GhAuthError as e:
                    result["logged_in"] = False
                    result["error"] = str(e)
            name = (data.get("git_user_name") or "").strip()
            email = (data.get("git_user_email") or "").strip()
            if name or email:
                import subprocess
                if name:
                    subprocess.run(["git", "config", "--global", "user.name", name], check=False)
                if email:
                    subprocess.run(["git", "config", "--global", "user.email", email], check=False)
                result["identity"] = {"name": name, "email": email}
            return result

        @api.get("/status")
        async def status():
            # Authoritative check via `gh api user` (see gh_auth.whoami) instead
            # of parsing `gh auth status`'s human-readable text — that text's
            # exit-code/format proved inconsistent across environments and
            # produced a logged_in:true response even when its own message said
            # "You are not logged into any GitHub hosts."
            has_token = "github_token" in ctx.secrets.keys()
            user = gh_auth.whoami()
            if not user:
                return {
                    "has_token": has_token,
                    "gh_auth_status": "not logged in",
                    "logged_in": False,
                    "username": None,
                    "token_masked": "—",
                    "token_kind": "—",
                    "scopes_text": "—",
                }

            # A secret input renders blank whether or not something is saved,
            # which reads as "nothing here". Report a masked fingerprint and
            # the ACTUAL granted scopes so the panel can show which
            # credential is in place and what it can do.
            info = gh_auth.token_info()
            missing = info.get("missing_scopes") or []
            scopes = info.get("scopes") or []
            scopes_text = ", ".join(scopes) if scopes else "unknown"
            if missing:
                scopes_text += f"  ⚠️ missing: {', '.join(missing)}"
            identity = gh_auth.current_git_identity()
            return {
                "has_token": has_token,
                "gh_auth_status": f"Logged in to github.com account {user.get('login')}",
                "logged_in": True,
                "username": user.get("login"),
                "token_masked": info.get("masked") or "—",
                "token_kind": info.get("kind") or "—",
                "scopes": scopes,
                "missing_scopes": missing,
                "scopes_text": scopes_text,
                "git_user_name": identity.get("user.name") or "—",
                "git_user_email": identity.get("user.email") or "—",
            }

        @api.post("/logout")
        async def logout():
            """Logs gh out and drops the stored token — used by the settings
            window's Logout button once the user is signed in."""
            try:
                gh_auth.logout()
            except gh_auth.GhAuthError as e:
                return {"ok": False, "error": str(e)}
            ctx.secrets.delete("github_token")
            return {"ok": True}

        return api
