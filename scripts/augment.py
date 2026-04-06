#!/usr/bin/env python3
"""Gather structured data from a target project for compathy-augment.

Reads the target project's compathy wiki (or falls back to bootstrap scan).
Emits JSON with the target's pages, tech stack, and file structure for
Claude to characterize strengths and offer augmentation candidates.

Current project MUST have a compathy wiki. Target is read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from compare import read_project_data  # noqa: E402
from paths import context_root, raw_dir  # noqa: E402


def augment_dir(current_root: Path, target_name: str) -> Path:
    """Return the augmented-sources directory for a target project."""
    return raw_dir(current_root) / "augmented" / target_name


def analyze_target(current_root: Path, target_root: Path) -> dict:
    """Read both projects and prepare augmentation data.

    Current project must have a compathy wiki.
    Target project can fall back to bootstrap.
    """
    current_root = Path(current_root).resolve()
    target_root = Path(target_root).resolve()

    # Validate current project has wiki
    if not (context_root(current_root) / "wiki").exists():
        raise RuntimeError(
            f"compathy wiki not found at {context_root(current_root)}. "
            f"Run /compathy in this project first."
        )

    # Read target (wiki if available, bootstrap fallback)
    target = read_project_data(target_root, require_wiki=False)

    # Count current project's pages for reference
    # pylint: disable=import-outside-toplevel
    from compare import read_wiki_pages  # noqa: E402
    from paths import wiki_dir  # noqa: E402

    current_wiki = read_wiki_pages(wiki_dir(current_root))
    current_page_count = sum(len(v) for v in current_wiki.values())

    # Check if we already have augmented sources from this target
    aug_dir = augment_dir(current_root, target_root.name)
    existing_augments = []
    if aug_dir.exists():
        existing_augments = sorted(
            str(p.relative_to(aug_dir))
            for p in aug_dir.rglob("*.md")
            if p.is_file()
        )

    return {
        "target": target,
        "target_wiki_available": target["has_wiki"],
        "current_project_name": current_root.name,
        "current_page_count": current_page_count,
        "augment_dir": str(aug_dir),
        "existing_augments": existing_augments,
    }


def main() -> int:
    """Main entry point for augment script."""
    ap = argparse.ArgumentParser(
        description="Analyze target project for compathy-augment skill"
    )
    ap.add_argument("--current", default=".",
                    help="current project root (must have compathy wiki)")
    ap.add_argument("--target", required=True,
                    help="target project to learn from")
    args = ap.parse_args()

    try:
        result = analyze_target(Path(args.current), Path(args.target))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
