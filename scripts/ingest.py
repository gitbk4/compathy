#!/usr/bin/env python3
"""Detect changes in context/raw/ and manage compile state.

Normalizes line endings (LF) before hashing to keep state.json portable across
platforms. Resolves .ref files (single-line pointers to files inside the repo).
Writes state atomically via tmp+rename.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from paths import SCHEMA_VERSION, raw_dir, state_path  # noqa: E402

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
SKIP_NAMES = {".gitkeep", ".gitignore", "README.md", ".DS_Store"}


def normalize_line_endings(data: bytes) -> bytes:
    """Normalize CRLF or CR line endings to LF."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_checksum(path: Path) -> str:
    """Calculate the normalized SHA-256 checksum for a given file."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise RuntimeError(f"cannot stat {path}: {e}") from e
    if size > MAX_FILE_BYTES:
        raise RuntimeError(f"file exceeds {MAX_FILE_BYTES} bytes: {path}")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e}") from e
    return _sha256(normalize_line_endings(data))


def repo_root(target: Path) -> Path:
    """Find the repo root for the target (git toplevel or target itself)."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return Path(target).resolve()


def resolve_ref_file(ref_path: Path, target_root: Path):
    """Read .ref file, validate path is inside target_root, return (resolved, bytes)."""
    try:
        content = ref_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(f".ref file unreadable: {ref_path}: {e}") from e
    if not content:
        raise RuntimeError(f".ref file empty: {ref_path}")
    # Take first line only (ignore comments / extra lines)
    first = content.splitlines()[0].strip()
    if not first or first.startswith("#"):
        raise RuntimeError(f".ref file has no target path: {ref_path}")
    if ".." in first.replace("\\", "/").split("/"):
        raise RuntimeError(f".ref path contains '..': {ref_path} -> {first}")
    if Path(first).is_absolute():
        raise RuntimeError(f".ref path must be relative: {ref_path} -> {first}")

    target_root = target_root.resolve()
    resolved = (target_root / first).resolve()
    try:
        resolved.relative_to(target_root)
    except ValueError as exc:
        raise RuntimeError(
            f".ref target outside project root: {ref_path} -> {first}"
        ) from exc
    if not resolved.exists():
        raise RuntimeError(f".ref target does not exist: {first}")
    if not resolved.is_file():
        raise RuntimeError(f".ref target is not a file: {first}")
    try:
        data = resolved.read_bytes()
    except OSError as e:
        raise RuntimeError(f"cannot read .ref target: {resolved}: {e}") from e
    return resolved, data


def walk_raw_files(raw_root: Path):
    """Yield (relative_path, kind) for each raw entry. kind is 'file' or 'ref'."""
    if not raw_root.exists():
        return
    for p in sorted(raw_root.rglob("*")):
        if not p.is_file():
            continue
        if p.name in SKIP_NAMES or p.name.startswith("."):
            continue
        rel = p.relative_to(raw_root)
        kind = "ref" if p.suffix == ".ref" else "file"
        yield rel, kind


def compute_entry_checksum(
    raw_root: Path, rel: Path, kind: str, target_root: Path
) -> str:
    """Compute the checksum for a given raw/ entry (.ref or raw file)."""
    full = raw_root / rel
    if kind == "ref":
        _, data = resolve_ref_file(full, target_root)
        return _sha256(normalize_line_endings(data))
    return compute_checksum(full)


def load_state(state_file: Path) -> dict:
    """Load the compilation state JSON file."""
    if not state_file.exists():
        return {"version": SCHEMA_VERSION, "entries": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": SCHEMA_VERSION, "entries": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": SCHEMA_VERSION, "entries": {}}


def save_state(state_file: Path, state: dict) -> None:
    """Safely save state atomically using a temporary file."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(state_file.parent), prefix=".state-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, state_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def detect_changes(target: Path) -> dict:
    """Identify added, modified, or deleted raw files compared to stored state."""
    target = Path(target).resolve()
    raw_root = raw_dir(target)
    if not raw_root.exists():
        return {
            "error": f"raw/ not found: {raw_root}",
            "added": [],
            "modified": [],
            "deleted": [],
            "current": {},
            "errors": [],
        }
    target_root = repo_root(target)
    state = load_state(state_path(target))
    old = state.get("entries", {})
    current = {}
    errors = []
    for rel, kind in walk_raw_files(raw_root):
        key = str(rel).replace(os.sep, "/")
        try:
            checksum = compute_entry_checksum(raw_root, rel, kind, target_root)
            current[key] = {"kind": kind, "sha256": checksum}
        except RuntimeError as e:
            errors.append({"path": key, "error": str(e)})
    added = sorted(k for k in current if k not in old)
    deleted = sorted(k for k in old if k not in current)
    modified = sorted(
        k
        for k, v in current.items()
        if k in old and v["sha256"] != old[k].get("sha256")
    )
    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "current": current,
        "errors": errors,
    }


def commit_state(target: Path, current: dict | None = None) -> None:
    """Save the current checksums into state file."""
    target = Path(target).resolve()
    if current is None:
        result = detect_changes(target)
        if "error" in result and result.get("error"):
            raise RuntimeError(result["error"])
        current = result["current"]
    state = {"version": SCHEMA_VERSION, "entries": current}
    save_state(state_path(target), state)


def main() -> int:
    """Main entry point for ingest script."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=".")
    ap.add_argument("--detect-changes", action="store_true")
    ap.add_argument("--commit-state", action="store_true")
    args = ap.parse_args()
    target = Path(args.target).resolve()

    if args.detect_changes:
        result = detect_changes(target)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.commit_state:
        try:
            commit_state(target)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print("state committed")
        return 0

    ap.error("must pass --detect-changes or --commit-state")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
