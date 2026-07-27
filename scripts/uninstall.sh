#!/usr/bin/env bash
# Reverses install_git.sh / install_gh.sh. Called on app uninstall
# (journal replay per the ADR's Decision 7 — this script IS the
# revert action for the commands:install journal entries).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get remove -y --purge gh git || true
rm -f /etc/apt/sources.list.d/github-cli.list /etc/apt/keyrings/githubcli-archive-keyring.gpg
apt-get update -qq || true
