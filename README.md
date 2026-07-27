# aw-app-git

First real decoupled app for aw-workspace, per the
[Decoupled Apps Framework ADR](../../docs/knowledge_base/docs/architecture/decoupled-apps-framework.md)
(`aw-app.json` manifest schema v1). Installs `git` + the GitHub CLI (`gh`)
into the workspace and provides a settings panel for logging into `gh`
(token or web flow — the token is a secret, routed to the zero-knowledge
secret store, never stored in plain config).

## Status

The framework runtime (Phase 1 — plugin loader, hot routes, `AppContext`)
isn't built yet. This app is **scaffolded and tested standalone**, ready to
plug in once Phase 1 ships (`runtime.entrypoint` in `aw-app.json` already
points at `git_app.plugin:GitAppPlugin`). It plugs in for real around
Phase 4 (commands, services, app DB, config/secrets split) — that's where
`commands:install` and `secrets:own` get enforced by a real `AppContext`.

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
- `git_app/plugin.py` — `GitAppPlugin(Plugin)` entrypoint; `activate()`
  installs both CLIs and logs `gh` in if a token secret is already
  granted, `deactivate()` uninstalls.
- `git_app/installer.py` — runs the install/uninstall scripts as
  subprocesses; used by both the plugin and the standalone test.
- `git_app/gh_auth.py` — `login_with_token()` (pipes the token on stdin,
  never argv/env/disk), `login_web()` (interactive device-code flow),
  `status()`.
- `git_app/_plugin_stub.py` — local stand-in for `aw_workspace.apps.Plugin`
  until Phase 1 exists; delete and import from the real module once it does.
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
- No real `AppContext`/`Plugin` runtime — `_plugin_stub.py` is a shim.
- No frontend settings UI — `windows/main.json` is the declarative spec
  the future settings UI will render; nothing renders it yet.
