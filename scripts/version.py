#!/usr/bin/env python3
"""Read and print the compathy version from the VERSION file."""
from __future__ import annotations

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_version() -> str:
    """Return the current compathy version string from the VERSION file."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


if __name__ == "__main__":
    print(get_version())
