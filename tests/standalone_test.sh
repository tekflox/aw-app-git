#!/usr/bin/env bash
# Standalone test — no framework runtime required. Run this INSIDE the
# aw-workspace container (as root) to prove the install scripts actually
# install git + gh and that `git --version` / `gh --version` work after.
#
# Usage (from inside the container, with this repo copied in):
#   bash tests/standalone_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== install_git.sh =="
bash scripts/install_git.sh

echo "== install_gh.sh =="
bash scripts/install_gh.sh

echo "== versions =="
git --version
gh --version

echo "== gh auth status (expected: not logged in yet) =="
gh auth status 2>&1 || true

echo "OK: git + gh installed and functional"
