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

from . import device_flow, gh_auth

log = logging.getLogger("aw_apps.git")


def _oauth_client_id(ctx) -> str:
    """Public OAuth App client_id (device flow needs no client_secret).

    Config takes precedence over env so a value saved via the settings
    window overrides the container-wide default.
    """
    return (
        (getattr(ctx, "config", {}) or {}).get("oauth_client_id")
        or os.environ.get("AW_APP_GIT_OAUTH_CLIENT_ID")
        or ""
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

    async def deactivate(self) -> None:
        # git + gh removal is driven by the framework's journal reverse-replay
        # (scripts/uninstall.sh); the secret namespace is purged by the runtime.
        log.info("aw-app-git deactivated")

    def _build_routes(self, ctx):
        from fastapi import Body, FastAPI

        api = FastAPI()

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
                        "Configure o OAuth App client_id (config oauth_client_id ou "
                        "env AW_APP_GIT_OAUTH_CLIENT_ID) antes de usar o Sign in with GitHub."
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
                    return {"status": "success", "logged_in": True}
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
                return {"ok": True, "logged_in": True}
            except gh_auth.GhAuthError as e:
                return {"ok": True, "logged_in": False, "error": str(e)}

        @api.post("/settings")
        async def save_settings(data: dict = Body(...)):
            """Generic config-window submit (the framework's Apps view posts the
            whole ``config_schema`` object here). Routes the ``x-secret`` token to
            the secret store, applies git identity, and logs gh in. Fields are all
            optional so a partial save (e.g. only the token) works."""
            result: dict = {"ok": True}
            token = data.get("github_token", "")
            if token:
                ctx.secrets.write("github_token", token)
                try:
                    gh_auth.login_with_token(token)
                    result["logged_in"] = True
                except gh_auth.GhAuthError as e:
                    result["logged_in"] = False
                    result["error"] = str(e)
            elif data.get("auth_method") == "web":
                try:
                    result["web_login"] = gh_auth.login_web()
                except gh_auth.GhAuthError as e:
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
            has_token = "github_token" in ctx.secrets.keys()
            try:
                auth = gh_auth.status()
            except gh_auth.GhAuthError as e:
                auth = str(e)
            return {"has_token": has_token, "gh_auth_status": auth}

        return api
