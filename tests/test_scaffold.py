"""Tests for scaffold.py — mode detection + structure creation."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import scaffold  # noqa: E402
from paths import context_root, raw_dir, schema_path, wiki_dir  # noqa: E402


class TestDetectMode(unittest.TestCase):
    def test_init_when_no_context_dir(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(scaffold.detect_mode(Path(td)), "INIT")

    def test_recompile_when_context_exists(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "context").mkdir()
            self.assertEqual(scaffold.detect_mode(Path(td)), "RECOMPILE")


class TestCreateStructure(unittest.TestCase):
    def test_creates_all_dirs_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            scaffold.create_structure(target, project_name="demo")
            self.assertTrue(context_root(target).is_dir())
            self.assertTrue(raw_dir(target).is_dir())
            self.assertTrue(wiki_dir(target).is_dir())
            for sub in ("concepts", "entities", "summaries", "patterns"):
                self.assertTrue((wiki_dir(target) / sub).is_dir())
                self.assertTrue((wiki_dir(target) / sub / ".gitkeep").exists())
            self.assertTrue(schema_path(target).is_file())
            self.assertTrue((wiki_dir(target) / "index.md").is_file())
            self.assertTrue((wiki_dir(target) / "log.md").is_file())
            self.assertTrue((wiki_dir(target) / "README.md").is_file())
            self.assertTrue((raw_dir(target) / "README.md").is_file())

    def test_refuses_to_clobber_existing_context(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / "context").mkdir()
            with self.assertRaises(FileExistsError):
                scaffold.create_structure(target)

    def test_template_substitution_applied(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            scaffold.create_structure(target, project_name="myproj")
            content = schema_path(target).read_text(encoding="utf-8")
            # Should not have raw {{ }} placeholders
            self.assertNotIn("{{project_name}}", content)
            self.assertNotIn("{{date}}", content)


if __name__ == "__main__":
    unittest.main()
