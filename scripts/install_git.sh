#!/usr/bin/env bash
# Installs git into the workspace via apt. Idempotent — safe to re-run
# (on install, and on every reconcile pass after workspace recreation).
set -euo pipefail

# A `git` on PATH is NOT proof of a working git. Found live 2026-08-12: a
# stray 4MB /usr/bin/git with no package behind it (`dpkg -l git` empty) and
# an EMPTY /usr/lib/git-core, so every https:// operation died with
#
#     git: 'remote-https' is not a git command
#
# git talks to remotes through separate helper executables in `git --exec-path`
# (git-remote-http, git-remote-https, ...), and a binary shipped without them
# can still print a version quite happily. The old `command -v git` guard saw
# that and exited 0 forever, so the repair never ran.
#
# The blast radius was not obvious: nvm installs itself by cloning over HTTPS,
# so this took out nvm and with it node, npm, npx, yarn and pnpm — reported as
# four unrelated "failed to heal system CLI" errors in aw-app-essentials.
git_is_healthy() {
  command -v git >/dev/null 2>&1 || return 1
  local exec_path
  exec_path="$(git --exec-path 2>/dev/null || true)"
  [ -n "$exec_path" ] || return 1
  # One helper is enough to tell a complete install from a bare binary; https
  # is the one everything here actually uses.
  [ -x "$exec_path/git-remote-https" ] || command -v git-remote-https >/dev/null 2>&1
}

if git_is_healthy; then
  echo "git already installed: $(git --version)"
  exit 0
fi

if command -v git >/dev/null 2>&1; then
  echo "install_git.sh: $(command -v git) exists but cannot speak https (no" \
       "git-remote-https in $(git --exec-path 2>/dev/null)) — reinstalling" >&2
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "install_git.sh: no apt-get on this system — unsupported base image" >&2
  exit 1
fi

# The container's default user (ubuntu) is non-root — apt-get needs root, so
# re-exec ourselves under sudo. -E keeps $HOME etc. pointed at ubuntu's, not
# root's.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# --reinstall so a half-present install is repaired rather than reported as
# "already the newest version" and skipped, which is the exact state that got
# us here.
apt-get install -y --no-install-recommends --reinstall git

if ! git_is_healthy; then
  echo "install_git.sh: git still cannot speak https after reinstall" >&2
  exit 1
fi

git --version
