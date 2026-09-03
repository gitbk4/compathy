#!/usr/bin/env python3
"""Stdio MCP server exposing the compathy wiki as tools for Claude Code.

Speaks JSON-RPC 2.0 over stdio. Implements only the minimum MCP surface needed
to expose the wiki as queryable tools:

  * initialize       (server info + capabilities)
  * tools/list       (enumerate the query tools)
  * tools/call       (dispatch to the Python implementation)

Out of scope (deliberate): resources, prompts, notifications, sampling,
roots, subscriptions. Keep this module stdlib-only and side-effect-free.

Federation
----------

When the project has ``context/lineage.json`` (it was linked to a team /
org layer via ``/compathy-persona import``), every read tool walks the
lineage: the project's own wiki first, then each parent layer's read-only
cache under ``~/.compathy/layers/``. Results carry ``layer`` so provenance
is never lost. The server reads ``lineage.json`` itself; ``.mcp.json`` does
not need per-layer arguments (and so stays portable across machines).
``--layer <dir>`` adds ad-hoc layers for debugging and tests.

Tools exposed:

  compathy_index()
      Return the wiki index.md content. When linked, also each parent
      layer's index under ``layers``.

  compathy_get_page(slug, include_neighbors=False, layer=None)
      Return the parsed frontmatter and body of a single wiki page,
      nearest layer first. ``also_in`` lists the same slug in other layers.

  compathy_search(query, max_results=10, layer=None)
      Grep-style case-insensitive search across all layers.

  compathy_list_pages(page_type=None, layer=None)
      List wiki pages (optionally filtered by ``type`` frontmatter / layer).

  compathy_log_recent(n=10)
      Return the last n entries from this project's log.md.

  compathy_layers()
      Lineage, pins and cache status. Empty lineage = standalone project.

  compathy_personas_search(query, max_results=10)
      Rank locally known team personas (this project's exports, cached
      parent layers' exports, and ~/.compathy/personas). Local only: the
      network path lives in the persona CLI so page content can never make
      an agent fetch arbitrary URLs.

The script also supports a non-MCP CLI smoke mode for manual testing:

    python3 compathy_query.py --target /path/to/proj \
        --tool compathy_search --args '{"query":"persona"}'

The output is the tool's JSON result on stdout.

Install (Claude Code)
---------------------

After ``python3 scripts/install.py --claude`` symlinks the skill to
``~/.claude/skills/compathy/``, register the server in your project's
``.mcp.json`` (or merge into ``~/.claude/.mcp.json``):

    {
      "mcpServers": {
        "compathy-wiki": {
          "command": "python3",
          "args": [
            "<absolute path to scripts/compathy_query.py>",
            "--target",
            "<absolute path to your project root>"
          ],
          "transport": "stdio"
        }
      }
    }

A copy-paste starter is at ``templates/compathy.mcp.json.tmpl``. Replace
``${HOME}`` and ``${PROJECT_ROOT}`` with absolute paths (Claude Code does
not expand env vars in ``args``). If you used ``--workspace`` install or a
copy install, point the path at your local copy of ``compathy_query.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Allow running as `python3 scripts/compathy_query.py` regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from lineage import (  # noqa: E402
    LineageError,
    describe_layers,
    merged_index,
    occurrences,
    page_path,
    read_json,
    resolve_layers,
    score_persona,
    validate_manifest,
)
from lint import (  # noqa: E402  (reuse, do not reimplement)
    parse_backlinks,
    parse_frontmatter,
)
from paths import (  # noqa: E402
    INDEX_FILE,
    LOG_FILE,
    PERSONAS_INDEX_FILE,
    PERSONAS_SUBDIR,
    WIKI_SUBDIRS,
    index_path,
    log_path,
    personas_home_dir,
    wiki_dir,
)

SERVER_NAME = "compathy-wiki"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC error codes (subset of the MCP / JSON-RPC 2.0 spec).
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

VALID_PAGE_TYPES = ("entity", "concept", "summary", "patterns", "index", "log")
SNIPPET_RADIUS = 80
LOG_ENTRY_RE = re.compile(r"^## ", re.MULTILINE)

# Ad-hoc layers from ``--layer`` (module-level so tool functions keep the
# (target, params) signature the dispatcher expects).
EXTRA_LAYERS: list = []


# ---------- helpers: error shaping ----------

class ToolError(Exception):
    """Raised by tool implementations to signal a recoverable failure.

    The MCP dispatcher converts this into a tool-call error response (a normal
    JSON-RPC result with ``isError: true``), per the MCP tools spec, rather
    than a JSON-RPC protocol error.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _wiki_root(target: Path) -> Path:
    root = wiki_dir(target)
    if not root.exists() or not root.is_dir():
        raise ToolError(f"no wiki found at {root} (run /compathy to scaffold)")
    return root


def _layers(target: Path) -> list:
    """Self layer first, then cached parents (from lineage.json), then --layer extras."""
    try:
        return resolve_layers(target, extra=EXTRA_LAYERS)
    except LineageError as e:
        raise ToolError(f"context/lineage.json is invalid: {e}") from e


def _readable_layers(target: Path) -> list:
    return [l for l in _layers(target) if l.get("cached") and l.get("wiki") is not None]


def _pick_layer(layers: list, layer_id) -> list:
    if layer_id is None:
        return layers
    if not isinstance(layer_id, str) or not layer_id.strip():
        raise ToolError("'layer' must be a non-empty string when given")
    chosen = [l for l in layers if l.get("id") == layer_id.strip()]
    if not chosen:
        raise ToolError(
            f"unknown or uncached layer '{layer_id}'; known: "
            f"{[l.get('id') for l in layers]}"
        )
    return chosen


def _page_path_for_slug(wiki: Path, slug: str) -> Path | None:
    """Return the on-disk path for ``slug`` in one wiki (see lineage.page_path)."""
    return page_path(wiki, slug)


def _iter_all_pages(wiki: Path):
    """Yield (slug, path, page_type) for every page including index/log."""
    idx = wiki / INDEX_FILE
    if idx.is_file():
        yield "index", idx, "index"
    lg = wiki / LOG_FILE
    if lg.is_file():
        yield "log", lg, "log"
    for sub in WIKI_SUBDIRS:
        d = wiki / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name == "README.md":
                continue
            slug = p.stem
            # Default type from subdir; frontmatter overrides if present.
            default_type = "patterns" if sub == "patterns" else sub.rstrip("s")
            yield slug, p, default_type


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _safe_frontmatter(text: str) -> tuple:
    """Return (fm, body). Falls back to ({}, text) on parse error."""
    try:
        return parse_frontmatter(text)
    except ValueError:
        return {}, text


def _title_from(fm: dict, body: str, slug: str) -> str:
    title = fm.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return slug


# ---------- tool implementations ----------

def _rel_or_abs(p: Path, target: Path) -> str:
    try:
        return str(p.relative_to(target))
    except ValueError:
        return str(p)


def tool_compathy_index(target: Path, _params: dict) -> dict:
    """Return the wiki's index.md content (plus parent indexes when linked)."""
    _wiki_root(target)  # validate wiki exists
    idx = index_path(target)
    if not idx.is_file():
        raise ToolError(f"index.md not found at {_rel_or_abs(idx, target)}")
    text = _read_text(idx)
    fm, body = _safe_frontmatter(text)
    out = {
        "slug": "index",
        "page_type": "index",
        "path": _rel_or_abs(idx, target),
        "frontmatter": fm,
        "body": body,
        "raw": text,
    }
    layers = _layers(target)
    if len(layers) > 1:
        out["layers"] = [
            {
                "layer": m["layer_id"],
                "role": m["role"],
                "self": m["self"],
                "cached": m["cached"],
                "path": m["path"],
                "body": (_safe_frontmatter(m["text"])[1] if m["text"] is not None else None),
            }
            for m in merged_index(layers)
        ]
    return out


def _resolve_page_type(p: Path, fm: dict) -> str:
    """Return the page's type — frontmatter wins, fallback by path."""
    ptype = fm.get("type")
    if isinstance(ptype, str):
        return ptype
    if p.name == INDEX_FILE:
        return "index"
    if p.name == LOG_FILE:
        return "log"
    sub = p.parent.name
    return "patterns" if sub == "patterns" else sub.rstrip("s")


def _neighbor_record(wiki: Path, slug: str) -> dict | None:
    """Resolve a slug to ``{slug, title, page_type}`` for a neighbor list.

    Returns None if the slug is not addressable or the page cannot be read
    (broken backlink, IO error). Callers drop None silently — the linter
    surfaces broken backlinks separately.
    """
    p = _page_path_for_slug(wiki, slug)
    if p is None:
        return None
    try:
        text = _read_text(p)
    except OSError:
        return None
    fm, body = _safe_frontmatter(text)
    return {
        "slug": slug,
        "title": _title_from(fm, body, slug),
        "page_type": _resolve_page_type(p, fm),
    }


def _compute_neighbors(
    wiki: Path, current_slug: str, current_body: str
) -> dict:
    """Compute 1-hop graph neighbors of ``current_slug`` within one layer.

    ``outbound`` — distinct slugs referenced via ``[[slug]]`` in this page's
    body that resolve to an existing wiki page.

    ``inbound`` — distinct pages whose body references ``[[current_slug]]``.
    O(N pages) per call.

    Index and log are excluded from both lists — they are catalogs, not
    knowledge nodes. Self-references and broken backlinks are skipped
    (the linter surfaces them separately). Output is sorted by slug for
    determinism.
    """
    CATALOGS = {"index", "log"}

    seen_out: set[str] = set()
    outbound: list[dict] = []
    for slug in parse_backlinks(current_body):
        if slug == current_slug or slug in seen_out or slug in CATALOGS:
            continue
        seen_out.add(slug)
        rec = _neighbor_record(wiki, slug)
        if rec is not None:
            outbound.append(rec)
    outbound.sort(key=lambda r: r["slug"])

    seen_in: set[str] = set()
    inbound: list[dict] = []
    for slug, p, _default_type in _iter_all_pages(wiki):
        if (
            slug in CATALOGS
            or slug == current_slug
            or slug in seen_in
        ):
            continue
        try:
            text = _read_text(p)
        except OSError:
            continue
        _fm, body = _safe_frontmatter(text)
        if current_slug not in parse_backlinks(body):
            continue
        seen_in.add(slug)
        rec = _neighbor_record(wiki, slug)
        if rec is not None:
            inbound.append(rec)
    inbound.sort(key=lambda r: r["slug"])

    return {"outbound": outbound, "inbound": inbound}


def tool_compathy_get_page(target: Path, params: dict) -> dict:
    """Return the parsed contents of a single wiki page, nearest layer first.

    When ``include_neighbors`` is true, the response also carries a
    ``neighbors`` field with ``outbound`` and ``inbound`` lists of 1-hop
    backlink targets (within the layer the page was found in). ``layer``
    pins the lookup to one layer id. ``also_in`` lists other layers that
    have the same slug (shadowed parents, or a child override when a parent
    layer was requested explicitly).
    """
    slug = params.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        raise ToolError("missing or invalid 'slug' (string required)")
    slug = slug.strip()
    include_neighbors = params.get("include_neighbors", False)
    if not isinstance(include_neighbors, bool):
        raise ToolError("'include_neighbors' must be a boolean")
    _wiki_root(target)
    layers = _readable_layers(target)
    found = occurrences(slug, layers)
    if not found:
        raise ToolError(f"no wiki page with slug '{slug}'")
    layer_id = params.get("layer")
    if layer_id is not None:
        chosen = _pick_layer(layers, layer_id)
        hits = [(l, p) for l, p in found if l is chosen[0]]
        if not hits:
            raise ToolError(f"layer '{layer_id}' has no page with slug '{slug}'")
        layer, p = hits[0]
    else:
        layer, p = found[0]
    text = _read_text(p)
    fm, body = _safe_frontmatter(text)
    out = {
        "slug": slug,
        "page_type": _resolve_page_type(p, fm),
        "path": _rel_or_abs(p, target),
        "frontmatter": fm,
        "body": body,
        "title": _title_from(fm, body, slug),
        "layer": layer.get("id"),
        "layer_role": layer.get("role"),
        "also_in": [
            {"layer": l.get("id"), "role": l.get("role"), "path": _rel_or_abs(pp, target)}
            for l, pp in found
            if l is not layer
        ],
    }
    if include_neighbors:
        out["neighbors"] = _compute_neighbors(layer["wiki"], slug, body)
    return out


def _make_snippet(body: str, query_lc: str) -> str:
    body_lc = body.lower()
    pos = body_lc.find(query_lc)
    if pos < 0:
        # Match was in slug/title; show the head of the body.
        head = body.strip().splitlines()
        return (head[0] if head else "")[: 2 * SNIPPET_RADIUS]
    start = max(0, pos - SNIPPET_RADIUS)
    end = min(len(body), pos + len(query_lc) + SNIPPET_RADIUS)
    snippet = body[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "... " + snippet
    if end < len(body):
        snippet = snippet + " ..."
    return snippet


def tool_compathy_search(target: Path, params: dict) -> dict:
    """Grep-style case-insensitive search across all layers' wiki pages."""
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("missing or invalid 'query' (non-empty string required)")
    max_results = params.get("max_results", 10)
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise ToolError("'max_results' must be an integer")
    if max_results < 1:
        raise ToolError("'max_results' must be >= 1")
    _wiki_root(target)
    layers = _pick_layer(_readable_layers(target), params.get("layer"))
    query_lc = query.strip().lower()
    matches = []
    for depth, layer in enumerate(layers):
        for slug, p, default_type in _iter_all_pages(layer["wiki"]):
            try:
                text = _read_text(p)
            except OSError:
                continue
            fm, body = _safe_frontmatter(text)
            haystack = body.lower()
            slug_hit = query_lc in slug.lower()
            body_hit = query_lc in haystack
            title = _title_from(fm, body, slug)
            title_hit = query_lc in title.lower()
            if not (slug_hit or body_hit or title_hit):
                continue
            # Rank: slug match > title match > body match; tie-break by hit count.
            score = 0
            if slug_hit:
                score += 100
            if title_hit:
                score += 50
            score += haystack.count(query_lc)
            page_type = fm.get("type") if isinstance(fm.get("type"), str) else default_type
            matches.append(
                {
                    "slug": slug,
                    "page_type": page_type,
                    "title": title,
                    "snippet": _make_snippet(body, query_lc),
                    "score": score,
                    "path": _rel_or_abs(p, target),
                    "layer": layer.get("id"),
                    "_depth": depth,
                }
            )
    # Same score: nearer layer first, then slug.
    matches.sort(key=lambda m: (-m["score"], m["_depth"], m["slug"]))
    for m in matches:
        del m["_depth"]
    return {
        "query": query,
        "count": len(matches[:max_results]),
        "total_matches": len(matches),
        "results": matches[:max_results],
    }


def tool_compathy_list_pages(target: Path, params: dict) -> dict:
    """Enumerate wiki pages across layers, optionally filtered by ``page_type``."""
    page_type = params.get("page_type")
    if page_type is not None:
        if not isinstance(page_type, str) or page_type not in VALID_PAGE_TYPES:
            raise ToolError(
                f"'page_type' must be one of {list(VALID_PAGE_TYPES)} (or omitted)"
            )
    _wiki_root(target)
    layers = _pick_layer(_readable_layers(target), params.get("layer"))
    out = []
    for depth, layer in enumerate(layers):
        for slug, p, default_type in _iter_all_pages(layer["wiki"]):
            try:
                text = _read_text(p)
            except OSError:
                continue
            fm, body = _safe_frontmatter(text)
            ptype = fm.get("type") if isinstance(fm.get("type"), str) else default_type
            if page_type is not None and ptype != page_type:
                continue
            out.append(
                {
                    "slug": slug,
                    "page_type": ptype,
                    "title": _title_from(fm, body, slug),
                    "path": _rel_or_abs(p, target),
                    "layer": layer.get("id"),
                    "_depth": depth,
                }
            )
    out.sort(key=lambda x: (x["_depth"], x["page_type"], x["slug"]))
    for x in out:
        del x["_depth"]
    return {
        "filter": page_type,
        "count": len(out),
        "pages": out,
    }


def tool_compathy_log_recent(target: Path, params: dict) -> dict:
    """Return the last ``n`` entries from log.md (most recent first)."""
    n = params.get("n", 10)
    if not isinstance(n, int) or isinstance(n, bool):
        raise ToolError("'n' must be an integer")
    if n < 1:
        raise ToolError("'n' must be >= 1")
    _wiki_root(target)
    lg = log_path(target)
    if not lg.is_file():
        raise ToolError(f"log.md not found at {lg}")
    text = _read_text(lg)
    _, body = _safe_frontmatter(text)
    # Split on H2 headings while keeping the heading with its body.
    parts = LOG_ENTRY_RE.split(body)
    # parts[0] is the prefix before the first H2 (intro/blockquote).
    entries = []
    for raw in parts[1:]:
        chunk = raw.rstrip()
        if not chunk:
            continue
        # Re-attach the "## " stripped by re.split.
        entry_text = "## " + chunk
        first_line, _, rest = entry_text.partition("\n")
        entries.append(
            {
                "heading": first_line.strip(),
                "body": rest.strip(),
            }
        )
    # Most recent assumed to be at the bottom (append-only).
    recent = list(reversed(entries))[:n]
    return {
        "count": len(recent),
        "total_entries": len(entries),
        "entries": recent,
    }


def tool_compathy_layers(target: Path, _params: dict) -> dict:
    """Describe the lineage: self + parent layers, pins, cache status."""
    layers = _layers(target)
    parents = [l for l in layers if not l.get("self")]
    warnings = [
        f"layer {l.get('id')} is not cached at pin {str(l.get('pin'))[:12]}; "
        f"run `/compathy-persona sync`"
        for l in parents
        if not l.get("cached")
    ]
    persona = None
    try:
        from lineage import load_lineage  # pylint: disable=import-outside-toplevel
        doc = load_lineage(target)
        persona = (doc or {}).get("persona")
    except LineageError:
        persona = None
    return {
        "linked": bool(parents),
        "persona": persona,
        "depth": len(layers),
        "layers": describe_layers(layers),
        "warnings": warnings,
    }


def _local_persona_entries(target: Path) -> list:
    """Collect persona index entries from every local source (no network)."""
    entries = []
    seen = set()

    def _add(entry: dict, where: str, path: Path):
        pid = entry.get("id")
        if not isinstance(pid, str) or pid in seen:
            return
        seen.add(pid)
        rec = {
            "id": pid,
            "title": entry.get("title"),
            "summary": entry.get("summary"),
            "tags": entry.get("tags") or [],
            "team": pid.rsplit("/", 1)[0],
            "updated": entry.get("updated") or (entry.get("provenance") or {}).get("exported_at"),
            "where": where,
            "path": str(path),
        }
        entries.append(rec)

    def _scan_dir(pdir: Path, where: str):
        if not pdir.is_dir():
            return
        for f in sorted(pdir.glob("*.json")):
            if f.name == PERSONAS_INDEX_FILE:
                continue
            try:
                data = read_json(f)
            except LineageError:
                continue
            if validate_manifest(data):
                continue
            _add(data, where, f)

    for layer in _layers(target):
        ctx = layer.get("context")
        if ctx is None or not layer.get("cached"):
            continue
        where = "this project" if layer.get("self") else f"layer {layer.get('id')}"
        _scan_dir(Path(ctx) / PERSONAS_SUBDIR, where)
    _scan_dir(personas_home_dir(), "~/.compathy/personas")
    return entries


def tool_compathy_personas_search(target: Path, params: dict) -> dict:
    """Rank locally known team personas against ``query`` (local only)."""
    query = params.get("query", "")
    if not isinstance(query, str):
        raise ToolError("'query' must be a string")
    max_results = params.get("max_results", 10)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1:
        raise ToolError("'max_results' must be an integer >= 1")
    entries = _local_persona_entries(target)
    results = []
    for e in entries:
        score, why = score_persona(query, e)
        if query.strip() and score <= 0:
            continue
        rec = dict(e)
        rec["score"] = score
        rec["why"] = why
        results.append(rec)
    results.sort(key=lambda r: (-r["score"], r["id"]))
    return {
        "query": query,
        "count": len(results[:max_results]),
        "total_matches": len(results),
        "results": results[:max_results],
        "note": "local sources only; use `/compathy-persona search` to query the org registry",
    }


# ---------- tool registry ----------

TOOLS = {
    "compathy_index": {
        "fn": tool_compathy_index,
        "description": (
            "Return the wiki index.md content (frontmatter + body). When the "
            "project is linked to parent layers, 'layers' also carries each "
            "parent's index body."
        ),
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "compathy_get_page": {
        "fn": tool_compathy_get_page,
        "description": (
            "Return the frontmatter and body of a single wiki page by slug, "
            "resolved nearest layer first (project, then team, then org); "
            "'layer' says where it came from and 'also_in' lists other layers "
            "with the same slug. Pass include_neighbors=true to also return "
            "outbound + inbound 1-hop [[backlink]] neighbors so the model can "
            "chain-navigate without a second call."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Page slug, e.g. 'authentication' or 'index'.",
                },
                "include_neighbors": {
                    "type": "boolean",
                    "description": (
                        "When true, include a 'neighbors' field with "
                        "outbound + inbound 1-hop backlinks. Each entry is "
                        "{slug, title, page_type}. Excludes index/log. "
                        "Default false."
                    ),
                    "default": False,
                },
                "layer": {
                    "type": "string",
                    "description": (
                        "Restrict the lookup to one layer id (e.g. 'acme'). "
                        "Omit for nearest-layer resolution."
                    ),
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
    "compathy_search": {
        "fn": tool_compathy_search,
        "description": (
            "Grep-style search across the wiki and its parent layers. Returns "
            "ranked matches with snippets and the layer each came from."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to search for (case-insensitive).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 10).",
                    "minimum": 1,
                    "default": 10,
                },
                "layer": {
                    "type": "string",
                    "description": "Restrict to one layer id. Omit for all layers.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "compathy_list_pages": {
        "fn": tool_compathy_list_pages,
        "description": (
            "Enumerate wiki pages across layers, optionally filtered by type "
            "(entity|concept|summary|patterns|index|log) and/or layer id."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "page_type": {
                    "type": "string",
                    "enum": list(VALID_PAGE_TYPES),
                    "description": "Filter to a single page type. Omit for all.",
                },
                "layer": {
                    "type": "string",
                    "description": "Restrict to one layer id. Omit for all layers.",
                },
            },
            "additionalProperties": False,
        },
    },
    "compathy_log_recent": {
        "fn": tool_compathy_log_recent,
        "description": "Return the last n entries from this project's log.md (most recent first).",
        "schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of entries (default 10).",
                    "minimum": 1,
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
    },
    "compathy_layers": {
        "fn": tool_compathy_layers,
        "description": (
            "Describe this project's lineage: the persona it works as, each "
            "parent layer (id, role, source, pin) and whether its read-only "
            "cache is present. linked=false means a standalone wiki."
        ),
        "schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "compathy_personas_search": {
        "fn": tool_compathy_personas_search,
        "description": (
            "Rank locally known team personas (exports in this project, in "
            "cached parent layers, and in ~/.compathy/personas) against a "
            "query. Local only; never touches the network."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query, e.g. 'payments backend'. Empty lists all.",
                    "default": "",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 10).",
                    "minimum": 1,
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
    },
}


# ---------- dispatch ----------

def call_tool(name: str, target: Path, params: dict) -> dict:
    """Invoke a registered tool. Raises ToolError on user-facing failures.

    Used both by the MCP server and by the CLI smoke mode.
    """
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"unknown tool '{name}'")
    if not isinstance(params, dict):
        raise ToolError("tool arguments must be a JSON object")
    return spec["fn"](target, params)


# ---------- JSON-RPC plumbing ----------

def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _tool_descriptor(name: str, spec: dict) -> dict:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": spec["schema"],
    }


def handle_request(req: dict, target: Path) -> dict | None:
    """Handle a single JSON-RPC request. Returns a response dict or None.

    Returns None for notifications (requests without an ``id``), which per
    JSON-RPC 2.0 must not be answered.
    """
    if not isinstance(req, dict):
        return _err(None, ERR_INVALID_REQUEST, "request must be a JSON object")
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(method, str):
        return _err(rid, ERR_INVALID_REQUEST, "missing 'method'")

    # Notifications (no id) are silently accepted; we don't act on them.
    is_notification = "id" not in req

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return None if is_notification else _ok(rid, result)

    if method == "notifications/initialized":
        return None  # client is signalling readiness; no response required

    if method == "tools/list":
        tools = [_tool_descriptor(n, s) for n, s in TOOLS.items()]
        return None if is_notification else _ok(rid, {"tools": tools})

    if method == "tools/call":
        if not isinstance(params, dict):
            return _err(rid, ERR_INVALID_PARAMS, "params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _err(rid, ERR_INVALID_PARAMS, "missing 'name'")
        if name not in TOOLS:
            # Per MCP, an unknown tool is a method-not-found-style error.
            return _err(rid, ERR_METHOD_NOT_FOUND, f"unknown tool '{name}'")
        if not isinstance(arguments, dict):
            return _err(rid, ERR_INVALID_PARAMS, "'arguments' must be an object")
        try:
            data = call_tool(name, target, arguments)
        except ToolError as e:
            # Tool-level errors are returned as a successful tool/call result
            # with isError=true (MCP convention), so the model can recover.
            return None if is_notification else _ok(
                rid,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": e.message}],
                },
            )
        except (OSError, ValueError) as e:
            return _err(rid, ERR_INTERNAL, f"internal error: {e}")
        # Wrap structured data as a single text content block of JSON.
        text = json.dumps(data, ensure_ascii=False, indent=2)
        return None if is_notification else _ok(
            rid,
            {
                "isError": False,
                "content": [{"type": "text", "text": text}],
                "structuredContent": data,
            },
        )

    # Anything else (resources/list, prompts/list, ...) is unsupported.
    if is_notification:
        return None
    return _err(rid, ERR_METHOD_NOT_FOUND, f"method not found: {method}")


def serve_stdio(target: Path, stdin=None, stdout=None) -> int:
    """Run the JSON-RPC stdio loop until EOF.

    One JSON-RPC message per line ('LSP Content-Length' framing is *not*
    used; Claude Code's MCP stdio transport is line-delimited). Malformed
    JSON yields a parse-error response with id=None and the loop continues.
    """
    fin = stdin if stdin is not None else sys.stdin
    fout = stdout if stdout is not None else sys.stdout
    for raw in fin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err(None, ERR_PARSE, f"parse error: {e}")
            fout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            fout.flush()
            continue
        resp = handle_request(req, target)
        if resp is None:
            continue
        fout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        fout.flush()
    return 0


# ---------- CLI smoke mode ----------

def _resolve_target(arg_target: str | None) -> Path:
    if arg_target:
        return Path(arg_target).resolve()
    env = os.environ.get("COMPATHY_TARGET")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def _cli_call(target: Path, tool: str, args_json: str) -> int:
    try:
        params = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        print(f"ERROR: --args is not valid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("ERROR: --args must be a JSON object", file=sys.stderr)
        return 2
    try:
        result = call_tool(tool, target, params)
    except ToolError as e:
        print(json.dumps({"isError": True, "message": e.message}, indent=2))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    """CLI entry point. Default mode is MCP stdio; --tool runs once."""
    ap = argparse.ArgumentParser(
        description=(
            "compathy MCP server (stdio JSON-RPC). "
            "Pass --tool to run a single tool and exit."
        ),
    )
    ap.add_argument("--target", default=None, help="project root (default: cwd or $COMPATHY_TARGET)")
    ap.add_argument("--layer", action="append", default=[],
                    help="extra layer directory (a context/ or wiki/ dir); repeatable")
    ap.add_argument("--tool", default=None, choices=list(TOOLS.keys()),
                    help="run one tool and print its JSON output (CLI smoke mode)")
    ap.add_argument("--args", default="{}",
                    help="JSON object of arguments for --tool (default: {})")
    args = ap.parse_args(argv)
    target = _resolve_target(args.target)
    EXTRA_LAYERS[:] = list(args.layer)

    if args.tool:
        return _cli_call(target, args.tool, args.args)
    return serve_stdio(target)


if __name__ == "__main__":
    sys.exit(main())
