#!/usr/bin/env python3
"""Install compathy skills for Claude Code or Antigravity.

Installs the main compathy skill plus optional subskills (compare, augment).
Default: install all skills. Use --skill to install a specific one.

Prefers symlinks so `git pull` updates all installed skills. Falls back to
a directory copy on platforms where symlinks are restricted.

Usage:
  python3 scripts/install.py --claude                       # all skills, global
  python3 scripts/install.py --antigravity                  # all skills, global
  python3 scripts/install.py --claude --skill compare       # just compathy-compare
  python3 scripts/install.py --claude --workspace           # all skills, project-local
  python3 scripts/install.py --claude --uninstall           # remove all
  python3 scripts/install.py --claude --uninstall --skill main  # remove just main
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# skill_key -> (symlink_name, source_dir_relative_to_repo)
SKILLS = {
    "main":    ("compathy",         "."),
    "compare": ("compathy-compare", "skills/compathy-compare"),
    "augment": ("compathy-augment", "skills/compathy-augment"),
}

TOOL_BASES = {
    ("claude", "global"):       ("home", Path(".claude/skills")),
    ("claude", "workspace"):    ("cwd",  Path(".claude/skills")),
    ("antigravity", "global"):  ("home", Path(".gemini/antigravity/skills")),
    ("antigravity", "workspace"): ("cwd", Path(".agent/skills")),
}


def resolve_dest(tool: str, scope: str, symlink_name: str) -> Path:
    """Resolve the destination path for the skill."""
    anchor_kind, base = TOOL_BASES[(tool, scope)]
    anchor = Path.home() if anchor_kind == "home" else Path.cwd()
    return anchor / base / symlink_name


def resolve_src(skill_key: str) -> Path:
    """Resolve the source directory for the skill."""
    _, rel = SKILLS[skill_key]
    return (REPO_ROOT / rel).resolve()


def windows_update_gate() -> None:
    """On Windows, block until the user confirms they've run Windows Update."""
    if sys.platform != "win32":
        return
    print("=" * 60)
    print("Windows detected.")
    print("=" * 60)
    print("Before installing, please run Windows Update and install any")
    print("pending updates. Symlink support depends on a current build +")
    print("Developer Mode enabled (Settings -> For developers -> Developer Mode).")
    print("")
    ans = input("Have you run Windows Update and enabled Developer Mode? [y/N]: ")
    if ans.strip().lower() not in ("y", "yes"):
        print("Aborting. Please update Windows and re-run.", file=sys.stderr)
        sys.exit(2)


def make_link(src: Path, dest: Path) -> str:
    """Create a symlink or copy if symlinks are restricted."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dest, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError) as e:
        print(f"  symlink failed ({e}); falling back to copy", file=sys.stderr)
        shutil.copytree(src, dest, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
        return "copy"


def uninstall_one(dest: Path) -> bool:
    """Uninstall a specific skill from the destination."""
    if not dest.exists() and not dest.is_symlink():
        print(f"  not installed: {dest}")
        return True
    if dest.is_symlink():
        dest.unlink()
        print(f"  removed symlink: {dest}")
        return True
    if dest.is_dir():
        shutil.rmtree(dest)
        print(f"  removed directory: {dest}")
        return True
    print(f"  cannot remove: {dest}", file=sys.stderr)
    return False


def install_one(src: Path, dest: Path, skill_name: str) -> bool:
    """Install a specific skill from source to destination."""
    if dest.exists() or dest.is_symlink():
        print(f"  SKIP {skill_name}: already exists at {dest}", file=sys.stderr)
        print("  Run with --uninstall first.", file=sys.stderr)
        return False

    # Verify source has a SKILL.md (except repo root which has it at top level)
    skill_md = src / "SKILL.md"
    if not skill_md.is_file():
        print(f"  ERROR: missing SKILL.md in {src}", file=sys.stderr)
        return False

    kind = make_link(src, dest)
    print(f"  {skill_name} ({kind}): {dest}")
    return True


def run_install(tool: str, scope: str, skill_keys: list) -> int:
    """Run the installation process for the specified tool and scope."""
    windows_update_gate()
    print(f"Installing to {tool} ({scope}):")
    ok = True
    for key in skill_keys:
        name, _ = SKILLS[key]
        src = resolve_src(key)
        dest = resolve_dest(tool, scope, name)
        if not install_one(src, dest, name):
            ok = False
    if ok:
        print("\nTry it:")
        if tool == "claude":
            names = [SKILLS[k][0] for k in skill_keys]
            print(f"  In Claude Code, run: /{names[0]}")
        else:
            print("  In Antigravity, invoke via the skill description trigger")
    return 0 if ok else 1


def run_uninstall(tool: str, scope: str, skill_keys: list) -> int:
    """Run the uninstallation process for the specified tool and scope."""
    print(f"Uninstalling from {tool} ({scope}):")
    ok = True
    for key in skill_keys:
        name, _ = SKILLS[key]
        dest = resolve_dest(tool, scope, name)
        if not uninstall_one(dest):
            ok = False
    return 0 if ok else 1


def main() -> int:
    """Main entry point for the installation script."""
    ap = argparse.ArgumentParser(
        description="Install compathy skills for Claude Code or Antigravity",
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
    ap.add_argument("--skill", choices=["main", "compare", "augment", "all"],
                    default="all",
                    help="which skill to install (default: all)")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove installed skill(s)")
    args = ap.parse_args()

    tool = "claude" if args.claude else "antigravity"
    scope = "workspace" if args.workspace else "global"
    skill_keys = list(SKILLS.keys()) if args.skill == "all" else [args.skill]

    if args.uninstall:
        return run_uninstall(tool, scope, skill_keys)
    return run_install(tool, scope, skill_keys)


if __name__ == "__main__":
    sys.exit(main())
