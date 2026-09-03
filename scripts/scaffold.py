#!/usr/bin/env python3
"""Scaffold a fresh context/ directory in the target project.

Creates: context/{raw/, wiki/{concepts,entities,summaries}/, schema.md,
wiki/index.md, wiki/log.md, wiki/README.md, raw/README.md}.

Refuses to clobber an existing context/ directory.

``--from-persona <file|url|org/team/role>`` scaffolds (when needed) and links
the project to a team persona in one step; see persona.py.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Allow running as `python scripts/scaffold.py` or as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
import discovery  # noqa: E402
import persona_integration  # noqa: E402
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

    _maybe_integrate_persona(target, tpl_vars["project_name"])
    _maybe_write_discovery(target, tpl_vars["project_name"])


def _maybe_write_discovery(target: Path, project_name: str) -> None:
    """Auto-write a "Project context" section into CLAUDE.md and README.md
    so Claude Code (and other agents) discover the wiki without prompting.

    Wrapped so a discovery failure NEVER aborts the scaffold (consistent
    with lane-1D's persona-integration posture). Appends a single log.md
    entry recording what was written, after any persona-integration entry
    so neither clobbers the other.
    """
    try:
        result = discovery.write_discovery_breadcrumbs(target, project_name)
    except Exception as e:  # pylint: disable=broad-except
        # Defense in depth: discovery already swallows OSError, but never
        # let an unexpected exception abort the scaffold.
        print(
            f"[compathy/discovery] warning: discovery breadcrumbs failed: {e}",
            file=sys.stderr,
        )
        return

    _append_discovery_log_entry(wiki_dir(target) / LOG_FILE, result)


def _append_discovery_log_entry(log_path: Path, result: dict) -> None:
    """Append a single discovery line to log.md (no-op if log missing)."""
    if not log_path.is_file():
        return
    parts = []
    for fname, key in (("CLAUDE.md", "claude_md"), ("README.md", "readme_md")):
        status = result.get(key, "skipped")
        if status != "skipped":
            parts.append(f"{fname} {status}")
    mcp_status = result.get("mcp_json", "skipped")
    if mcp_status not in ("skipped", "skipped-malformed", "error"):
        parts.append(f".mcp.json {mcp_status}")
    if not parts:
        return
    text = log_path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    today = date.today().isoformat()
    text += (
        f"\n## [{today}] discovery | breadcrumbs | {', '.join(parts)}\n"
    )
    log_path.write_text(text, encoding="utf-8")


def _maybe_integrate_persona(target: Path, project_name: str) -> None:
    """Opt-in persona integration: read ai-quickstart's persona.json (if any)
    and seed entities/builder.md + patterns/style.md.

    Strict invariant: when persona is None, this is a no-op — the scaffold
    output is byte-identical to a run without ai-quickstart installed.
    """
    persona = persona_integration.load_persona_if_available()
    if persona is None:
        return

    wiki_root = wiki_dir(target)
    builder_path = wiki_root / "entities" / "builder.md"
    style_path = wiki_root / "patterns" / "style.md"

    builder_md = persona_integration.render_builder_md(persona, project_name)
    builder_path.write_text(builder_md, encoding="utf-8")

    style_md = persona_integration.render_style_md(persona)
    style_written = bool(style_md.strip())
    if style_written:
        style_path.write_text(style_md, encoding="utf-8")

    persona_integration.update_index_with_persona_pages(
        wiki_root / INDEX_FILE,
        builder=True,
        style=style_written,
    )
    persona_integration.append_persona_log_entry(
        wiki_root / LOG_FILE,
        builder=True,
        style=style_written,
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
    ap.add_argument(
        "--from-persona",
        default=None,
        metavar="FILE|URL|ID",
        help="scaffold (if needed) and link this project to a team persona "
             "(delegates to persona.py import --apply)",
    )
    ap.add_argument(
        "--consent",
        default="fetch,link",
        help="with --from-persona: comma list of fetch,link,toolkit (default fetch,link)",
    )
    ap.add_argument("--org", default=None, help="with --from-persona: org compathy source for id lookups")
    args = ap.parse_args()
    target = Path(args.target).resolve()

    if args.check:
        print(detect_mode(target))
        return 0

    if args.from_persona:
        # Import's link step scaffolds context/ when it is missing, so this
        # works for an empty dir and for an existing wiki alike.
        import persona  # pylint: disable=import-outside-toplevel
        argv = ["import", args.from_persona, "--target", str(target), "--apply",
                "--consent", args.consent]
        if args.project_name:
            argv += ["--project-name", args.project_name]
        if args.org:
            argv += ["--org", args.org]
        return persona.main(argv)

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
