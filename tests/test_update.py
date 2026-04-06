"""Tests for update.py — auto-update logic."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import update  # noqa: E402
import version  # noqa: E402


class TestVersion(unittest.TestCase):
    def test_reads_version_file(self):
        v = version.get_version()
        # Should be a semver-ish string
        self.assertRegex(v, r"^\d+\.\d+\.\d+")

    def test_version_file_exists(self):
        self.assertTrue(version.VERSION_FILE.exists())


class TestUpdateLogic(unittest.TestCase):
    """Test update behavior by mocking REPO_ROOT to temp repos."""

    def _make_repo(self, root):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(root),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(root),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(root),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(root),
                       check=True, capture_output=True)
        (root / "VERSION").write_text("0.1.0\n")
        subprocess.run(["git", "add", "."], cwd=str(root),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(root),
                       check=True, capture_output=True)

    def test_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "VERSION").write_text("0.1.0\n")
            with patch.object(update, "REPO_ROOT", root):
                result = update.update()
            self.assertEqual(result["action"], "skipped")
            self.assertIn("not a git repo", result["message"])

    def test_no_remote(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            # No remote added
            with patch.object(update, "REPO_ROOT", root):
                result = update.update()
            self.assertEqual(result["action"], "skipped")
            self.assertIn("no git remote", result["message"])

    def test_already_current(self):
        """Clone a repo, don't change origin — should be up to date."""
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            self._make_repo(origin)
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                           check=True, capture_output=True)
            (clone / "VERSION").write_text("0.1.0\n")  # ensure VERSION exists
            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "already-current")

    def test_pulls_new_version(self):
        """Push a new commit to origin, then update should pull it."""
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            self._make_repo(origin)
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                           check=True, capture_output=True)

            # Push a new commit to origin
            (origin / "VERSION").write_text("0.2.0\n")
            subprocess.run(["git", "add", "VERSION"], cwd=str(origin),
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "bump version"],
                           cwd=str(origin), check=True, capture_output=True)

            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "updated")
            self.assertEqual(result["old_version"], "0.1.0")
            self.assertEqual(result["new_version"], "0.2.0")

    def test_diverged_fails_gracefully(self):
        """If local has diverged from remote, update should fail gracefully."""
        with tempfile.TemporaryDirectory() as td:
            origin = Path(td) / "origin"
            clone = Path(td) / "clone"
            origin.mkdir()
            self._make_repo(origin)
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                           check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"],
                           cwd=str(clone), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"],
                           cwd=str(clone), check=True, capture_output=True)
            subprocess.run(["git", "config", "commit.gpgsign", "false"],
                           cwd=str(clone), check=True, capture_output=True)

            # Diverge: different commits on origin and clone
            (origin / "a.txt").write_text("origin change")
            subprocess.run(["git", "add", "."], cwd=str(origin),
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "origin"],
                           cwd=str(origin), check=True, capture_output=True)

            (clone / "b.txt").write_text("local change")
            subprocess.run(["git", "add", "."], cwd=str(clone),
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "local"],
                           cwd=str(clone), check=True, capture_output=True)

            with patch.object(update, "REPO_ROOT", clone):
                result = update.update()
            self.assertEqual(result["action"], "failed")
            # Should not crash — exit code would be 0 in main()

    def test_main_always_returns_zero(self):
        """main() should never block the skill, always returns 0."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "VERSION").write_text("0.1.0\n")
            with patch.object(update, "REPO_ROOT", root):
                rc = update.main()
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
