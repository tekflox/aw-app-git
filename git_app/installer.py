"""
Install/uninstall logic for the git + gh system CLIs. Invoked by
GitAppPlugin.activate()/deactivate() through the framework, and directly
by scripts/standalone_test.sh for out-of-framework testing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = APP_ROOT / "scripts"


class InstallError(RuntimeError):
    pass


def _run_script(script: str) -> str:
    path = SCRIPTS_DIR / script
    result = subprocess.run(
        ["bash", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise InstallError(
            f"{script} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def install_git() -> str:
    """Installs git via apt. Returns `git --version` output."""
    return _run_script("install_git.sh")


def install_gh() -> str:
    """Installs the GitHub CLI via the official apt repo. Returns `gh --version` output."""
    return _run_script("install_gh.sh")


def install_all() -> dict[str, str]:
    return {"git": install_git(), "gh": install_gh()}


def uninstall_all() -> None:
    _run_script("uninstall.sh")
