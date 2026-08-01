#!/usr/bin/env bash
# Installs the GitHub CLI (gh) via the official GitHub apt repository.
# Idempotent — safe to re-run (on install, and on every reconcile pass
# after workspace recreation). Follows GitHub's documented Debian/Ubuntu
# install method: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
set -euo pipefail

if command -v gh >/dev/null 2>&1; then
  echo "gh already installed: $(gh --version | head -1)"
  exit 0
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "install_gh.sh: no apt-get on this system — unsupported base image" >&2
  exit 1
fi

# The container's default user (ubuntu) is non-root — apt-get and the
# /etc/apt/keyrings writes below need root, so re-exec ourselves under sudo.
# -E keeps $HOME etc. pointed at ubuntu's, not root's.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E bash "$0" "$@"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends curl ca-certificates gpg

mkdir -p -m 755 /etc/apt/keyrings
out=$(mktemp)
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$out"
cat "$out" > /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
rm -f "$out"

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list

apt-get update -qq
apt-get install -y --no-install-recommends gh

gh --version
