#!/usr/bin/env python3
"""compathy publish: share a core subset of a local wiki as a read-only layer.

A wiki owner keeps one full, private compathy. Pages marked
``visibility: org`` (or team / public) are the shareable core. Publishing
copies only those pages into a separate git repository (the *core repo*),
which teammates link to as a pinned, read-only parent layer with
``/compathy-persona import``. Their own wikis stay theirs to edit.

    init   --layer-id ID --repo DIR [--title T] [--remote SRC] [--bundle PATH]
           [--audience org] [--role team] [--owner-name N] [--owner-email E] [--owner-github H]
    mark   SLUG... --visibility org|team|public|private
    plan   [--diff]                build in a temp dir; report pages, unlinks, scan, lint, diff
    apply  [--note TEXT]           write the build into the core repo; commit content, then personas
    push   [--confirm-private]     push to the remote (public-remote guard) and refresh the bundle
    status                         config, core repo HEAD, last publish, bundle freshness

Every subcommand prints one JSON document; the SKILL.md renders it and asks
the owner before ``apply`` and ``push``. Exit 0 = ok, 1 = user-facing
failure (``error`` in the JSON), 2 = usage.

Security model (details in ARCHITECTURE.md):

- Default deny. A page leaves only when its ``visibility`` reaches the
  configured audience (private < team < org < public). ``raw/``, the log,
  compile state, ``respond/``, personas, lineage and this config never leave.
- Private sections. ``<!-- private -->`` ... ``<!-- /private -->`` is cut
  from a published page. An unbalanced marker blocks the publish.
- Links to unpublished pages become plain text; frontmatter is reduced to
  an allowlist (``sources`` and ``related_paths`` never leave).
- A leak scan blocks the publish on the owner's deny patterns, secrets and
  tokens, SSN-shaped numbers, personal file paths and emails outside the
  allowed domains. Findings are reported by rule, file, line and a
  fingerprint; the matched text is never printed.
- ``push`` refuses a remote that answers an anonymous ``ls-remote`` (a
  public repository) and asks for ``--confirm-private`` when it cannot tell.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
import layers as layers_mod  # noqa: E402
import lineage as lin  # noqa: E402
from lint import VALID_VISIBILITY, iter_wiki_pages, lint as run_lint, parse_frontmatter  # noqa: E402
from paths import (  # noqa: E402
    CONTEXT_DIR,
    INDEX_FILE,
    LINEAGE_FILE,
    LOG_FILE,
    PERSONAS_INDEX_FILE,
    PERSONAS_SUBDIR,
    SCHEMA_FILE,
    SCHEMA_VERSION,
    WIKI_SUBDIR,
    WIKI_SUBDIRS,
    context_root,
    wiki_dir,
)
from version import get_version  # noqa: E402

PUBLISH_FILE = "publish.json"
REPORTS_SUBDIR = "compathy-reports"
CONFIG_SCHEMA_VERSION = 1
VISIBILITY_LEVELS = VALID_VISIBILITY  # ordered: private < team < org < public
AUDIENCES = ("team", "org", "public")
DEFAULT_AUDIENCE = "org"
DEFAULT_BRANCH = "main"
# Frontmatter keys a published page keeps. Everything else (sources,
# related_paths, custom notes) stays in the owner's wiki.
KEEP_FRONTMATTER = ("type", "schema_version", "created", "updated", "aliases",
                    "authority", "override", "extends", "visibility")
GENERATED = {"index", "log"}
MAX_DIFF_BYTES = 2_000_000

PRIVATE_OPEN_RE = re.compile(r"<!--\s*private\s*-->", re.IGNORECASE)
PRIVATE_CLOSE_RE = re.compile(r"<!--\s*/\s*private\s*-->", re.IGNORECASE)
PRIVATE_ANY_RE = re.compile(r"<!--\s*(/?)\s*private\s*-->", re.IGNORECASE)
# Same code spans lint ignores (fenced first, then inline), so the links we
# rewrite are exactly the links lint sees.
CODE_SPAN_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
LINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
INDEX_LINE_RE = re.compile(
    r"^\s*[-*]\s+\[\[([a-z0-9-]+)(?:\|[^\]]*)?\]\]\s*(?::|-|\u2013|\u2014)?\s*(.*)$")
LOG_ENTRY_RE = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] ([\w-]+) \| (.*)$")

# Built-in scan rules: (rule id, pattern, flags). Deny patterns from the
# owner's config are added as deny:<n>. Emails are handled separately so
# the allowed domains can pass.
SCAN_RULES = (
    ("secret:private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0),
    ("secret:aws-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 0),
    ("secret:github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})", 0),
    ("secret:slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", 0),
    ("secret:api-key", r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9]{32,}|AIza[0-9A-Za-z_-]{35})", 0),
    ("secret:jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0),
    ("secret:assignment",
     r"\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_.\-]{12,}", re.IGNORECASE),
    ("pii:ssn", r"\b\d{3}-\d{2}-\d{4}\b", 0),
    ("pii:phone", r"(?:\(\d{3}\)\s?|\b\d{3}[-.])\d{3}[-.]\d{4}\b", 0),
    ("path:home", r"(?:/Users|/home)/[A-Za-z0-9._-]+/[^\s`'\"()<>]*|\b[A-Za-z]:\\Users\\[^\s`'\"()<>]+", 0),
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")


class PublishError(Exception):
    """User-facing failure; rendered as {"error": ...} with exit 1."""


# ---------- small helpers ----------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today() -> str:
    return _dt.date.today().isoformat()


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(text: str) -> str:
    return _sha(text)[:12]


def config_path(target) -> Path:
    """context/publish.json in the owner's project (never published)."""
    return context_root(target) / PUBLISH_FILE


def reports_dir(target) -> Path:
    """context/compathy-reports/ (local, gitignored by convention)."""
    return context_root(target) / REPORTS_SUBDIR


def _level(visibility) -> int:
    return VISIBILITY_LEVELS.index(visibility) if visibility in VISIBILITY_LEVELS else -1


def _append_log(target: Path, op: str, summary: str, details: Optional[list] = None) -> None:
    lp = wiki_dir(target) / LOG_FILE
    if not lp.is_file():
        return
    text = lp.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n## [{_today()}] {op} | {summary}\n"
    for d in details or []:
        text += f"- {d}\n"
    lp.write_text(text, encoding="utf-8")


def _git(repo: Path, *args, check: bool = True):
    r = layers_mod.git(list(args), cwd=Path(repo))
    if check and r.returncode != 0:
        raise PublishError(f"git {' '.join(args[:2])} failed in {repo}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def _title_of(body: str, slug: str) -> str:
    for line in (body or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return slug.replace("-", " ")


def _expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


# ---------- config ----------

def validate_config(cfg) -> list:
    """Return problems with a publish.json document (empty list = valid)."""
    if not isinstance(cfg, dict):
        return ["publish.json: top level must be an object"]
    probs = []
    if cfg.get("schema_version") != CONFIG_SCHEMA_VERSION:
        probs.append(f"publish.json: schema_version must be {CONFIG_SCHEMA_VERSION}")
    layer = cfg.get("layer")
    if not isinstance(layer, dict):
        probs.append("publish.json: 'layer' must be an object with id, role, title")
    else:
        if not isinstance(layer.get("id"), str) or not lin.LAYER_ID_RE.match(layer["id"]):
            probs.append("publish.json: layer.id must be like 'acme' or 'acme/pricing'")
        if layer.get("role") not in lin.LAYER_ROLES:
            probs.append(f"publish.json: layer.role must be one of {lin.LAYER_ROLES}")
        if not isinstance(layer.get("title"), str) or not layer["title"].strip():
            probs.append("publish.json: layer.title required")
    if cfg.get("audience", DEFAULT_AUDIENCE) not in AUDIENCES:
        probs.append(f"publish.json: audience must be one of {AUDIENCES}")
    if not isinstance(cfg.get("repo"), str) or not cfg["repo"].strip():
        probs.append("publish.json: 'repo' (the local core repo path) required")
    if not lin.valid_layer_path(cfg.get("path", CONTEXT_DIR)):
        probs.append("publish.json: 'path' must be relative without '..'")
    if not isinstance(cfg.get("branch", DEFAULT_BRANCH), str) or not cfg.get("branch", DEFAULT_BRANCH):
        probs.append("publish.json: 'branch' must be a string")
    remote = cfg.get("remote")
    if remote is not None and not lin.valid_source(remote):
        probs.append(f"publish.json: remote must start with one of {lin.SOURCE_SCHEMES}")
    elif remote is not None and re.match(r"^git\+https://[^/]*@", remote):
        probs.append("publish.json: remote must not embed credentials; let git's credential helper "
                     "sign in (the remote is copied into the README and the personas)")
    bundle = cfg.get("bundle")
    if bundle is not None and (not isinstance(bundle, str) or not bundle.endswith(".bundle")):
        probs.append("publish.json: bundle must be a file path ending in .bundle")
    owner = cfg.get("owner", {})
    if not isinstance(owner, dict):
        probs.append("publish.json: 'owner' must be an object")
    scan = cfg.get("scan", {})
    if not isinstance(scan, dict):
        probs.append("publish.json: 'scan' must be an object")
        scan = {}
    deny = scan.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(d, str) and d for d in deny):
        probs.append("publish.json: scan.deny must be a list of regular expressions")
    else:
        for i, d in enumerate(deny):
            try:
                re.compile(d)
            except re.error as e:
                probs.append(f"publish.json: scan.deny[{i}] is not a valid regular expression ({e})")
    domains = scan.get("email_domains", [])
    if not isinstance(domains, list) or not all(isinstance(d, str) and d for d in domains):
        probs.append("publish.json: scan.email_domains must be a list of domains")
    allow = scan.get("allow", [])
    if not isinstance(allow, list):
        probs.append("publish.json: scan.allow must be a list")
    else:
        for i, a in enumerate(allow):
            if not isinstance(a, dict) or not isinstance(a.get("rule"), str) or not a.get("rule"):
                probs.append(f"publish.json: scan.allow[{i}] needs a 'rule'")
            elif not isinstance(a.get("reason"), str) or not a["reason"].strip():
                probs.append(f"publish.json: scan.allow[{i}] needs a 'reason'")
    personas = cfg.get("personas", {})
    if not isinstance(personas, dict):
        probs.append("publish.json: 'personas' must be an object keyed by role")
    else:
        for role, spec in personas.items():
            if not lin.SLUG_RE.match(str(role)):
                probs.append(f"publish.json: persona role {role!r} must be kebab-case")
            if not isinstance(spec, dict) or not isinstance(spec.get("title"), str) or not spec["title"].strip():
                probs.append(f"publish.json: personas.{role}.title required")
                continue
            for key in ("tags", "reads_first", "responsibilities"):
                v = spec.get(key, [])
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    probs.append(f"publish.json: personas.{role}.{key} must be a list of strings")
    return probs


def load_config(target: Path) -> dict:
    path = config_path(target)
    if not path.is_file():
        raise PublishError(f"{path} not found; run `publish.py init` first")
    try:
        cfg = lin.read_json(path)
    except lin.LineageError as e:
        raise PublishError(str(e)) from e
    probs = validate_config(cfg)
    if probs:
        raise PublishError("; ".join(probs))
    return cfg


def _core_paths(cfg: dict) -> dict:
    repo = _expand(cfg["repo"]).resolve()
    rel = (cfg.get("path") or CONTEXT_DIR).strip("/")
    ctx = repo / rel
    return {"repo": repo, "rel": rel, "context": ctx, "wiki": ctx / WIKI_SUBDIR,
            "personas": ctx / PERSONAS_SUBDIR}


def layer_source(cfg: dict) -> str:
    """The canonical source teammates pin: remote, else bundle, else the local repo."""
    if cfg.get("remote"):
        return cfg["remote"]
    if cfg.get("bundle"):
        return "git+file://" + str(_expand(cfg["bundle"]).resolve())
    return "git+file://" + str(_core_paths(cfg)["repo"])


# ---------- page transforms ----------

def split_frontmatter(text: str):
    """Return (frontmatter_lines, body) or (None, text) when there is none."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None, text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return lines[1:i], "".join(lines[i + 1:])
    raise ValueError("frontmatter: missing closing '---' delimiter")


def filter_frontmatter(fm_lines: list) -> tuple:
    """Keep allowlisted keys, line for line. Returns (kept_lines, dropped_keys)."""
    kept, dropped = [], []
    for raw in fm_lines:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key = line.partition(":")[0].strip()
        if key in KEEP_FRONTMATTER:
            kept.append(line + "\n")
        else:
            dropped.append(key)
    return kept, dropped


def _segments(body: str) -> list:
    """Split body into [(is_code, text)] using lint's notion of code."""
    out, pos = [], 0
    for m in CODE_SPAN_RE.finditer(body):
        if m.start() > pos:
            out.append((False, body[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(body):
        out.append((False, body[pos:]))
    return out


def strip_private(body: str) -> tuple:
    """Remove <!-- private --> ... <!-- /private --> blocks outside code.

    Returns (body, blocks_removed). Raises ValueError on an unbalanced or
    nested marker: a page whose private sections cannot be told apart is
    not published (fail closed).
    """
    out = []
    inside = False
    removed = 0
    for is_code, text in _segments(body):
        if is_code:
            if not inside:
                out.append(text)
            continue
        pos = 0
        for m in PRIVATE_ANY_RE.finditer(text):
            closing = bool(m.group(1))
            if not closing and inside:
                raise ValueError("nested <!-- private --> marker")
            if closing and not inside:
                raise ValueError("<!-- /private --> without an opening marker")
            if not closing:
                out.append(text[pos:m.start()])
                inside = True
            else:
                inside = False
                removed += 1
            pos = m.end()
        if not inside:
            out.append(text[pos:])
    if inside:
        raise ValueError("<!-- private --> is never closed")
    result = "".join(out)
    if removed:
        result = re.sub(r"\n{3,}", "\n\n", result)
    return result, removed


def rewrite_links(body: str, keep: set, titles: dict) -> tuple:
    """Turn [[slug]] into plain text unless slug is in ``keep``.

    ``[[slug|label]]`` becomes ``label``; a bare ``[[slug]]`` becomes the
    page's title (or the slug in words). Code spans are left alone.
    Returns (body, [unlinked slugs]).
    """
    unlinked = []

    def sub(m):
        token = m.group(1)
        slug, _, label = token.partition("|")
        slug = slug.strip()
        if slug in keep:
            return m.group(0)
        unlinked.append(slug)
        return label.strip() if label.strip() else titles.get(slug) or slug.replace("-", " ")

    out = []
    for is_code, text in _segments(body):
        out.append(text if is_code else LINK_RE.sub(sub, text))
    return "".join(out), unlinked


# ---------- scan ----------

def _compiled_rules(cfg: dict) -> list:
    rules = [(rid, re.compile(p, flags)) for rid, p, flags in SCAN_RULES]
    for i, d in enumerate((cfg.get("scan") or {}).get("deny") or []):
        rules.append((f"deny:{i}", re.compile(d, re.IGNORECASE)))
    return rules


def _mask(line: str, spans: list) -> str:
    """The line with every finding replaced by its rule id, trimmed around the first one."""
    out, pos, first = [], 0, None
    for start, end, rule in sorted(spans):
        if start < pos:
            continue
        out.append(line[pos:start])
        if first is None:
            first = sum(len(x) for x in out)
        out.append(f"[{rule}]")
        pos = end
    out.append(line[pos:])
    masked = "".join(out)
    lo = max(0, (first or 0) - 60)
    snippet = masked[lo:lo + 160]
    return ("..." if lo else "") + snippet.strip()


def scan_text(rel: str, text: str, cfg: dict) -> list:
    """Findings for one file. The matched text itself is never returned."""
    domains = [d.lower().lstrip("@") for d in (cfg.get("scan") or {}).get("email_domains") or []]
    rules = _compiled_rules(cfg)
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        spans = []
        for rid, rx in rules:
            for m in rx.finditer(line):
                if m.end() > m.start():
                    spans.append((m.start(), m.end(), rid))
        for m in EMAIL_RE.finditer(line):
            dom = m.group(1).lower()
            if any(dom == d or dom.endswith("." + d) for d in domains):
                continue
            spans.append((m.start(), m.end(), "pii:email"))
        if not spans:
            continue
        for start, end, rid in spans:
            findings.append({"rule": rid, "file": rel, "line": lineno, "col": start + 1,
                             "fingerprint": _fingerprint(line[start:end]),
                             "context": _mask(line, spans)})
    return findings


def apply_allow(findings: list, cfg: dict) -> tuple:
    """Split findings into (blocking, allowed, unused_allow_entries)."""
    allow = (cfg.get("scan") or {}).get("allow") or []
    used = set()
    blocking, allowed = [], []
    for f in findings:
        hit = None
        for i, a in enumerate(allow):
            rule = a.get("rule", "")
            if rule.endswith("*"):
                if not f["rule"].startswith(rule[:-1]):
                    continue
            elif rule != f["rule"]:
                continue
            if a.get("file") and a["file"] != f["file"]:
                continue
            if a.get("fingerprint") and a["fingerprint"] != f["fingerprint"]:
                continue
            hit = i
            break
        if hit is None:
            blocking.append(f)
        else:
            used.add(hit)
            allowed.append({**f, "reason": allow[hit]["reason"]})
    unused = [a for i, a in enumerate(allow) if i not in used]
    return blocking, allowed, unused


# ---------- build ----------

def _parent_slugs(target: Path) -> set:
    """Slugs resolvable in the owner's own cached parent layers (inherited by the core)."""
    try:
        doc = lin.load_lineage(target)
    except lin.LineageError:
        return set()
    if not doc:
        return set()
    out = set()
    for layer in lin.parent_layers(lin.resolve_layers(target, doc)):
        wiki = layer.get("wiki")
        if wiki is None or not layer.get("cached"):
            continue
        for slug, _ in iter_wiki_pages(Path(wiki)):
            out.add(slug)
    return out


def _parent_entries(target: Path) -> list:
    try:
        doc = lin.load_lineage(target)
    except lin.LineageError as e:
        raise PublishError(f"context/lineage.json is invalid: {e}") from e
    if not doc:
        return []
    return [{k: l.get(k) for k in ("id", "role", "source", "path", "pin", "tree_sha")}
            for l in doc.get("layers", [])]


def _source_index_summaries(target: Path) -> tuple:
    """(order, {slug: summary}) from the owner's index.md."""
    order, summaries = [], {}
    idx = wiki_dir(target) / INDEX_FILE
    if not idx.is_file():
        return order, summaries
    for line in idx.read_text(encoding="utf-8").splitlines():
        m = INDEX_LINE_RE.match(line)
        if m and m.group(1) not in summaries:
            order.append(m.group(1))
            summaries[m.group(1)] = m.group(2).strip()
    return order, summaries


def _read_existing(paths: dict, rel: str) -> Optional[str]:
    p = paths["repo"] / rel
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None


def _frontmatter_value(text: Optional[str], key: str) -> Optional[str]:
    if not text:
        return None
    try:
        fm, _ = parse_frontmatter(text)
    except ValueError:
        return None
    v = fm.get(key)
    return str(v) if v is not None else None


def _owner_label(cfg: dict) -> str:
    owner = cfg.get("owner") or {}
    return owner.get("name") or owner.get("email") or "the owner"


def _bundle_hint(cfg: dict) -> Optional[str]:
    """A display name for the bundle without the owner's personal path."""
    b = cfg.get("bundle")
    if not b:
        return None
    parts = Path(b).parts
    for marker in ("Shared drives", "My Drive", "Shared with me"):
        if marker in parts:
            return "/".join(parts[parts.index(marker):])
    return Path(b).name


def _render_index(cfg: dict, pages: dict, order: list, summaries: dict, keep: set,
                  titles: dict, created: str) -> str:
    layer = cfg["layer"]
    lines = ["---", "type: index", f"schema_version: {SCHEMA_VERSION}", f"created: {created}",
             "updated: {updated}", "---", "", f"# {layer['title']}: index", "",
             f"The shared core of {_owner_label(cfg)}'s compathy wiki, published with "
             f"`/compathy-publish`. Read it as the source of truth for {layer['title']}. It is "
             "read-only for everyone who links to it and changes only when the owner publishes "
             "again; [[log]] lists every publish.", ""]
    rank = {s: i for i, s in enumerate(order)}
    for sub, heading in zip(WIKI_SUBDIRS, ("Concepts", "Entities", "Summaries", "Patterns")):
        slugs = [s for s, p in pages.items() if p["sub"] == sub]
        if not slugs:
            continue
        slugs.sort(key=lambda s: (rank.get(s, len(rank)), s))
        lines += [f"## {heading}", ""]
        for s in slugs:
            summary = summaries.get(s) or pages[s]["title"]
            summary, _ = rewrite_links(summary, keep, titles)
            lines.append(f"- [[{s}]]: {summary}".rstrip())
        lines.append("")
    lines += ["---", "", "## How to use this layer", "",
              "- Link to these pages from your own wiki with `[[slug]]`; do not copy them.",
              "- `/compathy-persona update` shows what changed when the owner publishes again.",
              f"- Corrections go to {_owner_label(cfg)}; edits made in a copy of this layer are "
              "overwritten by the next publish.", ""]
    return "\n".join(lines)


def _render_schema(cfg: dict, created: str) -> str:
    title = cfg["layer"]["title"]
    return "\n".join([
        f"# Wiki schema: {title}", "",
        "> Generated by `/compathy-publish`. This layer is a published subset of a",
        "> larger compathy wiki; edit the source wiki, never this copy.", "",
        f"schema_version: {SCHEMA_VERSION}", f"created: {created}", "", "---", "",
        "## Pages", "",
        "Page types and folders follow compathy: `concepts/`, `entities/`, `summaries/`,",
        "`patterns/`, plus the generated `index.md` and `log.md`. Frontmatter is flat YAML",
        "with `type` and `schema_version` required; `visibility` records the audience the",
        "page was published to.", "",
        "## Backlinks", "",
        "`[[slug]]` links resolve inside this layer (and its own parents). Links to pages",
        "that were not published were turned into plain text.", "",
        "## Log", "",
        "`log.md` holds one `publish` entry per publish: the owner's note and the pages",
        "added, changed and removed.", ""])


def _render_readme(cfg: dict, roles: list) -> str:
    layer = cfg["layer"]
    owner = cfg.get("owner") or {}
    lines = [f"# {layer['title']}", "",
             f"Read-only context published from {_owner_label(cfg)}'s compathy wiki with "
             "`/compathy-publish`. Every publish replaces "
             f"`{(cfg.get('path') or CONTEXT_DIR)}/`, so do not edit files here.", "",
             "## Use it in your own compathy", "",
             "Install compathy (https://github.com/gitbk4/compathy), open the folder for your "
             "own work, and import the persona for your role:", ""]
    if cfg.get("remote"):
        # Only a remote is printed; a local path would name the owner's machine.
        lines.append("```")
        for role in roles or ["<role>"]:
            lines.append(f"/compathy-persona import {cfg['remote']}#{role}")
        lines += ["```", ""]
    hint = _bundle_hint(cfg)
    if hint:
        lines += [("Without access to this repository, use" if cfg.get("remote") else "Use") +
                  f" the bundle file kept in `{hint}` (sync it to your machine first):", "", "```"]
        for role in roles or ["<role>"]:
            lines.append(f"/compathy-persona import \"<path to {Path(cfg['bundle']).name}>#{role}\"")
        lines += ["```", ""]
    lines += ["Your own wiki stays yours to edit. This layer is cached read-only at a pinned "
              "commit, and `/compathy-persona update` shows what changed after each publish.", "",
              "## Corrections", ""]
    contact = owner.get("email") or owner.get("name") or "the owner"
    lines += [f"Send corrections to {contact}. Changes made in this repository are overwritten "
              "by the next publish.", "",
              "## Handling", "",
              f"Shared for the {cfg.get('audience', DEFAULT_AUDIENCE)} audience only. Keep it in "
              "private repositories and approved tools; do not copy it anywhere public.", ""]
    return "\n".join(lines)


def _log_entry(note: Optional[str], counts: dict, changes: dict) -> list:
    summary = note.strip() if note and note.strip() else (
        f"{counts['published']} pages (+{len(changes['added'])} ~{len(changes['changed'])} "
        f"-{len(changes['removed'])})")
    lines = [f"## [{_today()}] publish | {summary}", "",
             f"- pages: {counts['published']} (+{len(changes['added'])} ~{len(changes['changed'])} "
             f"-{len(changes['removed'])})"]
    for key in ("added", "changed", "removed"):
        if changes[key]:
            lines.append(f"- {key}: {', '.join(changes[key])}")
    return lines


def build(target: Path, cfg: dict, note: Optional[str] = None) -> dict:
    """Compute everything a publish would write, without touching the core repo.

    Returns {files: {relpath: text}, managed_dirs, pages, report...}. Paths in
    ``files`` are relative to the core repo root.
    """
    target = Path(target).resolve()
    wiki = wiki_dir(target)
    if not (wiki / INDEX_FILE).is_file():
        raise PublishError("no compathy wiki here; run /compathy first")
    paths = _core_paths(cfg)
    rel = paths["rel"]
    audience = cfg.get("audience", DEFAULT_AUDIENCE)
    need = _level(audience)

    pages, titles, problems = {}, {}, []
    counts = {"published": 0, "private": 0, "by_visibility": {}}
    raw = {}
    for slug, path in iter_wiki_pages(wiki):
        try:
            text = path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
        except (OSError, ValueError) as e:
            problems.append({"page": slug, "problem": f"unreadable, not published: {e}"})
            continue
        titles[slug] = _title_of(body, slug)
        vis = fm.get("visibility") or "private"
        counts["by_visibility"][str(vis)] = counts["by_visibility"].get(str(vis), 0) + 1
        if vis not in VISIBILITY_LEVELS:
            problems.append({"page": slug, "problem": f"visibility {vis!r} is not one of {VISIBILITY_LEVELS}",
                             "blocking": True})
            continue
        if _level(vis) < need:
            counts["private"] += 1
            continue
        raw[slug] = (path, text)

    keep = set(raw) | GENERATED | _parent_slugs(target)
    unlinked, private_blocks, dropped = [], [], {}
    for slug, (path, text) in sorted(raw.items()):
        try:
            fm_lines, body = split_frontmatter(text)
            body, removed = strip_private(body)
        except ValueError as e:
            problems.append({"page": slug, "problem": str(e), "blocking": True})
            continue
        kept, gone = filter_frontmatter(fm_lines or [])
        for k in gone:
            dropped[k] = dropped.get(k, 0) + 1
        body, links = rewrite_links(body, keep, titles)
        if links:
            unlinked.append({"page": slug, "targets": sorted(set(links))})
        if removed:
            private_blocks.append({"page": slug, "blocks": removed})
        pages[slug] = {"sub": path.parent.name, "title": titles[slug],
                       "text": "---\n" + "".join(kept) + "---\n" + body}
    counts["published"] = len(pages)

    files = {}
    for slug, p in pages.items():
        files[f"{rel}/{WIKI_SUBDIR}/{p['sub']}/{slug}.md"] = p["text"]

    # Compare pages with what the core repo holds now.
    existing_pages = {}
    if paths["wiki"].is_dir():
        for slug, path in iter_wiki_pages(paths["wiki"]):
            try:
                existing_pages[slug] = path.read_text(encoding="utf-8")
            except OSError:
                existing_pages[slug] = ""
    changes = {"added": sorted(s for s in pages if s not in existing_pages),
               "changed": sorted(s for s in pages if s in existing_pages and existing_pages[s] != pages[s]["text"]),
               "removed": sorted(s for s in existing_pages if s not in pages)}
    unchanged = len(pages) - len(changes["added"]) - len(changes["changed"])

    order, summaries = _source_index_summaries(target)
    idx_rel = f"{rel}/{WIKI_SUBDIR}/{INDEX_FILE}"
    old_index = _read_existing(paths, idx_rel)
    created = _frontmatter_value(old_index, "created") or _today()
    index_tpl = _render_index(cfg, pages, order, summaries, keep, titles, created)
    old_updated = _frontmatter_value(old_index, "updated")
    index_text = index_tpl.replace("updated: {updated}", f"updated: {old_updated or _today()}", 1)
    if old_index is not None and index_text != old_index:
        index_text = index_tpl.replace("updated: {updated}", f"updated: {_today()}", 1)
    files[idx_rel] = index_text

    schema_rel = f"{rel}/{SCHEMA_FILE}"
    files[schema_rel] = _render_schema(cfg, _frontmatter_created_from_schema(_read_existing(paths, schema_rel)) or created)

    parents = _parent_entries(target)
    lineage_rel = f"{rel}/{LINEAGE_FILE}"
    if parents:
        doc = {"schema_version": lin.LINEAGE_SCHEMA_VERSION, "layers": parents,
               "self": {"id": cfg["layer"]["id"], "role": cfg["layer"]["role"]}}
        files[lineage_rel] = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    roles = sorted((cfg.get("personas") or {}).keys())
    files["README.md"] = _render_readme(cfg, roles)
    gh = ((cfg.get("owner") or {}).get("github") or "").lstrip("@")
    if gh:
        files[".github/CODEOWNERS"] = f"# Only the owner publishes this layer.\n* @{gh}\n"

    # Pages (or their index) changed: that is a publish teammates should hear
    # about, so it gets a log entry. README/schema/lineage-only edits are
    # committed quietly and leave wiki/ (and its tree hash) alone.
    pages_changed = any(changes.values()) or index_text != (old_index or "")
    other_files = [schema_rel, lineage_rel, "README.md", ".github/CODEOWNERS"]
    other_changed = any(files.get(r) != _read_existing(paths, r) for r in other_files)
    log_rel = f"{rel}/{WIKI_SUBDIR}/{LOG_FILE}"
    old_log = _read_existing(paths, log_rel)
    if old_log is None:
        old_log = "\n".join(["---", "type: log", f"schema_version: {SCHEMA_VERSION}", f"created: {created}",
                             "---", "", f"# {cfg['layer']['title']}: log", "",
                             "> Append-only. One `publish` entry per publish.",
                             "> Format: `## [YYYY-MM-DD] publish | <note>`", ""])
    log_text = old_log
    if pages_changed:
        if not log_text.endswith("\n"):
            log_text += "\n"
        log_text += "\n" + "\n".join(_log_entry(note, counts, changes)) + "\n"
    files[log_rel] = log_text

    managed = [f"{rel}/{WIKI_SUBDIR}", schema_rel, lineage_rel, "README.md", ".github/CODEOWNERS"]
    return {"files": files, "managed": managed, "pages": pages, "titles": titles,
            "keep": keep, "counts": counts, "changes": changes, "unchanged": unchanged,
            "unlinked": unlinked, "private_blocks": private_blocks, "dropped_frontmatter": dropped,
            "problems": problems, "pages_changed": pages_changed,
            "content_changed": pages_changed or other_changed, "paths": paths}


def _frontmatter_created_from_schema(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"^created: (\d{4}-\d{2}-\d{2})$", text, re.MULTILINE)
    return m.group(1) if m else None


# ---------- personas ----------

def build_personas(cfg: dict, pin: str, tree: str, parents: list, published: set,
                   exported_by: str) -> dict:
    """{role: manifest} for the configured personas, pinned to the content commit."""
    import persona as persona_mod  # pylint: disable=import-outside-toplevel
    layer = cfg["layer"]
    core = {"id": layer["id"], "role": layer["role"], "source": layer_source(cfg),
            "path": (cfg.get("path") or CONTEXT_DIR).strip("/"), "pin": pin, "tree_sha": tree}
    parent_ids = [p["id"] for p in parents]
    out = {}
    for role, spec in sorted((cfg.get("personas") or {}).items()):
        reads = []
        for ref in spec.get("reads_first") or []:
            lid, slug = lin.parse_ref(ref)
            if not lid:
                if slug not in published:
                    raise PublishError(f"personas.{role}.reads_first: {slug!r} is not a published page")
                reads.append(f"{layer['id']}/{slug}")
            elif lid == layer["id"]:
                if slug not in published:
                    raise PublishError(f"personas.{role}.reads_first: {slug!r} is not a published page")
                reads.append(ref)
            elif lid in parent_ids:
                reads.append(ref)
            else:
                raise PublishError(f"personas.{role}.reads_first: unknown layer in {ref!r}")
        manifest = {
            "schema_version": lin.MANIFEST_SCHEMA_VERSION,
            "kind": lin.MANIFEST_KIND,
            "id": f"{layer['id']}/{role}",
            "title": spec["title"],
            "summary": str(spec.get("summary") or ""),
            "tags": [str(t) for t in spec.get("tags") or []],
            "layers": [core] + parents,
            "reads_first": reads,
            "responsibilities": [str(r) for r in spec.get("responsibilities") or []],
            "toolkit": spec.get("toolkit") or persona_mod.DEFAULT_TOOLKIT,
            "policy": {"may_edit": ["project"], "may_propose_to": [layer["id"]],
                       "read_only": [layer["id"]] + parent_ids},
            "provenance": {"exported_by": exported_by, "exported_at": _now_iso(),
                           "from_commit": pin, "exporter_version": get_version(),
                           "via": "compathy-publish"},
            "signature": None,
        }
        probs = lin.validate_manifest(manifest)
        if probs:
            raise PublishError(f"persona {role} failed validation: " + "; ".join(probs))
        out[role] = manifest
    return out


# ---------- plan ----------

def _source_line(target: Path, b: dict, finding: dict, cfg: dict) -> Optional[dict]:
    """Where the owner fixes a finding: the same match in the source page.

    Published pages lose frontmatter lines and private sections, so their
    line numbers drift from the source. The match itself survives, so look
    it up by rule and fingerprint.
    """
    prefix = f"{b['paths']['rel']}/{WIKI_SUBDIR}/"
    if not finding["file"].startswith(prefix):
        return None
    rel = finding["file"][len(prefix):]
    src = wiki_dir(target) / rel
    try:
        lines = src.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for i, line in enumerate(lines, start=1):
        for f in scan_text(rel, line, cfg):
            if f["rule"] == finding["rule"] and f["fingerprint"] == finding["fingerprint"]:
                return {"file": f"{WIKI_SUBDIR}/{rel}", "line": i}
    return None


def _scan_build(b: dict, cfg: dict, target: Optional[Path] = None) -> list:
    findings = []
    for rel, text in sorted(b["files"].items()):
        for f in scan_text(rel, text, cfg):
            if target is not None:
                f["source"] = _source_line(target, b, f, cfg)
            findings.append(f)
    # Persona text comes from the config, so scan it before it is written.
    for role, spec in sorted((cfg.get("personas") or {}).items()):
        blob = "\n".join([spec.get("title") or "", spec.get("summary") or ""] +
                         list(spec.get("responsibilities") or []) + list(spec.get("tags") or []))
        findings.extend(scan_text(f"personas/{role}", blob, cfg))
    return findings


def _lint_build(b: dict, cfg: dict) -> dict:
    """Write the build to a temp context/ and run the regular linter on it."""
    tmp = Path(tempfile.mkdtemp(prefix="compathy-publish-"))
    try:
        rel = b["paths"]["rel"]
        for path, text in b["files"].items():
            if not path.startswith(rel + "/"):
                continue
            dest = tmp / CONTEXT_DIR / path[len(rel) + 1:]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        result = run_lint(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return result


def _diff_text(b: dict) -> str:
    repo = b["paths"]["repo"]
    chunks = []
    seen = set()
    for rel, text in sorted(b["files"].items()):
        seen.add(rel)
        old = _read_existing(b["paths"], rel)
        if old == text:
            continue
        chunks.append("".join(difflib.unified_diff(
            (old or "").splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=f"a/{rel}" if old is not None else "/dev/null", tofile=f"b/{rel}")))
    for slug in b["changes"]["removed"]:
        for sub in WIKI_SUBDIRS:
            rel = f"{b['paths']['rel']}/{WIKI_SUBDIR}/{sub}/{slug}.md"
            if rel in seen or not (repo / rel).is_file():
                continue
            old = (repo / rel).read_text(encoding="utf-8")
            chunks.append("".join(difflib.unified_diff(
                old.splitlines(keepends=True), [], fromfile=f"a/{rel}", tofile="/dev/null")))
    return "\n".join(c for c in chunks if c)


def plan(target: Path, cfg: dict, note: Optional[str] = None, write_diff: bool = False) -> dict:
    b = build(target, cfg, note)
    findings = _scan_build(b, cfg, Path(target).resolve())
    blocking, allowed, unused = apply_allow(findings, cfg)
    lint_result = _lint_build(b, cfg)
    blockers = []
    if blocking:
        blockers.append(f"{len(blocking)} scan finding(s); remove the text, mark it private, "
                        "or add an allow entry with a reason")
    bad_pages = [p for p in b["problems"] if p.get("blocking")]
    if bad_pages:
        blockers.append(f"{len(bad_pages)} page problem(s)")
    if lint_result["summary"]["errors"]:
        blockers.append(f"lint: {lint_result['summary']['errors']} error(s) in the built layer")
    if not b["pages"]:
        blockers.append(f"no page has visibility {cfg.get('audience', DEFAULT_AUDIENCE)} or wider; "
                        "mark pages with `publish.py mark`")
    out = {
        "layer": cfg["layer"], "audience": cfg.get("audience", DEFAULT_AUDIENCE),
        "source": layer_source(cfg), "repo": str(b["paths"]["repo"]),
        "pages": {"published": b["counts"]["published"], "private": b["counts"]["private"],
                  "by_visibility": b["counts"]["by_visibility"]},
        "published": [{"slug": s, "folder": p["sub"], "title": p["title"]}
                      for s, p in sorted(b["pages"].items())],
        "changes": {**b["changes"], "unchanged": b["unchanged"],
                    "nothing_to_publish": not b["content_changed"]},
        "unlinked": b["unlinked"],
        "unlinked_total": sum(len(u["targets"]) for u in b["unlinked"]),
        "private_blocks": b["private_blocks"],
        "dropped_frontmatter": b["dropped_frontmatter"],
        "page_problems": b["problems"],
        "findings": blocking, "allowed": allowed, "unused_allow": unused,
        "lint": {"errors": lint_result["errors"], "warnings": lint_result["summary"]["warnings"]},
        "personas": sorted((cfg.get("personas") or {}).keys()),
        "blockers": blockers, "ok": not blockers,
    }
    if write_diff:
        text = _diff_text(b)
        rd = reports_dir(target)
        rd.mkdir(parents=True, exist_ok=True)
        dp = rd / f"publish-{_today()}.diff"
        dp.write_text(text[:MAX_DIFF_BYTES], encoding="utf-8")
        out["diff_file"] = str(dp)
        out["diff_lines"] = text.count("\n")
    if out["changes"]["nothing_to_publish"] and not blockers:
        out["next"] = "nothing to publish: the core repo already matches"
    elif blockers:
        out["next"] = "fix the blockers, then run plan again"
    else:
        out["next"] = "review the plan (and the diff), then run: publish.py apply --note '<what changed>'"
    out["_build"] = b
    return out


# ---------- apply ----------

def _check_repo(paths: dict, target: Path, branch: str) -> dict:
    repo = paths["repo"]
    src = Path(target).resolve()
    if repo == src or src in repo.parents or repo in src.parents:
        raise PublishError(f"the core repo {repo} must live outside the source project {src}")
    if not (repo / ".git").exists():
        raise PublishError(f"{repo} is not a git repository; run `publish.py init` first")
    r = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if r.stdout.strip():
        raise PublishError(f"{repo} has uncommitted changes; the core repo is written only by publish. "
                           "Commit or discard them first")
    head = layers_mod.head_sha(repo)
    cur = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    if cur and cur != branch:
        raise PublishError(f"{repo} is on branch {cur!r}; publish writes {branch!r}. Check it out first")
    return {"head": head, "branch": cur or branch}


def _sync_files(b: dict) -> None:
    repo = b["paths"]["repo"]
    wiki = b["paths"]["wiki"]
    keep = {(repo / rel).resolve() for rel in b["files"]}
    if wiki.is_dir():
        for p in sorted(wiki.rglob("*")):
            if p.is_file() and p.resolve() not in keep:
                p.unlink()
    for rel in ("README.md", ".github/CODEOWNERS", f"{b['paths']['rel']}/{LINEAGE_FILE}"):
        p = repo / rel
        if p.is_file() and p.resolve() not in keep:
            p.unlink()
    for rel, text in b["files"].items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


def _identity_args(repo: Path, cfg: dict) -> list:
    """``-c user.name/email`` for commits when git has no identity of its own.

    Checked before anything is written, so a machine without a git identity
    never ends up with a half-written core repo.
    """
    # useConfigOnly: an identity git would only guess from the hostname
    # (user@host.local) counts as missing.
    r = _git(repo, "-c", "user.useConfigOnly=true", "var", "GIT_COMMITTER_IDENT", check=False)
    a = _git(repo, "-c", "user.useConfigOnly=true", "var", "GIT_AUTHOR_IDENT", check=False)
    if r.returncode == 0 and a.returncode == 0:
        return []
    owner = cfg.get("owner") or {}
    if owner.get("name") and owner.get("email"):
        return ["-c", f"user.name={owner['name']}", "-c", f"user.email={owner['email']}"]
    raise PublishError("git has no user.name/user.email here and publish.json has no owner name and "
                       "email; set one of them before publishing")


def _commit(repo: Path, paths_to_add: list, message: str, ident: Optional[list] = None) -> Optional[str]:
    present = [p for p in paths_to_add if (repo / p).exists() or
               _git(repo, "ls-files", "--", p, check=False).stdout.strip()]
    if present:
        _git(repo, "add", "-A", "--", *present)
    if not _git(repo, "diff", "--cached", "--name-only", check=False).stdout.strip():
        return None
    _git(repo, *(ident or []), "commit", "-q", "-m", message)
    return layers_mod.head_sha(repo)


def _rollback(repo: Path, had_head: bool) -> None:
    """Return the core repo to the clean state apply started from."""
    if had_head:
        _git(repo, "reset", "-q", "--hard", "HEAD", check=False)
    else:
        _git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", ".", check=False)
    _git(repo, "clean", "-q", "-fd", check=False)


def apply(target: Path, cfg: dict, note: Optional[str] = None) -> dict:
    target = Path(target).resolve()
    p = plan(target, cfg, note)
    b = p.pop("_build")
    if not p["ok"]:
        raise PublishError("publish blocked: " + "; ".join(p["blockers"]))
    branch = cfg.get("branch") or DEFAULT_BRANCH
    paths = b["paths"]
    before = _check_repo(paths, target, branch)
    ident = _identity_args(paths["repo"], cfg)
    try:
        return _apply_checked(target, cfg, note, p, b, before, ident)
    except Exception:
        # The repo was clean when we started; leave it that way.
        _rollback(paths["repo"], bool(layers_mod.head_sha(paths["repo"]) or before["head"]))
        raise


def _apply_checked(target: Path, cfg: dict, note: Optional[str], p: dict, b: dict,
                   before: dict, ident: list) -> dict:
    paths = b["paths"]
    result = {"repo": str(paths["repo"]), "content_commit": None, "personas_commit": None,
              "changes": p["changes"], "pages": p["pages"]}
    if b["content_changed"] or not before["head"]:
        _sync_files(b)
        ch = b["changes"]
        if note and note.strip():
            subject = f"publish: {note.strip()}"
        elif b["pages_changed"]:
            subject = (f"publish: {b['counts']['published']} pages (+{len(ch['added'])} "
                       f"~{len(ch['changed'])} -{len(ch['removed'])})")
        else:
            subject = "publish: refresh README, schema and owners (no page changes)"
        body = []
        for key in ("added", "changed", "removed"):
            if ch[key]:
                body.append(f"{key}: {', '.join(ch[key])}")
        body.append(f"Published with compathy {get_version()} (/compathy-publish).")
        result["content_commit"] = _commit(paths["repo"], b["managed"], subject + "\n\n" + "\n".join(body),
                                           ident)
    pin = layers_mod.head_sha(paths["repo"])
    if pin is None:
        raise PublishError("nothing was committed and the core repo has no commits")
    if cfg.get("personas"):
        # Personas pin the latest commit whose wiki/ teammates should read.
        # Re-pin only when the content moved or the persona specs changed.
        pdir = paths["personas"]
        current = {}
        if pdir.is_dir():
            for f in pdir.glob("*.json"):
                if f.name != PERSONAS_INDEX_FILE:
                    try:
                        current[f.stem] = json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        current[f.stem] = None
        pinned = {(m.get("layers") or [{}])[0].get("pin") for m in current.values() if isinstance(m, dict)}
        if result["content_commit"] is None and len(pinned) == 1:
            pin = next(iter(pinned)) or pin
        tree = layers_mod.tree_sha(paths["repo"], pin, f"{paths['rel']}/{WIKI_SUBDIR}")
        r = _git(paths["repo"], "config", "user.email", check=False)
        exported_by = (cfg.get("owner") or {}).get("email") or r.stdout.strip() or "unknown"
        manifests = build_personas(cfg, pin, tree, _parent_entries(target), set(b["pages"]), exported_by)

        stale = set(current) != set(manifests) or any(
            not _same_manifest(current.get(role), m) for role, m in manifests.items())
        if stale:
            pdir.mkdir(parents=True, exist_ok=True)
            for f in pdir.glob("*.json"):
                if f.name != PERSONAS_INDEX_FILE and f.stem not in manifests:
                    f.unlink()
            for role, m in manifests.items():
                lin.write_json_atomic(pdir / f"{role}.json", m)
            _write_personas_index(pdir, cfg["layer"]["id"], manifests)
            result["personas_commit"] = _commit(paths["repo"], [f"{paths['rel']}/{PERSONAS_SUBDIR}"],
                                                f"personas: pin {pin[:12]}\n\n"
                                                f"{', '.join(sorted(manifests))} now pin the publish "
                                                f"at {pin[:12]}.", ident)
        result["personas"] = {role: f"{cfg['layer']['id']}/{role}" for role in manifests}
    result["pin"] = pin
    if result["content_commit"] or result["personas_commit"]:
        ch = b["changes"]
        _append_log(target, "publish", f"{cfg['layer']['id']} at {pin[:12]}"
                    + (f": {note.strip()}" if note and note.strip() else ""),
                    [f"pages: {b['counts']['published']} (+{len(ch['added'])} ~{len(ch['changed'])} "
                     f"-{len(ch['removed'])})", f"repo: {paths['repo']}"])
        result["next"] = "run `publish.py push` to send it to the remote and refresh the bundle"
    else:
        result["next"] = "nothing to publish: the core repo already matches"
    return result


def _same_manifest(a, b) -> bool:
    """Equal apart from the export timestamp."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False

    def strip(m):
        prov = {k: v for k, v in (m.get("provenance") or {}).items() if k != "exported_at"}
        return json.dumps({**m, "provenance": prov}, sort_keys=True)

    return strip(a) == strip(b)


def _write_personas_index(pdir: Path, layer_id: str, manifests: dict) -> None:
    entries = [{"id": m["id"], "file": f"{role}.json", "title": m.get("title"), "tags": m.get("tags") or [],
                "summary": m.get("summary"), "updated": m["provenance"]["exported_at"][:10]}
               for role, m in sorted(manifests.items())]
    lin.write_json_atomic(pdir / PERSONAS_INDEX_FILE, {"schema_version": lin.MANIFEST_SCHEMA_VERSION,
                                                        "team": layer_id, "generated_at": _now_iso(),
                                                        "personas": entries})


# ---------- push ----------

PRIVATE_HINTS = ("could not read username", "authentication failed", "repository not found",
                 "not found", "403", "401", "permission denied", "terminal prompts disabled",
                 "invalid username or password", "access denied")
UNREACHABLE_HINTS = ("could not resolve host", "failed to connect", "timed out", "network is unreachable",
                     "connection refused", "operation timed out")


def _anonymous_url(source: str) -> Optional[str]:
    """An https URL for an anonymous readability check, or None when there is none."""
    url = source[len("git+"):] if source.startswith("git+") else source
    if url.startswith("https://"):
        return url
    m = re.match(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url)
    if m and m.group(1) in ("github.com", "gitlab.com", "bitbucket.org"):
        return f"https://{m.group(1)}/{m.group(2)}"
    return None


def anonymous_readable(source: str) -> dict:
    """Can anyone read ``source`` without credentials?

    Runs ``git ls-remote`` with every credential source switched off (no
    helpers, no prompts, an empty HOME and no system config). Returns
    {public: True|False|None, reason}. None means compathy cannot tell.
    """
    url = _anonymous_url(source)
    if source.startswith("git+file://"):
        return {"public": False, "reason": "local file source"}
    if url is None:
        return {"public": None, "reason": "no https form of this remote to check anonymously"}
    home = Path(tempfile.mkdtemp(prefix="compathy-anon-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"})
    try:
        r = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "--heads", url],
                           capture_output=True, text=True, timeout=60, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"public": None, "reason": f"check failed: {e}"}
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if r.returncode == 0:
        return {"public": True, "reason": f"{url} answers without credentials"}
    err = (r.stderr or "").lower()
    if any(h in err for h in UNREACHABLE_HINTS):
        return {"public": None, "reason": "remote unreachable; cannot check"}
    if any(h in err for h in PRIVATE_HINTS):
        return {"public": False, "reason": "remote requires credentials"}
    return {"public": None, "reason": (r.stderr or "").strip()[:200] or "unknown git error"}


def write_bundle(repo: Path, bundle: Path, branch: str) -> dict:
    """Write ``bundle`` atomically (temp file in the same folder, verify, rename)."""
    bundle = Path(bundle)
    if not bundle.parent.is_dir():
        raise PublishError(f"bundle folder {bundle.parent} does not exist (is the drive synced on this machine?)")
    tmp = bundle.parent / f".{bundle.name}.tmp-{os.getpid()}"
    try:
        _git(repo, "bundle", "create", str(tmp), "HEAD", branch)
        _git(repo, "bundle", "verify", str(tmp))
        os.replace(tmp, bundle)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"path": str(bundle), "bytes": bundle.stat().st_size, "head": layers_mod.head_sha(repo)}


def same_remote(a: Optional[str], b: Optional[str]) -> bool:
    """True when two layer sources name the same repository.

    Ignores the transport for the big hosts (https vs ssh), a trailing
    '.git' or '/', and case in the host name.
    """
    def norm(s):
        if not s:
            return None
        s = s[len("git+"):] if s.startswith("git+") else s
        m = re.match(r"^(?:https://|ssh://(?:[^@/]+@)?)([^/:]+)(?::\d+)?/(.+)$", s)
        if m:
            host, path = m.group(1).lower(), m.group(2)
            s = f"{host}/{path}"
        elif s.startswith("file://"):
            s = s[len("file://"):]
        s = s.rstrip("/")
        return s[:-4] if s.endswith(".git") else s
    return norm(a) is not None and norm(a) == norm(b)


def push(target: Path, cfg: dict, confirm_private: bool = False, remote_only: bool = False,
         bundle_only: bool = False) -> dict:
    paths = _core_paths(cfg)
    repo = paths["repo"]
    branch = cfg.get("branch") or DEFAULT_BRANCH
    _check_repo(paths, target, branch)
    head = layers_mod.head_sha(repo)
    if head is None:
        raise PublishError("the core repo has no commits; run `publish.py apply` first")
    out = {"head": head, "remote": None, "bundle": None}
    if cfg.get("remote") and not bundle_only:
        url = layers_mod.source_to_git_url(cfg["remote"])
        r = _git(repo, "remote", "get-url", "origin", check=False)
        if r.returncode != 0:
            _git(repo, "remote", "add", "origin", url)
        elif not same_remote(layers_mod.git_url_to_source(r.stdout.strip()), cfg["remote"]):
            raise PublishError(f"origin is {r.stdout.strip()!r} but publish.json says {url!r}; fix one of them")
        guard = anonymous_readable(cfg["remote"])
        if guard["public"] is True:
            raise PublishError(f"refusing to push: {guard['reason']}. Publish only to a private repository")
        if guard["public"] is None and not confirm_private:
            raise PublishError(f"cannot confirm the remote is private ({guard['reason']}); "
                               "check it yourself, then pass --confirm-private")
        pr = layers_mod.git(["push", "-u", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo)
        if pr.returncode != 0:
            raise PublishError(f"git push failed: {(pr.stderr or pr.stdout).strip()[:400]}")
        out["remote"] = {"url": url, "branch": branch, "private_check": guard}
    if cfg.get("bundle") and not remote_only:
        out["bundle"] = write_bundle(repo, _expand(cfg["bundle"]), branch)
    if out["remote"] is None and out["bundle"] is None:
        raise PublishError("nothing to push to: set 'remote' and/or 'bundle' in publish.json")
    _append_log(Path(target).resolve(), "publish-push", f"{cfg['layer']['id']} at {head[:12]}",
                [f"remote: {out['remote']['url']}" if out["remote"] else "remote: (not pushed)",
                 f"bundle: {_bundle_hint(cfg)}" if out["bundle"] else "bundle: (not written)"])
    return out


# ---------- init / mark / status ----------

def init(target: Path, args) -> dict:
    target = Path(target).resolve()
    if not (wiki_dir(target) / INDEX_FILE).is_file():
        raise PublishError("no compathy wiki here; run /compathy first")
    cp = config_path(target)
    if cp.exists() and not args.force:
        raise PublishError(f"{cp} exists; edit it, or pass --force to start over")
    cfg = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "layer": {"id": args.layer_id, "role": args.role, "title": args.title or args.layer_id},
        "audience": args.audience,
        "repo": args.repo,
        "path": CONTEXT_DIR,
        "branch": args.branch,
        "owner": {k: v for k, v in (("name", args.owner_name), ("email", args.owner_email),
                                     ("github", args.owner_github)) if v},
        "scan": {"deny": [], "email_domains": [d for d in (args.email_domain or []) if d], "allow": []},
        "personas": {},
    }
    if args.remote:
        cfg["remote"] = args.remote
    if args.bundle:
        cfg["bundle"] = args.bundle
    probs = validate_config(cfg)
    if probs:
        raise PublishError("; ".join(probs))
    repo = _expand(args.repo).resolve()
    src = target
    if repo == src or src in repo.parents or repo in src.parents:
        raise PublishError(f"the core repo {repo} must live outside the source project {src}")
    created = None
    if not repo.exists():
        if args.remote:
            r = layers_mod.git(["clone", "--quiet", layers_mod.source_to_git_url(args.remote), str(repo)])
            created = "cloned" if r.returncode == 0 else None
        if created is None:
            repo.mkdir(parents=True, exist_ok=True)
            _git(repo, "init", "-q", "-b", args.branch)
            if args.remote:
                _git(repo, "remote", "add", "origin", layers_mod.source_to_git_url(args.remote))
            created = "initialized"
    elif not (repo / ".git").exists():
        raise PublishError(f"{repo} exists and is not a git repository")
    lin.write_json_atomic(cp, cfg)
    _append_log(target, "publish-init", f"core layer {args.layer_id}", [f"repo: {repo}"])
    return {"written": str(cp), "config": cfg, "repo": str(repo), "repo_action": created or "existing",
            "next": "add deny patterns and personas to publish.json, mark pages with "
                    "`publish.py mark`, then run `publish.py plan`"}


def set_visibility(path: Path, visibility: str) -> str:
    """Set (or clear, for private) ``visibility`` in one page's frontmatter."""
    text = path.read_text(encoding="utf-8")
    fm_lines, body = split_frontmatter(text)
    if fm_lines is None:
        raise PublishError(f"{path.name} has no frontmatter")
    lines = [l for l in fm_lines if l.partition(":")[0].strip() != "visibility"]
    if visibility != "private":
        at = next((i + 1 for i, l in enumerate(lines) if l.partition(":")[0].strip() == "schema_version"),
                  len(lines))
        lines.insert(at, f"visibility: {visibility}\n")
    new = "---\n" + "".join(lines) + "---\n" + body
    if new == text:
        return "unchanged"
    path.write_text(new, encoding="utf-8")
    return "set" if visibility != "private" else "cleared"


def mark(target: Path, slugs: list, visibility: str) -> dict:
    target = Path(target).resolve()
    if visibility not in VISIBILITY_LEVELS:
        raise PublishError(f"--visibility must be one of {VISIBILITY_LEVELS}")
    wiki = wiki_dir(target)
    results, missing = [], []
    for slug in slugs:
        path = lin.page_path(wiki, slug)
        if path is None or slug in GENERATED:
            missing.append(slug)
            continue
        results.append({"slug": slug, "action": set_visibility(path, visibility)})
    if missing:
        raise PublishError(f"no such page(s): {', '.join(missing)}" +
                           (f" (marked before the error: {[r['slug'] for r in results]})" if results else ""))
    changed = [r["slug"] for r in results if r["action"] != "unchanged"]
    if changed:
        _append_log(target, "visibility", f"{visibility}: {len(changed)} page(s)", [", ".join(changed)])
    return {"visibility": visibility, "results": results}


def status(target: Path, cfg: dict) -> dict:
    target = Path(target).resolve()
    paths = _core_paths(cfg)
    repo = paths["repo"]
    out = {"layer": cfg["layer"], "audience": cfg.get("audience", DEFAULT_AUDIENCE),
           "source": layer_source(cfg), "repo": str(repo),
           "scan": {"deny_patterns": len((cfg.get("scan") or {}).get("deny") or []),
                    "email_domains": (cfg.get("scan") or {}).get("email_domains") or [],
                    "allow_entries": len((cfg.get("scan") or {}).get("allow") or [])},
           "personas": sorted((cfg.get("personas") or {}).keys())}
    counts = {}
    for slug, path in iter_wiki_pages(wiki_dir(target)):
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        v = str(fm.get("visibility") or "private")
        counts[v] = counts.get(v, 0) + 1
    out["visibility"] = counts
    head = layers_mod.head_sha(repo) if (repo / ".git").exists() else None
    out["core_head"] = head
    log = paths["wiki"] / LOG_FILE
    last = None
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            m = LOG_ENTRY_RE.match(line)
            if m and m.group(2) == "publish":
                last = {"date": m.group(1), "summary": m.group(3)}
    out["last_publish"] = last
    if head and cfg.get("remote"):
        branch = cfg.get("branch") or DEFAULT_BRANCH
        r = layers_mod.git(["rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"], cwd=repo)
        if r.returncode == 0 and r.stdout.split():
            behind, ahead = (int(x) for x in r.stdout.split()[:2])
            out["remote"] = {"ahead": ahead, "behind": behind, "note": "as of the last fetch or push"}
        else:
            out["remote"] = {"ahead": None, "behind": None, "note": "never pushed"}
    if cfg.get("bundle"):
        bp = _expand(cfg["bundle"])
        if not bp.is_file():
            out["bundle"] = {"path_hint": _bundle_hint(cfg), "state": "missing"}
        else:
            bh = layers_mod.git(["ls-remote", str(bp), "HEAD"], cwd=repo if head else None)
            bhead = bh.stdout.split()[0] if bh.returncode == 0 and bh.stdout.split() else None
            out["bundle"] = {"path_hint": _bundle_hint(cfg), "head": bhead,
                             "state": "current" if bhead and bhead == head else "stale"}
    return out


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="publish.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--target", default=".", help="the owner's project (holds context/)")

    pi = sub.add_parser("init", help="write context/publish.json and create the core repo")
    common(pi)
    pi.add_argument("--layer-id", required=True, help="e.g. acme/pricing")
    pi.add_argument("--repo", required=True, help="local path of the core repo (outside this project)")
    pi.add_argument("--title")
    pi.add_argument("--role", default="team", choices=lin.LAYER_ROLES)
    pi.add_argument("--audience", default=DEFAULT_AUDIENCE, choices=AUDIENCES)
    pi.add_argument("--remote", help="git+https://... or git+ssh://... of the private core repo")
    pi.add_argument("--bundle", help="path of a .bundle file in a synced, access-controlled folder")
    pi.add_argument("--branch", default=DEFAULT_BRANCH)
    pi.add_argument("--owner-name")
    pi.add_argument("--owner-email")
    pi.add_argument("--owner-github")
    pi.add_argument("--email-domain", action="append", help="emails at this domain pass the scan")
    pi.add_argument("--force", action="store_true")

    pm = sub.add_parser("mark", help="set visibility on pages")
    common(pm)
    pm.add_argument("slugs", nargs="+")
    pm.add_argument("--visibility", required=True, choices=VISIBILITY_LEVELS)

    pp = sub.add_parser("plan", help="what a publish would write (no side effects)")
    common(pp)
    pp.add_argument("--note")
    pp.add_argument("--diff", action="store_true", help="write the full diff to context/compathy-reports/")

    pa = sub.add_parser("apply", help="write and commit the core repo")
    common(pa)
    pa.add_argument("--note", help="one line for the publish log and commit message")

    pu = sub.add_parser("push", help="push to the remote and refresh the bundle")
    common(pu)
    pu.add_argument("--confirm-private", action="store_true")
    g = pu.add_mutually_exclusive_group()
    g.add_argument("--remote-only", action="store_true")
    g.add_argument("--bundle-only", action="store_true")

    ps = sub.add_parser("status", help="config, core repo, last publish, bundle state")
    common(ps)
    return ap


def main(argv=None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    target = Path(args.target).resolve()
    try:
        if args.cmd == "init":
            out = init(target, args)
        elif args.cmd == "mark":
            out = mark(target, args.slugs, args.visibility)
        else:
            cfg = load_config(target)
            if args.cmd == "plan":
                out = plan(target, cfg, args.note, write_diff=args.diff)
                out.pop("_build", None)
            elif args.cmd == "apply":
                out = apply(target, cfg, args.note)
            elif args.cmd == "push":
                out = push(target, cfg, args.confirm_private, args.remote_only, args.bundle_only)
            elif args.cmd == "status":
                out = status(target, cfg)
            else:  # pragma: no cover
                raise PublishError(f"unknown command {args.cmd}")
    except (PublishError, lin.LineageError, layers_mod.LayerFetchError) as e:
        _emit({"error": str(e), "command": args.cmd})
        return 1
    _emit(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
