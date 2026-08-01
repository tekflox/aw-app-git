# aw-app-git

AW workspace app that installs `git` and the GitHub CLI (`gh`) and provides a
settings panel for logging into `gh`. Supported auth paths include OAuth
Device Flow "Sign in with GitHub", pasted tokens, and the interactive web
flow. Any resulting token is routed to the zero-knowledge secret store and is
never stored in plain config.

## Status

**Plugged into the real framework (F4).** `GitAppPlugin.activate(ctx)` now
drives the gated `ctx` facades — `ctx.commands.install_system_cli(...)`
installs git + gh (journaled, reverted on uninstall), `ctx.secrets` stores/
reads the gh token via the workspace-side secure store, and `ctx.routes`
mounts a small settings sub-app (`POST /api/apps/git/settings/token`,
`GET /api/apps/git/status`, `POST /api/apps/git/device/start`,
`POST /api/apps/git/device/poll`). No raw shell in the plugin path anymore.

## Sign in with GitHub (OAuth Device Flow)

`gh auth login --web` (see `gh_auth.login_web()`) is **interactive** — it
needs a TTY/browser on the same machine, so it doesn't work from a web UI.
The Device Flow (RFC 8628) is the API-driven equivalent GitHub recommends for
exactly this case:

1. `POST /api/apps/git/device/start` — calls GitHub's
   `POST https://github.com/login/device/code` with `client_id` + `scope=repo
   read:org`. Returns `user_code` + `verification_uri`
   (`github.com/login/device`) for the UI to show, and stashes `device_code`
   server-side (one login in flight at a time — this is a single-user
   workspace).
2. User opens the link, enters the code.
3. `POST /api/apps/git/device/poll` — calls GitHub's
   `POST https://github.com/login/oauth/access_token` once per click (or a
   UI-driven interval poll). Handles `authorization_pending` (keep polling),
   `slow_down` (back off to the interval GitHub returns), `expired_token`,
   `access_denied`. On `success`: writes the token to `ctx.secrets`
   (`secrets:own`) and runs `login_with_token()`, same as the token-paste path.

`client_id` is **public** (device flow needs no `client_secret`) — resolved
from `config_schema.oauth_client_id` (settings window) first, falling back to
the `AW_APP_GIT_OAUTH_CLIENT_ID` env var. Neither set → `/device/start`
returns `{"error": "not_configured", ...}` instead of guessing a client_id.
Frederico creates the GitHub OAuth App (**Enable Device Flow** must be ON)
and provides the client_id.

Token-paste (`POST /api/apps/git/settings/token` or `/settings`) stays as the
fallback auth method — not removed.

`git_app/device_flow.py` — the two GitHub calls (`start`, `poll`), pure
functions taking `client_id`/`device_code`, no framework coupling, easy to
unit-test with a mocked `httpx.post`.

## Layout

- `aw-app.json` — the manifest (id `git`, tier `inprocess`).
- `schemas/aw-app.schema.json` — local structural validator (stand-in for
  the framework's eventual published schema).
- `scripts/install_git.sh`, `scripts/install_gh.sh` — idempotent apt
  installers (Debian/Ubuntu — the aw-workspace container's actual base
  image, confirmed via `podman exec aw-remote-host-workspace cat
  /etc/os-release` → Debian 13 trixie). `gh` installs via GitHub's
  official apt repo (signed keyring + `sources.list.d` entry). Both land in
  `/usr/bin` (apt's regular system path — already on every shell's `PATH`).
  Since the container's default user (`ubuntu`) is non-root, each script
  re-execs itself under `sudo -E` before touching apt/`/etc`.
- `scripts/uninstall.sh` — reverses both (apt purge + repo file cleanup),
  same `sudo -E` re-exec.
- `git_app/plugin.py` — `GitAppPlugin` entrypoint; `activate(ctx)` installs
  git + gh via `ctx.commands`, logs `gh` in from the `ctx.secrets` token if
  present, and mounts the settings sub-app via `ctx.routes`. Revert is driven
  by the framework's journal reverse-replay (runs `scripts/uninstall.sh`).
- `git_app/installer.py` — runs the install/uninstall scripts as
  subprocesses; used by the standalone test (the framework path runs the
  scripts through `ctx.commands` directly).
- `git_app/gh_auth.py` — `login_with_token()` (pipes the token on stdin,
  never argv/env/disk), `login_web()` (interactive device-code flow, TUI
  only), `status()`.
- `git_app/device_flow.py` — `start()` / `poll()`, the OAuth Device Flow
  calls to GitHub, no `client_secret` needed (see section above).
- `windows/main.json` — declarative settings/login window: "Sign in with
  GitHub" + "Check login status" buttons (device flow) wired to
  `/api/apps/git/device/start` and `/device/poll`, plus the token-paste
  fallback (status + auth-method form + login button) wired to
  `/api/apps/git/status` and `/api/apps/git/login`.
- `tests/validate_manifest.py` — validates `aw-app.json` against the
  schema + checks every `system_clis` installer path exists.
- `tests/test_device_flow.py` — unit tests for `device_flow.start/poll`
  against a mocked `httpx.post` (no real GitHub calls): success, invalid
  client_id, `authorization_pending`, `slow_down`, `expired_token`,
  `access_denied`.
- `tests/test_plugin_routes.py` — `/device/start` + `/device/poll` through a
  real `FastAPI TestClient` with a fake `ctx` (secrets facade only) and
  `device_flow.start/poll` monkeypatched — covers the not-configured case,
  the pending→success transition (asserts the token lands in
  `ctx.secrets`), `slow_down`, `expired_token`, `access_denied`, and polling
  with no login in flight.
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
4. **Device flow**: `.venv/aw/bin/python -m pytest tests/` →
   `16 passed` (both new test files), all GitHub calls mocked. Not tested
   against a real GitHub OAuth App / real device code — no `client_id` was
   provisioned for this run; Frederico still needs to create the OAuth App
   with Device Flow enabled and set `oauth_client_id` / the env var before
   the button works end-to-end.

## NOT done here (explicitly out of scope)

- No install into the production workspace — Frederico installs manually
  after reviewing this.
- No `oauth_client_id` provisioned yet — Frederico still needs to create the
  GitHub OAuth App (Device Flow enabled) and set the client_id.
- No auto-poll / big-code / copy-to-clipboard UX — the current declarative
  `AppWindow` renderer (`aw-frontend`, F5) only supports `button` widgets
  that POST with no body and show raw JSON; there's no bind/interval-polling
  widget yet to drive the "Waiting for authorization..." auto-poll loop the task
  asked for. Backend fully supports it (`/device/poll` is idempotent, safe to
  call repeatedly) — "Check login status" is a manual click today. Wiring a
  real polling UX needs a widget-vocabulary addition in `aw-frontend`
  (separate repo, out of scope for this aw-app-git-only card).
