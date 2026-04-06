#!/usr/bin/env python3
"""Gather structured comparison data from two projects.

Reads both projects' compathy wikis (or falls back to bootstrap scan for
the target). Emits JSON with entity/concept/pattern overlap, unique sets,
and tech stack signals for Claude to write the comparison report.

Current project MUST have a compathy wiki. Target project can fall back
to bootstrap scan.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from bootstrap import (  # noqa: E402
    collect_file_tree,
    collect_manifests,
    collect_readmes,
)
from lint import iter_wiki_pages, parse_frontmatter  # noqa: E402
from paths import context_root, wiki_dir  # noqa: E402

# Tech stack detection patterns applied to manifests
STACK_SIGNALS = {
    "package.json": {
        "react": r'"react"',
        "vue": r'"vue"',
        "angular": r'"@angular/core"',
        "express": r'"express"',
        "next": r'"next"',
        "typescript": r'"typescript"',
        "prisma": r'"prisma"|"@prisma/client"',
        "tailwind": r'"tailwindcss"',
    },
    "pyproject.toml": {
        "django": r'django',
        "flask": r'flask',
        "fastapi": r'fastapi',
        "sqlalchemy": r'sqlalchemy',
        "pytest": r'pytest',
    },
    "Cargo.toml": {"rust": r'\[package\]'},
    "go.mod": {"go": r'^module\s'},
    "Gemfile": {"ruby": r'source\s'},
    "pom.xml": {"java-maven": r'<project'},
    "build.gradle": {"java-gradle": r'plugins\s*\{'},
    "build.gradle.kts": {"kotlin-gradle": r'plugins\s*\{'},
}


def detect_tech_stack(manifests: dict) -> list:
    """Return sorted list of tech signals found in manifests."""
    found = set()
    for filename, patterns in STACK_SIGNALS.items():
        content = manifests.get(filename, "")
        if not content:
            continue
        for tech, regex in patterns.items():
            if re.search(regex, content, re.MULTILINE):
                found.add(tech)
    # Infer language from manifest existence
    if "package.json" in manifests:
        found.add("node")
    if "pyproject.toml" in manifests or "requirements.txt" in manifests:
        found.add("python")
    return sorted(found)


def read_wiki_pages(wiki_root: Path) -> dict:
    """Extract structured data from all wiki pages.

    Returns dict with keys: entities, concepts, summaries, patterns.
    Each value is a list of {slug, frontmatter, backlinks, path}.
    """
    # pylint: disable=import-outside-toplevel
    from lint import parse_backlinks  # noqa: E402

    result = {"entities": [], "concepts": [], "summaries": [], "patterns": []}
    for slug, path in iter_wiki_pages(wiki_root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            fm, body = parse_frontmatter(text)
        except ValueError:
            continue
        page_type = fm.get("type", "concept")
        # Map plural folder name to type
        type_to_bucket = {
            "entity": "entities",
            "concept": "concepts",
            "summary": "summaries",
            "patterns": "patterns",
        }
        bucket = type_to_bucket.get(page_type, "concepts")
        result[bucket].append({
            "slug": slug,
            "type": page_type,
            "frontmatter": fm,
            "backlinks": parse_backlinks(body),
            "related_paths": fm.get("related_paths", []),
            "sources": fm.get("sources", []),
        })
    return result


def read_project_data(root: Path, require_wiki: bool = False) -> dict:
    """Read project data from wiki or bootstrap."""
    root = Path(root).resolve()
    has_wiki = wiki_dir(root).exists()

    if require_wiki and not has_wiki:
        raise RuntimeError(
            f"compathy wiki not found at {context_root(root)}. "
            f"Run /compathy in this project first."
        )

    data = {
        "name": root.name,
        "root": str(root),
        "has_wiki": has_wiki,
        "wiki_pages": {"entities": [], "concepts": [], "summaries": [], "patterns": []},
        "manifests": {},
        "tech_stack": [],
        "readmes": [],
        "file_tree": [],
    }

    if has_wiki:
        data["wiki_pages"] = read_wiki_pages(wiki_dir(root))

    # Always collect manifests + tech stack (useful even with wiki)
    data["manifests"] = collect_manifests(root)
    data["tech_stack"] = detect_tech_stack(data["manifests"])
    data["readmes"] = collect_readmes(root)
    data["file_tree"] = collect_file_tree(root)

    return data


def compute_overlap(current: dict, target: dict) -> dict:
    """Compute set overlap between two projects' wiki data."""
    def slug_set(pages: dict, key: str) -> set:
        return {p["slug"] for p in pages.get(key, [])}

    c_ent = slug_set(current["wiki_pages"], "entities")
    t_ent = slug_set(target["wiki_pages"], "entities")
    c_con = slug_set(current["wiki_pages"], "concepts")
    t_con = slug_set(target["wiki_pages"], "concepts")
    c_pat = slug_set(current["wiki_pages"], "patterns")
    t_pat = slug_set(target["wiki_pages"], "patterns")

    c_stack = set(current["tech_stack"])
    t_stack = set(target["tech_stack"])

    return {
        "entities": {
            "shared": sorted(c_ent & t_ent),
            "only_current": sorted(c_ent - t_ent),
            "only_target": sorted(t_ent - c_ent),
        },
        "concepts": {
            "shared": sorted(c_con & t_con),
            "only_current": sorted(c_con - t_con),
            "only_target": sorted(t_con - c_con),
        },
        "patterns": {
            "shared": sorted(c_pat & t_pat),
            "only_current": sorted(c_pat - t_pat),
            "only_target": sorted(t_pat - c_pat),
        },
        "tech_stack": {
            "shared": sorted(c_stack & t_stack),
            "only_current": sorted(c_stack - t_stack),
            "only_target": sorted(t_stack - c_stack),
        },
    }


def compare(current_root: Path, target_root: Path) -> dict:
    """Run full comparison. Current must have wiki, target can fall back."""
    current = read_project_data(current_root, require_wiki=True)
    target = read_project_data(target_root, require_wiki=False)
    overlap = compute_overlap(current, target)

    return {
        "current": current,
        "target": target,
        "target_wiki_available": target["has_wiki"],
        "overlap": overlap,
    }


def main() -> int:
    """Main entry point for compare script."""
    ap = argparse.ArgumentParser(
        description="Compare two projects for compathy-compare skill"
    )
    ap.add_argument("--current", default=".",
                    help="current project root (must have compathy wiki)")
    ap.add_argument("--target", required=True,
                    help="target project to compare against")
    args = ap.parse_args()

    try:
        result = compare(Path(args.current), Path(args.target))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
