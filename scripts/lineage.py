#!/usr/bin/env python3
"""Lineage: how a compathy wiki points at its parent layers.

A *layer* is one compathy ``context/`` directory (org, team, or project).
A child declares its parents in ``context/lineage.json``; parents are never
copied into the child - they are pinned to a commit and cached read-only
under ``~/.compathy/layers/<layer>/<pin>/`` (compathy design decision #4,
".ref over copies", applied across repos).

This module is pure and local: it reads ``lineage.json``, resolves slugs
across cached layers, validates persona manifests and registries. It never
touches the network - see ``layers.py`` for git.

Public API (everything else imports from here):

    load_lineage(target) -> dict | None      # None = standalone (today's behavior)
    save_lineage(target, data)
    validate_lineage(data) -> [str]
    resolve_layers(target, lineage=None, extra=None) -> [layer dict]
    page_path(wiki_root, slug) -> Path | None
    resolve_slug(slug, layers) -> (layer, path) | (None, None)
    occurrences(slug, layers) -> [(layer, path)]
    parse_ref("acme/payments/go-service-patterns") -> ("acme/payments", "go-service-patterns")
    validate_manifest(data) -> [str]
    validate_registry(data) -> [str]

Layer dict shape (as returned by ``resolve_layers``):

    {"id": "acme/payments", "role": "team", "self": False,
     "source": "git+https://...", "path": "context", "pin": "<sha>",
     "tree_sha": "<sha>", "context": Path | None, "wiki": Path | None,
     "cached": bool}

The first entry is always the project itself (``self: True``).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from paths import (
    INDEX_FILE,
    LOG_FILE,
    MAX_LINEAGE_DEPTH,
    WIKI_SUBDIR,
    WIKI_SUBDIRS,
    context_root,
    layer_slug,
    layers_cache_dir,
    lineage_path,
    wiki_dir,
)

LINEAGE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
MANIFEST_KIND = "compathy-persona"

LAYER_ROLES = ("org", "team", "project")
SOURCE_SCHEMES = ("git+https://", "git+ssh://", "git+file://")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LAYER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
MAX_JSON_BYTES = 1_000_000


class LineageError(Exception):
    """Raised when lineage.json exists but cannot be used."""


# ---------- small helpers ----------

def _is_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def read_json(path: Path, max_bytes: int = MAX_JSON_BYTES):
    """Read a JSON file. Raises LineageError on missing/oversize/malformed."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as e:
        raise LineageError(f"cannot read {path}: {e}") from e
    if size > max_bytes:
        raise LineageError(f"{path} is {size} bytes; refusing to parse > {max_bytes}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise LineageError(f"malformed JSON in {path}: {e}") from e


def write_json_atomic(path: Path, data) -> None:
    """Write JSON via tmp + os.replace (never leaves a half-written file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def valid_source(source) -> bool:
    """True if ``source`` uses one of the allowed git schemes."""
    return _is_str(source) and source.startswith(SOURCE_SCHEMES)


def valid_layer_path(p) -> bool:
    """Relative, no '..', no leading '/', no backslashes."""
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("/") or "\\" in p:
        return False
    return ".." not in p.split("/")


# ---------- validation ----------

def validate_layer_entry(entry, where: str = "layer") -> list:
    """Return a list of human-readable problems with one layer entry."""
    probs = []
    if not isinstance(entry, dict):
        return [f"{where}: must be an object"]
    lid = entry.get("id")
    if not _is_str(lid) or not LAYER_ID_RE.match(lid):
        probs.append(f"{where}: 'id' must be like 'acme' or 'acme/payments'")
    if entry.get("role") not in LAYER_ROLES:
        probs.append(f"{where}: 'role' must be one of {LAYER_ROLES}")
    if not valid_source(entry.get("source")):
        probs.append(f"{where}: 'source' must start with one of {SOURCE_SCHEMES}")
    if not valid_layer_path(entry.get("path", "context")):
        probs.append(f"{where}: 'path' must be a relative path without '..'")
    pin = entry.get("pin")
    if not (_is_str(pin) and SHA_RE.match(pin)):
        probs.append(f"{where}: 'pin' must be a 40-hex commit sha")
    tree = entry.get("tree_sha")
    if tree is not None and not (_is_str(tree) and SHA_RE.match(tree)):
        probs.append(f"{where}: 'tree_sha' must be a 40-hex tree sha when present")
    return probs


def validate_lineage(data) -> list:
    """Return problems with a lineage.json document (empty list = valid)."""
    if not isinstance(data, dict):
        return ["lineage.json: top level must be an object"]
    probs = []
    if data.get("schema_version") != LINEAGE_SCHEMA_VERSION:
        probs.append(f"lineage.json: schema_version must be {LINEAGE_SCHEMA_VERSION}")
    layers = data.get("layers")
    if not isinstance(layers, list) or not layers:
        probs.append("lineage.json: 'layers' must be a non-empty list")
        layers = []
    if len(layers) > MAX_LINEAGE_DEPTH - 1:
        probs.append(
            f"lineage.json: at most {MAX_LINEAGE_DEPTH - 1} parent layers "
            f"(depth cap {MAX_LINEAGE_DEPTH})"
        )
    seen = set()
    for i, entry in enumerate(layers):
        probs.extend(validate_layer_entry(entry, f"layers[{i}]"))
        if isinstance(entry, dict) and entry.get("id") in seen:
            probs.append(f"layers[{i}]: duplicate layer id {entry.get('id')!r}")
        if isinstance(entry, dict):
            seen.add(entry.get("id"))
    selfd = data.get("self")
    if selfd is not None:
        if not isinstance(selfd, dict):
            probs.append("lineage.json: 'self' must be an object")
        else:
            if not _is_str(selfd.get("id")):
                probs.append("lineage.json: self.id must be a string")
            if selfd.get("role") not in LAYER_ROLES:
                probs.append(f"lineage.json: self.role must be one of {LAYER_ROLES}")
            if selfd.get("id") in seen:
                probs.append("lineage.json: self.id collides with a parent layer id")
    persona = data.get("persona")
    if persona is not None and not _is_str(persona):
        probs.append("lineage.json: 'persona' must be a string id when present")
    return probs


def validate_manifest(data) -> list:
    """Return problems with a persona manifest (empty list = valid)."""
    if not isinstance(data, dict):
        return ["manifest: top level must be an object"]
    probs = []
    if data.get("kind") != MANIFEST_KIND:
        probs.append(f"manifest: 'kind' must be {MANIFEST_KIND!r}")
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        probs.append(f"manifest: schema_version must be {MANIFEST_SCHEMA_VERSION}")
    pid = data.get("id")
    if not _is_str(pid) or not LAYER_ID_RE.match(pid) or "/" not in pid:
        probs.append("manifest: 'id' must be '<layer-id>/<role>' e.g. 'acme/payments/backend-engineer'")
    else:
        role = pid.rsplit("/", 1)[1]
        if not SLUG_RE.match(role):
            probs.append("manifest: role segment of 'id' must be kebab-case")
    if not _is_str(data.get("title")):
        probs.append("manifest: 'title' required")
    if not isinstance(data.get("summary", ""), str):
        probs.append("manifest: 'summary' must be a string")
    if not _is_str_list(data.get("tags", [])):
        probs.append("manifest: 'tags' must be a list of strings")
    layers = data.get("layers")
    if not isinstance(layers, list) or not layers:
        probs.append("manifest: 'layers' must be a non-empty list")
        layers = []
    if len(layers) > MAX_LINEAGE_DEPTH - 1:
        probs.append(f"manifest: at most {MAX_LINEAGE_DEPTH - 1} layers")
    for i, entry in enumerate(layers):
        probs.extend(validate_layer_entry(entry, f"manifest.layers[{i}]"))
    if isinstance(pid, str) and layers and isinstance(layers[0], dict):
        owner = pid.rsplit("/", 1)[0]
        if layers[0].get("id") != owner:
            probs.append(
                f"manifest: layers[0].id must be the exporting layer {owner!r} "
                f"(got {layers[0].get('id')!r})"
            )
    for key in ("reads_first", "responsibilities"):
        if not _is_str_list(data.get(key, [])):
            probs.append(f"manifest: '{key}' must be a list of strings")
    for ref in data.get("reads_first", []) if _is_str_list(data.get("reads_first", [])) else []:
        lid, slug = parse_ref(ref)
        if not lid or not SLUG_RE.match(slug):
            probs.append(f"manifest: reads_first entry {ref!r} must be '<layer-id>/<slug>'")
    toolkit = data.get("toolkit", {})
    if not isinstance(toolkit, dict):
        probs.append("manifest: 'toolkit' must be an object")
    else:
        for key in ("claude_skills", "mcp_servers"):
            items = toolkit.get(key, [])
            if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                probs.append(f"manifest: toolkit.{key} must be a list of objects")
    policy = data.get("policy", {})
    if not isinstance(policy, dict):
        probs.append("manifest: 'policy' must be an object")
    else:
        for key in ("may_edit", "may_propose_to", "read_only"):
            if not _is_str_list(policy.get(key, [])):
                probs.append(f"manifest: policy.{key} must be a list of strings")
    prov = data.get("provenance", {})
    if not isinstance(prov, dict):
        probs.append("manifest: 'provenance' must be an object")
    sig = data.get("signature")
    if sig is not None and not isinstance(sig, str):
        probs.append("manifest: 'signature' must be a string or null")
    return probs


def validate_registry(data) -> list:
    """Return problems with an org registry.json (empty list = valid)."""
    if not isinstance(data, dict):
        return ["registry.json: top level must be an object"]
    probs = []
    if data.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        probs.append(f"registry.json: schema_version must be {REGISTRY_SCHEMA_VERSION}")
    org = data.get("org")
    if not _is_str(org) or not LAYER_ID_RE.match(org):
        probs.append("registry.json: 'org' must be a layer id like 'acme'")
    teams = data.get("teams")
    if not isinstance(teams, list):
        probs.append("registry.json: 'teams' must be a list")
        teams = []
    seen = set()
    for i, t in enumerate(teams):
        where = f"teams[{i}]"
        if not isinstance(t, dict):
            probs.append(f"{where}: must be an object")
            continue
        tid = t.get("id")
        if not _is_str(tid) or not LAYER_ID_RE.match(tid) or "/" in tid:
            probs.append(f"{where}: 'id' must be a single segment like 'payments'")
        if tid in seen:
            probs.append(f"{where}: duplicate team id {tid!r}")
        seen.add(tid)
        if not valid_source(t.get("source")):
            probs.append(f"{where}: 'source' must start with one of {SOURCE_SCHEMES}")
        if not valid_layer_path(t.get("path", "context")):
            probs.append(f"{where}: 'path' must be a relative path without '..'")
    return probs


def validate_personas_index(data, team_id: Optional[str] = None) -> list:
    """Return problems with a generated context/personas/index.json."""
    if not isinstance(data, dict):
        return ["personas/index.json: top level must be an object"]
    probs = []
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        probs.append("personas/index.json: bad schema_version")
    if team_id is not None and data.get("team") != team_id:
        probs.append(f"personas/index.json: 'team' should be {team_id!r}")
    personas = data.get("personas")
    if not isinstance(personas, list):
        return probs + ["personas/index.json: 'personas' must be a list"]
    for i, p in enumerate(personas):
        if not isinstance(p, dict) or not _is_str(p.get("id")) or not _is_str(p.get("file")):
            probs.append(f"personas/index.json: personas[{i}] needs 'id' and 'file'")
    return probs


# ---------- load / save ----------

def load_lineage(target) -> Optional[dict]:
    """Return the parsed lineage.json, or None when the project is standalone.

    Raises LineageError when the file exists but is malformed or invalid, so
    callers can surface a precise message instead of silently ignoring a
    broken federation link.
    """
    path = lineage_path(target)
    if not path.is_file():
        return None
    data = read_json(path)
    probs = validate_lineage(data)
    if probs:
        raise LineageError("; ".join(probs))
    return data


def save_lineage(target, data: dict) -> Path:
    """Validate then atomically write context/lineage.json."""
    probs = validate_lineage(data)
    if probs:
        raise LineageError("; ".join(probs))
    path = lineage_path(target)
    write_json_atomic(path, data)
    return path


def self_role(lineage: Optional[dict]) -> Optional[str]:
    """Return the role this layer claims for itself (None when unknown)."""
    if not lineage:
        return None
    selfd = lineage.get("self")
    if isinstance(selfd, dict) and selfd.get("role") in LAYER_ROLES:
        return selfd["role"]
    return "project"


# ---------- layer cache paths ----------

def layer_cache_root(layer: dict) -> Path:
    """~/.compathy/layers/<slug>/<pin>/ - the clone root for one pinned layer."""
    return layers_cache_dir() / layer_slug(layer["id"]) / str(layer["pin"])


def layer_context_dir(layer: dict) -> Path:
    """The layer's context/ directory inside its cache root."""
    rel = layer.get("path") or "context"
    return layer_cache_root(layer) / rel


def layer_wiki_dir(layer: dict) -> Path:
    """The layer's wiki/ directory inside its cache root."""
    return layer_context_dir(layer) / WIKI_SUBDIR


def is_layer_cached(layer: dict) -> bool:
    """True when the layer's wiki (with an index.md) is present in the cache."""
    return (layer_wiki_dir(layer) / INDEX_FILE).is_file()


# ---------- resolution ----------

def resolve_layers(target, lineage: Optional[dict] = None, extra: Optional[list] = None) -> list:
    """Return the ordered layer list: self first, then parents nearest-first.

    ``lineage`` may be passed to avoid re-reading; when omitted it is loaded
    (a malformed file raises LineageError). ``extra`` is a list of explicit
    context/ or wiki/ directories appended as ad-hoc layers (used by the MCP
    server's ``--layer`` flag and by tests).
    """
    target = Path(target)
    if lineage is None:
        lineage = load_lineage(target)
    selfd = (lineage or {}).get("self") or {}
    layers = [
        {
            "id": selfd.get("id") or target.resolve().name,
            "role": selfd.get("role") or ("project" if lineage else "root"),
            "self": True,
            "source": None,
            "path": None,
            "pin": None,
            "tree_sha": None,
            "context": context_root(target),
            "wiki": wiki_dir(target),
            "cached": True,
        }
    ]
    for entry in (lineage or {}).get("layers", []):
        layer = dict(entry)
        layer["self"] = False
        layer["context"] = layer_context_dir(entry)
        layer["wiki"] = layer_wiki_dir(entry)
        layer["cached"] = is_layer_cached(entry)
        layers.append(layer)
    for p in extra or []:
        p = Path(p).resolve()
        wiki = p / WIKI_SUBDIR if (p / WIKI_SUBDIR).is_dir() else p
        layers.append(
            {
                "id": p.name,
                "role": "layer",
                "self": False,
                "source": None,
                "path": None,
                "pin": None,
                "tree_sha": None,
                "context": wiki.parent,
                "wiki": wiki,
                "cached": (wiki / INDEX_FILE).is_file(),
            }
        )
    return layers


def parent_layers(layers: list) -> list:
    """Everything but the self layer."""
    return [l for l in layers if not l.get("self")]


def page_path(wiki_root: Path, slug: str) -> Optional[Path]:
    """Return the on-disk path for ``slug`` inside one wiki, or None.

    Searches the canonical subdirectories plus index.md / log.md at the root.
    """
    wiki_root = Path(wiki_root)
    if slug == "index":
        p = wiki_root / INDEX_FILE
        return p if p.is_file() else None
    if slug == "log":
        p = wiki_root / LOG_FILE
        return p if p.is_file() else None
    for sub in WIKI_SUBDIRS:
        p = wiki_root / sub / f"{slug}.md"
        if p.is_file():
            return p
    return None


def occurrences(slug: str, layers: list) -> list:
    """All (layer, path) pairs where ``slug`` exists, nearest layer first."""
    out = []
    for layer in layers:
        wiki = layer.get("wiki")
        if wiki is None or not layer.get("cached"):
            continue
        p = page_path(wiki, slug)
        if p is not None:
            out.append((layer, p))
    return out


def resolve_slug(slug: str, layers: list):
    """Nearest (layer, path) for ``slug`` or (None, None)."""
    found = occurrences(slug, layers)
    return found[0] if found else (None, None)


def parse_ref(ref: str):
    """Split 'acme/payments/go-service-patterns' -> ('acme/payments', 'go-service-patterns').

    Layer ids may contain '/', slugs may not, so the split is at the last '/'.
    Returns ('', ref) when there is no layer prefix.
    """
    if not isinstance(ref, str) or "/" not in ref:
        return "", str(ref)
    lid, slug = ref.rsplit("/", 1)
    return lid, slug


def layer_by_id(layers: list, layer_id: str) -> Optional[dict]:
    """Find a layer dict by id."""
    for layer in layers:
        if layer.get("id") == layer_id:
            return layer
    return None


def resolve_ref(ref: str, layers: list):
    """Resolve a '<layer-id>/<slug>' ref to (layer, path); falls back to
    nearest-layer slug lookup when the layer id is unknown or not cached."""
    lid, slug = parse_ref(ref)
    layer = layer_by_id(layers, lid) if lid else None
    if layer is not None and layer.get("cached") and layer.get("wiki") is not None:
        p = page_path(layer["wiki"], slug)
        if p is not None:
            return layer, p
    return resolve_slug(slug, layers)


def merged_index(layers: list) -> list:
    """Return [{layer_id, role, self, cached, path, text}] for every layer's index.md."""
    out = []
    for layer in layers:
        wiki = layer.get("wiki")
        idx = (Path(wiki) / INDEX_FILE) if wiki is not None else None
        text = None
        if idx is not None and layer.get("cached") and idx.is_file():
            try:
                text = idx.read_text(encoding="utf-8")
            except OSError:
                text = None
        out.append(
            {
                "layer_id": layer.get("id"),
                "role": layer.get("role"),
                "self": bool(layer.get("self")),
                "cached": bool(layer.get("cached")),
                "path": str(idx) if idx is not None else None,
                "text": text,
            }
        )
    return out


def describe_layers(layers: list) -> list:
    """JSON-safe view of a layer list (Paths -> str)."""
    out = []
    for layer in layers:
        d = {k: v for k, v in layer.items() if k not in ("context", "wiki")}
        d["context"] = str(layer["context"]) if layer.get("context") is not None else None
        d["wiki"] = str(layer["wiki"]) if layer.get("wiki") is not None else None
        out.append(d)
    return out


# ---------- persona ranking (deterministic, no LLM) ----------

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(text) -> set:
    if not isinstance(text, str):
        return set()
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if len(t) > 1}


def score_persona(query: str, entry: dict) -> tuple:
    """Return (score, why[]) for a persona index entry against ``query``.

    Deterministic token overlap, mirroring ai-quickstart's next-project
    ranking: query tokens vs title + tags + summary (+0.5 scaled by overlap
    fraction), team/id segment match (+0.2), exact tag hits (+0.1 each,
    capped), and a small recency bonus when ``updated`` is within 90 days.
    An empty query ranks everything equally (score 0) so callers can list.
    """
    q = _tokens(query)
    why = []
    if not q:
        return 0.0, why
    title_t = _tokens(entry.get("title"))
    summary_t = _tokens(entry.get("summary"))
    tags = [str(t).lower() for t in (entry.get("tags") or []) if isinstance(t, str)]
    tag_t = set()
    for t in tags:
        tag_t |= _tokens(t)
    id_t = _tokens(str(entry.get("id") or "").replace("/", " ").replace("-", " "))
    team_t = _tokens(str(entry.get("team") or "").replace("/", " "))
    hay = title_t | summary_t | tag_t
    overlap = q & hay
    score = 0.0
    if overlap:
        score += 0.5 * (len(overlap) / len(q))
        why.append("matches " + ", ".join(sorted(overlap)))
    team_hit = q & (team_t | id_t)
    if team_hit:
        score += 0.2
        why.append("team/id " + ", ".join(sorted(team_hit)))
    exact_tags = [t for t in tags if t in q]
    if exact_tags:
        score += min(0.1 * len(exact_tags), 0.3)
        why.append("tags " + ", ".join(sorted(exact_tags)))
    updated = entry.get("updated")
    if isinstance(updated, str) and len(updated) >= 10:
        try:
            import datetime as _dt  # pylint: disable=import-outside-toplevel
            d = _dt.date.fromisoformat(updated[:10])
            if (_dt.date.today() - d).days <= 90:
                score += 0.1
                why.append("updated recently")
        except ValueError:
            pass
    return round(score, 4), why
