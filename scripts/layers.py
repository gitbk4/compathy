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

Public API:
    source_to_git_url(source) / git_url_to_source(url)
    fetch_layer(layer, force=False) -> dict
    verify_layer(layer) -> dict
    fetch_paths(source, ref, paths, dest, ttl_seconds=None, refresh=False) -> dict
    upstream_head(source, ref="HEAD") -> sha | None
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
    layer_cache_root,
    valid_source,
    write_json_atomic,
)
from paths import WIKI_SUBDIR  # noqa: E402

GIT_TIMEOUT = 180
META_FILE = ".compathy-fetch.json"


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
    """'git+https://x' -> 'https://x'. Rejects unknown schemes."""
    if not valid_source(source):
        raise LayerFetchError(f"unsupported layer source {source!r} (allowed: git+https, git+ssh, git+file)")
    return source[len("git+"):]


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


def fetch_layer(layer: dict, force: bool = False) -> dict:
    """Ensure ``layer`` is cached at its pin and verified.

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

    url = source_to_git_url(layer["source"])
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
        write_json_atomic(dest / META_FILE, {
            "id": layer["id"], "pin": layer["pin"], "source": layer["source"],
            "path": layer.get("path") or "context", "fetched_at": int(time.time()),
        })
        make_readonly(dest)
        remove_tree(root)
        os.replace(dest, root)
    except Exception:
        remove_tree(tmp)
        raise
    remove_tree(tmp)
    return {"action": "fetched", "root": str(root), "verify": v}


# ---------- small sparse fetches (registries, persona indexes) ----------

def upstream_head(source: str, ref: str = "HEAD") -> Optional[str]:
    """Return the sha ``ref`` points at on the remote, or None if unreachable."""
    try:
        url = source_to_git_url(source)
        r = git(["ls-remote", url, ref], timeout=60)
    except LayerFetchError:
        return None
    if not _ok(r):
        return None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and SHA_RE.match(parts[0]):
            return parts[0]
    return None


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

    url = source_to_git_url(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=str(dest.parent)))
    clone = tmp / "clone"
    try:
        try:
            _clone_no_checkout(url, clone, depth=1)
        except LayerFetchError:
            if (dest / ".git").exists():
                # Unreachable but we have a previous fetch: serve stale, flag it.
                return {"action": "stale", "root": str(dest), "sha": None, "fetched_at": None}
            raise
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
