#!/usr/bin/env bash
# Reverses install_git.sh / install_gh.sh. Called on app uninstall
# (journal replay per the ADR's Decision 7 — this script IS the
# revert action for the commands:install journal entries).
set -euo pipefail

# apt-get and the /etc/apt writes below need root — the container's default
# user (ubuntu) is non-root, so re-exec ourselves under sudo.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get remove -y --purge gh git || true
rm -f /etc/apt/sources.list.d/github-cli.list /etc/apt/keyrings/githubcli-archive-keyring.gpg
apt-get update -qq || true
