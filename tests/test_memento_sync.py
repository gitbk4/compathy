"""Tests for memento_sync.py — Memento-Skills version tracking."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import memento_sync  # noqa: E402


class TestReadWriteTracked(unittest.TestCase):
    def test_read_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            vf = Path(td) / "MEMENTO_VERSION"
            with patch.object(memento_sync, "MEMENTO_VERSION_FILE", vf):
                self.assertEqual(memento_sync._read_tracked(), "")

    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as td:
            vf = Path(td) / "MEMENTO_VERSION"
            with patch.object(memento_sync, "MEMENTO_VERSION_FILE", vf):
                memento_sync._write_tracked("v0.2.0")
                self.assertEqual(memento_sync._read_tracked(), "v0.2.0")


class TestCheckLogic(unittest.TestCase):
    def _patched_check(self, latest_tag, installed, tracked=""):
        with tempfile.TemporaryDirectory() as td:
            vf = Path(td) / "MEMENTO_VERSION"
            if tracked:
                vf.write_text(tracked + "\n")
            with (
                patch.object(memento_sync, "MEMENTO_VERSION_FILE", vf),
                patch.object(memento_sync, "_fetch_latest_tag", return_value=latest_tag),
                patch.object(memento_sync, "_get_installed_version", return_value=installed),
            ):
                return memento_sync.check()

    def test_network_failure_returns_failed(self):
        result = self._patched_check(None, None)
        self.assertEqual(result["action"], "failed")

    def test_up_to_date_installed(self):
        result = self._patched_check("v0.2.0", "0.2.0", tracked="v0.2.0")
        self.assertEqual(result["action"], "already-current")
        self.assertEqual(result["latest"], "v0.2.0")

    def test_update_available_when_installed_older(self):
        result = self._patched_check("v0.3.0", "0.2.0")
        self.assertEqual(result["action"], "update-available")
        self.assertIn("0.2.0", result["message"])
        self.assertIn("v0.3.0", result["message"])

    def test_not_installed(self):
        result = self._patched_check("v0.2.0", None)
        self.assertEqual(result["action"], "not-installed")
        self.assertIn("v0.2.0", result["message"])

    def test_updates_tracked_file_on_new_version(self):
        with tempfile.TemporaryDirectory() as td:
            vf = Path(td) / "MEMENTO_VERSION"
            vf.write_text("v0.1.0\n")
            with (
                patch.object(memento_sync, "MEMENTO_VERSION_FILE", vf),
                patch.object(memento_sync, "_fetch_latest_tag", return_value="v0.2.0"),
                patch.object(memento_sync, "_get_installed_version", return_value=None),
            ):
                memento_sync.check()
            self.assertEqual(vf.read_text().strip(), "v0.2.0")

    def test_main_always_returns_zero(self):
        with (
            patch.object(memento_sync, "_fetch_latest_tag", return_value=None),
            patch.object(memento_sync, "_get_installed_version", return_value=None),
            patch.object(
                memento_sync,
                "MEMENTO_VERSION_FILE",
                Path(tempfile.mkdtemp()) / "MEMENTO_VERSION",
            ),
        ):
            self.assertEqual(memento_sync.main(), 0)


if __name__ == "__main__":
    unittest.main()
