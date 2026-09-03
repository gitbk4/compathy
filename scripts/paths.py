"""Shared path constants and helpers for compathy scripts.

All scripts import from here to keep directory layout DRY.
"""
from pathlib import Path

CONTEXT_DIR = "context"
RAW_SUBDIR = "raw"
WIKI_SUBDIR = "wiki"
SCHEMA_FILE = "schema.md"
INDEX_FILE = "index.md"
LOG_FILE = "log.md"
STATE_FILE = ".compile-state.json"
WIKI_SUBDIRS = ("concepts", "entities", "summaries", "patterns")

SCHEMA_VERSION = 1


def context_root(target) -> Path:
    """Return the context directory path for the given target."""
    return Path(target) / CONTEXT_DIR


def raw_dir(target) -> Path:
    """Return the raw subdirectory path for the given target."""
    return context_root(target) / RAW_SUBDIR


def wiki_dir(target) -> Path:
    """Return the wiki subdirectory path for the given target."""
    return context_root(target) / WIKI_SUBDIR


def schema_path(target) -> Path:
    """Return the schema file path for the given target."""
    return context_root(target) / SCHEMA_FILE


def index_path(target) -> Path:
    """Return the index file path for the given target."""
    return wiki_dir(target) / INDEX_FILE


def log_path(target) -> Path:
    """Return the log file path for the given target."""
    return wiki_dir(target) / LOG_FILE


def state_path(target) -> Path:
    """Return the state file path for the given target."""
    return wiki_dir(target) / STATE_FILE


# ---------- federation (layers, lineage, personas) ----------

LINEAGE_FILE = "lineage.json"          # context/lineage.json — parent layers + pins
PERSONA_FILE = "persona.json"          # context/persona.json — imported manifest, verbatim
PERSONAS_SUBDIR = "personas"           # context/personas/<role>.json (exported by a layer)
PERSONAS_INDEX_FILE = "index.json"     # context/personas/index.json (generated)
REGISTRY_FILE = "registry.json"        # context/registry.json (org lists its teams)

STATE_HOME_ENV = "COMPATHY_STATE_HOME"  # override ~/.compathy (tests). NOTE: not
# COMPATHY_HOME — ai-quickstart already uses that name for the skill install root.
STATE_HOME_DIRNAME = ".compathy"
LAYERS_CACHE_SUBDIR = "layers"
PERSONAS_HOME_SUBDIR = "personas"
REGISTRY_CACHE_SUBDIR = "cache/registry"
CONFIG_FILE = "config.json"
IMPORT_LOG_FILE = "import-log.jsonl"

MAX_LINEAGE_DEPTH = 3  # project + team + org. Deeper trees are a v2 question.


def lineage_path(target) -> Path:
    """Return context/lineage.json for the target project."""
    return context_root(target) / LINEAGE_FILE


def persona_path(target) -> Path:
    """Return context/persona.json (the imported manifest) for the target."""
    return context_root(target) / PERSONA_FILE


def personas_dir(target) -> Path:
    """Return context/personas/ (personas this layer exports)."""
    return context_root(target) / PERSONAS_SUBDIR


def personas_index_path(target) -> Path:
    """Return context/personas/index.json."""
    return personas_dir(target) / PERSONAS_INDEX_FILE


def registry_path(target) -> Path:
    """Return context/registry.json (org-level team registry)."""
    return context_root(target) / REGISTRY_FILE


def state_home() -> Path:
    """Return the per-user compathy state dir (~/.compathy, overridable)."""
    import os  # pylint: disable=import-outside-toplevel
    override = os.environ.get(STATE_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / STATE_HOME_DIRNAME


def layers_cache_dir() -> Path:
    """Return ~/.compathy/layers/ (read-only clones of parent layers at pins)."""
    return state_home() / LAYERS_CACHE_SUBDIR


def personas_home_dir() -> Path:
    """Return ~/.compathy/personas/ (imported manifests, verbatim)."""
    return state_home() / PERSONAS_HOME_SUBDIR


def registry_cache_dir() -> Path:
    """Return ~/.compathy/cache/registry/ (sparse fetches of registries)."""
    return state_home() / REGISTRY_CACHE_SUBDIR


def config_path() -> Path:
    """Return ~/.compathy/config.json."""
    return state_home() / CONFIG_FILE


def import_log_path() -> Path:
    """Return ~/.compathy/import-log.jsonl."""
    return state_home() / IMPORT_LOG_FILE


def layer_slug(layer_id: str) -> str:
    """Filesystem-safe form of a layer id: 'acme/payments' -> 'acme--payments'."""
    out = []
    for ch in str(layer_id):
        if ch == "/":
            out.append("--")
        elif ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "layer"
