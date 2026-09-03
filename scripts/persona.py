#!/usr/bin/env python3
"""compathy persona: export, import, search, and maintain federated layers.

One dispatcher, many subcommands (the same shape as ai-quickstart's
``/ai-quickstart heal``). Every subcommand prints one JSON document on
stdout so the SKILL.md can hand it to the model verbatim; human rendering
is the model's job. Exit code 0 = ok, 1 = user-facing failure (the JSON
carries ``error``), 2 = usage.

    export propose --role R           what a persona for R could contain
    export write   --role R --spec F  write context/personas/R.json (+ index)
    import ARG                        plan only (file | https URL | org/team/role id)
    import ARG --apply --consent fetch,link,toolkit [--as team --self-id ID]
    search [QUERY] [--org SRC] [--local] [--refresh]
    sync                              fetch missing parent layers from lineage.json
    status [--check-upstream]         pins, cache, verification per layer
    update [--layer ID] [--to SHA] [--apply]   re-pin with an index.md diff
    unlink                            remove lineage.json + persona.json
    whoami                            active persona + lineage for cwd
    resolve SLUG                      which layers have this slug
    registry init --org ID | add-team --id T --source S [--path P]
    config set-org SRC [--path P] | show

Python does the deterministic parts: git pins, tree hashes, fetching,
verification, JSON writes, ranking. The model interviews, chooses, and
briefs. Nothing here installs anything without an explicit ``--consent``.

Permission model in one paragraph: git decides who may write a layer
(branch protection + CODEOWNERS); lint decides whether a child respects a
parent's ``authority``/``override``; pins + tree hashes decide what an
importer trusts; the org registry is the only trusted path to discover
teams; every import is consented per category and logged. compathy has no
accounts, tokens, or servers.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
import discovery  # noqa: E402
import layers as layers_mod  # noqa: E402
import lineage as lin  # noqa: E402
import persona_integration  # noqa: E402
from lint import iter_wiki_pages, lint as run_lint, parse_frontmatter  # noqa: E402
from paths import (  # noqa: E402
    INDEX_FILE,
    LOG_FILE,
    PERSONAS_INDEX_FILE,
    PERSONAS_SUBDIR,
    WIKI_SUBDIR,
    config_path,
    context_root,
    import_log_path,
    layer_slug,
    lineage_path,
    persona_path,
    personas_dir,
    personas_home_dir,
    personas_index_path,
    registry_cache_dir,
    registry_path,
    state_home,
    wiki_dir,
)
from version import get_version  # noqa: E402

REGISTRY_TTL_SECONDS = 6 * 3600
URL_TIMEOUT = 20
MAX_URL_BYTES = 1_000_000
PIN_AGE_WARN_DAYS = 90
CONSENT_KINDS = ("fetch", "link", "toolkit")
DEFAULT_TOOLKIT = {
    "claude_skills": [{"name": "compathy", "github": "gitbk4/compathy"}],
    "mcp_servers": [],
}


class PersonaError(Exception):
    """User-facing failure; rendered as {"error": ...} with exit 1."""


# ---------- small helpers ----------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _today() -> str:
    return _dt.date.today().isoformat()


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _append_log(target: Path, op: str, summary: str, details: Optional[list] = None) -> None:
    """Append a log.md entry (no-op if the wiki has no log)."""
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


def _append_import_log(record: dict) -> None:
    """Append one <=4096-byte line to ~/.compathy/import-log.jsonl."""
    path = import_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(line.encode("utf-8")) > 4096:
        record = {k: record[k] for k in ("at", "persona", "target", "trust") if k in record}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _load_config() -> dict:
    cp = config_path()
    if not cp.is_file():
        return {}
    try:
        data = lin.read_json(cp)
    except lin.LineageError:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_layer(layer: dict) -> dict:
    """Keep only the persisted fields of a layer entry."""
    out = {k: layer.get(k) for k in ("id", "role", "source", "path", "pin", "tree_sha")}
    out["path"] = out["path"] or "context"
    return out


def _wiki_slugs(wiki_root: Path) -> dict:
    """slug -> {type, title, authority} for one wiki."""
    out = {}
    for slug, path in iter_wiki_pages(wiki_root):
        try:
            fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        title = slug
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        out[slug] = {"type": fm.get("type"), "title": title,
                     "authority": fm.get("authority"), "body": body}
    return out


# ---------- org / registry resolution ----------

def _org_ref(args, target: Path) -> Optional[dict]:
    """{source, path} of the org compathy: --org > config > cwd lineage."""
    org = getattr(args, "org", None)
    if org:
        if not lin.valid_source(org):
            raise PersonaError(f"--org must start with one of {lin.SOURCE_SCHEMES}")
        return {"source": org, "path": getattr(args, "org_path", None) or "context"}
    cfg = _load_config().get("default_org")
    if isinstance(cfg, dict) and lin.valid_source(cfg.get("source")):
        return {"source": cfg["source"], "path": cfg.get("path") or "context"}
    try:
        doc = lin.load_lineage(target)
    except lin.LineageError:
        doc = None
    if doc:
        for layer in doc.get("layers", []):
            if layer.get("role") == "org":
                return {"source": layer["source"], "path": layer.get("path") or "context"}
        last = doc["layers"][-1]
        return {"source": last["source"], "path": last.get("path") or "context"}
    return None


def _fetch_org_registry(org: dict, refresh: bool = False) -> dict:
    """Fetch (cached) the org registry + the org's own personas dir."""
    dest = registry_cache_dir() / layer_slug(org["source"])
    paths = [f"{org['path']}/registry.json", f"{org['path']}/{PERSONAS_SUBDIR}"]
    try:
        r = layers_mod.fetch_paths(org["source"], "HEAD", paths, dest,
                                   ttl_seconds=REGISTRY_TTL_SECONDS, refresh=refresh)
    except layers_mod.LayerFetchError as e:
        raise PersonaError(f"cannot reach org registry at {org['source']}: {e}") from e
    reg_path = Path(r["root"]) / org["path"] / "registry.json"
    if not reg_path.is_file():
        raise PersonaError(
            f"org repo has no {org['path']}/registry.json at HEAD "
            f"(an org maintainer creates it with `compathy-persona registry init`)"
        )
    data = lin.read_json(reg_path)
    probs = lin.validate_registry(data)
    if probs:
        raise PersonaError("org registry.json is invalid: " + "; ".join(probs))
    return {"registry": data, "root": Path(r["root"]), "fetch": r,
            "personas_dir": Path(r["root"]) / org["path"] / PERSONAS_SUBDIR}


def _fetch_team_personas(team: dict, refresh: bool = False) -> dict:
    dest = registry_cache_dir() / layer_slug(team["source"]) / layer_slug(team.get("path") or "context")
    path = f"{team.get('path') or 'context'}/{PERSONAS_SUBDIR}"
    r = layers_mod.fetch_paths(team["source"], "HEAD", [path], dest,
                               ttl_seconds=REGISTRY_TTL_SECONDS, refresh=refresh)
    return {"root": Path(r["root"]), "personas_dir": Path(r["root"]) / path, "fetch": r}


def _read_personas_dir(pdir: Path, team_id: str, source: str) -> list:
    """Entries for every valid manifest in a personas dir."""
    out = []
    if not pdir.is_dir():
        return out
    for f in sorted(pdir.glob("*.json")):
        if f.name == PERSONAS_INDEX_FILE:
            continue
        try:
            data = lin.read_json(f)
        except lin.LineageError:
            continue
        if lin.validate_manifest(data):
            continue
        out.append({
            "id": data["id"], "title": data.get("title"), "summary": data.get("summary"),
            "tags": data.get("tags") or [], "team": team_id,
            "updated": ((data.get("provenance") or {}).get("exported_at") or "")[:10] or None,
            "source": source, "file": f.name, "path": str(f),
        })
    return out


def _lookup_in_registry(persona_id: str, org: dict, refresh: bool = False):
    """Resolve 'org/team/role' via the org registry. Returns (manifest, path, team)."""
    layer_id, role = lin.parse_ref(persona_id)
    reg = _fetch_org_registry(org, refresh=refresh)
    registry = reg["registry"]
    org_id = registry["org"]
    if layer_id == org_id:
        pdir = reg["personas_dir"]
        team = {"id": org_id, "source": org["source"], "path": org["path"], "role": "org"}
    else:
        if not layer_id.startswith(org_id + "/"):
            raise PersonaError(f"persona {persona_id!r} does not belong to org {org_id!r}")
        team_seg = layer_id[len(org_id) + 1:]
        team = next((t for t in registry.get("teams", []) if t.get("id") == team_seg), None)
        if team is None:
            raise PersonaError(
                f"team {team_seg!r} is not listed in {org_id}'s registry "
                f"(known: {[t.get('id') for t in registry.get('teams', [])]})"
            )
        team = dict(team, role="team")
        try:
            pdir = _fetch_team_personas(team, refresh=refresh)["personas_dir"]
        except layers_mod.LayerFetchError as e:
            raise PersonaError(f"cannot reach team {layer_id} at {team['source']}: {e}") from e
    f = pdir / f"{role}.json"
    if not f.is_file():
        raise PersonaError(f"{layer_id} exports no persona named {role!r} "
                           f"(has: {[p.stem for p in pdir.glob('*.json') if p.name != PERSONAS_INDEX_FILE] if pdir.is_dir() else []})")
    data = lin.read_json(f)
    probs = lin.validate_manifest(data)
    if probs:
        raise PersonaError(f"persona {persona_id} in registry is invalid: " + "; ".join(probs))
    if data["id"] != persona_id:
        raise PersonaError(f"registry file {role}.json declares id {data['id']!r}, expected {persona_id!r}")
    return data, f, team


# ---------- manifest argument resolution (import) ----------

def _fetch_url_json(url: str) -> dict:
    if not url.startswith("https://"):
        raise PersonaError("only https:// URLs are accepted for persona files")
    req = urllib.request.Request(url, headers={"User-Agent": f"compathy/{get_version()}"})
    try:
        with urllib.request.urlopen(req, timeout=URL_TIMEOUT) as resp:  # nosec - https only
            raw = resp.read(MAX_URL_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise PersonaError(f"cannot fetch {url}: {e}") from e
    if len(raw) > MAX_URL_BYTES:
        raise PersonaError(f"{url} exceeds {MAX_URL_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PersonaError(f"{url} is not valid JSON: {e}") from e


def resolve_manifest_arg(arg: str, org: Optional[dict], refresh: bool = False) -> dict:
    """Turn a file path, https URL, or persona id into a validated manifest.

    Returns {manifest, kind: file|url|registry, origin, registry_match}.
    For file/url manifests, when an org is known the same id is looked up in
    the registry; a byte-equal match lifts trust to the registry level
    ("registry re-resolution" - the org is the trust anchor, not the file).
    """
    p = Path(arg).expanduser()
    if p.is_file():
        data = lin.read_json(p)
        kind, origin = "file", str(p.resolve())
    elif arg.startswith("https://") or arg.startswith("http://"):
        data = _fetch_url_json(arg)
        kind, origin = "url", arg
    elif lin.LAYER_ID_RE.match(arg) and "/" in arg:
        if org is None:
            raise PersonaError(
                f"{arg!r} looks like a persona id; pass --org <git+https://.../org-repo.git> "
                f"or set one with `compathy-persona config set-org`"
            )
        data, f, team = _lookup_in_registry(arg, org, refresh=refresh)
        return {"manifest": data, "kind": "registry", "origin": f"{team['source']}::{f.name}",
                "registry_match": True, "team": team}
    else:
        raise PersonaError(f"cannot resolve {arg!r}: not a file, https URL, or org/team/role id")
    probs = lin.validate_manifest(data)
    if probs:
        raise PersonaError("persona manifest is invalid: " + "; ".join(probs))
    match = False
    if org is not None:
        try:
            reg_data, _, _ = _lookup_in_registry(data["id"], org, refresh=refresh)
            match = json.dumps(reg_data, sort_keys=True) == json.dumps(data, sort_keys=True)
        except (PersonaError, lin.LineageError):
            match = False
    return {"manifest": data, "kind": kind, "origin": origin, "registry_match": match}


def trust_score(kind: str, registry_match: bool, verified: bool) -> tuple:
    """Deterministic 1-5 trust for a persona (5 reserved for signed, v1.1)."""
    reasons = []
    if not verified:
        reasons.append("a layer is not pinned, cached, or hash-verified")
        return 1, reasons
    reasons.append("all layers pinned and tree-hash verified")
    if kind == "registry":
        reasons.append("reached via the org registry")
        return 4, reasons
    if registry_match:
        reasons.append("byte-identical to the copy published in the org registry")
        return 4, reasons
    if kind == "url":
        reasons.append("fetched from a direct URL (not via the org registry)")
        return 3, reasons
    reasons.append("local file handed out-of-band (not cross-checked against the org registry)")
    return 2, reasons


# ---------- export ----------

def _self_layer(target: Path, args) -> dict:
    """Describe the layer being exported from (id, role, source, path, pin, tree)."""
    repo = layers_mod.repo_toplevel(target)
    if repo is None:
        raise PersonaError("export requires a git repository (pins are commit shas)")
    ctx_rel = layers_mod.relpath_in_repo(repo, context_root(target))
    ctx_rel = "context" if ctx_rel == "." else ctx_rel
    pin = layers_mod.head_sha(repo)
    if pin is None:
        raise PersonaError("repository has no commits yet; commit context/ first")
    tree = layers_mod.tree_sha(repo, "HEAD", f"{ctx_rel}/{WIKI_SUBDIR}")
    if tree is None:
        raise PersonaError(f"{ctx_rel}/{WIKI_SUBDIR} is not committed at HEAD; commit the wiki first")
    try:
        doc = lin.load_lineage(target)
    except lin.LineageError as e:
        raise PersonaError(f"context/lineage.json is invalid: {e}") from e
    selfd = (doc or {}).get("self") or {}
    layer_id = getattr(args, "layer_id", None) or selfd.get("id")
    if not layer_id or not lin.LAYER_ID_RE.match(layer_id):
        raise PersonaError(
            "cannot determine this layer's id; pass --layer-id (e.g. 'acme' for an org, "
            "'acme/payments' for a team)"
        )
    role = getattr(args, "layer_role", None) or selfd.get("role") or ("org" if not doc else "project")
    source = getattr(args, "source", None) or layers_mod.remote_source(repo)
    if not lin.valid_source(source):
        raise PersonaError(
            "cannot determine this repo's git source (no 'origin' remote?); pass --source git+https://..."
        )
    return {
        "id": layer_id, "role": role, "source": source, "path": ctx_rel,
        "pin": pin, "tree_sha": tree, "repo": str(repo),
        "clean": layers_mod.is_clean(repo, ctx_rel),
        "parents": [_strip_layer(l) for l in (doc or {}).get("layers", [])],
    }


def cmd_export_propose(args) -> dict:
    target = Path(args.target).resolve()
    if not (wiki_dir(target) / INDEX_FILE).is_file():
        raise PersonaError("no compathy wiki here; run /compathy first")
    me = _self_layer(target, args)
    lint_result = run_lint(target)
    pages = _wiki_slugs(wiki_dir(target))
    role_words = [w for w in re.split(r"[^a-z0-9]+", (args.role or "").lower()) if len(w) > 2]
    mentions = []
    for slug, info in pages.items():
        hay = (info["body"] or "").lower() + " " + slug
        if role_words and all(w in hay for w in role_words):
            mentions.append(slug)
    existing = []
    if personas_dir(target).is_dir():
        existing = sorted(p.stem for p in personas_dir(target).glob("*.json") if p.name != PERSONAS_INDEX_FILE)
    return {
        "role": args.role,
        "persona_id": f"{me['id']}/{args.role}",
        "layer": {k: me[k] for k in ("id", "role", "source", "path", "pin", "tree_sha")},
        "parents": me["parents"],
        "preconditions": {
            "git_clean": me["clean"],
            "lint_errors": lint_result["summary"]["errors"],
            "lint_warnings": lint_result["summary"]["warnings"],
        },
        "candidates": {
            "patterns": [s for s, i in pages.items() if i["type"] == "patterns"],
            "concepts": [s for s, i in pages.items() if i["type"] == "concept"],
            "entities": [s for s, i in pages.items() if i["type"] == "entity"],
            "authoritative": [s for s, i in pages.items() if i["authority"]],
            "mentioning_role": mentions,
        },
        "existing_personas": existing,
        "spec_template": {
            "title": f"{args.role.replace('-', ' ').title()}, {me['id'].split('/')[-1]}",
            "summary": "",
            "tags": [],
            "reads_first": [],
            "responsibilities": [],
            "toolkit": DEFAULT_TOOLKIT,
            "policy": {"may_edit": ["project"], "may_propose_to": [me["id"]],
                       "read_only": [p["id"] for p in me["parents"]]},
        },
        "next": "write a spec JSON and run: persona.py export write --role "
                f"{args.role} --spec <file>",
    }


def _resolve_reads_first(refs: list, me_layers: list) -> list:
    """Normalize reads_first to '<layer-id>/<slug>' and verify each exists."""
    out = []
    missing = []
    for ref in refs:
        lid, slug = lin.parse_ref(ref)
        hit = None
        for layer in me_layers:
            if lid and layer.get("id") != lid:
                continue
            wiki = layer.get("wiki")
            if wiki is not None and layer.get("cached") and lin.page_path(wiki, slug) is not None:
                hit = layer
                break
        if hit is None:
            missing.append(ref)
        else:
            out.append(f"{hit['id']}/{slug}")
    if missing:
        raise PersonaError(f"reads_first pages not found in this layer or its cached parents: {missing}")
    return out


def _regenerate_personas_index(target: Path, team_id: str) -> Path:
    pdir = personas_dir(target)
    entries = []
    for e in _read_personas_dir(pdir, team_id, source=""):
        entries.append({"id": e["id"], "file": e["file"], "title": e["title"],
                        "tags": e["tags"], "summary": e["summary"], "updated": e["updated"]})
    idx = {"schema_version": lin.MANIFEST_SCHEMA_VERSION, "team": team_id,
           "generated_at": _now_iso(), "personas": entries}
    path = personas_index_path(target)
    lin.write_json_atomic(path, idx)
    return path


def cmd_export_write(args) -> dict:
    target = Path(args.target).resolve()
    if not (wiki_dir(target) / INDEX_FILE).is_file():
        raise PersonaError("no compathy wiki here; run /compathy first")
    if not lin.SLUG_RE.match(args.role or ""):
        raise PersonaError("--role must be kebab-case, e.g. backend-engineer")
    me = _self_layer(target, args)
    if not me["clean"] and not args.allow_dirty:
        raise PersonaError(
            f"{me['path']} has uncommitted changes; commit first so the pin is reproducible "
            f"(or pass --allow-dirty)"
        )
    lint_result = run_lint(target)
    if lint_result["summary"]["errors"] and not args.allow_lint_errors:
        raise PersonaError(
            f"lint reports {lint_result['summary']['errors']} error(s); fix them first "
            f"(or pass --allow-lint-errors)"
        )
    spec = lin.read_json(Path(args.spec)) if args.spec else {}
    if not isinstance(spec, dict):
        raise PersonaError("--spec must be a JSON object")
    title = args.title or spec.get("title")
    if not title:
        raise PersonaError("a title is required (--title or spec.title)")

    self_layer = {k: me[k] for k in ("id", "role", "source", "path", "pin", "tree_sha")}
    manifest_layers = [self_layer] + me["parents"]
    # Layers as seen from *this* repo, for resolving reads_first.
    me_layers = [{"id": me["id"], "role": me["role"], "wiki": wiki_dir(target), "cached": True}]
    for p in me["parents"]:
        me_layers.append({"id": p["id"], "role": p["role"], "wiki": lin.layer_wiki_dir(p),
                          "cached": lin.is_layer_cached(p)})
    reads_first = _resolve_reads_first(list(spec.get("reads_first") or []), me_layers)

    exported_by = args.exported_by or spec.get("exported_by")
    if not exported_by:
        r = layers_mod.git(["config", "user.email"], cwd=Path(me["repo"]))
        exported_by = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"

    manifest = {
        "schema_version": lin.MANIFEST_SCHEMA_VERSION,
        "kind": lin.MANIFEST_KIND,
        "id": f"{me['id']}/{args.role}",
        "title": title,
        "summary": str(spec.get("summary") or args.summary or ""),
        "tags": [str(t) for t in (spec.get("tags") or [])] or [t for t in (args.tags or "").split(",") if t],
        "layers": manifest_layers,
        "reads_first": reads_first,
        "responsibilities": [str(x) for x in (spec.get("responsibilities") or [])],
        "toolkit": spec.get("toolkit") or DEFAULT_TOOLKIT,
        "policy": spec.get("policy") or {
            "may_edit": ["project"], "may_propose_to": [me["id"]],
            "read_only": [p["id"] for p in me["parents"]],
        },
        "provenance": {
            "exported_by": exported_by, "exported_at": _now_iso(),
            "from_commit": me["pin"], "exporter_version": get_version(),
        },
        "signature": None,
    }
    probs = lin.validate_manifest(manifest)
    if probs:
        raise PersonaError("built manifest failed validation: " + "; ".join(probs))
    out_path = personas_dir(target) / f"{args.role}.json"
    existed = out_path.exists()
    lin.write_json_atomic(out_path, manifest)
    idx_path = _regenerate_personas_index(target, me["id"])
    _append_log(target, "export", f"persona {manifest['id']}",
                [f"file: {out_path.relative_to(target)}", f"pin: {me['pin'][:12]}",
                 f"reads_first: {', '.join(reads_first) or '(none)'}"])
    return {
        "written": str(out_path), "replaced": existed, "index": str(idx_path),
        "persona": manifest,
        "next": f"commit {out_path.relative_to(target).parent}/ so `compathy-persona search` can find it",
    }


# ---------- import ----------

def _existing_link(target: Path):
    try:
        return lin.load_lineage(target)
    except lin.LineageError as e:
        raise PersonaError(f"context/lineage.json is invalid: {e}") from e


def _layer_status(layer: dict) -> dict:
    v = layers_mod.verify_layer(layer)
    return {
        **_strip_layer(layer),
        "cached": lin.is_layer_cached(layer),
        "verified": v["ok"],
        "verify_reasons": v["reasons"],
        "cache_root": str(lin.layer_cache_root(layer)),
    }


def _plan_warnings(manifest: dict, existing: Optional[dict]) -> list:
    warnings = ["unsigned persona (signature verification arrives in v1.1); trust comes from pins + the org registry"]
    exported_at = (manifest.get("provenance") or {}).get("exported_at")
    if isinstance(exported_at, str) and len(exported_at) >= 10:
        try:
            d = _dt.date.fromisoformat(exported_at[:10])
            age = (_dt.date.today() - d).days
            if age > PIN_AGE_WARN_DAYS:
                warnings.append(f"persona exported {age} days ago; pins may be stale (run `update` after import)")
        except ValueError:
            pass
    for layer in manifest.get("layers", []):
        if str(layer.get("source", "")).startswith("git+file://"):
            warnings.append(f"layer {layer.get('id')} uses a local file:// source (fine for tests, not for sharing)")
    if existing and existing.get("persona") and existing.get("persona") != manifest.get("id"):
        warnings.append(f"this project is already linked as {existing.get('persona')}; import will replace it")
    return warnings


def cmd_import(args) -> dict:
    target = Path(args.target).resolve()
    org = _org_ref(args, target)
    res = resolve_manifest_arg(args.persona, org, refresh=args.refresh)
    manifest = res["manifest"]
    existing = _existing_link(target)
    layer_status = [_layer_status(l) for l in manifest["layers"]]
    all_verified_now = all(s["verified"] for s in layer_status)
    same = bool(existing and existing.get("persona") == manifest["id"] and
                [_strip_layer(l) for l in existing.get("layers", [])] == [_strip_layer(l) for l in manifest["layers"]])
    pin_changes = []
    if existing:
        old = {l["id"]: l for l in existing.get("layers", [])}
        for l in manifest["layers"]:
            if l["id"] in old and old[l["id"]].get("pin") != l.get("pin"):
                pin_changes.append({"id": l["id"], "old": old[l["id"]].get("pin"), "new": l.get("pin")})
    trust, reasons = trust_score(res["kind"], res["registry_match"], all_verified_now)
    trust_if_verified, _ = trust_score(res["kind"], res["registry_match"], True)
    plan = {
        "mode": "apply" if args.apply else "plan",
        "persona": manifest,
        "source_kind": res["kind"],
        "origin": res["origin"],
        "registry_match": res["registry_match"],
        "trust": trust if all_verified_now else trust_if_verified,
        "trust_provisional": not all_verified_now,
        "trust_reasons": reasons if all_verified_now else
            [f"expected after fetch + verification (currently {trust}: layers not yet cached)"],
        "target": str(target),
        "scaffold_needed": not context_root(target).exists(),
        "already_linked": {"persona": existing.get("persona"), "self": existing.get("self")} if existing else None,
        "already_linked_at_these_pins": same,
        "pin_changes": pin_changes,
        "layers": layer_status,
        "toolkit": manifest.get("toolkit") or {},
        "policy": manifest.get("policy") or {},
        "reads_first": manifest.get("reads_first") or [],
        "warnings": _plan_warnings(manifest, existing),
        "consent_kinds": list(CONSENT_KINDS),
    }
    if not args.apply:
        plan["next"] = ("ask the user per category, then run again with --apply "
                        "--consent fetch,link,toolkit (any subset)")
        return plan

    consent = {c.strip() for c in (args.consent or "").split(",") if c.strip()}
    bad = consent - set(CONSENT_KINDS)
    if bad:
        raise PersonaError(f"unknown consent kind(s) {sorted(bad)}; allowed: {CONSENT_KINDS}")
    if not consent:
        raise PersonaError("--apply requires --consent with at least one of fetch,link,toolkit")
    if args.as_role == "team" and not args.self_id:
        raise PersonaError("--as team requires --self-id (e.g. acme/payments)")

    result = dict(plan)
    result.update({"fetched": [], "linked": False, "written": [], "toolkit_actions": {},
                   "briefing": [], "index_diffs": []})

    # 1. fetch
    if "fetch" in consent:
        for layer in manifest["layers"]:
            try:
                r = layers_mod.fetch_layer(layer, force=args.force)
            except layers_mod.LayerFetchError as e:
                raise PersonaError(f"fetch aborted, nothing linked: {e}") from e
            result["fetched"].append({"id": layer["id"], "action": r["action"], "root": r["root"]})
    result["layers"] = [_layer_status(l) for l in manifest["layers"]]
    verified = all(s["verified"] for s in result["layers"])
    trust, reasons = trust_score(res["kind"], res["registry_match"], verified)
    result["trust"], result["trust_reasons"], result["trust_provisional"] = trust, reasons, False
    if not verified:
        result["warnings"].append("not all layers are cached + verified; trust 1. Run `sync` after consenting to fetch")

    # index diffs for pin changes (both old and new now cached)
    if existing and pin_changes:
        for ch in pin_changes:
            old_layer = next(l for l in existing["layers"] if l["id"] == ch["id"])
            new_layer = next(l for l in manifest["layers"] if l["id"] == ch["id"])
            result["index_diffs"].append({"id": ch["id"], "diff": _index_diff(old_layer, new_layer)})

    # 2. link
    if "link" in consent:
        if same and not args.force:
            result["linked"] = "already"
        else:
            result["written"].extend(_link(target, manifest, res, trust, args))
            result["linked"] = True

    # 3. toolkit
    if "toolkit" in consent:
        result["toolkit_actions"] = _apply_toolkit(target, manifest)

    # 4. briefing material (paths the model reads to brief the new member)
    all_layers = lin.resolve_layers(target) if lineage_path(target).is_file() else None
    if all_layers is None:
        all_layers = [{"id": target.name, "role": "project", "self": True, "wiki": wiki_dir(target),
                       "cached": wiki_dir(target).is_dir()}]
        for l in manifest["layers"]:
            all_layers.append({**l, "self": False, "wiki": lin.layer_wiki_dir(l), "cached": lin.is_layer_cached(l)})
    for ref in manifest.get("reads_first") or []:
        layer, path = lin.resolve_ref(ref, all_layers)
        result["briefing"].append({"ref": ref, "layer": layer.get("id") if layer else None,
                                   "path": str(path) if path else None, "found": path is not None})
    _append_import_log({"at": _now_iso(), "persona": manifest["id"], "target": str(target),
                        "kind": res["kind"], "origin": res["origin"], "trust": trust,
                        "consent": sorted(consent), "linked": result["linked"]})
    result["next"] = "run `/compathy` to compile this project against its lineage"
    return result


def _index_diff(old_layer: dict, new_layer: dict) -> str:
    def _read(layer):
        p = lin.layer_wiki_dir(layer) / INDEX_FILE
        try:
            return p.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return []
    a, b = _read(old_layer), _read(new_layer)
    return "".join(difflib.unified_diff(
        a, b, fromfile=f"{old_layer['id']}@{str(old_layer.get('pin'))[:12]}/index.md",
        tofile=f"{new_layer['id']}@{str(new_layer.get('pin'))[:12]}/index.md"))


def _link(target: Path, manifest: dict, res: dict, trust: int, args) -> list:
    """Write lineage.json, persona.json, the persona entity page, breadcrumbs, log."""
    written = []
    project_name = args.project_name or target.name
    if not context_root(target).exists():
        import scaffold  # pylint: disable=import-outside-toplevel
        scaffold.create_structure(target, project_name)
        written.append(str(context_root(target)))
    self_id = args.self_id or project_name
    doc = {
        "schema_version": lin.LINEAGE_SCHEMA_VERSION,
        "layers": [_strip_layer(l) for l in manifest["layers"]],
        "self": {"id": self_id, "role": args.as_role},
        "persona": manifest["id"],
        "imported_at": _now_iso(),
        "imported_from": res["origin"],
        "trust_at_import": trust,
    }
    written.append(str(lin.save_lineage(target, doc)))
    lin.write_json_atomic(persona_path(target), manifest)
    written.append(str(persona_path(target)))
    page = _write_persona_entity(target, manifest)
    if page:
        written.append(str(page))
    discovery.write_discovery_breadcrumbs(target, project_name)
    written.append(str(target / discovery.CLAUDE_MD))
    home_copy = personas_home_dir() / f"{layer_slug(manifest['id'])}.json"
    lin.write_json_atomic(home_copy, {**manifest, "_imported": {"at": doc["imported_at"], "target": str(target),
                                                                  "origin": res["origin"], "trust": trust}})
    written.append(str(home_copy))
    _append_log(target, "import", f"persona {manifest['id']} (trust {trust})",
                [f"layers: {', '.join(l['id'] + '@' + l['pin'][:12] for l in manifest['layers'])}",
                 f"from: {res['origin']}"])
    return written


def _write_persona_entity(target: Path, manifest: dict) -> Optional[Path]:
    """Seed entities/persona-<role>.md (once) so the wiki records who it works as."""
    role = manifest["id"].rsplit("/", 1)[1]
    slug = f"persona-{role}"
    path = wiki_dir(target) / "entities" / f"{slug}.md"
    if path.exists() or not path.parent.is_dir():
        return None
    layers_desc = [f"- `{l['id']}` ({l['role']}) pinned at `{l['pin'][:12]}`" for l in manifest["layers"]]
    reads = []
    seen_slugs = set()
    for ref in manifest.get("reads_first") or []:
        lid, s = lin.parse_ref(ref)
        if s in seen_slugs:
            continue  # same slug in two layers: one link resolves nearest-first
        seen_slugs.add(s)
        reads.append(f"- [[{s}]] (in `{lid}`)" if lid else f"- [[{s}]]")
    lines = [
        "---", "type: entity", "schema_version: 1", f"created: {_today()}",
        f"updated: {_today()}", "provenance: from-persona", "---", "",
        f"# {manifest.get('title') or role}", "",
        f"Persona `{manifest['id']}` imported into this project.", "",
    ]
    if manifest.get("summary"):
        lines += [str(manifest["summary"]).strip(), ""]
    lines += ["## Layers", ""] + layers_desc + [""]
    if reads:
        lines += ["## Read first", ""] + reads + [""]
    if manifest.get("responsibilities"):
        lines += ["## Responsibilities", ""] + [f"- {r}" for r in manifest["responsibilities"]] + [""]
    policy = manifest.get("policy") or {}
    lines += ["## Policy", "",
              f"- may edit: {', '.join(policy.get('may_edit') or []) or 'unspecified'}",
              f"- may propose to: {', '.join(policy.get('may_propose_to') or []) or 'unspecified'}",
              f"- read only: {', '.join(policy.get('read_only') or []) or 'unspecified'}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    idx = wiki_dir(target) / INDEX_FILE
    if idx.is_file():
        text = idx.read_text(encoding="utf-8")
        entry = f"- [[{slug}]] - {manifest.get('title') or role} (imported team persona)"
        # pylint: disable=protected-access
        idx.write_text(persona_integration._insert_index_entry(text, section="## Entities", entry=entry),
                       encoding="utf-8")
    return path


def _upsert_mcp_server(target: Path, key: str, entry: dict) -> str:
    """Merge one mcpServers entry into <target>/.mcp.json (touches only that key)."""
    path = target / discovery.MCP_JSON
    if not path.exists():
        path.write_text(json.dumps({"mcpServers": {key: entry}}, indent=2) + "\n", encoding="utf-8")
        return "created"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "skipped-malformed"
    if not isinstance(existing, dict):
        return "skipped-malformed"
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers
    status = "replaced" if key in servers else "added"
    servers[key] = entry
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return status


def _apply_toolkit(target: Path, manifest: dict) -> dict:
    """MCP servers with a command get merged into .mcp.json; skills become commands to run."""
    toolkit = manifest.get("toolkit") or {}
    actions = {"mcp_servers": [], "skill_commands": []}
    for srv in toolkit.get("mcp_servers") or []:
        sid = srv.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        if isinstance(srv.get("command"), str):
            entry = {"command": srv["command"], "args": list(srv.get("args") or []),
                     "transport": srv.get("transport") or "stdio"}
            if isinstance(srv.get("env"), dict):
                entry["env"] = srv["env"]
            actions["mcp_servers"].append({"id": sid, "status": _upsert_mcp_server(target, sid, entry)})
        else:
            actions["mcp_servers"].append({"id": sid, "status": "manual",
                                           "note": srv.get("description") or "no command given; configure by hand"})
    for sk in toolkit.get("claude_skills") or []:
        name = sk.get("name")
        gh = sk.get("github")
        if isinstance(gh, str) and gh:
            actions["skill_commands"].append(
                f"git clone https://github.com/{gh}.git ~/Code/{name or gh.split('/')[-1]}  # then follow its install")
        elif isinstance(name, str):
            actions["skill_commands"].append(f"# install skill '{name}' (no source given)")
    return actions


# ---------- search ----------

def cmd_search(args) -> dict:
    target = Path(args.target).resolve()
    query = args.query or ""
    warnings = []
    entries = []
    org_used = None
    if args.local:
        import compathy_query  # pylint: disable=import-outside-toplevel
        # pylint: disable=protected-access
        entries = compathy_query._local_persona_entries(target)
        for e in entries:
            e["trust"] = None
    else:
        org = _org_ref(args, target)
        if org is None:
            raise PersonaError("no org known; pass --org git+https://.../org-repo.git, set one with "
                               "`config set-org`, or use --local")
        reg = _fetch_org_registry(org, refresh=args.refresh)
        registry = reg["registry"]
        org_used = {"id": registry["org"], "source": org["source"], "path": org["path"],
                    "fetched": reg["fetch"]["action"]}
        for e in _read_personas_dir(reg["personas_dir"], registry["org"], org["source"]):
            e["trust"] = 4
            entries.append(e)
        for team in registry.get("teams", []):
            tid = f"{registry['org']}/{team['id']}"
            try:
                tp = _fetch_team_personas(team, refresh=args.refresh)
            except layers_mod.LayerFetchError as e:
                warnings.append(f"team {tid} unreachable ({team.get('source')}): {str(e)[:120]}")
                continue
            if tp["fetch"]["action"] == "stale":
                warnings.append(f"team {tid} unreachable; showing cached copy")
            for e in _read_personas_dir(tp["personas_dir"], tid, team["source"]):
                e["trust"] = 4
                entries.append(e)
    results = []
    for e in entries:
        score, why = lin.score_persona(query, e)
        if query.strip() and score <= 0:
            continue
        results.append({**e, "score": score, "why": why})
    results.sort(key=lambda r: (-r["score"], r["id"]))
    return {"query": query, "org": org_used, "local_only": bool(args.local),
            "count": len(results[:args.max]), "total": len(results),
            "results": results[:args.max], "warnings": warnings,
            "next": "import with: persona.py import <id> (registry) or the listed path"}


# ---------- sync / status / update / unlink ----------

def _require_lineage(target: Path) -> dict:
    doc = _existing_link(target)
    if not doc:
        raise PersonaError("this project is not linked (no context/lineage.json); import a persona first")
    return doc


def cmd_sync(args) -> dict:
    target = Path(args.target).resolve()
    doc = _require_lineage(target)
    out = {"persona": doc.get("persona"), "layers": [], "ok": True}
    for layer in doc["layers"]:
        try:
            r = layers_mod.fetch_layer(layer, force=args.force)
            out["layers"].append({"id": layer["id"], "pin": layer["pin"], "action": r["action"], "ok": True})
        except layers_mod.LayerFetchError as e:
            out["ok"] = False
            out["layers"].append({"id": layer["id"], "pin": layer["pin"], "action": "failed", "ok": False,
                                  "error": str(e)})
    if out["ok"]:
        discovery.write_discovery_breadcrumbs(target, target.name)
    return out


def cmd_status(args) -> dict:
    target = Path(args.target).resolve()
    doc = _require_lineage(target)
    layers = []
    for layer in doc["layers"]:
        st = _layer_status(layer)
        if args.check_upstream:
            head = layers_mod.upstream_head(layer["source"])
            st["upstream_head"] = head
            st["upstream"] = "unreachable" if head is None else ("current" if head == layer["pin"] else "differs")
        layers.append(st)
    persona = None
    if persona_path(target).is_file():
        try:
            persona = lin.read_json(persona_path(target))
        except lin.LineageError:
            persona = None
    return {"persona": doc.get("persona"), "self": doc.get("self"), "imported_at": doc.get("imported_at"),
            "imported_from": doc.get("imported_from"), "trust_at_import": doc.get("trust_at_import"),
            "title": (persona or {}).get("title"), "layers": layers,
            "all_cached": all(l["cached"] for l in layers), "all_verified": all(l["verified"] for l in layers)}


def cmd_update(args) -> dict:
    target = Path(args.target).resolve()
    doc = _require_lineage(target)
    changes = []
    new_layers = []
    for layer in doc["layers"]:
        if args.layer and layer["id"] != args.layer:
            new_layers.append(layer)
            continue
        if args.to and not args.layer and len(doc["layers"]) > 1:
            raise PersonaError("--to needs --layer when the lineage has more than one parent")
        new_pin = args.to or layers_mod.upstream_head(layer["source"])
        if new_pin is None:
            changes.append({"id": layer["id"], "status": "unreachable", "old": layer["pin"]})
            new_layers.append(layer)
            continue
        if not lin.SHA_RE.match(new_pin):
            raise PersonaError(f"--to must be a 40-hex commit sha (got {new_pin!r})")
        if new_pin == layer["pin"]:
            changes.append({"id": layer["id"], "status": "current", "old": layer["pin"]})
            new_layers.append(layer)
            continue
        candidate = {**_strip_layer(layer), "pin": new_pin, "tree_sha": None}
        try:
            layers_mod.fetch_layer(candidate)
        except layers_mod.LayerFetchError as e:
            changes.append({"id": layer["id"], "status": "fetch-failed", "old": layer["pin"], "new": new_pin,
                            "error": str(e)})
            new_layers.append(layer)
            continue
        root = lin.layer_cache_root(candidate)
        candidate["tree_sha"] = layers_mod.tree_sha(root, "HEAD", f"{candidate['path']}/{WIKI_SUBDIR}")
        if lin.is_layer_cached(layer):
            diff = _index_diff(layer, candidate) or "(index.md unchanged between pins; other pages may differ)"
        else:
            diff = "(old pin not cached; no index.md diff available)"
        ch = {"id": layer["id"], "status": "changed", "old": layer["pin"], "new": new_pin,
              "new_tree_sha": candidate["tree_sha"], "index_diff": diff}
        changes.append(ch)
        new_layers.append(candidate)
    applied = False
    if args.apply and any(c["status"] == "changed" for c in changes):
        doc["layers"] = new_layers
        doc["updated_at"] = _now_iso()
        lin.save_lineage(target, doc)
        discovery.write_discovery_breadcrumbs(target, target.name)
        _append_log(target, "update", "re-pinned parent layers",
                    [f"{c['id']}: {c['old'][:12]} -> {c['new'][:12]}" for c in changes if c["status"] == "changed"])
        applied = True
    return {"persona": doc.get("persona"), "changes": changes, "applied": applied,
            "next": None if applied else "review index_diff per layer, then run again with --apply"}


def _remove_persona_entity(target: Path, persona_id: Optional[str]) -> Optional[Path]:
    """Remove the entities/persona-<role>.md page import seeded (only if it
    still carries provenance: from-persona) plus its index line."""
    if not persona_id or "/" not in persona_id:
        return None
    role = persona_id.rsplit("/", 1)[1]
    slug = f"persona-{role}"
    page = wiki_dir(target) / "entities" / f"{slug}.md"
    if not page.is_file():
        return None
    try:
        fm, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if fm.get("provenance") != "from-persona":
        return None
    page.unlink()
    idx = wiki_dir(target) / INDEX_FILE
    if idx.is_file():
        lines = idx.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [l for l in lines if f"[[{slug}]]" not in l]
        idx.write_text("".join(kept), encoding="utf-8")
    return page


def cmd_unlink(args) -> dict:
    target = Path(args.target).resolve()
    doc = _require_lineage(target)
    removed = []
    for p in (lineage_path(target), persona_path(target)):
        if p.exists():
            p.unlink()
            removed.append(str(p))
    page = _remove_persona_entity(target, doc.get("persona"))
    if page is not None:
        removed.append(str(page))
    discovery.write_discovery_breadcrumbs(target, target.name)
    _append_log(target, "unlink", f"removed lineage (was {doc.get('persona')})",
                ["layer caches under ~/.compathy/layers were kept"]
                + ([f"removed seeded page {page.relative_to(wiki_dir(target))}"] if page else []))
    return {"removed": removed, "was": doc.get("persona"), "kept_cache": str(state_home() / "layers")}


# ---------- whoami / resolve ----------

def cmd_whoami(args) -> dict:
    target = Path(args.target).resolve()
    try:
        doc = lin.load_lineage(target)
        err = None
    except lin.LineageError as e:
        doc, err = None, str(e)
    persona = None
    if persona_path(target).is_file():
        try:
            persona = lin.read_json(persona_path(target))
        except lin.LineageError:
            persona = None
    local = sorted(p.stem for p in personas_home_dir().glob("*.json")) if personas_home_dir().is_dir() else []
    out = {"target": str(target), "linked": bool(doc), "lineage_error": err,
           "has_wiki": (wiki_dir(target) / INDEX_FILE).is_file(),
           "persona": None, "self": None, "layers": [], "needs_sync": False,
           "state_home": str(state_home()), "imported_personas": local}
    if doc:
        out["persona"] = {"id": doc.get("persona"), "title": (persona or {}).get("title"),
                          "summary": (persona or {}).get("summary"),
                          "reads_first": (persona or {}).get("reads_first") or [],
                          "responsibilities": (persona or {}).get("responsibilities") or [],
                          "policy": (persona or {}).get("policy") or {}}
        out["self"] = doc.get("self")
        out["layers"] = [_layer_status(l) for l in doc["layers"]]
        out["needs_sync"] = not all(l["cached"] and l["verified"] for l in out["layers"])
    return out


def cmd_resolve(args) -> dict:
    target = Path(args.target).resolve()
    try:
        layers = lin.resolve_layers(target)
    except lin.LineageError as e:
        raise PersonaError(f"context/lineage.json is invalid: {e}") from e
    found = lin.occurrences(args.slug, layers)
    hits = []
    for layer, path in found:
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fm = {}
        hits.append({"layer": layer["id"], "role": layer["role"], "self": bool(layer.get("self")),
                     "path": str(path), "authority": fm.get("authority"), "override": fm.get("override")})
    advice = "free to create"
    if hits:
        nearest = hits[0]
        if nearest["self"]:
            advice = "exists locally; edit it"
        elif nearest.get("authority"):
            pol = nearest.get("override") or "narrow"
            advice = {"forbidden": f"owned by {nearest['layer']} and may not be overridden; link to it",
                      "narrow": f"authoritative in {nearest['layer']}; link to it, or narrow it with `extends: {nearest['layer']}`",
                      "free": f"exists in {nearest['layer']}; link to it or override freely"}[pol]
        else:
            advice = f"exists in {nearest['layer']}; link to it rather than duplicating"
    return {"slug": args.slug, "found_in": hits, "advice": advice,
            "uncached_layers": [l["id"] for l in layers if not l.get("cached")]}


# ---------- registry / config ----------

def cmd_registry(args) -> dict:
    target = Path(args.target).resolve()
    rp = registry_path(target)
    if args.registry_cmd == "init":
        if rp.exists() and not args.force:
            raise PersonaError(f"{rp} exists; pass --force to overwrite")
        if not lin.LAYER_ID_RE.match(args.org or "") or "/" in args.org:
            raise PersonaError("--org must be a single segment id like 'acme'")
        data = {"schema_version": lin.REGISTRY_SCHEMA_VERSION, "org": args.org, "teams": []}
        lin.write_json_atomic(rp, data)
        _append_log(target, "registry", f"initialized org registry for {args.org}")
        return {"written": str(rp), "registry": data}
    if args.registry_cmd == "add-team":
        if not rp.is_file():
            raise PersonaError(f"{rp} missing; run `registry init --org <id>` first")
        data = lin.read_json(rp)
        if lin.validate_registry(data):
            raise PersonaError("existing registry.json is invalid: " + "; ".join(lin.validate_registry(data)))
        entry = {"id": args.id, "source": args.source, "path": args.path or "context"}
        teams = [t for t in data.get("teams", []) if t.get("id") != args.id]
        replaced = len(teams) != len(data.get("teams", []))
        teams.append(entry)
        data["teams"] = sorted(teams, key=lambda t: t["id"])
        probs = lin.validate_registry(data)
        if probs:
            raise PersonaError("; ".join(probs))
        lin.write_json_atomic(rp, data)
        _append_log(target, "registry", f"{'updated' if replaced else 'added'} team {args.id}",
                    [f"source: {args.source}"])
        return {"written": str(rp), "replaced": replaced, "registry": data}
    raise PersonaError("registry: use init or add-team")


def cmd_config(args) -> dict:
    cfg = _load_config()
    if args.config_cmd == "show":
        return {"path": str(config_path()), "config": cfg}
    if args.config_cmd == "set-org":
        if not lin.valid_source(args.source):
            raise PersonaError(f"source must start with one of {lin.SOURCE_SCHEMES}")
        cfg["default_org"] = {"source": args.source, "path": args.path or "context"}
        lin.write_json_atomic(config_path(), cfg)
        return {"path": str(config_path()), "config": cfg}
    raise PersonaError("config: use show or set-org")


# ---------- CLI ----------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="persona.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _tgt(p):
        p.add_argument("--target", default=".", help="project root (default: cwd)")

    def _org(p):
        p.add_argument("--org", default=None, help="org compathy source, e.g. git+https://github.com/acme/compathy-org.git")
        p.add_argument("--org-path", default=None, help="context dir inside the org repo (default: context)")
        p.add_argument("--refresh", action="store_true", help="bypass the 6h registry cache")

    ex = sub.add_parser("export", help="export a team persona from this layer")
    exs = ex.add_subparsers(dest="export_cmd", required=True)
    for name in ("propose", "write"):
        p = exs.add_parser(name)
        _tgt(p)
        p.add_argument("--role", required=True, help="kebab-case role slug, e.g. backend-engineer")
        p.add_argument("--layer-id", default=None, help="this layer's id (e.g. acme or acme/payments) if not in lineage.json")
        p.add_argument("--layer-role", default=None, choices=lin.LAYER_ROLES, help="this layer's role")
        p.add_argument("--source", default=None, help="override the git source for this layer")
        if name == "write":
            p.add_argument("--spec", default=None, help="JSON file: title, summary, tags, reads_first, responsibilities, toolkit, policy")
            p.add_argument("--title", default=None)
            p.add_argument("--summary", default=None)
            p.add_argument("--tags", default=None, help="comma-separated")
            p.add_argument("--exported-by", default=None)
            p.add_argument("--allow-dirty", action="store_true")
            p.add_argument("--allow-lint-errors", action="store_true")

    im = sub.add_parser("import", help="plan or apply an import (file | https url | org/team/role)")
    _tgt(im)
    _org(im)
    im.add_argument("persona")
    im.add_argument("--apply", action="store_true")
    im.add_argument("--consent", default="", help="comma list of fetch,link,toolkit")
    im.add_argument("--as", dest="as_role", default="project", choices=("project", "team"),
                    help="what this repo becomes in the lineage (default project)")
    im.add_argument("--self-id", default=None, help="this layer's id when --as team (e.g. acme/payments)")
    im.add_argument("--project-name", default=None)
    im.add_argument("--force", action="store_true", help="re-fetch cached layers / re-link same pins")

    se = sub.add_parser("search", help="find team personas via the org registry")
    _tgt(se)
    _org(se)
    se.add_argument("query", nargs="?", default="")
    se.add_argument("--local", action="store_true", help="no network: only local personas")
    se.add_argument("--max", type=int, default=10)

    sy = sub.add_parser("sync", help="fetch missing parent layers from lineage.json")
    _tgt(sy)
    sy.add_argument("--force", action="store_true")

    st = sub.add_parser("status", help="pins, cache and verification per layer")
    _tgt(st)
    st.add_argument("--check-upstream", action="store_true", help="git ls-remote each layer (network)")

    up = sub.add_parser("update", help="re-pin parent layers with an index.md diff")
    _tgt(up)
    up.add_argument("--layer", default=None)
    up.add_argument("--to", default=None, help="explicit commit sha (with --layer)")
    up.add_argument("--apply", action="store_true")

    un = sub.add_parser("unlink", help="remove lineage.json and persona.json")
    _tgt(un)

    wh = sub.add_parser("whoami", help="active persona + lineage for this project")
    _tgt(wh)

    rs = sub.add_parser("resolve", help="which layers have a slug")
    _tgt(rs)
    rs.add_argument("slug")

    rg = sub.add_parser("registry", help="org registry helpers")
    rgs = rg.add_subparsers(dest="registry_cmd", required=True)
    ri = rgs.add_parser("init")
    _tgt(ri)
    ri.add_argument("--org", required=True)
    ri.add_argument("--force", action="store_true")
    ra = rgs.add_parser("add-team")
    _tgt(ra)
    ra.add_argument("--id", required=True)
    ra.add_argument("--source", required=True)
    ra.add_argument("--path", default=None)

    cf = sub.add_parser("config", help="~/.compathy/config.json")
    cfs = cf.add_subparsers(dest="config_cmd", required=True)
    cfs.add_parser("show")
    so = cfs.add_parser("set-org")
    so.add_argument("source")
    so.add_argument("--path", default=None)
    return ap


def main(argv=None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "export":
            out = cmd_export_propose(args) if args.export_cmd == "propose" else cmd_export_write(args)
        elif args.cmd == "import":
            out = cmd_import(args)
        elif args.cmd == "search":
            out = cmd_search(args)
        elif args.cmd == "sync":
            out = cmd_sync(args)
        elif args.cmd == "status":
            out = cmd_status(args)
        elif args.cmd == "update":
            out = cmd_update(args)
        elif args.cmd == "unlink":
            out = cmd_unlink(args)
        elif args.cmd == "whoami":
            out = cmd_whoami(args)
        elif args.cmd == "resolve":
            out = cmd_resolve(args)
        elif args.cmd == "registry":
            out = cmd_registry(args)
        elif args.cmd == "config":
            out = cmd_config(args)
        else:  # pragma: no cover
            raise PersonaError(f"unknown command {args.cmd}")
    except (PersonaError, lin.LineageError) as e:
        _emit({"error": str(e), "command": args.cmd})
        return 1
    _emit(out)
    if args.cmd == "sync" and not out.get("ok", True):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
