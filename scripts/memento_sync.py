#!/usr/bin/env python3
"""Track and sync with the upstream Memento-Skills repo.

Checks GitHub for the latest Memento-Skills version tag and compares
it to the locally tracked version in MEMENTO_VERSION. If a newer
version is available, notifies the user. Also checks whether
memento-skills is installed in the current Python environment.

Exit codes:
  0 — always; never blocks the skill
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMENTO_VERSION_FILE = REPO_ROOT / "MEMENTO_VERSION"
GITHUB_API = "https://api.github.com/repos/Memento-Teams/Memento-Skills/tags"
INSTALL_URL = "https://github.com/Memento-Teams/Memento-Skills"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_latest_tag() -> str | None:
    """Return the latest version tag from GitHub (e.g. 'v0.2.0'), or None."""
    try:
        req = Request(GITHUB_API, headers={"Accept": "application/vnd.github.v3+json"})
        with urlopen(req, timeout=10) as resp:
            tags = json.loads(resp.read().decode())
            if tags and isinstance(tags, list):
                return tags[0]["name"]
    except (URLError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _read_tracked() -> str:
    """Return the last-persisted Memento-Skills version, or ''."""
    try:
        return MEMENTO_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_tracked(version: str) -> None:
    """Persist the latest known Memento-Skills version tag."""
    MEMENTO_VERSION_FILE.write_text(version + "\n", encoding="utf-8")


def _get_installed_version() -> str | None:
    """Return the pip-installed memento-s version string, or None."""
    # Try `memento --version` first (CLI may not exist on all installs)
    for cmd in [["memento", "--version"], ["memento-s", "--version"]]:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, check=False
            )
            if r.returncode == 0 and r.stdout.strip():
                # Output may be "memento-s 0.2.0" or just "0.2.0"
                return r.stdout.strip().split()[-1]
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    # Fallback: pip show
    try:
        r = subprocess.run(
            ["pip", "show", "memento-s"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    return None


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def check() -> dict:
    """Check Memento-Skills version against GitHub.

    Returns a dict with keys:
      action   — update-available | already-current | not-installed | failed
      latest   — latest tag from GitHub (or None)
      installed — locally installed version (or None)
      message  — human-readable summary
    """
    latest_tag = _fetch_latest_tag()

    if latest_tag is None:
        return {
            "action": "failed",
            "latest": None,
            "installed": None,
            "message": "could not reach GitHub to check Memento-Skills version",
        }

    tracked = _read_tracked()
    installed = _get_installed_version()

    # Always refresh tracked version
    if tracked != latest_tag:
        _write_tracked(latest_tag)

    latest_clean = latest_tag.lstrip("v")

    if installed:
        installed_clean = installed.lstrip("v")
        if installed_clean == latest_clean:
            return {
                "action": "already-current",
                "latest": latest_tag,
                "installed": installed,
                "message": f"Memento-Skills {latest_tag} (up to date)",
            }
        return {
            "action": "update-available",
            "latest": latest_tag,
            "installed": installed,
            "message": (
                f"Memento-Skills update available: {installed} → {latest_tag}\n"
                f"  upgrade: pip install -e git+{INSTALL_URL}.git"
            ),
        }

    # Not installed — still useful to surface the latest tag
    return {
        "action": "not-installed",
        "latest": latest_tag,
        "installed": None,
        "message": (
            f"Memento-Skills {latest_tag} not installed locally.\n"
            f"  install: git clone {INSTALL_URL} && cd Memento-Skills && pip install -e ."
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    result = check()
    action = result["action"]

    if action == "already-current":
        print(f"memento-skills: {result['message']}")
    elif action in ("update-available", "not-installed"):
        print(f"memento-skills: {result['message']}", file=sys.stderr)
    elif action == "failed":
        print(f"memento-skills: WARNING: {result['message']}", file=sys.stderr)

    return 0  # always 0 — never block the skill


if __name__ == "__main__":
    sys.exit(main())
