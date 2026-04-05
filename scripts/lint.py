#!/usr/bin/env python3
"""Lint a context/wiki for structural integrity and staleness.

Checks:
  - Backlinks [[slug]] resolve to existing pages
  - No orphan pages (every page appears in index.md; every index entry has a page)
  - Schema compliance (required frontmatter fields, slug naming, version match)
  - Staleness (wiki page mtime vs. git log of related_paths)

Includes a tiny flat-YAML parser for frontmatter (scalars + flat lists only).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from paths import (  # noqa: E402
    INDEX_FILE,
    LOG_FILE,
    SCHEMA_VERSION,
    STATE_FILE,
    WIKI_SUBDIRS,
    schema_path,
    wiki_dir,
)

STALENESS_COMMIT_THRESHOLD = 10  # warn if N+ commits to related_paths since page mtime
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BACKLINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
REQUIRED_FRONTMATTER = ("type", "schema_version")
VALID_TYPES = ("concept", "entity", "summary", "index", "log", "patterns")


# ---------- flat-YAML parser ----------

def parse_frontmatter(text: str):
    """Return (frontmatter_dict, body) from a markdown doc.

    Supports: scalars (str, int, bool), flat lists [a, b, c].
    Rejects: nested maps, nested lists, multi-line scalars.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text

    # Find closing delimiter on its own line
    lines = text.splitlines(keepends=True)
    if not lines:
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        raise ValueError("frontmatter: missing closing '---' delimiter")

    fm_lines = lines[1:end_idx]
    body = "".join(lines[end_idx + 1 :])
    data = {}
    for lineno, raw in enumerate(fm_lines, start=2):
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            raise ValueError(
                f"frontmatter line {lineno}: indented lines not allowed (flat YAML only)"
            )
        if ":" not in line:
            raise ValueError(f"frontmatter line {lineno}: missing ':' separator")
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            raise ValueError(f"frontmatter line {lineno}: empty key")
        data[key] = _parse_value(val, lineno)
    return data, body


def _parse_value(val: str, lineno: int):
    if val == "":
        return ""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        # Flat list — split on commas, no nested brackets allowed
        if "[" in inner or "]" in inner:
            raise ValueError(f"frontmatter line {lineno}: nested lists not allowed")
        parts = [p.strip() for p in inner.split(",")]
        return [_scalar(p) for p in parts if p]
    return _scalar(val)


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() in ("null", "~"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ---------- backlinks ----------

def _strip_code(body: str) -> str:
    body = CODE_FENCE_RE.sub("", body)
    body = INLINE_CODE_RE.sub("", body)
    return body


def parse_backlinks(body: str) -> list:
    """Return list of slugs referenced via [[slug]] or [[slug|alias]]."""
    stripped = _strip_code(body)
    out = []
    for m in BACKLINK_RE.finditer(stripped):
        token = m.group(1).strip()
        slug = token.split("|", 1)[0].strip()
        if slug:
            out.append(slug)
    return out


# ---------- wiki walking ----------

def iter_wiki_pages(wiki_root: Path):
    """Yield (slug, Path) for every wiki page in concepts/entities/summaries."""
    for sub in WIKI_SUBDIRS:
        d = wiki_root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name == "README.md":
                continue
            slug = p.stem
            yield slug, p


def read_page(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, None, f"cannot read: {e}"
    try:
        fm, body = parse_frontmatter(text)
    except ValueError as e:
        return None, None, f"frontmatter: {e}"
    return fm, body, None


# ---------- checks ----------

def check_backlinks(wiki_root: Path) -> list:
    errors = []
    slugs = set()
    pages = []
    for slug, path in iter_wiki_pages(wiki_root):
        slugs.add(slug)
        pages.append((slug, path))
    for slug, path in pages:
        fm, body, err = read_page(path)
        if err:
            continue
        for target in parse_backlinks(body):
            if target == slug:
                errors.append(
                    {
                        "kind": "self-backlink",
                        "severity": "warning",
                        "page": slug,
                        "target": target,
                        "path": str(path.relative_to(wiki_root)),
                    }
                )
                continue
            if target not in slugs:
                errors.append(
                    {
                        "kind": "broken-backlink",
                        "severity": "error",
                        "page": slug,
                        "target": target,
                        "path": str(path.relative_to(wiki_root)),
                    }
                )
    return errors


def parse_index_entries(index_text: str) -> set:
    """Return the set of slugs referenced in index.md via [[slug]]."""
    return set(parse_backlinks(index_text))


def check_orphans(wiki_root: Path) -> list:
    issues = []
    idx_path = wiki_root / INDEX_FILE
    if not idx_path.exists():
        return [
            {
                "kind": "missing-index",
                "severity": "error",
                "path": INDEX_FILE,
            }
        ]
    idx_text = idx_path.read_text(encoding="utf-8")
    indexed = parse_index_entries(idx_text)
    existing = {slug for slug, _ in iter_wiki_pages(wiki_root)}
    for slug in sorted(existing - indexed):
        issues.append(
            {
                "kind": "orphan-page",
                "severity": "warning",
                "slug": slug,
                "hint": "page not referenced from index.md",
            }
        )
    for slug in sorted(indexed - existing):
        issues.append(
            {
                "kind": "index-stale",
                "severity": "error",
                "slug": slug,
                "hint": "index.md references a page that does not exist",
            }
        )
    return issues


def check_schema_compliance(wiki_root: Path, schema_file: Path) -> list:
    issues = []
    # Slug naming + required frontmatter
    for slug, path in iter_wiki_pages(wiki_root):
        if not SLUG_RE.match(slug):
            issues.append(
                {
                    "kind": "bad-slug",
                    "severity": "error",
                    "slug": slug,
                    "hint": "slugs must be kebab-case ASCII (a-z0-9-)",
                }
            )
        fm, _, err = read_page(path)
        if err:
            issues.append(
                {
                    "kind": "frontmatter-error",
                    "severity": "error",
                    "slug": slug,
                    "hint": err,
                }
            )
            continue
        for field in REQUIRED_FRONTMATTER:
            if field not in fm:
                issues.append(
                    {
                        "kind": "missing-frontmatter-field",
                        "severity": "error",
                        "slug": slug,
                        "field": field,
                    }
                )
        type_val = fm.get("type")
        if type_val and type_val not in VALID_TYPES:
            issues.append(
                {
                    "kind": "invalid-type",
                    "severity": "error",
                    "slug": slug,
                    "value": type_val,
                    "hint": f"type must be one of {VALID_TYPES}",
                }
            )
        sv = fm.get("schema_version")
        if sv is not None and sv != SCHEMA_VERSION:
            issues.append(
                {
                    "kind": "schema-version-mismatch",
                    "severity": "warning",
                    "slug": slug,
                    "page_version": sv,
                    "current_version": SCHEMA_VERSION,
                    "hint": "recompile this page",
                }
            )
    return issues


def check_staleness(wiki_root: Path, target_root: Path) -> list:
    """For each page with related_paths, compare page mtime to git log."""
    issues = []
    # Single batched call: recent commits with names
    try:
        r = subprocess.run(
            ["git", "log", "--name-only", "--since=365.days", "--pretty=format:COMMIT %H %ct"],
            cwd=str(target_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return issues
    if r.returncode != 0:
        return issues

    # Parse: list of (timestamp, [paths])
    commits = []
    cur_ts = None
    cur_paths = []
    for line in r.stdout.splitlines():
        if line.startswith("COMMIT "):
            if cur_ts is not None:
                commits.append((cur_ts, cur_paths))
            parts = line.split()
            try:
                cur_ts = int(parts[2])
            except (IndexError, ValueError):
                cur_ts = None
            cur_paths = []
        elif line.strip() and cur_ts is not None:
            cur_paths.append(line.strip())
    if cur_ts is not None:
        commits.append((cur_ts, cur_paths))

    for slug, path in iter_wiki_pages(wiki_root):
        fm, _, err = read_page(path)
        if err:
            continue
        related = fm.get("related_paths") or []
        if not isinstance(related, list) or not related:
            continue
        try:
            page_mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        count = 0
        for ts, paths in commits:
            if ts <= page_mtime:
                break
            for rp in related:
                rp_norm = str(rp).rstrip("/")
                for touched in paths:
                    if touched == rp_norm or touched.startswith(rp_norm + "/"):
                        count += 1
                        break
        if count >= STALENESS_COMMIT_THRESHOLD:
            issues.append(
                {
                    "kind": "stale-page",
                    "severity": "warning",
                    "slug": slug,
                    "commits_since_compile": count,
                    "related_paths": related,
                    "hint": f"{count} commits to tracked paths since page was last updated",
                }
            )
    return issues


# ---------- report ----------

def lint(target: Path) -> dict:
    target = Path(target).resolve()
    wiki_root = wiki_dir(target)
    if not wiki_root.exists():
        return {
            "errors": [{"kind": "no-wiki", "path": str(wiki_root)}],
            "warnings": [],
            "summary": {"errors": 1, "warnings": 0},
        }
    all_issues = []
    all_issues.extend(check_backlinks(wiki_root))
    all_issues.extend(check_orphans(wiki_root))
    all_issues.extend(check_schema_compliance(wiki_root, schema_path(target)))
    all_issues.extend(check_staleness(wiki_root, target))

    errors = [i for i in all_issues if i.get("severity") == "error"]
    warnings = [i for i in all_issues if i.get("severity") == "warning"]
    return {
        "errors": errors,
        "warnings": warnings,
        "summary": {"errors": len(errors), "warnings": len(warnings)},
    }


def _human_report(result: dict) -> str:
    lines = []
    s = result["summary"]
    lines.append(f"lint: {s['errors']} error(s), {s['warnings']} warning(s)")
    for kind, items in (("ERROR", result["errors"]), ("WARN", result["warnings"])):
        for i in items:
            tag = i.get("kind", "?")
            slug = i.get("slug") or i.get("page") or i.get("path") or ""
            hint = i.get("hint") or i.get("target") or ""
            lines.append(f"  [{kind}] {tag}: {slug} {hint}".rstrip())
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=".")
    ap.add_argument(
        "--format", choices=("json", "human"), default="human",
    )
    args = ap.parse_args()
    target = Path(args.target).resolve()
    result = lint(target)
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_human_report(result))
    return 0 if result["summary"]["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
