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

import json
import logging
import os

# Module level, unlike the other fastapi imports (which stay local to
# _build_routes): this file uses `from __future__ import annotations`, so a
# handler's annotations are strings FastAPI resolves against the MODULE's
# globals. A WebSocket imported inside the function isn't there, and the socket
# param gets mistaken for a required query field (close 1008 on connect).
from fastapi import WebSocket, WebSocketDisconnect

from . import device_flow, gh_auth, github_prs, uncommitted_watchdog

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
    """The PR-dashboard's team list (buddy logins whose PRs are also shown).

    Stored as JSON in ``ctx.secrets`` (app-writable) with install-time
    ``ctx.config`` as fallback, same precedence as :func:`_oauth_client_id`. A
    comma-separated string is accepted too — that's what a plain settings text
    field posts, and what auto-discovery used to write into the monolith's
    aw.json.
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


class GitAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        for cli in manifest.get("contributes", {}).get("system_clis", []):
            ctx.commands.install_system_cli(
                cli["name"], cli["installer"], uninstall="scripts/uninstall.sh"
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

    def _build_routes(self, ctx):
        from fastapi import Body, FastAPI

        api = FastAPI()

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
