# aw-app-git

First real decoupled app for aw-workspace, per the
[Decoupled Apps Framework ADR](../../docs/knowledge_base/docs/architecture/decoupled-apps-framework.md)
(`aw-app.json` manifest schema v1). Installs `git` + the GitHub CLI (`gh`)
into the workspace and provides a settings panel for logging into `gh`
(token or web flow — the token is a secret, routed to the zero-knowledge
secret store, never stored in plain config).

## Status

**Plugged into the real framework (F4).** `GitAppPlugin.activate(ctx)` now
drives the gated `ctx` facades — `ctx.commands.install_system_cli(...)`
installs git + gh (journaled, reverted on uninstall), `ctx.secrets` stores/
reads the gh token via the workspace-side secure store, and `ctx.routes`
mounts a small settings sub-app (`POST /api/apps/git/settings/token`,
`GET /api/apps/git/status`). No raw shell in the plugin path anymore.

## Layout

- `aw-app.json` — the manifest (id `git`, tier `inprocess`).
- `schemas/aw-app.schema.json` — local structural validator (stand-in for
  the framework's eventual published schema).
- `scripts/install_git.sh`, `scripts/install_gh.sh` — idempotent apt
  installers (Debian/Ubuntu — the aw-workspace container's actual base
  image, confirmed via `podman exec aw-remote-host-workspace cat
  /etc/os-release` → Debian 13 trixie). `gh` installs via GitHub's
  official apt repo (signed keyring + `sources.list.d` entry).
- `scripts/uninstall.sh` — reverses both (apt purge + repo file cleanup).
- `git_app/plugin.py` — `GitAppPlugin` entrypoint; `activate(ctx)` installs
  git + gh via `ctx.commands`, logs `gh` in from the `ctx.secrets` token if
  present, and mounts the settings sub-app via `ctx.routes`. Revert is driven
  by the framework's journal reverse-replay (runs `scripts/uninstall.sh`).
- `git_app/installer.py` — runs the install/uninstall scripts as
  subprocesses; used by the standalone test (the framework path runs the
  scripts through `ctx.commands` directly).
- `git_app/gh_auth.py` — `login_with_token()` (pipes the token on stdin,
  never argv/env/disk), `login_web()` (interactive device-code flow),
  `status()`.
- `windows/main.json` — declarative settings/login window (status +
  auth-method form + login button), wired to `/api/apps/git/status` and
  `/api/apps/git/login` per the manifest's `contributes.routes`.
- `tests/validate_manifest.py` — validates `aw-app.json` against the
  schema + checks every `system_clis` installer path exists.
- `tests/standalone_test.sh` — installs git+gh for real and checks
  `--version` output; run inside the aw-workspace container.

## Testing done

1. **Manifest validation**: `.venv/aw/bin/python tests/validate_manifest.py`
   → `OK: aw-app.json is valid and all system_clis installers exist`.
2. **Real install, standalone, inside the target container**
   (`aw-remote-host-workspace` on macbook-fred, Debian 13 trixie, confirmed
   via `podman exec ... cat /etc/os-release`): ran the exact contents of
   `install_git.sh` / `install_gh.sh` inside the container as root via
   `podman exec`. Both installed cleanly; `git --version` and `gh --version`
   printed real version strings afterward. See the delivery report on the
   Kanban card for the raw output.
3. **gh-login flow**: `gh_auth.login_with_token()` / `.status()` exercised
   against a scratch/expired token to confirm the subprocess wiring and
   error surface (not a claim of a working login — no real token was
   provisioned for this standalone test).

## NOT done here (explicitly out of scope)

- No install into the production workspace — Frederico installs manually
  after reviewing this.
- No frontend settings UI — `windows/main.json` is the declarative spec
  the future settings UI (F5) will render; the backend token round-trip
  works today via `POST /api/apps/git/settings/token`.
