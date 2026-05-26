"""Tests for compathy_query.py — wiki query tools and stdio MCP dispatcher."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import compathy_query as cq  # noqa: E402
import persona_integration  # noqa: E402
import scaffold  # noqa: E402
from paths import wiki_dir  # noqa: E402


def _page(type_, slug, body="", schema_version=1):
    return (
        f"---\ntype: {type_}\nschema_version: {schema_version}\n"
        f"created: 2026-04-04\nupdated: 2026-04-04\nsources: []\n---\n"
        f"# {slug.replace('-', ' ').title()}\n\n{body}\n"
    )


class WikiFixture(unittest.TestCase):
    """Base class that builds a small wiki for the query tools to consume.

    Isolates HOME and ``persona_integration.PERSONA_JSON_PATH`` so the user's
    real ``~/.ai-quickstart/persona/persona.json`` (if any) cannot leak in and
    seed unexpected ``entities/builder.md`` or ``patterns/style.md`` pages.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        # Isolate HOME so persona integration is a no-op during scaffold.
        self._old_home = os.environ.get("HOME")
        self._old_persona_path = persona_integration.PERSONA_JSON_PATH
        fake_home = self.root / "_fake_home"
        fake_home.mkdir()
        os.environ["HOME"] = str(fake_home)
        persona_integration.PERSONA_JSON_PATH = (
            fake_home / ".ai-quickstart" / "persona" / "persona.json"
        )
        scaffold.create_structure(self.root / "proj", project_name="demo")
        self.root = self.root / "proj"
        self.wiki = wiki_dir(self.root)
        # Seed pages across all four subdirectories.
        (self.wiki / "concepts" / "alpha.md").write_text(
            _page("concept", "alpha", "Alpha concept references [[beta]] and discusses persona injection.")
        )
        (self.wiki / "concepts" / "beta.md").write_text(
            _page("concept", "beta", "Beta concept. Mentions FRONTMATTER schema once.")
        )
        (self.wiki / "entities" / "claude.md").write_text(
            _page("entity", "claude", "Claude is the agent driving the wiki.")
        )
        (self.wiki / "summaries" / "readme-summary.md").write_text(
            _page("summary", "readme-summary", "Summary of the README.")
        )
        (self.wiki / "patterns" / "technical-patterns.md").write_text(
            _page("patterns", "technical-patterns", "Stdlib only. No third-party deps.")
        )
        # Replace index.md and log.md with deterministic content.
        (self.wiki / "index.md").write_text(
            "---\ntype: index\nschema_version: 1\n---\n"
            "# Index\n\n## Concepts\n- [[alpha]]\n- [[beta]]\n\n"
            "## Entities\n- [[claude]]\n\n## Summaries\n- [[readme-summary]]\n\n"
            "## Patterns\n- [[technical-patterns]]\n"
        )
        (self.wiki / "log.md").write_text(
            "---\ntype: log\nschema_version: 1\n---\n"
            "# Log\n\n> intro line\n\n"
            "## [2026-04-01] init | scaffold\n\n- created context/\n\n"
            "## [2026-04-02] compile | first pass\n\n- wrote 5 pages\n\n"
            "## [2026-04-03] lint | clean\n\n- 0 errors\n\n"
            "## [2026-04-04] compile | persona pass\n\n- added persona\n"
        )

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        persona_integration.PERSONA_JSON_PATH = self._old_persona_path
        self.td.cleanup()


# ---------- direct tool tests ----------

class TestCompathyIndex(WikiFixture):
    def test_returns_index_content(self):
        out = cq.tool_compathy_index(self.root, {})
        self.assertEqual(out["slug"], "index")
        self.assertEqual(out["page_type"], "index")
        self.assertIn("alpha", out["raw"])
        self.assertEqual(out["frontmatter"]["type"], "index")

    def test_missing_index_raises(self):
        (self.wiki / "index.md").unlink()
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_index(self.root, {})


class TestCompathyGetPage(WikiFixture):
    def test_returns_known_page(self):
        out = cq.tool_compathy_get_page(self.root, {"slug": "alpha"})
        self.assertEqual(out["slug"], "alpha")
        self.assertEqual(out["page_type"], "concept")
        self.assertIn("Alpha concept", out["body"])
        self.assertEqual(out["frontmatter"]["type"], "concept")

    def test_unknown_slug_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_get_page(self.root, {"slug": "does-not-exist"})

    def test_missing_slug_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_get_page(self.root, {})

    def test_index_and_log_are_addressable(self):
        idx = cq.tool_compathy_get_page(self.root, {"slug": "index"})
        self.assertEqual(idx["page_type"], "index")
        lg = cq.tool_compathy_get_page(self.root, {"slug": "log"})
        self.assertEqual(lg["page_type"], "log")


class TestCompathyGetPageNeighbors(WikiFixture):
    """Coverage for include_neighbors on compathy_get_page.

    The WikiFixture seeds alpha.md with body "... references [[beta]] ...",
    so outbound from alpha is [beta]; nothing else in the fixture references
    alpha in body content (the index references [[alpha]] but index/log are
    catalogs and excluded by design).
    """

    def test_default_omits_neighbors_field(self):
        out = cq.tool_compathy_get_page(self.root, {"slug": "alpha"})
        self.assertNotIn("neighbors", out)

    def test_explicit_false_omits_neighbors_field(self):
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "alpha", "include_neighbors": False}
        )
        self.assertNotIn("neighbors", out)

    def test_outbound_resolves_to_existing_page(self):
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "alpha", "include_neighbors": True}
        )
        outbound = out["neighbors"]["outbound"]
        self.assertEqual([n["slug"] for n in outbound], ["beta"])
        beta = outbound[0]
        self.assertEqual(beta["page_type"], "concept")
        self.assertEqual(beta["title"], "Beta")

    def test_inbound_discovers_referring_page(self):
        """Add a summary that references [[alpha]]; it must appear in
        alpha's inbound list."""
        (self.wiki / "summaries" / "uses-alpha.md").write_text(
            _page("summary", "uses-alpha", "Talks about [[alpha]] in depth.")
        )
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "alpha", "include_neighbors": True}
        )
        inbound_slugs = [n["slug"] for n in out["neighbors"]["inbound"]]
        self.assertIn("uses-alpha", inbound_slugs)

    def test_catalogs_excluded_from_inbound(self):
        """index.md references [[alpha]] but is a catalog, not a knowledge
        node — it must not appear in inbound."""
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "alpha", "include_neighbors": True}
        )
        inbound_slugs = [n["slug"] for n in out["neighbors"]["inbound"]]
        self.assertNotIn("index", inbound_slugs)
        self.assertNotIn("log", inbound_slugs)

    def test_self_references_excluded_from_outbound(self):
        (self.wiki / "concepts" / "selfish.md").write_text(
            _page("concept", "selfish", "I reference [[selfish]] myself.")
        )
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "selfish", "include_neighbors": True}
        )
        self.assertEqual(out["neighbors"]["outbound"], [])

    def test_broken_backlinks_excluded_from_outbound(self):
        (self.wiki / "concepts" / "links-nowhere.md").write_text(
            _page("concept", "links-nowhere", "Points at [[ghost-page]].")
        )
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "links-nowhere", "include_neighbors": True}
        )
        self.assertEqual(out["neighbors"]["outbound"], [])

    def test_duplicate_backlinks_deduped_in_outbound(self):
        (self.wiki / "concepts" / "dupe.md").write_text(
            _page("concept", "dupe", "Mentions [[beta]] and again [[beta]].")
        )
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "dupe", "include_neighbors": True}
        )
        self.assertEqual(
            [n["slug"] for n in out["neighbors"]["outbound"]], ["beta"]
        )

    def test_alias_backlink_resolves_to_slug(self):
        (self.wiki / "concepts" / "aliased.md").write_text(
            _page("concept", "aliased", "See [[beta|the second concept]].")
        )
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "aliased", "include_neighbors": True}
        )
        self.assertEqual(
            [n["slug"] for n in out["neighbors"]["outbound"]], ["beta"]
        )

    def test_invalid_include_neighbors_type_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_get_page(
                self.root, {"slug": "alpha", "include_neighbors": "yes"}
            )

    def test_outbound_sorted_by_slug(self):
        (self.wiki / "concepts" / "multi.md").write_text(
            _page("concept", "multi", "Refs [[claude]], [[beta]], [[alpha]].")
        )
        out = cq.tool_compathy_get_page(
            self.root, {"slug": "multi", "include_neighbors": True}
        )
        slugs = [n["slug"] for n in out["neighbors"]["outbound"]]
        self.assertEqual(slugs, sorted(slugs))


class TestCompathySearch(WikiFixture):
    def test_finds_term_in_body(self):
        out = cq.tool_compathy_search(self.root, {"query": "persona"})
        slugs = [r["slug"] for r in out["results"]]
        self.assertIn("alpha", slugs)
        # Snippet should contain context around the hit.
        alpha = next(r for r in out["results"] if r["slug"] == "alpha")
        self.assertIn("persona", alpha["snippet"].lower())

    def test_empty_result_is_success(self):
        out = cq.tool_compathy_search(self.root, {"query": "xyzzy-no-match"})
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["results"], [])

    def test_max_results_cap(self):
        out = cq.tool_compathy_search(self.root, {"query": "concept", "max_results": 1})
        self.assertEqual(len(out["results"]), 1)

    def test_invalid_query_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_search(self.root, {"query": ""})
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_search(self.root, {})

    def test_invalid_max_results_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_search(self.root, {"query": "a", "max_results": 0})
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_search(self.root, {"query": "a", "max_results": "lots"})

    def test_slug_match_ranks_high(self):
        out = cq.tool_compathy_search(self.root, {"query": "alpha"})
        self.assertEqual(out["results"][0]["slug"], "alpha")


class TestCompathyListPages(WikiFixture):
    def test_lists_all(self):
        out = cq.tool_compathy_list_pages(self.root, {})
        slugs = {p["slug"] for p in out["pages"]}
        self.assertEqual(
            slugs,
            {"alpha", "beta", "claude", "readme-summary", "technical-patterns",
             "index", "log"},
        )

    def test_filter_by_entity(self):
        out = cq.tool_compathy_list_pages(self.root, {"page_type": "entity"})
        self.assertEqual([p["slug"] for p in out["pages"]], ["claude"])

    def test_filter_by_concept(self):
        out = cq.tool_compathy_list_pages(self.root, {"page_type": "concept"})
        self.assertEqual(
            sorted(p["slug"] for p in out["pages"]),
            ["alpha", "beta"],
        )

    def test_invalid_filter_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_list_pages(self.root, {"page_type": "banana"})


class TestCompathyLogRecent(WikiFixture):
    def test_returns_last_n(self):
        out = cq.tool_compathy_log_recent(self.root, {"n": 3})
        self.assertEqual(out["count"], 3)
        # Most recent first.
        self.assertIn("2026-04-04", out["entries"][0]["heading"])
        self.assertIn("2026-04-03", out["entries"][1]["heading"])
        self.assertIn("2026-04-02", out["entries"][2]["heading"])

    def test_default_n_is_10(self):
        out = cq.tool_compathy_log_recent(self.root, {})
        # Only 4 entries exist; default cap of 10 still returns all 4.
        self.assertEqual(out["count"], 4)

    def test_invalid_n_raises(self):
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_log_recent(self.root, {"n": 0})
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_log_recent(self.root, {"n": "two"})

    def test_missing_log_raises(self):
        (self.wiki / "log.md").unlink()
        with self.assertRaises(cq.ToolError):
            cq.tool_compathy_log_recent(self.root, {})


# ---------- JSON-RPC dispatcher tests ----------

class TestJsonRpcInitialize(WikiFixture):
    def test_initialize_returns_server_info(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": cq.PROTOCOL_VERSION}}
        resp = cq.handle_request(req, self.root)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("serverInfo", resp["result"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], cq.SERVER_NAME)
        self.assertEqual(resp["result"]["protocolVersion"], cq.PROTOCOL_VERSION)
        self.assertIn("tools", resp["result"]["capabilities"])


class TestJsonRpcToolsList(WikiFixture):
    def test_lists_five_tools_with_schemas(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp = cq.handle_request(req, self.root)
        tools = resp["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        self.assertEqual(
            names,
            sorted([
                "compathy_index", "compathy_get_page", "compathy_search",
                "compathy_list_pages", "compathy_log_recent",
            ]),
        )
        for t in tools:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")


class TestJsonRpcToolsCall(WikiFixture):
    def test_call_known_tool(self):
        req = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "compathy_index", "arguments": {}},
        }
        resp = cq.handle_request(req, self.root)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("content", resp["result"])
        self.assertIn("structuredContent", resp["result"])
        self.assertEqual(resp["result"]["structuredContent"]["slug"], "index")

    def test_unknown_tool_is_method_not_found(self):
        req = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "compathy_bogus", "arguments": {}},
        }
        resp = cq.handle_request(req, self.root)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], cq.ERR_METHOD_NOT_FOUND)

    def test_invalid_params_is_minus_32602(self):
        # Missing 'name' in tools/call params.
        req = {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"arguments": {}},
        }
        resp = cq.handle_request(req, self.root)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], cq.ERR_INVALID_PARAMS)

    def test_unknown_method_is_minus_32601(self):
        req = {"jsonrpc": "2.0", "id": 6, "method": "resources/list"}
        resp = cq.handle_request(req, self.root)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], cq.ERR_METHOD_NOT_FOUND)

    def test_tool_error_returned_as_isError(self):
        # ToolError (e.g. unknown slug) should come back as isError=True
        # inside a successful JSON-RPC result, not as a protocol error.
        req = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "compathy_get_page", "arguments": {"slug": "ghost"}},
        }
        resp = cq.handle_request(req, self.root)
        self.assertNotIn("error", resp)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("ghost", resp["result"]["content"][0]["text"])

    def test_notification_returns_none(self):
        # No 'id' field => notification, no response per JSON-RPC 2.0.
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = cq.handle_request(req, self.root)
        self.assertIsNone(resp)


# ---------- stdio loop tests ----------

class TestServeStdio(WikiFixture):
    def test_handles_initialize_then_tools_list(self):
        in_lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        fin = io.StringIO("\n".join(in_lines) + "\n")
        fout = io.StringIO()
        cq.serve_stdio(self.root, stdin=fin, stdout=fout)
        responses = [json.loads(l) for l in fout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["id"], 2)

    def test_malformed_json_yields_parse_error_and_continues(self):
        in_lines = [
            "not valid json {{{",
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "initialize"}),
        ]
        fin = io.StringIO("\n".join(in_lines) + "\n")
        fout = io.StringIO()
        cq.serve_stdio(self.root, stdin=fin, stdout=fout)
        responses = [json.loads(l) for l in fout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], cq.ERR_PARSE)
        self.assertEqual(responses[1]["id"], 9)


# ---------- CLI smoke mode tests ----------

class TestCliSmokeMode(WikiFixture):
    def _run(self, argv):
        # Capture stdout
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = cq.main(argv)
        finally:
            sys.stdout = old
        return rc, buf.getvalue()

    def test_index(self):
        rc, out = self._run(["--target", str(self.root), "--tool", "compathy_index"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["slug"], "index")

    def test_get_page(self):
        rc, out = self._run([
            "--target", str(self.root),
            "--tool", "compathy_get_page",
            "--args", json.dumps({"slug": "alpha"}),
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["slug"], "alpha")

    def test_search(self):
        rc, out = self._run([
            "--target", str(self.root),
            "--tool", "compathy_search",
            "--args", json.dumps({"query": "persona"}),
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertGreaterEqual(data["count"], 1)

    def test_list_pages(self):
        rc, out = self._run([
            "--target", str(self.root),
            "--tool", "compathy_list_pages",
            "--args", json.dumps({"page_type": "entity"}),
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual([p["slug"] for p in data["pages"]], ["claude"])

    def test_log_recent(self):
        rc, out = self._run([
            "--target", str(self.root),
            "--tool", "compathy_log_recent",
            "--args", json.dumps({"n": 2}),
        ])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["count"], 2)

    def test_tool_error_exit_code(self):
        rc, out = self._run([
            "--target", str(self.root),
            "--tool", "compathy_get_page",
            "--args", json.dumps({"slug": "ghost"}),
        ])
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertTrue(data["isError"])


if __name__ == "__main__":
    unittest.main()
