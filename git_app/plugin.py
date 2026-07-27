"""
Entrypoint referenced by aw-app.json's runtime.entrypoint
("git_app.plugin:GitAppPlugin"). Framework runtime (F1) isn't built yet —
this is ready to plug in once it is; see _plugin_stub.py for what's
substituted in the meantime.
"""

from __future__ import annotations

import logging

from . import gh_auth, installer
from ._plugin_stub import AppContext, Plugin

log = logging.getLogger("aw_apps.git")


class GitAppPlugin(Plugin):
    async def activate(self, ctx: AppContext) -> None:
        """
        Installs git + gh (idempotent — also runs on every reconcile pass
        after workspace recreation, per Decision 5's reconciler). If a
        `github_token` secret is already granted/present, logs gh in too.
        """
        versions = installer.install_all()
        log.info("aw-app-git activated: %s", versions)

        token = getattr(ctx, "get_secret", None)
        if callable(token):
            secret_value = await ctx.get_secret("github_token")  # type: ignore[attr-defined]
            if secret_value:
                gh_auth.login_with_token(secret_value)
                log.info("gh auth: logged in via stored token")

    async def deactivate(self) -> None:
        installer.uninstall_all()
        log.info("aw-app-git deactivated")
