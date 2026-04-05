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
    return Path(target) / CONTEXT_DIR


def raw_dir(target) -> Path:
    return context_root(target) / RAW_SUBDIR


def wiki_dir(target) -> Path:
    return context_root(target) / WIKI_SUBDIR


def schema_path(target) -> Path:
    return context_root(target) / SCHEMA_FILE


def index_path(target) -> Path:
    return wiki_dir(target) / INDEX_FILE


def log_path(target) -> Path:
    return wiki_dir(target) / LOG_FILE


def state_path(target) -> Path:
    return wiki_dir(target) / STATE_FILE
