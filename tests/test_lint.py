"""Tests for lint.py — frontmatter parser, backlinks, structural checks."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lint  # noqa: E402
import scaffold  # noqa: E402
from paths import wiki_dir  # noqa: E402


# ---------- frontmatter parser ----------

class TestParseFrontmatter(unittest.TestCase):
    def test_no_frontmatter(self):
        fm, body = lint.parse_frontmatter("# Hello\n\nbody")
        self.assertEqual(fm, {})
        self.assertIn("Hello", body)

    def test_basic_scalars(self):
        text = "---\ntype: concept\nschema_version: 1\ncreated: 2026-04-04\n---\nbody\n"
        fm, body = lint.parse_frontmatter(text)
        self.assertEqual(fm["type"], "concept")
        self.assertEqual(fm["schema_version"], 1)
        self.assertEqual(fm["created"], "2026-04-04")
        self.assertEqual(body, "body\n")

    def test_flat_list(self):
        text = "---\ntype: concept\nsources: [a, b, c]\n---\n"
        fm, _ = lint.parse_frontmatter(text)
        self.assertEqual(fm["sources"], ["a", "b", "c"])

    def test_empty_list(self):
        text = "---\nrelated_paths: []\n---\n"
        fm, _ = lint.parse_frontmatter(text)
        self.assertEqual(fm["related_paths"], [])

    def test_booleans_and_null(self):
        text = "---\na: true\nb: False\nc: null\n---\n"
        fm, _ = lint.parse_frontmatter(text)
        self.assertIs(fm["a"], True)
        self.assertIs(fm["b"], False)
        self.assertIsNone(fm["c"])

    def test_quoted_string(self):
        text = '---\ntitle: "true"\n---\n'
        fm, _ = lint.parse_frontmatter(text)
        self.assertEqual(fm["title"], "true")

    def test_rejects_indented_line(self):
        text = "---\ntype: concept\n  nested: x\n---\n"
        with self.assertRaises(ValueError):
            lint.parse_frontmatter(text)

    def test_rejects_nested_list(self):
        text = "---\nbad: [[a, b], c]\n---\n"
        with self.assertRaises(ValueError):
            lint.parse_frontmatter(text)

    def test_rejects_missing_closing_delim(self):
        text = "---\ntype: concept\nbody without close\n"
        with self.assertRaises(ValueError):
            lint.parse_frontmatter(text)

    def test_rejects_missing_colon(self):
        text = "---\nno colon here\n---\n"
        with self.assertRaises(ValueError):
            lint.parse_frontmatter(text)


# ---------- backlinks ----------

class TestParseBacklinks(unittest.TestCase):
    def test_basic(self):
        body = "See [[foo]] and [[bar-baz]]."
        self.assertEqual(lint.parse_backlinks(body), ["foo", "bar-baz"])

    def test_alias_syntax(self):
        body = "See [[foo|Foo Page]]"
        self.assertEqual(lint.parse_backlinks(body), ["foo"])

    def test_strips_code_fence(self):
        body = "```\n[[fake]]\n```\nreal [[real]]"
        self.assertEqual(lint.parse_backlinks(body), ["real"])

    def test_strips_inline_code(self):
        body = "`[[fake]]` real [[real]]"
        self.assertEqual(lint.parse_backlinks(body), ["real"])

    def test_empty(self):
        self.assertEqual(lint.parse_backlinks("nothing here"), [])


# ---------- integration: check_* against a scaffolded wiki ----------

def _page(type_, slug, body="", sources=None, schema_version=1):
    src = f"[{', '.join(sources)}]" if sources else "[]"
    return (
        f"---\ntype: {type_}\nschema_version: {schema_version}\n"
        f"created: 2026-04-04\nupdated: 2026-04-04\nsources: {src}\n---\n"
        f"# {slug}\n\n{body}\n"
    )


class TestLintChecks(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        scaffold.create_structure(self.root, project_name="demo")
        self.wiki = wiki_dir(self.root)

    def tearDown(self):
        self.td.cleanup()

    def _write_page(self, sub, slug, body="", type_=None):
        # concepts→concept, entities→entity, summaries→summary, patterns→patterns
        default_type = "patterns" if sub == "patterns" else sub.rstrip("s")
        type_ = type_ or default_type
        (self.wiki / sub / f"{slug}.md").write_text(_page(type_, slug, body))

    def _write_index(self, slugs):
        text = "# Index\n\n## Concepts\n"
        for s in slugs:
            text += f"- [[{s}]]\n"
        (self.wiki / "index.md").write_text(text)

    def test_clean_wiki(self):
        self._write_page("concepts", "alpha", "see [[beta]]")
        self._write_page("concepts", "beta", "see [[alpha]]")
        self._write_index(["alpha", "beta"])
        result = lint.lint(self.root)
        self.assertEqual(result["summary"]["errors"], 0)

    def test_patterns_page_is_valid(self):
        self._write_page("patterns", "technical-patterns", "see [[alpha]]")
        self._write_page("concepts", "alpha")
        self._write_index(["alpha", "technical-patterns"])
        result = lint.lint(self.root)
        self.assertEqual(result["summary"]["errors"], 0)

    def test_broken_backlink_is_error(self):
        self._write_page("concepts", "alpha", "see [[missing]]")
        self._write_index(["alpha"])
        result = lint.lint(self.root)
        kinds = [e["kind"] for e in result["errors"]]
        self.assertIn("broken-backlink", kinds)

    def test_self_backlink_is_warning(self):
        self._write_page("concepts", "alpha", "see [[alpha]]")
        self._write_index(["alpha"])
        result = lint.lint(self.root)
        kinds = [w["kind"] for w in result["warnings"]]
        self.assertIn("self-backlink", kinds)

    def test_orphan_page_warns(self):
        self._write_page("concepts", "alpha")
        self._write_page("concepts", "beta")
        self._write_index(["alpha"])  # beta orphaned
        result = lint.lint(self.root)
        kinds = [w["kind"] for w in result["warnings"]]
        self.assertIn("orphan-page", kinds)

    def test_index_stale_is_error(self):
        self._write_page("concepts", "alpha")
        self._write_index(["alpha", "ghost"])
        result = lint.lint(self.root)
        kinds = [e["kind"] for e in result["errors"]]
        self.assertIn("index-stale", kinds)

    def test_bad_slug_is_error(self):
        # Write a page with uppercase slug
        (self.wiki / "concepts" / "BadSlug.md").write_text(
            _page("concept", "BadSlug")
        )
        self._write_index(["BadSlug"])
        result = lint.lint(self.root)
        kinds = [e["kind"] for e in result["errors"]]
        self.assertIn("bad-slug", kinds)

    def test_missing_frontmatter_field_error(self):
        (self.wiki / "concepts" / "nofm.md").write_text(
            "---\ntype: concept\n---\n# nofm\n"  # missing schema_version
        )
        self._write_index(["nofm"])
        result = lint.lint(self.root)
        kinds = [e["kind"] for e in result["errors"]]
        self.assertIn("missing-frontmatter-field", kinds)

    def test_invalid_type_is_error(self):
        (self.wiki / "concepts" / "weird.md").write_text(
            "---\ntype: banana\nschema_version: 1\n---\n# weird\n"
        )
        self._write_index(["weird"])
        result = lint.lint(self.root)
        kinds = [e["kind"] for e in result["errors"]]
        self.assertIn("invalid-type", kinds)

    def test_schema_version_mismatch_warns(self):
        (self.wiki / "concepts" / "old.md").write_text(
            "---\ntype: concept\nschema_version: 0\n---\n# old\n"
        )
        self._write_index(["old"])
        result = lint.lint(self.root)
        kinds = [w["kind"] for w in result["warnings"]]
        self.assertIn("schema-version-mismatch", kinds)

    def test_frontmatter_error_is_reported(self):
        (self.wiki / "concepts" / "bad.md").write_text(
            "---\ntype: concept\n  indented: bad\n---\n# bad\n"
        )
        self._write_index(["bad"])
        result = lint.lint(self.root)
        kinds = [e["kind"] for e in result["errors"]]
        self.assertIn("frontmatter-error", kinds)


if __name__ == "__main__":
    unittest.main()
