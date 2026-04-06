#!/usr/bin/env python3
"""Scaffold a fresh context/ directory in the target project.

Creates: context/{raw/, wiki/{concepts,entities,summaries}/, schema.md,
wiki/index.md, wiki/log.md, wiki/README.md, raw/README.md}.

Refuses to clobber an existing context/ directory.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Allow running as `python scripts/scaffold.py` or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from paths import (  # noqa: E402
    INDEX_FILE,
    LOG_FILE,
    SCHEMA_VERSION,
    WIKI_SUBDIRS,
    context_root,
    raw_dir,
    schema_path,
    wiki_dir,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def detect_mode(target: Path) -> str:
    """Return 'INIT' if context/ doesn't exist, else 'RECOMPILE'."""
    return "RECOMPILE" if context_root(target).exists() else "INIT"


def render_template(name: str, ctx: dict) -> str:
    """Read a template file and substitute {{key}} placeholders."""
    tpl = TEMPLATES_DIR / f"{name}.tmpl"
    if not tpl.exists():
        raise FileNotFoundError(f"Template not found: {tpl}")
    content = tpl.read_text(encoding="utf-8")
    for k, v in ctx.items():
        content = content.replace("{{" + k + "}}", str(v))
    return content


def create_structure(target: Path, project_name: str = "") -> None:
    """Create the context directory structure and write initial templates."""
    target = Path(target)
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"Target must be a directory: {target}")
    target.mkdir(parents=True, exist_ok=True)

    ctx_root = context_root(target)
    if ctx_root.exists():
        raise FileExistsError(
            f"{ctx_root} already exists. Use RECOMPILE mode instead "
            f"(the skill will detect this automatically)."
        )

    # Directories
    ctx_root.mkdir()
    raw_dir(target).mkdir()
    wiki_dir(target).mkdir()
    for sub in WIKI_SUBDIRS:
        (wiki_dir(target) / sub).mkdir()
        (wiki_dir(target) / sub / ".gitkeep").touch()
    (raw_dir(target) / ".gitkeep").touch()

    # Template variables
    tpl_vars = {
        "project_name": project_name or target.resolve().name,
        "date": date.today().isoformat(),
        "schema_version": str(SCHEMA_VERSION),
    }

    # Template files
    schema_path(target).write_text(render_template("schema.md", tpl_vars), encoding="utf-8")
    (wiki_dir(target) / INDEX_FILE).write_text(
        render_template("index.md", tpl_vars), encoding="utf-8"
    )
    (wiki_dir(target) / LOG_FILE).write_text(
        render_template("log.md", tpl_vars), encoding="utf-8"
    )
    (wiki_dir(target) / "README.md").write_text(
        render_template("wiki-README.md", tpl_vars), encoding="utf-8"
    )
    (raw_dir(target) / "README.md").write_text(
        render_template("raw-README.md", tpl_vars), encoding="utf-8"
    )


def main() -> int:
    """Main entry point for the scaffold script."""
    ap = argparse.ArgumentParser(
        description="Scaffold context/ structure in target project"
    )
    ap.add_argument("--target", default=".", help="target project root (default: cwd)")
    ap.add_argument(
        "--project-name",
        default="",
        help="project name for templates (default: target dir name)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="just detect mode (INIT|RECOMPILE) and exit",
    )
    args = ap.parse_args()
    target = Path(args.target).resolve()

    if args.check:
        print(detect_mode(target))
        return 0

    try:
        create_structure(target, args.project_name)
    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except NotADirectoryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Scaffolded {context_root(target)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
