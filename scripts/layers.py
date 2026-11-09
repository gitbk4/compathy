#!/usr/bin/env python3
"""Git plumbing for federated layers: fetch a layer at a pin, verify it,
cache it read-only, and sparse-fetch small registry files.

Everything here shells out to ``git`` (stdlib only, no GitPython). All
network calls run with ``GIT_TERMINAL_PROMPT=0`` so a missing credential
fails fast instead of hanging a skill run.

Strategy for a pinned layer (``fetch_layer``):

    git clone --no-checkout --filter=blob:none <url> <tmp>   # partial clone
      (falls back to a plain --no-checkout clone if the server refuses filters)
    git sparse-checkout set --no-cone <path>                 # only the layer dir
    git checkout <pin>
    verify: HEAD == pin, and rev-parse HEAD:<path>/wiki == tree_sha
    chmod -R a-w  (best effort; convention, not security)
    rename <tmp> -> ~/.compathy/layers/<slug>/<pin>/

Fetching by pin means a moved branch or tag can't change what an importer
reads; the tree sha covers every page under wiki/, not just index.md.

A layer's bytes may come from a *mirror*: a per-user alternate location of
the same repository, such as a git bundle in a synced shared drive. Mirrors
live in ~/.compathy/config.json (``mirrors``: canonical source -> mirror
source). The pin and tree sha are still verified, so a mirror can change
where the bytes come from but never what a pinned layer contains.

Public API:
    source_to_git_url(source) / git_url_to_source(url)
    load_mirrors() / effective_source(source)
    fetch_layer(layer, force=False, via=None) -> dict
    verify_layer(layer) -> dict
    fetch_paths(source, ref, paths, dest, ttl_seconds=None, refresh=False) -> dict
    upstream_head(source, ref="HEAD") -> sha | None
    probe_upstream(source, path="context") -> {head, tree_sha, date, last_publish}
    repo_toplevel(path), head_sha(repo), tree_sha(repo, rev, relpath),
    is_clean(repo, relpath), remote_source(repo)
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from lineage import (  # noqa: E402
    SHA_RE,
    is_online_source,
    layer_cache_root,
    valid_source,
    write_json_atomic,
)
from paths import LOG_FILE, WIKI_SUBDIR, config_path  # noqa: E402

GIT_TIMEOUT = 180
META_FILE = ".compathy-fetch.json"
PROBE_TIMEOUT = 120


class LayerFetchError(Exception):
    """Raised when a layer cannot be fetched or fails verification."""


# ---------- git wrapper ----------

def git(args: list, cwd: Optional[Path] = None, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run git with prompts disabled. Never raises on non-zero exit."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as e:
        raise LayerFetchError("git is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise LayerFetchError(f"git {' '.join(args[:2])} timed out after {timeout}s") from e


def _ok(r: subprocess.CompletedProcess) -> bool:
    return r.returncode == 0


# ---------- source <-> url ----------

def source_to_git_url(source: str) -> str:
    """'git+https://x' -> 'https://x'. Rejects unknown schemes.

    A ``git+file://`` source that names a bundle file comes back as a plain
    path: git reads bundles from paths, not from file:// URLs.
    """
    if not valid_source(source):
        raise LayerFetchError(f"unsupported layer source {source!r} (allowed: git+https, git+ssh, git+file)")
    url = source[len("git+"):]
    if url.startswith("file://"):
        path = url[len("file://"):]
        if path.endswith(".bundle") or Path(path).is_file():
            return path
    return url


def _is_bundle(url: str) -> bool:
    """True when ``url`` (from source_to_git_url) is a local bundle file."""
    return "://" not in url and (url.endswith(".bundle") or Path(url).is_file())


# ---------- mirrors ----------

def load_mirrors() -> dict:
    """{canonical source: mirror source} from ~/.compathy/config.json."""
    cp = config_path()
    try:
        data = json.loads(cp.read_text(encoding="utf-8")) if cp.is_file() else {}
    except (OSError, ValueError):
        return {}
    mirrors = data.get("mirrors") if isinstance(data, dict) else None
    if not isinstance(mirrors, dict):
        return {}
    return {k: v for k, v in mirrors.items() if valid_source(k) and valid_source(v)}


def candidate_sources(source: str) -> list:
    """Where to read ``source`` from, in order of preference.

    An online source of truth comes first and the user's mirror (a bundle)
    is the fallback, so anyone who can reach the repository never reads a
    stale copy. A local canonical source (tests, air-gapped setups) is most
    likely a path on someone else's machine, so its mirror comes first.
    """
    mirror = load_mirrors().get(source)
    if not mirror or mirror == source:
        return [source]
    return [source, mirror] if is_online_source(source) else [mirror, source]


def effective_source(source: str) -> str:
    """The first source to try for ``source`` (see candidate_sources)."""
    return candidate_sources(source)[0]


def git_url_to_source(url: str) -> Optional[str]:
    """Normalize a git remote URL into a layer ``source`` string.

    Handles https://, ssh://, file:// and scp-like ``git@host:owner/repo.git``.
    Returns None for anything else (the caller asks the user for --source).
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("https://"):
        return "git+" + url
    if url.startswith("ssh://"):
        return "git+" + url
    if url.startswith("file://"):
        return "git+" + url
    if url.startswith("/"):
        return "git+file://" + url
    if "@" in url and ":" in url and "://" not in url:
        user_host, _, path = url.partition(":")
        return f"git+ssh://{user_host}/{path}"
    return None


# ---------- local repo inspection ----------

def repo_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path`` or None."""
    try:
        r = git(["rev-parse", "--show-toplevel"], cwd=Path(path))
    except LayerFetchError:
        return None
    if not _ok(r):
        return None
    return Path(r.stdout.strip())


def head_sha(repo: Path) -> Optional[str]:
    """Return the HEAD commit sha of ``repo``."""
    r = git(["rev-parse", "HEAD"], cwd=repo)
    sha = r.stdout.strip()
    return sha if _ok(r) and SHA_RE.match(sha) else None


def tree_sha(repo: Path, rev: str, relpath: str) -> Optional[str]:
    """Return the tree object id of ``relpath`` at ``rev`` (None if absent)."""
    rel = relpath.strip("/")
    spec = f"{rev}:{rel}" if rel else f"{rev}:"
    r = git(["rev-parse", "--verify", "--quiet", spec], cwd=repo)
    sha = r.stdout.strip()
    return sha if _ok(r) and SHA_RE.match(sha) else None


def is_clean(repo: Path, relpath: str = ".") -> bool:
    """True when ``git status --porcelain -- relpath`` is empty."""
    r = git(["status", "--porcelain", "--untracked-files=all", "--", relpath], cwd=repo)
    return _ok(r) and r.stdout.strip() == ""


def remote_source(repo: Path, remote: str = "origin") -> Optional[str]:
    """Return the normalized layer source for ``remote`` or None."""
    r = git(["remote", "get-url", remote], cwd=repo)
    if not _ok(r):
        return None
    return git_url_to_source(r.stdout.strip())


def relpath_in_repo(repo: Path, path: Path) -> str:
    """POSIX-style path of ``path`` relative to ``repo`` ('.' for the root)."""
    rel = Path(path).resolve().relative_to(Path(repo).resolve())
    return rel.as_posix() if str(rel) != "." else "."


# ---------- read-only cache helpers ----------

def _chmod_tree(root: Path, writable: bool) -> None:
    """Best-effort recursive chmod. Never raises."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            try:
                mode = p.stat().st_mode
                if writable:
                    p.chmod(mode | stat.S_IWUSR)
                else:
                    p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass
    try:
        mode = root.stat().st_mode
        root.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def make_readonly(root: Path) -> None:
    """Mark a cached layer read-only (convention: the agent must not edit parents)."""
    # Keep .git writable so git itself keeps working (gc, index).
    for child in Path(root).iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            _chmod_tree(child, writable=False)
        else:
            try:
                child.chmod(child.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass


def make_writable(root: Path) -> None:
    """Undo make_readonly (used before deleting a cache entry)."""
    _chmod_tree(Path(root), writable=True)


def remove_tree(root: Path) -> None:
    """rmtree that copes with read-only files."""
    root = Path(root)
    if not root.exists():
        return
    make_writable(root)
    shutil.rmtree(root, ignore_errors=True)


# ---------- clone at pin ----------

def _clone_no_checkout(url: str, dest: Path, depth: Optional[int] = None) -> None:
    """Partial clone (blob:none) with a plain-clone fallback."""
    base = ["clone", "--quiet", "--no-checkout", "--no-recurse-submodules"]
    if _is_bundle(url):
        # A bundle is a local file: no filters or depth, just clone it.
        r = git([*base, url, str(dest)])
        if not _ok(r):
            raise LayerFetchError(f"git clone failed for bundle {url}: {r.stderr.strip()[:400]}")
        return
    if depth:
        base += ["--depth", str(depth)]
    r = git([*base, "--filter=blob:none", url, str(dest)])
    if _ok(r):
        return
    remove_tree(dest)
    r2 = git([*base, url, str(dest)])
    if not _ok(r2):
        raise LayerFetchError(
            f"git clone failed for {url}: {(r2.stderr or r.stderr).strip()[:400]}"
        )


def _sparse(dest: Path, paths: list) -> None:
    if not paths or paths == ["."]:
        return
    r = git(["sparse-checkout", "set", "--no-cone", *paths], cwd=dest)
    if not _ok(r):
        # Older git: fall back to a full checkout rather than failing.
        git(["sparse-checkout", "disable"], cwd=dest)


def _checkout(dest: Path, rev: str) -> None:
    r = git(["checkout", "--quiet", "--detach", rev], cwd=dest)
    if _ok(r):
        return
    # Pin may be older than a shallow/default fetch reached; fetch it explicitly.
    f = git(["fetch", "--quiet", "origin", rev], cwd=dest)
    if _ok(f):
        r = git(["checkout", "--quiet", "--detach", rev], cwd=dest)
        if _ok(r):
            return
    raise LayerFetchError(f"cannot check out {rev[:12]}: {r.stderr.strip()[:300]}")


def verify_layer(layer: dict, root: Optional[Path] = None) -> dict:
    """Check that the cached clone is at the pin and its wiki tree matches.

    Returns {ok, head, tree_sha, expected_tree, reasons[]}. Missing cache is
    reported as not ok with reason 'not-cached'.
    """
    root = Path(root) if root is not None else layer_cache_root(layer)
    result = {"ok": False, "head": None, "tree_sha": None,
              "expected_tree": layer.get("tree_sha"), "reasons": []}
    if not (root / ".git").exists():
        result["reasons"].append("not-cached")
        return result
    head = head_sha(root)
    result["head"] = head
    if head != layer.get("pin"):
        result["reasons"].append(f"head {str(head)[:12]} != pin {str(layer.get('pin'))[:12]}")
    rel = (layer.get("path") or "context").strip("/")
    actual_tree = tree_sha(root, "HEAD", f"{rel}/{WIKI_SUBDIR}")
    result["tree_sha"] = actual_tree
    if actual_tree is None:
        result["reasons"].append(f"no {rel}/{WIKI_SUBDIR} at pin")
    elif layer.get("tree_sha") and actual_tree != layer["tree_sha"]:
        result["reasons"].append("wiki tree sha mismatch (content differs from what the persona pinned)")
    if not (root / rel / WIKI_SUBDIR).is_dir():
        result["reasons"].append("wiki dir not checked out")
    result["ok"] = not result["reasons"]
    return result


def fetch_layer(layer: dict, force: bool = False, via: Optional[str] = None) -> dict:
    """Ensure ``layer`` is cached at its pin and verified.

    ``via`` reads the bytes from one given source (a mirror such as a
    bundle); otherwise ``candidate_sources`` decides, online source first.
    Returns {action: 'cached'|'fetched', root, verify}. Raises LayerFetchError
    on any failure; on failure nothing is left behind in the cache dir
    (the clone happens in a sibling temp dir and is renamed only after
    verification passes).
    """
    root = layer_cache_root(layer)
    if root.exists() and not force:
        v = verify_layer(layer, root)
        if v["ok"]:
            return {"action": "cached", "root": str(root), "verify": v}
        # Corrupt or partial cache entry: rebuild below (replaced only after
        # the fresh clone verifies, so a failed refetch never loses a good one).

    errors = []
    for src in ([via] if via else candidate_sources(layer["source"])):
        try:
            v = _fetch_from(layer, src, root)
            return {"action": "fetched", "root": str(root), "verify": v, "from": src}
        except LayerFetchError as e:
            errors.append((src, str(e)))
    if len(errors) == 1:
        raise LayerFetchError(errors[0][1])
    raise LayerFetchError("; ".join(f"from {s}: {msg}" for s, msg in errors))


def _fetch_from(layer: dict, src: str, root: Path) -> dict:
    """Clone ``layer`` at its pin from ``src``, verify it, and move it into ``root``."""
    url = source_to_git_url(src)
    root.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=str(root.parent)))
    dest = tmp / "clone"
    try:
        _clone_no_checkout(url, dest)
        _sparse(dest, [(layer.get("path") or "context").strip("/")])
        _checkout(dest, layer["pin"])
        v = verify_layer(layer, dest)
        if not v["ok"]:
            raise LayerFetchError(
                f"layer {layer['id']} failed verification: {'; '.join(v['reasons'])}"
            )
        meta = {"id": layer["id"], "pin": layer["pin"], "source": layer["source"],
                "path": layer.get("path") or "context", "fetched_at": int(time.time())}
        if src != layer["source"]:
            meta["fetched_from"] = src
        write_json_atomic(dest / META_FILE, meta)
        make_readonly(dest)
        remove_tree(root)
        os.replace(dest, root)
    except Exception:
        remove_tree(tmp)
        raise
    remove_tree(tmp)
    return v


# ---------- small sparse fetches (registries, persona indexes) ----------

def upstream_head(source: str, ref: str = "HEAD", timeout: int = 60) -> Optional[str]:
    """Return the sha ``ref`` points at upstream, or None if unreachable.

    Tries the online source first, then the user's mirror (candidate_sources).
    """
    for src in candidate_sources(source):
        try:
            r = git(["ls-remote", source_to_git_url(src), ref], timeout=timeout)
        except LayerFetchError:
            continue
        if not _ok(r):
            continue
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and SHA_RE.match(parts[0]):
                return parts[0]
    return None


def probe_upstream(source: str, path: str = "context", timeout: int = PROBE_TIMEOUT) -> dict:
    """Read the upstream HEAD's wiki tree without touching any cache.

    Fetches HEAD (depth 1; whole bundle for a bundle) into a scratch bare
    repo and returns {head, tree_sha, date, last_publish}: the wiki tree
    lets callers tell a real content change from a commit that only touched
    personas or the README. Tries the online source first, then the mirror.
    Raises LayerFetchError when neither is reachable.
    """
    last_error = None
    for src in candidate_sources(source):
        try:
            return _probe(source_to_git_url(src), path, timeout)
        except LayerFetchError as e:
            last_error = e
    raise last_error or LayerFetchError(f"cannot read {source}")


def _probe(url: str, path: str, timeout: int) -> dict:
    rel = (path or "context").strip("/")
    tmp = Path(tempfile.mkdtemp(prefix="compathy-probe-"))
    try:
        r = git(["init", "-q", "--bare", str(tmp)])
        if not _ok(r):
            raise LayerFetchError(f"cannot create a scratch repo: {r.stderr.strip()[:200]}")
        cmd = ["fetch", "--quiet", "--no-tags"]
        if not _is_bundle(url):
            cmd += ["--depth", "1"]
        r = git([*cmd, url, "HEAD"], cwd=tmp, timeout=timeout)
        if not _ok(r):
            raise LayerFetchError(f"cannot read {url}: {r.stderr.strip()[:300]}")
        head = git(["rev-parse", "FETCH_HEAD"], cwd=tmp).stdout.strip()
        date = git(["log", "-1", "--format=%cs", "FETCH_HEAD"], cwd=tmp).stdout.strip() or None
        last = None
        log = git(["show", f"FETCH_HEAD:{rel}/{WIKI_SUBDIR}/{LOG_FILE}"], cwd=tmp)
        if _ok(log):
            for line in log.stdout.splitlines():
                if line.startswith("## [") and "] publish |" in line:
                    last = line[3:].strip()
        return {"head": head if SHA_RE.match(head) else None,
                "tree_sha": tree_sha(tmp, "FETCH_HEAD", f"{rel}/{WIKI_SUBDIR}"),
                "date": date, "last_publish": last}
    finally:
        remove_tree(tmp)


def fetch_paths(source: str, ref: str, paths: list, dest: Path,
                ttl_seconds: Optional[int] = None, refresh: bool = False) -> dict:
    """Shallow, sparse fetch of ``paths`` at ``ref`` into ``dest``.

    Used for registry.json and personas/ directories - a few small files at
    the remote's HEAD. Cached: when ``ttl_seconds`` is given and the last
    fetch is younger than that, the cache is served after a single
    ``ls-remote`` revalidation. Returns {action: 'cached'|'fetched'|'stale',
    root, sha, fetched_at}; 'stale' means the remote was unreachable and the
    previous fetch was served. Raises LayerFetchError when unreachable and
    nothing is cached.
    """
    dest = Path(dest)
    meta_path = dest / META_FILE
    now = int(time.time())
    if not refresh and meta_path.is_file() and (dest / ".git").exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        age = now - int(meta.get("fetched_at", 0))
        if ttl_seconds is not None and age < ttl_seconds:
            cached = {"action": "cached", "root": str(dest), "sha": meta.get("sha"),
                      "fetched_at": meta.get("fetched_at")}
            # Cheap revalidation: one ls-remote. Same sha -> serve the cache;
            # moved -> refetch (so a newly registered team shows up at once);
            # unreachable -> serve the cache and say so.
            remote = upstream_head(source, ref)
            if remote is None:
                return dict(cached, action="stale")
            if remote == meta.get("sha"):
                return cached

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=str(dest.parent)))
    clone = tmp / "clone"
    try:
        last_error = None
        for src in candidate_sources(source):
            try:
                _clone_no_checkout(source_to_git_url(src), clone, depth=1)
                last_error = None
                break
            except LayerFetchError as e:
                last_error = e
                remove_tree(clone)
        if last_error is not None:
            if (dest / ".git").exists():
                # Unreachable but we have a previous fetch: serve stale, flag it.
                return {"action": "stale", "root": str(dest), "sha": None, "fetched_at": None}
            raise last_error
        _sparse(clone, [p.strip("/") for p in paths])
        _checkout(clone, ref)
        sha = head_sha(clone)
        write_json_atomic(clone / META_FILE, {"source": source, "ref": ref, "sha": sha,
                                              "paths": paths, "fetched_at": now})
        remove_tree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(clone, dest)
    finally:
        remove_tree(tmp)
    return {"action": "fetched", "root": str(dest), "sha": sha, "fetched_at": now}
