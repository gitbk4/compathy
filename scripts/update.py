#!/usr/bin/env python3
"""Auto-update compathy from GitHub before a skill run.

Runs `git pull --ff-only` in the compathy repo. If the pull succeeds,
prints the old → new version. If anything fails (network, dirty tree,
diverged history), prints a warning and continues — never blocks the skill.

Exit codes:
  0 — updated successfully or already up-to-date
  0 — update failed (warning printed, but non-blocking)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _read_version() -> str:
    try:
        return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _is_git_repo() -> bool:
    try:
        r = _git(["rev-parse", "--is-inside-work-tree"], REPO_ROOT)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _has_remote() -> bool:
    try:
        r = _git(["remote"], REPO_ROOT)
        return r.returncode == 0 and r.stdout.strip() != ""
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


# pylint: disable=too-many-return-statements
def update() -> dict:
    """Attempt to auto-update. Returns a status dict.

    Keys: action (updated|already-current|skipped|failed),
          old_version, new_version, message
    """
    old_version = _read_version()

    if not _is_git_repo():
        return {
            "action": "skipped",
            "old_version": old_version,
            "new_version": old_version,
            "message": "compathy installed via copy (not a git repo); update manually",
        }

    if not _has_remote():
        return {
            "action": "skipped",
            "old_version": old_version,
            "new_version": old_version,
            "message": "no git remote configured; update manually",
        }

    # Fetch first to see if we're behind
    try:
        fetch = _git(["fetch", "--quiet"], REPO_ROOT)
        if fetch.returncode != 0:
            return {
                "action": "failed",
                "old_version": old_version,
                "new_version": old_version,
                "message": f"git fetch failed: {fetch.stderr.strip()}",
            }
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return {
            "action": "failed",
            "old_version": old_version,
            "new_version": old_version,
            "message": f"git fetch failed: {e}",
        }

    # Check if behind
    try:
        behind = _git(["rev-list", "--count", "HEAD..@{upstream}"], REPO_ROOT)
        if behind.returncode != 0 or behind.stdout.strip() == "0":
            return {
                "action": "already-current",
                "old_version": old_version,
                "new_version": old_version,
                "message": f"compathy v{old_version} (up to date)",
            }
    except (FileNotFoundError, subprocess.SubprocessError):
        pass  # proceed with pull anyway

    # Pull
    try:
        pull = _git(["pull", "--ff-only"], REPO_ROOT)
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        return {
            "action": "failed",
            "old_version": old_version,
            "new_version": old_version,
            "message": f"git pull failed: {e}",
        }

    if pull.returncode != 0:
        stderr = pull.stderr.strip()
        if "not possible to fast-forward" in stderr or "diverge" in stderr.lower():
            msg = "local changes diverge from remote; run `cd ~/Code/compathy && git pull` manually"
        else:
            msg = f"git pull --ff-only failed: {stderr}"
        return {
            "action": "failed",
            "old_version": old_version,
            "new_version": old_version,
            "message": msg,
        }

    new_version = _read_version()
    return {
        "action": "updated",
        "old_version": old_version,
        "new_version": new_version,
        "message": f"compathy updated: v{old_version} -> v{new_version}",
    }


def main() -> int:
    """Main entry point for the update script."""
    result = update()
    action = result["action"]

    if action == "updated":
        print(f"compathy: {result['message']}")
    elif action == "already-current":
        print(f"compathy: {result['message']}")
    elif action == "skipped":
        print(f"compathy: {result['message']}", file=sys.stderr)
    elif action == "failed":
        print(f"compathy: WARNING: {result['message']}", file=sys.stderr)
        print("compathy: continuing with current version", file=sys.stderr)

    return 0  # always 0 — never block the skill


if __name__ == "__main__":
    sys.exit(main())
