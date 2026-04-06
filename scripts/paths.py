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
