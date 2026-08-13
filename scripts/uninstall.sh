#!/usr/bin/env bash
# Reverses install_git.sh / install_gh.sh. Called on app uninstall
# (journal replay per the ADR's Decision 7 — this script IS the
# revert action for the commands:install journal entries).
#
# **Only remove what this app actually installed.** `git` ships in the
# workspace base image (aw-workspace's Dockerfile), so install_git.sh normally
# finds a healthy one and does nothing — purging it here reverts something we
# never did, and takes the rest of the system with it.
#
# That is not hypothetical: an app UPDATE is an uninstall+install cycle, so
# every update of this app ran `apt-get remove --purge git`. On 2026-08-13
# that pass took libcurl down with it, leaving `curl: error while loading
# shared libraries: libcurl.so.4` — which then failed aw-app-essentials'
# installers, i.e. a different app entirely, for the rest of the boot.
set -euo pipefail

# apt-get and the /etc/apt writes below need root — the container's default
# user (ubuntu) is non-root, so re-exec ourselves under sudo.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive

# gh is genuinely ours: it comes from the GitHub apt repo this app adds below,
# and nothing in the base image provides it.
apt-get remove -y --purge gh || true
rm -f /etc/apt/sources.list.d/github-cli.list /etc/apt/keyrings/githubcli-archive-keyring.gpg
apt-get update -qq || true
