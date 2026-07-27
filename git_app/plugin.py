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

from . import gh_auth

log = logging.getLogger("aw_apps.git")


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

        @api.get("/status")
        async def status():
            has_token = "github_token" in ctx.secrets.keys()
            try:
                auth = gh_auth.status()
            except gh_auth.GhAuthError as e:
                auth = str(e)
            return {"has_token": has_token, "gh_auth_status": auth}

        return api
