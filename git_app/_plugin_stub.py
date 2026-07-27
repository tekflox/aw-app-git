"""
Local stand-in for `aw_workspace.apps.Plugin` (Decision 3 of the Decoupled
Apps Framework ADR — docs/knowledge_base/docs/architecture/decoupled-apps-framework.md).
The real runtime (Phase 1) doesn't exist yet, so this app can't actually
be loaded by aw-workspace today. This stub mirrors the documented
activate(ctx)/deactivate() contract so git_app.plugin.GitAppPlugin is
already shaped correctly to drop in once Phase 1 ships — just delete this
file and import from aw_workspace.apps instead.
"""

from __future__ import annotations


class AppContext:
    """Stand-in for the real capability-gated AppContext facade."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir


class Plugin:
    async def activate(self, ctx: AppContext) -> None:
        raise NotImplementedError

    async def deactivate(self) -> None:
        raise NotImplementedError
