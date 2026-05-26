"""Tests for ingest.py — checksums, .ref resolution, change detection, state I/O."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ingest  # noqa: E402
import scaffold  # noqa: E402
from paths import raw_dir, state_path  # noqa: E402


def _git_init(root):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(root), check=True,
                   capture_output=True)


class TestChecksum(unittest.TestCase):
    def test_lf_normalization(self):
        """CRLF and LF produce identical checksums."""
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.txt"
            b = Path(td) / "b.txt"
            a.write_bytes(b"hello\nworld\n")
            b.write_bytes(b"hello\r\nworld\r\n")
            self.assertEqual(ingest.compute_checksum(a), ingest.compute_checksum(b))

    def test_different_content_different_checksum(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.txt"
            b = Path(td) / "b.txt"
            a.write_text("hello")
            b.write_text("world")
            self.assertNotEqual(ingest.compute_checksum(a), ingest.compute_checksum(b))

    def test_file_too_large_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "big.bin"
            p.write_bytes(b"x" * (ingest.MAX_FILE_BYTES + 1))
            with self.assertRaises(RuntimeError):
                ingest.compute_checksum(p)


class TestRefResolution(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        _git_init(self.root)
        self.target_root = ingest.repo_root(self.root)

    def tearDown(self):
        self.td.cleanup()

    def test_valid_ref(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "spec.md").write_text("# Spec\n")
        ref = self.root / "pointer.ref"
        ref.write_text("docs/spec.md\n")
        resolved, data = ingest.resolve_ref_file(ref, self.target_root)
        self.assertTrue(resolved.name == "spec.md")
        self.assertIn(b"Spec", data)

    def test_rejects_dotdot(self):
        ref = self.root / "p.ref"
        ref.write_text("../etc/passwd\n")
        with self.assertRaises(RuntimeError) as ctx:
            ingest.resolve_ref_file(ref, self.target_root)
        self.assertIn("..", str(ctx.exception))

    def test_rejects_absolute(self):
        ref = self.root / "p.ref"
        ref.write_text("/etc/passwd\n")
        with self.assertRaises(RuntimeError) as ctx:
            ingest.resolve_ref_file(ref, self.target_root)
        self.assertIn("relative", str(ctx.exception))

    def test_rejects_missing_target(self):
        ref = self.root / "p.ref"
        ref.write_text("does/not/exist.md\n")
        with self.assertRaises(RuntimeError) as ctx:
            ingest.resolve_ref_file(ref, self.target_root)
        self.assertIn("does not exist", str(ctx.exception))

    def test_rejects_empty_ref(self):
        ref = self.root / "p.ref"
        ref.write_text("\n")
        with self.assertRaises(RuntimeError):
            ingest.resolve_ref_file(ref, self.target_root)

    def test_rejects_comment_only(self):
        ref = self.root / "p.ref"
        ref.write_text("# just a comment\n")
        with self.assertRaises(RuntimeError):
            ingest.resolve_ref_file(ref, self.target_root)


class TestDetectChanges(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        _git_init(self.root)
        scaffold.create_structure(self.root, project_name="demo")

    def tearDown(self):
        self.td.cleanup()

    def test_added_files(self):
        (raw_dir(self.root) / "a.md").write_text("hello\n")
        (raw_dir(self.root) / "b.md").write_text("world\n")
        result = ingest.detect_changes(self.root)
        self.assertEqual(sorted(result["added"]), ["a.md", "b.md"])
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["deleted"], [])

    def test_skips_readme_and_gitkeep(self):
        # raw/README.md and raw/.gitkeep are created by scaffold
        result = ingest.detect_changes(self.root)
        self.assertEqual(result["current"], {})

    def test_modified_after_commit(self):
        f = raw_dir(self.root) / "a.md"
        f.write_text("v1\n")
        ingest.commit_state(self.root)
        f.write_text("v2 changed\n")
        result = ingest.detect_changes(self.root)
        self.assertEqual(result["modified"], ["a.md"])
        self.assertEqual(result["added"], [])

    def test_deleted_after_commit(self):
        f = raw_dir(self.root) / "a.md"
        f.write_text("v1\n")
        ingest.commit_state(self.root)
        f.unlink()
        result = ingest.detect_changes(self.root)
        self.assertEqual(result["deleted"], ["a.md"])

    def test_no_changes_after_recommit(self):
        (raw_dir(self.root) / "a.md").write_text("v1\n")
        ingest.commit_state(self.root)
        result = ingest.detect_changes(self.root)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["deleted"], [])

    def test_ref_file_change_detected(self):
        (self.root / "doc.md").write_text("v1\n")
        ref = raw_dir(self.root) / "doc.md.ref"
        ref.write_text("doc.md\n")
        ingest.commit_state(self.root)
        # Modify the target
        (self.root / "doc.md").write_text("v2 different\n")
        result = ingest.detect_changes(self.root)
        self.assertEqual(result["modified"], ["doc.md.ref"])

    def test_bad_ref_surfaces_error(self):
        ref = raw_dir(self.root) / "bad.ref"
        ref.write_text("../outside.md\n")
        result = ingest.detect_changes(self.root)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["path"], "bad.ref")


class TestStateIO(unittest.TestCase):
    def test_atomic_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="x")
            state = {"version": 1, "entries": {"a.md": {"kind": "file", "sha256": "abc"}}}
            ingest.save_state(state_path(root), state)
            loaded = ingest.load_state(state_path(root))
            self.assertEqual(loaded["entries"]["a.md"]["sha256"], "abc")

    def test_corrupt_state_rebuilds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="x")
            state_path(root).write_text("{not valid json")
            loaded = ingest.load_state(state_path(root))
            self.assertEqual(loaded["entries"], {})

    def test_no_tmp_files_left_after_save(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="x")
            ingest.save_state(state_path(root), {"version": 1, "entries": {}})
            leftovers = list(state_path(root).parent.glob(".state-*.tmp"))
            self.assertEqual(leftovers, [])

    def test_fresh_state_has_embeddings_slot(self):
        """No state file yet → default dict carries embeddings=None,
        reserved for future opt-in embedding-based pre-filtering."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="x")
            loaded = ingest.load_state(state_path(root))
            self.assertIn("embeddings", loaded)
            self.assertIsNone(loaded["embeddings"])

    def test_older_state_backfills_embeddings_slot(self):
        """A state file written by an older version (no embeddings key)
        is backfilled to None on load so callers can rely on the shape
        without a one-shot migration."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="x")
            state_path(root).write_text(
                '{"version": 1, "entries": '
                '{"a.md": {"kind": "file", "sha256": "abc"}}}',
                encoding="utf-8",
            )
            loaded = ingest.load_state(state_path(root))
            self.assertIn("embeddings", loaded)
            self.assertIsNone(loaded["embeddings"])
            # Existing entries are preserved (no clobber).
            self.assertEqual(loaded["entries"]["a.md"]["sha256"], "abc")


if __name__ == "__main__":
    unittest.main()
