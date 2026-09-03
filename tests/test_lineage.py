"""Tests for lineage.py: validation, loading, resolution, ranking (no git, no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lineage as lin  # noqa: E402
import paths  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40


def _layer(lid="acme", role="org", **over):
    d = {"id": lid, "role": role, "source": "git+https://github.com/acme/org.git",
         "path": "context", "pin": SHA_A, "tree_sha": SHA_B}
    d.update(over)
    return d


def _manifest(**over):
    m = {"schema_version": 1, "kind": "compathy-persona", "id": "acme/payments/backend-engineer",
         "title": "Backend engineer", "summary": "", "tags": ["backend"],
         "layers": [_layer("acme/payments", "team"), _layer("acme", "org")],
         "reads_first": ["acme/api-standards"], "responsibilities": [],
         "toolkit": {"claude_skills": [], "mcp_servers": []},
         "policy": {"may_edit": ["project"], "may_propose_to": [], "read_only": []},
         "provenance": {}, "signature": None}
    m.update(over)
    return m


class TestValidateLineage(unittest.TestCase):
    def test_valid(self):
        doc = {"schema_version": 1, "layers": [_layer()], "self": {"id": "svc", "role": "project"}}
        self.assertEqual(lin.validate_lineage(doc), [])

    def test_depth_cap(self):
        doc = {"schema_version": 1, "layers": [_layer("a"), _layer("b", "team"), _layer("c", "team")]}
        self.assertTrue(any("depth cap" in p for p in lin.validate_lineage(doc)))

    def test_bad_source_scheme(self):
        doc = {"schema_version": 1, "layers": [_layer(source="http://x")]}
        self.assertTrue(any("source" in p for p in lin.validate_lineage(doc)))

    def test_bad_pin_and_path(self):
        doc = {"schema_version": 1, "layers": [_layer(pin="deadbeef", path="../x")]}
        probs = lin.validate_lineage(doc)
        self.assertTrue(any("pin" in p for p in probs))
        self.assertTrue(any("path" in p for p in probs))

    def test_duplicate_and_self_collision(self):
        doc = {"schema_version": 1, "layers": [_layer("a"), _layer("a", "team")],
               "self": {"id": "a", "role": "project"}}
        probs = lin.validate_lineage(doc)
        self.assertTrue(any("duplicate" in p for p in probs))
        self.assertTrue(any("collides" in p for p in probs))

    def test_bad_role(self):
        doc = {"schema_version": 1, "layers": [_layer(role="boss")]}
        self.assertTrue(any("role" in p for p in lin.validate_lineage(doc)))


class TestValidateManifest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(lin.validate_manifest(_manifest()), [])

    def test_wrong_kind_and_version(self):
        probs = lin.validate_manifest(_manifest(kind="x", schema_version=2))
        self.assertEqual(len([p for p in probs if "kind" in p or "schema_version" in p]), 2)

    def test_first_layer_must_be_exporting_layer(self):
        m = _manifest(layers=[_layer("acme", "org")])
        self.assertTrue(any("layers[0].id" in p for p in lin.validate_manifest(m)))

    def test_id_shape(self):
        self.assertTrue(lin.validate_manifest(_manifest(id="nolayer")))
        self.assertTrue(lin.validate_manifest(_manifest(id="acme/payments/Bad Role")))

    def test_reads_first_shape(self):
        m = _manifest(reads_first=["justslug"])
        self.assertTrue(any("reads_first" in p for p in lin.validate_manifest(m)))

    def test_toolkit_and_policy_types(self):
        self.assertTrue(lin.validate_manifest(_manifest(toolkit={"claude_skills": "x"})))
        self.assertTrue(lin.validate_manifest(_manifest(policy={"may_edit": "project"})))


class TestValidateRegistry(unittest.TestCase):
    def test_valid(self):
        reg = {"schema_version": 1, "org": "acme",
               "teams": [{"id": "payments", "source": "git+https://x/y.git", "path": "context"}]}
        self.assertEqual(lin.validate_registry(reg), [])

    def test_duplicate_team_and_bad_source(self):
        reg = {"schema_version": 1, "org": "acme",
               "teams": [{"id": "p", "source": "git+https://x/y.git"}, {"id": "p", "source": "ftp://z"}]}
        probs = lin.validate_registry(reg)
        self.assertTrue(any("duplicate" in p for p in probs))
        self.assertTrue(any("source" in p for p in probs))

    def test_team_id_single_segment(self):
        reg = {"schema_version": 1, "org": "acme", "teams": [{"id": "a/b", "source": "git+https://x/y.git"}]}
        self.assertTrue(lin.validate_registry(reg))


class TestLoadAndResolve(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.state = self.root / "state"
        os.environ[paths.STATE_HOME_ENV] = str(self.state)
        self.proj = self.root / "proj"
        (self.proj / "context" / "wiki" / "concepts").mkdir(parents=True)
        (self.proj / "context" / "wiki" / "index.md").write_text("# idx\n")
        (self.proj / "context" / "wiki" / "concepts" / "local.md").write_text("---\ntype: concept\n---\n# L\n")

    def tearDown(self):
        os.environ.pop(paths.STATE_HOME_ENV, None)
        self.td.cleanup()

    def _cache_layer(self, layer, pages):
        wiki = lin.layer_wiki_dir(layer)
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "index.md").write_text("# parent idx\n")
        for slug in pages:
            (wiki / "concepts" / f"{slug}.md").write_text("---\ntype: concept\n---\n# P\n")

    def test_standalone_when_absent(self):
        self.assertIsNone(lin.load_lineage(self.proj))
        layers = lin.resolve_layers(self.proj)
        self.assertEqual(len(layers), 1)
        self.assertTrue(layers[0]["self"])
        self.assertEqual(layers[0]["role"], "root")

    def test_malformed_raises(self):
        (self.proj / "context" / "lineage.json").write_text("{not json")
        with self.assertRaises(lin.LineageError):
            lin.load_lineage(self.proj)
        (self.proj / "context" / "lineage.json").write_text(json.dumps({"schema_version": 1, "layers": []}))
        with self.assertRaises(lin.LineageError):
            lin.load_lineage(self.proj)

    def test_resolution_walks_child_to_parent(self):
        team = _layer("acme/payments", "team", pin="1" * 40)
        org = _layer("acme", "org", pin="2" * 40)
        lin.save_lineage(self.proj, {"schema_version": 1, "layers": [team, org],
                                     "self": {"id": "svc", "role": "project"}})
        self._cache_layer(team, ["shared", "team-only"])
        self._cache_layer(org, ["shared", "org-only"])
        layers = lin.resolve_layers(self.proj)
        self.assertEqual([l["id"] for l in layers], ["svc", "acme/payments", "acme"])
        self.assertTrue(all(l["cached"] for l in layers))
        layer, path = lin.resolve_slug("shared", layers)
        self.assertEqual(layer["id"], "acme/payments")
        self.assertEqual([l["id"] for l, _ in lin.occurrences("shared", layers)], ["acme/payments", "acme"])
        self.assertEqual(lin.resolve_slug("org-only", layers)[0]["id"], "acme")
        self.assertEqual(lin.resolve_slug("local", layers)[0]["id"], "svc")
        self.assertEqual(lin.resolve_slug("nope", layers), (None, None))
        # explicit ref pins the layer
        self.assertEqual(lin.resolve_ref("acme/shared", layers)[0]["id"], "acme")
        # unknown layer id falls back to nearest
        self.assertEqual(lin.resolve_ref("zzz/shared", layers)[0]["id"], "acme/payments")
        self.assertEqual(lin.self_role(lin.load_lineage(self.proj)), "project")
        merged = lin.merged_index(layers)
        self.assertEqual([m["layer_id"] for m in merged], ["svc", "acme/payments", "acme"])
        self.assertIn("parent idx", merged[1]["text"])

    def test_uncached_parent_reported(self):
        org = _layer("acme", "org")
        lin.save_lineage(self.proj, {"schema_version": 1, "layers": [org]})
        layers = lin.resolve_layers(self.proj)
        self.assertFalse(layers[1]["cached"])
        self.assertEqual(lin.occurrences("anything", layers), [])
        self.assertEqual(lin.self_role(lin.load_lineage(self.proj)), "project")

    def test_extra_layer_dirs(self):
        extra = self.root / "extra" / "wiki"
        (extra / "entities").mkdir(parents=True)
        (extra / "index.md").write_text("# e\n")
        (extra / "entities" / "thing.md").write_text("---\ntype: entity\n---\n# T\n")
        layers = lin.resolve_layers(self.proj, extra=[extra.parent])
        self.assertEqual(layers[-1]["role"], "layer")
        self.assertEqual(lin.resolve_slug("thing", layers)[0]["id"], "extra")

    def test_page_path_catalogs(self):
        wiki = self.proj / "context" / "wiki"
        self.assertEqual(lin.page_path(wiki, "index"), wiki / "index.md")
        self.assertIsNone(lin.page_path(wiki, "log"))
        self.assertEqual(lin.page_path(wiki, "local").name, "local.md")


class TestHelpers(unittest.TestCase):
    def test_parse_ref(self):
        self.assertEqual(lin.parse_ref("acme/payments/go-patterns"), ("acme/payments", "go-patterns"))
        self.assertEqual(lin.parse_ref("acme/x"), ("acme", "x"))
        self.assertEqual(lin.parse_ref("bare"), ("", "bare"))

    def test_layer_slug(self):
        self.assertEqual(paths.layer_slug("acme/payments"), "acme--payments")
        url_slug = paths.layer_slug("git+https://github.com/a/b.git")
        self.assertNotIn("/", url_slug)
        self.assertEqual(url_slug, paths.layer_slug("git+https://github.com/a/b.git"))

    def test_state_home_override(self):
        os.environ[paths.STATE_HOME_ENV] = "/tmp/x-compathy"
        try:
            self.assertEqual(paths.state_home(), Path("/tmp/x-compathy"))
            self.assertEqual(paths.layers_cache_dir(), Path("/tmp/x-compathy/layers"))
        finally:
            os.environ.pop(paths.STATE_HOME_ENV, None)
        self.assertEqual(paths.state_home(), Path.home() / ".compathy")

    def test_score_persona_ranking(self):
        a = {"id": "acme/payments/backend-engineer", "title": "Backend engineer, Payments",
             "tags": ["backend", "go"], "summary": "ledger", "team": "acme/payments"}
        b = {"id": "acme/web/frontend-engineer", "title": "Frontend engineer, Web",
             "tags": ["react"], "summary": "storefront", "team": "acme/web"}
        sa, why = lin.score_persona("payments backend", a)
        sb, _ = lin.score_persona("payments backend", b)
        self.assertGreater(sa, sb)
        self.assertEqual(sb, 0.0)
        self.assertTrue(any("matches" in w for w in why))
        self.assertEqual(lin.score_persona("", a), (0.0, []))
        # deterministic
        self.assertEqual(lin.score_persona("payments backend", a), lin.score_persona("payments backend", a))

    def test_write_json_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "f.json"
            lin.write_json_atomic(p, {"a": 1})
            self.assertEqual(lin.read_json(p), {"a": 1})
            self.assertFalse(p.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
