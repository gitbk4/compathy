#!/usr/bin/env python3
"""Install compathy as a skill for Claude Code or Antigravity.

Single target per invocation. Prefers a symlink to this repo so
`git pull` updates the skill everywhere. Falls back to a copy on
platforms where symlinks are restricted (Windows without dev mode).

Usage:
  python3 scripts/install.py --claude              # ~/.claude/skills/compathy
  python3 scripts/install.py --antigravity         # ~/.gemini/antigravity/skills/compathy
  python3 scripts/install.py --claude --workspace  # ./.claude/skills/compathy
  python3 scripts/install.py --antigravity --workspace  # ./.agent/skills/compathy
  python3 scripts/install.py --claude --uninstall
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

SKILL_NAME = "compathy"
REPO_ROOT = Path(__file__).resolve().parent.parent

TARGETS = {
    # (tool, scope) -> relative path from anchor
    ("claude", "global"):       ("home",      Path(".claude/skills") / SKILL_NAME),
    ("claude", "workspace"):    ("cwd",       Path(".claude/skills") / SKILL_NAME),
    ("antigravity", "global"):  ("home",      Path(".gemini/antigravity/skills") / SKILL_NAME),
    ("antigravity", "workspace"): ("cwd",     Path(".agent/skills") / SKILL_NAME),
}


def resolve_target(tool: str, scope: str) -> Path:
    anchor_kind, rel = TARGETS[(tool, scope)]
    anchor = Path.home() if anchor_kind == "home" else Path.cwd()
    return anchor / rel


def windows_update_gate() -> None:
    """On Windows, block until the user confirms they've run Windows Update.

    Symlinks on Windows require Dev Mode (Win 10 1703+) OR admin privileges.
    Out-of-date Windows builds frequently lack Dev Mode toggles or have
    broken symlink support. We export that maintenance to the user.
    """
    if sys.platform != "win32":
        return
    print("=" * 60)
    print("Windows detected.")
    print("=" * 60)
    print("Before installing, please run Windows Update and install any")
    print("pending updates. Symlink support depends on a current build +")
    print("Developer Mode enabled (Settings -> For developers -> Developer Mode).")
    print("")
    print("After updating, re-run this installer.")
    print("")
    ans = input("Have you run Windows Update and enabled Developer Mode? [y/N]: ")
    if ans.strip().lower() not in ("y", "yes"):
        print("Aborting. Please update Windows and re-run.", file=sys.stderr)
        sys.exit(2)


def make_link(src: Path, dest: Path) -> str:
    """Create a symlink src <- dest. Falls back to copytree on failure.

    Returns 'symlink' or 'copy' to indicate what was used.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dest, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError) as e:
        print(f"  symlink failed ({e}); falling back to copy", file=sys.stderr)
        shutil.copytree(src, dest, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
        return "copy"


def uninstall(dest: Path) -> int:
    if not dest.exists() and not dest.is_symlink():
        print(f"not installed: {dest}")
        return 0
    if dest.is_symlink():
        dest.unlink()
        print(f"removed symlink: {dest}")
        return 0
    if dest.is_dir():
        shutil.rmtree(dest)
        print(f"removed directory: {dest}")
        return 0
    print(f"cannot remove (unexpected file type): {dest}", file=sys.stderr)
    return 1


def install(tool: str, scope: str) -> int:
    windows_update_gate()
    dest = resolve_target(tool, scope)
    src = REPO_ROOT

    if dest.exists() or dest.is_symlink():
        print(f"ERROR: already exists at {dest}", file=sys.stderr)
        print(f"Run with --uninstall first, or remove it manually.", file=sys.stderr)
        return 1

    # Verify the source looks right
    if not (src / "SKILL.md").is_file():
        print(f"ERROR: source missing SKILL.md: {src}", file=sys.stderr)
        return 1

    kind = make_link(src, dest)
    print(f"installed ({kind}): {dest} -> {src}")
    print(f"")
    print(f"Try it:")
    scope_label = "workspace" if scope == "workspace" else "global"
    if tool == "claude":
        print(f"  In Claude Code, run: /{SKILL_NAME}")
    else:
        print(f"  In Antigravity, invoke via its description trigger")
        print(f"  (installed to your {scope_label} Skills directory)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install compathy for Claude Code or Antigravity",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    tool_group = ap.add_mutually_exclusive_group(required=True)
    tool_group.add_argument("--claude", action="store_true",
                            help="install for Claude Code")
    tool_group.add_argument("--antigravity", action="store_true",
                            help="install for Google Antigravity")
    ap.add_argument("--workspace", action="store_true",
                    help="install into current workspace instead of global")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove an existing install at the target path")
    args = ap.parse_args()

    tool = "claude" if args.claude else "antigravity"
    scope = "workspace" if args.workspace else "global"
    dest = resolve_target(tool, scope)

    if args.uninstall:
        return uninstall(dest)
    return install(tool, scope)


if __name__ == "__main__":
    sys.exit(main())
