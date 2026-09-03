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

# pylint: disable=wrong-import-position
from paths import (  # noqa: E402
    INDEX_FILE,
    LOG_FILE,
    PERSONAS_INDEX_FILE,
    SCHEMA_VERSION,
    STATE_FILE,
    WIKI_SUBDIRS,
    personas_dir,
    personas_index_path,
    registry_path,
    schema_path,
    wiki_dir,
)
from lineage import (  # noqa: E402
    LineageError,
    load_lineage,
    page_path,
    parent_layers,
    read_json,
    resolve_layers,
    self_role,
    validate_manifest,
    validate_personas_index,
    validate_registry,
)

STALENESS_COMMIT_THRESHOLD = 10  # warn if N+ commits to related_paths since page mtime
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BACKLINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
REQUIRED_FRONTMATTER = ("type", "schema_version")
VALID_TYPES = ("concept", "entity", "summary", "index", "log", "patterns")
# Federation fields (all optional, all additive; see schema.md "Federation").
VALID_AUTHORITY = ("org", "team", "project")
VALID_OVERRIDE = ("forbidden", "narrow", "free")


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


# pylint: disable=too-many-return-statements
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
    """Read a markdown page and parse its frontmatter and body."""
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

def _resolves_upward(slug: str, parents: list) -> bool:
    """True if any *cached* parent layer has a page with this slug."""
    for layer in parents:
        if not layer.get("cached") or layer.get("wiki") is None:
            continue
        if page_path(layer["wiki"], slug) is not None:
            return True
    return False


def check_backlinks(wiki_root: Path, parents: list = None) -> list:
    """Check all wiki pages for self-backlinks and broken backlinks.

    ``parents`` (from ``lineage.resolve_layers``) lets a backlink resolve
    *upward* into a cached parent layer (team -> org). Backlinks never
    resolve downward: a parent lints on its own and knows nothing about its
    children. When a parent layer is not cached, unresolved backlinks are
    downgraded to ``unverified-backlink`` warnings instead of errors, so a
    fresh clone of a linked repo does not fail lint before ``persona sync``.
    """
    parents = parents or []
    any_uncached = any(not l.get("cached") for l in parents)
    errors = []
    slugs = set()
    pages = []
    for slug, path in iter_wiki_pages(wiki_root):
        slugs.add(slug)
        pages.append((slug, path))
    for slug, path in pages:
        _, body, err = read_page(path)
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
                if parents and _resolves_upward(target, parents):
                    continue
                if any_uncached:
                    errors.append(
                        {
                            "kind": "unverified-backlink",
                            "severity": "warning",
                            "page": slug,
                            "target": target,
                            "path": str(path.relative_to(wiki_root)),
                            "hint": "no local page; a parent layer is not cached "
                                    "(run `/compathy-persona sync`)",
                        }
                    )
                    continue
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
    """Check for orphaned pages or index staleness."""
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
    # index and log are catalogs, not pages: the index template links
    # [[log]] and that must not read as a stale entry.
    indexed = parse_index_entries(idx_text) - {"index", "log"}
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


def check_schema_compliance(wiki_root: Path, own_role: str = None) -> list:
    """Verify that all wiki pages comply with the defined schema.

    ``own_role`` (from lineage ``self.role``) enables the authority-claim
    rule: a page may only declare the ``authority`` of the layer it lives
    in. A team page claiming ``authority: org`` is an error. Root layers
    (no lineage.json) may claim any authority.
    """
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
        issues.extend(_check_federation_fields(slug, fm, own_role))
    return issues


def _check_federation_fields(slug: str, fm: dict, own_role: str = None) -> list:
    """Validate the optional authority / override / extends fields."""
    issues = []
    authority = fm.get("authority")
    override = fm.get("override")
    extends = fm.get("extends")
    if authority is not None and authority not in VALID_AUTHORITY:
        issues.append(
            {
                "kind": "invalid-authority",
                "severity": "error",
                "slug": slug,
                "value": authority,
                "hint": f"authority must be one of {VALID_AUTHORITY}",
            }
        )
    elif authority is not None and own_role and authority != own_role:
        issues.append(
            {
                "kind": "authority-claim",
                "severity": "error",
                "slug": slug,
                "value": authority,
                "hint": f"this layer is a {own_role}; pages here may only "
                        f"declare authority: {own_role}",
            }
        )
    if override is not None and override not in VALID_OVERRIDE:
        issues.append(
            {
                "kind": "invalid-override",
                "severity": "error",
                "slug": slug,
                "value": override,
                "hint": f"override must be one of {VALID_OVERRIDE}",
            }
        )
    elif override is not None and authority is None:
        issues.append(
            {
                "kind": "override-without-authority",
                "severity": "warning",
                "slug": slug,
                "hint": "override only applies to pages that declare authority:",
            }
        )
    if extends is not None and (not isinstance(extends, str) or not extends.strip()):
        issues.append(
            {
                "kind": "invalid-extends",
                "severity": "error",
                "slug": slug,
                "hint": "extends must be a parent layer id, e.g. extends: acme",
            }
        )
    return issues


def check_shadowing(wiki_root: Path, parents: list) -> list:
    """Apply the parent's override policy to same-slug pages in this layer.

    Shadowing is *opt-in per page*: only parent pages that declare
    ``authority:`` carry a policy. Every other same-slug collision (for
    example each layer's own ``technical-patterns``) is layer-local and
    produces nothing. The nearest cached parent that has the slug decides.

      override: forbidden  -> error  shadow-forbidden
      override: narrow     -> error  shadow-missing-extends unless the child
                              declares ``extends: <parent layer id>``
      override: free       -> nothing
    """
    issues = []
    parent_ids = {l.get("id") for l in parents}
    for slug, path in iter_wiki_pages(wiki_root):
        fm, _, err = read_page(path)
        if err:
            continue
        extends = fm.get("extends")
        if isinstance(extends, str) and extends.strip() and extends not in parent_ids:
            issues.append(
                {
                    "kind": "extends-unknown-layer",
                    "severity": "warning",
                    "slug": slug,
                    "value": extends,
                    "hint": f"extends names a layer not in this lineage: {sorted(parent_ids)}",
                }
            )
        for parent in parents:
            if not parent.get("cached") or parent.get("wiki") is None:
                continue
            ppath = page_path(parent["wiki"], slug)
            if ppath is None:
                continue
            pfm, _, perr = read_page(ppath)
            if perr or not pfm.get("authority"):
                break  # layer-local collision; nearest parent decides, nothing to enforce
            policy = pfm.get("override") or "narrow"
            if policy == "forbidden":
                issues.append(
                    {
                        "kind": "shadow-forbidden",
                        "severity": "error",
                        "slug": slug,
                        "layer": parent.get("id"),
                        "hint": f"{parent.get('id')} ({parent.get('role')}) owns this page "
                                f"and forbids overriding it; link to it instead",
                    }
                )
            elif policy == "narrow" and extends != parent.get("id"):
                issues.append(
                    {
                        "kind": "shadow-missing-extends",
                        "severity": "error",
                        "slug": slug,
                        "layer": parent.get("id"),
                        "hint": f"this page narrows an authoritative {parent.get('role')} page; "
                                f"declare `extends: {parent.get('id')}` in its frontmatter",
                    }
                )
            break
    return issues


def check_persona_manifests(target: Path) -> list:
    """Validate context/personas/*.json and the generated index.json."""
    issues = []
    pdir = personas_dir(target)
    if not pdir.is_dir():
        return issues
    files = sorted(p for p in pdir.glob("*.json") if p.name != PERSONAS_INDEX_FILE)
    ids_by_file = {}
    for f in files:
        try:
            data = read_json(f)
        except LineageError as e:
            issues.append({"kind": "persona-manifest-invalid", "severity": "error",
                           "path": f"personas/{f.name}", "hint": str(e)})
            continue
        probs = validate_manifest(data)
        for prob in probs:
            issues.append({"kind": "persona-manifest-invalid", "severity": "error",
                           "path": f"personas/{f.name}", "hint": prob})
        if probs:
            continue
        role = data["id"].rsplit("/", 1)[1]
        if role != f.stem:
            issues.append({"kind": "persona-id-file-mismatch", "severity": "error",
                           "path": f"personas/{f.name}",
                           "hint": f"id role segment {role!r} must match filename {f.stem!r}"})
        ids_by_file[f.name] = data["id"]
    idx_path = personas_index_path(target)
    if not idx_path.is_file():
        if files:
            issues.append({"kind": "personas-index-missing", "severity": "warning",
                           "path": f"personas/{PERSONAS_INDEX_FILE}",
                           "hint": "re-run `/compathy-persona export` to regenerate"})
        return issues
    try:
        idx = read_json(idx_path)
    except LineageError as e:
        issues.append({"kind": "personas-index-invalid", "severity": "error",
                       "path": f"personas/{PERSONAS_INDEX_FILE}", "hint": str(e)})
        return issues
    for prob in validate_personas_index(idx):
        issues.append({"kind": "personas-index-invalid", "severity": "error",
                       "path": f"personas/{PERSONAS_INDEX_FILE}", "hint": prob})
    listed = {}
    for entry in idx.get("personas", []) if isinstance(idx.get("personas"), list) else []:
        if isinstance(entry, dict):
            listed[entry.get("file")] = entry.get("id")
    for fname, pid in sorted(ids_by_file.items()):
        if listed.get(fname) != pid:
            issues.append({"kind": "personas-index-stale", "severity": "error",
                           "path": f"personas/{PERSONAS_INDEX_FILE}",
                           "hint": f"{fname} ({pid}) missing or mismatched in index.json; "
                                   f"re-run `/compathy-persona export`"})
    for fname in sorted(set(listed) - set(ids_by_file)):
        issues.append({"kind": "personas-index-stale", "severity": "error",
                       "path": f"personas/{PERSONAS_INDEX_FILE}",
                       "hint": f"index.json lists {fname} which does not exist"})
    return issues


def check_registry(target: Path) -> list:
    """Validate context/registry.json when present."""
    rpath = registry_path(target)
    if not rpath.is_file():
        return []
    try:
        data = read_json(rpath)
    except LineageError as e:
        return [{"kind": "registry-invalid", "severity": "error",
                 "path": "registry.json", "hint": str(e)}]
    return [{"kind": "registry-invalid", "severity": "error",
             "path": "registry.json", "hint": prob} for prob in validate_registry(data)]


# pylint: disable=too-many-locals, too-many-branches
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
            check=False,
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
    """Run all linter checks on the target wiki directory."""
    target = Path(target).resolve()
    wiki_root = wiki_dir(target)
    if not wiki_root.exists():
        return {
            "errors": [{"kind": "no-wiki", "path": str(wiki_root)}],
            "warnings": [],
            "summary": {"errors": 1, "warnings": 0},
        }
    lineage_doc = None
    lineage_err = None
    try:
        lineage_doc = load_lineage(target)
    except LineageError as e:
        lineage_err = str(e)
    layers = resolve_layers(target, lineage_doc) if lineage_doc else []
    parents = parent_layers(layers)
    own_role = self_role(lineage_doc)

    all_issues = []
    if lineage_err:
        all_issues.append({"kind": "lineage-invalid", "severity": "error",
                           "path": "lineage.json", "hint": lineage_err})
    for layer in parents:
        if not layer.get("cached"):
            all_issues.append({"kind": "layer-not-cached", "severity": "warning",
                               "layer": layer.get("id"), "pin": layer.get("pin"),
                               "hint": "parent layer not in ~/.compathy/layers; "
                                       "run `/compathy-persona sync`"})
    all_issues.extend(check_backlinks(wiki_root, parents))
    all_issues.extend(check_orphans(wiki_root))
    all_issues.extend(check_schema_compliance(wiki_root, own_role))
    if parents:
        all_issues.extend(check_shadowing(wiki_root, parents))
    all_issues.extend(check_persona_manifests(target))
    all_issues.extend(check_registry(target))
    all_issues.extend(check_staleness(wiki_root, target))

    errors = [i for i in all_issues if i.get("severity") == "error"]
    warnings = [i for i in all_issues if i.get("severity") == "warning"]
    result = {
        "errors": errors,
        "warnings": warnings,
        "summary": {"errors": len(errors), "warnings": len(warnings)},
    }
    if lineage_doc:
        # Only present when linked, so standalone wikis keep byte-identical output.
        result["lineage"] = {
            "persona": lineage_doc.get("persona"),
            "self": lineage_doc.get("self"),
            "layers": [
                {"id": l.get("id"), "role": l.get("role"),
                 "pin": (l.get("pin") or "")[:12], "cached": bool(l.get("cached"))}
                for l in parents
            ],
        }
    return result


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
    """Main entry point for the linter script."""
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
