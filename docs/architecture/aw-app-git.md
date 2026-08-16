---
repo: architecture
path: docs/architecture/aw-app-git.md
source: generated
edited: false
checksum: sha256:ea099a87f7f7f7ce5a90a127d618372bfde06781522929593b7f459d882edccc
---
# Git + GitHub CLI

- **repo**: aw-app-git
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Installs git and the GitHub CLI (gh) into the workspace, survives restarts, provides a settings panel for gh login (token stored in the zero-knowledge secret store), and serves the GitHub PR dashboard (open PRs for you and your team, plus repo status) the workspace nav reads.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/git
- `other` → **aw-app-diff-tool** — The repos nav's "show diff" arrow opens aw-app-diff-tool's window (POST /api/apps/diff-tool/diffs/render)

## MCP tools
_none exposed_

## Requirements
_none documented_
