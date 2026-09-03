"""Tests for layers.py: clone-at-pin, verification, read-only cache, sparse fetches.

Uses real git against local file:// remotes in a temp dir. No network.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import layers  # noqa: E402
import lineage as lin  # noqa: E402
import paths  # noqa: E402


def git(repo: Path, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )


def make_repo(root: Path, ctx_rel: str = "context") -> Path:
    """A git repo with a tiny wiki at <ctx_rel>/wiki and two commits."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    wiki = root / ctx_rel / "wiki" / "concepts"
    wiki.mkdir(parents=True)
    (root / ctx_rel / "wiki" / "index.md").write_text("# index v1\n")
    (wiki / "alpha.md").write_text("---\ntype: concept\nschema_version: 1\n---\n# Alpha\n")
    (root / "unrelated.txt").write_text("x\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "one")
    (root / ctx_rel / "wiki" / "index.md").write_text("# index v2\n")
    git(root, "commit", "-qam", "two")
    return root


class LayersBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.state = self.root / "state"
        os.environ[paths.STATE_HOME_ENV] = str(self.state)
        self.repo = make_repo(self.root / "remote")
        self.pin_old = git(self.repo, "rev-parse", "HEAD~1").stdout.strip()
        self.pin_new = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.source = "git+file://" + str(self.repo)

    def tearDown(self):
        os.environ.pop(paths.STATE_HOME_ENV, None)
        layers.remove_tree(self.state)
        self.td.cleanup()

    def layer(self, pin=None, tree=True, **over):
        pin = pin or self.pin_old
        d = {"id": "acme", "role": "org", "source": self.source, "path": "context", "pin": pin,
             "tree_sha": layers.tree_sha(self.repo, pin, "context/wiki") if tree else None}
        d.update(over)
        return d


class TestUrlMapping(unittest.TestCase):
    def test_git_url_to_source(self):
        self.assertEqual(layers.git_url_to_source("https://github.com/a/b.git"), "git+https://github.com/a/b.git")
        self.assertEqual(layers.git_url_to_source("git@github.com:a/b.git"), "git+ssh://git@github.com/a/b.git")
        self.assertEqual(layers.git_url_to_source("ssh://git@host/a/b.git"), "git+ssh://git@host/a/b.git")
        self.assertEqual(layers.git_url_to_source("/abs/path"), "git+file:///abs/path")
        self.assertIsNone(layers.git_url_to_source("ftp://x"))
        self.assertIsNone(layers.git_url_to_source(""))

    def test_source_to_git_url_rejects_unknown(self):
        self.assertEqual(layers.source_to_git_url("git+https://x/y.git"), "https://x/y.git")
        with self.assertRaises(layers.LayerFetchError):
            layers.source_to_git_url("https://x/y.git")


class TestLocalInspection(LayersBase):
    def test_toplevel_head_tree_clean(self):
        self.assertEqual(layers.repo_toplevel(self.repo / "context"), self.repo.resolve())
        self.assertEqual(layers.head_sha(self.repo), self.pin_new)
        t1 = layers.tree_sha(self.repo, self.pin_old, "context/wiki")
        t2 = layers.tree_sha(self.repo, self.pin_new, "context/wiki")
        self.assertTrue(lin.SHA_RE.match(t1))
        self.assertNotEqual(t1, t2)
        self.assertIsNone(layers.tree_sha(self.repo, self.pin_new, "nope/wiki"))
        self.assertTrue(layers.is_clean(self.repo, "context"))
        (self.repo / "context" / "wiki" / "index.md").write_text("dirty\n")
        self.assertFalse(layers.is_clean(self.repo, "context"))
        self.assertEqual(layers.relpath_in_repo(self.repo, self.repo / "context"), "context")
        self.assertEqual(layers.relpath_in_repo(self.repo, self.repo), ".")

    def test_remote_source(self):
        self.assertIsNone(layers.remote_source(self.repo))
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/org.git")
        self.assertEqual(layers.remote_source(self.repo), "git+ssh://git@github.com/acme/org.git")


class TestFetchLayer(LayersBase):
    def test_fetch_then_cached_and_readonly(self):
        layer = self.layer()
        r = layers.fetch_layer(layer)
        self.assertEqual(r["action"], "fetched")
        self.assertTrue(r["verify"]["ok"])
        root = lin.layer_cache_root(layer)
        self.assertEqual(root, self.state / "layers" / "acme" / self.pin_old)
        self.assertEqual((root / "context" / "wiki" / "index.md").read_text(), "# index v1\n")
        self.assertFalse((root / "unrelated.txt").exists(), "sparse checkout should exclude other paths")
        self.assertTrue(lin.is_layer_cached(layer))
        with self.assertRaises(PermissionError):
            (root / "context" / "wiki" / "index.md").write_text("tamper")
        r2 = layers.fetch_layer(layer)
        self.assertEqual(r2["action"], "cached")

    def test_tree_sha_mismatch_rejected_and_nothing_cached(self):
        layer = self.layer(tree_sha="0" * 40)
        with self.assertRaises(layers.LayerFetchError) as cm:
            layers.fetch_layer(layer)
        self.assertIn("tree sha mismatch", str(cm.exception))
        self.assertFalse(lin.layer_cache_root(layer).exists())
        leftovers = [p for p in (self.state / "layers" / "acme").glob(".fetch-*")] if (self.state / "layers" / "acme").exists() else []
        self.assertEqual(leftovers, [])

    def test_unknown_pin_rejected(self):
        layer = self.layer(pin="f" * 40, tree=False)
        with self.assertRaises(layers.LayerFetchError):
            layers.fetch_layer(layer)
        self.assertFalse(lin.layer_cache_root(layer).exists())

    def test_force_refetch_keeps_good_cache_on_failure(self):
        good = self.layer()
        layers.fetch_layer(good)
        bad = dict(good, tree_sha="0" * 40)
        with self.assertRaises(layers.LayerFetchError):
            layers.fetch_layer(bad, force=True)
        self.assertTrue(lin.is_layer_cached(good), "a failed forced refetch must not delete the good cache")

    def test_subpath_layer(self):
        repo = make_repo(self.root / "mono", ctx_rel="teams/payments/context")
        pin = git(repo, "rev-parse", "HEAD").stdout.strip()
        layer = {"id": "acme/payments", "role": "team", "source": "git+file://" + str(repo),
                 "path": "teams/payments/context", "pin": pin,
                 "tree_sha": layers.tree_sha(repo, pin, "teams/payments/context/wiki")}
        r = layers.fetch_layer(layer)
        self.assertTrue(r["verify"]["ok"])
        self.assertTrue((lin.layer_wiki_dir(layer) / "index.md").is_file())

    def test_verify_not_cached(self):
        v = layers.verify_layer(self.layer())
        self.assertFalse(v["ok"])
        self.assertIn("not-cached", v["reasons"])

    def test_unreachable_source(self):
        layer = self.layer(source="git+file:///nonexistent/repo/path")
        with self.assertRaises(layers.LayerFetchError):
            layers.fetch_layer(layer)


class TestFetchPaths(LayersBase):
    def test_fetch_cached_refresh(self):
        (self.repo / "context" / "registry.json").write_text('{"schema_version":1,"org":"acme","teams":[]}')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "reg")
        dest = self.state / "cache" / "registry" / "acme"
        r = layers.fetch_paths(self.source, "HEAD", ["context/registry.json"], dest, ttl_seconds=3600)
        self.assertEqual(r["action"], "fetched")
        self.assertTrue((dest / "context" / "registry.json").is_file())
        self.assertFalse((dest / "unrelated.txt").exists())
        r2 = layers.fetch_paths(self.source, "HEAD", ["context/registry.json"], dest, ttl_seconds=3600)
        self.assertEqual(r2["action"], "cached")
        r3 = layers.fetch_paths(self.source, "HEAD", ["context/registry.json"], dest, ttl_seconds=3600, refresh=True)
        self.assertEqual(r3["action"], "fetched")
        # expired ttl refetches
        meta = dest / layers.META_FILE
        os.utime(meta, (time.time() - 10, time.time() - 10))
        r4 = layers.fetch_paths(self.source, "HEAD", ["context/registry.json"], dest, ttl_seconds=0)
        self.assertEqual(r4["action"], "fetched")

    def test_unreachable_without_cache_raises_and_with_cache_is_stale(self):
        dest = self.state / "cache" / "registry" / "x"
        with self.assertRaises(layers.LayerFetchError):
            layers.fetch_paths("git+file:///nonexistent/repo", "HEAD", ["context"], dest)
        layers.fetch_paths(self.source, "HEAD", ["context"], dest, ttl_seconds=3600)
        r = layers.fetch_paths("git+file:///nonexistent/repo", "HEAD", ["context"], dest, ttl_seconds=0)
        self.assertEqual(r["action"], "stale")

    def test_upstream_head(self):
        self.assertEqual(layers.upstream_head(self.source), self.pin_new)
        self.assertIsNone(layers.upstream_head("git+file:///nonexistent/repo"))


if __name__ == "__main__":
    unittest.main()
