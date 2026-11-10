"""Tests for discovery.py: sentinel-fenced markdown upsert + .mcp.json upsert."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discovery  # noqa: E402


class TestRenderContextSection(unittest.TestCase):
    def test_includes_both_sentinels(self):
        body = discovery.render_context_section("demo")
        self.assertIn(discovery.SENTINEL_START, body)
        self.assertIn(discovery.SENTINEL_END, body)
        # START must precede END.
        self.assertLess(
            body.index(discovery.SENTINEL_START),
            body.index(discovery.SENTINEL_END),
        )

    def test_lists_key_entry_points(self):
        body = discovery.render_context_section("demo")
        self.assertIn("context/wiki/index.md", body)
        self.assertIn("context/schema.md", body)
        self.assertIn("context/wiki/entities/", body)
        self.assertIn("context/wiki/concepts/", body)
        self.assertIn("context/wiki/patterns/", body)
        self.assertIn("context/wiki/summaries/", body)
        self.assertIn("context/wiki/log.md", body)
        self.assertIn("flat-YAML", body)

    def test_no_em_dashes(self):
        body = discovery.render_context_section("demo")
        # Repo convention: no em-dashes anywhere.
        self.assertNotIn("—", body)


class TestUpsertSection(unittest.TestCase):
    def test_creates_file_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "CLAUDE.md"
            body = discovery.render_context_section("demo")
            status = discovery.upsert_section(path, body)
            self.assertEqual(status, "created")
            self.assertTrue(path.is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn(discovery.SENTINEL_START, text)
            self.assertIn(discovery.SENTINEL_END, text)

    def test_appends_when_no_sentinels(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "CLAUDE.md"
            existing = "# My project\n\nSome existing notes.\n"
            path.write_text(existing, encoding="utf-8")
            body = discovery.render_context_section("demo")
            status = discovery.upsert_section(path, body)
            self.assertEqual(status, "appended")
            text = path.read_text(encoding="utf-8")
            # Existing content preserved at the top.
            self.assertTrue(text.startswith("# My project"))
            self.assertIn("Some existing notes.", text)
            # New section appended.
            self.assertIn(discovery.SENTINEL_START, text)
            self.assertIn(discovery.SENTINEL_END, text)

    def test_replaces_when_well_formed_sentinels(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "CLAUDE.md"
            before = "# My project\n\nIntro paragraph.\n\n"
            after = "\n\n## Other section\n\nKept verbatim.\n"
            old_body = (
                discovery.SENTINEL_START
                + "\n## Project context\n\nstale body\n"
                + discovery.SENTINEL_END
            )
            path.write_text(before + old_body + after, encoding="utf-8")

            new_body = discovery.render_context_section("demo")
            status = discovery.upsert_section(path, new_body)
            self.assertEqual(status, "replaced")

            text = path.read_text(encoding="utf-8")
            # Surrounding content preserved verbatim.
            self.assertTrue(text.startswith(before))
            self.assertTrue(text.endswith(after))
            # Stale body is gone, new body is present.
            self.assertNotIn("stale body", text)
            self.assertIn("Karpathy-style wiki", text)

    def test_round_trip_no_duplicate_sections(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "CLAUDE.md"
            body = discovery.render_context_section("demo")
            discovery.upsert_section(path, body)
            first = path.read_text(encoding="utf-8")
            discovery.upsert_section(path, body)
            second = path.read_text(encoding="utf-8")
            self.assertEqual(first, second)
            # And only one sentinel pair in the file.
            self.assertEqual(second.count(discovery.SENTINEL_START), 1)
            self.assertEqual(second.count(discovery.SENTINEL_END), 1)

    def test_malformed_sentinels_falls_back_to_append(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "CLAUDE.md"
            existing = (
                "# My project\n\n"
                + discovery.SENTINEL_START
                + "\n## stale, no end sentinel\n"
            )
            path.write_text(existing, encoding="utf-8")
            body = discovery.render_context_section("demo")
            status = discovery.upsert_section(path, body)
            self.assertEqual(status, "appended")
            text = path.read_text(encoding="utf-8")
            # Original malformed section still there (we don't touch it).
            self.assertIn("stale, no end sentinel", text)
            # Our new section is present too (with both sentinels).
            self.assertIn(discovery.SENTINEL_END, text)
            # SENTINEL_START appears twice now (the malformed one + ours).
            self.assertEqual(text.count(discovery.SENTINEL_START), 2)
            # SENTINEL_END appears exactly once (only ours).
            self.assertEqual(text.count(discovery.SENTINEL_END), 1)


class TestWriteDiscoveryBreadcrumbs(unittest.TestCase):
    def test_happy_path_creates_both_files(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            result = discovery.write_discovery_breadcrumbs(target, "demo")
            self.assertEqual(
                result,
                {
                    "claude_md": "created",
                    "agents_md": "created",
                    "gemini_md": "created",
                    "readme_md": "created",
                    "mcp_json": "created",
                },
            )
            self.assertTrue((target / "CLAUDE.md").is_file())
            self.assertTrue((target / "AGENTS.md").is_file())
            self.assertTrue((target / "README.md").is_file())
            # Gemini CLI reads GEMINI.md, which imports AGENTS.md instead of repeating it.
            gemini = (target / "GEMINI.md").read_text(encoding="utf-8")
            self.assertIn("@./AGENTS.md", gemini)
            self.assertNotIn("Project context", gemini)
            # All of them carry the sentinel pair, so re-runs stay idempotent.
            for fname in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "README.md"):
                text = (target / fname).read_text(encoding="utf-8")
                self.assertIn(discovery.SENTINEL_START, text)
                self.assertIn(discovery.SENTINEL_END, text)

    def test_existing_claude_md_replaced_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            preface = "# My project\n\nUser-written notes above the section.\n\n"
            old_body = (
                discovery.SENTINEL_START
                + "\nstale section body\n"
                + discovery.SENTINEL_END
                + "\n"
            )
            tail = "\n## User-written tail\n\nKept verbatim.\n"
            (target / "CLAUDE.md").write_text(preface + old_body + tail, encoding="utf-8")
            # README.md does not exist yet.
            result = discovery.write_discovery_breadcrumbs(target, "demo")
            self.assertEqual(result["claude_md"], "replaced")
            self.assertEqual(result["readme_md"], "created")

            claude_text = (target / "CLAUDE.md").read_text(encoding="utf-8")
            # User content above and below preserved.
            self.assertTrue(claude_text.startswith(preface))
            self.assertTrue(claude_text.endswith(tail))
            # Stale body gone.
            self.assertNotIn("stale section body", claude_text)
            # New body present.
            self.assertIn("Karpathy-style wiki", claude_text)

    def test_pre_existing_content_above_and_below_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            preface = (
                "# My project\n\n"
                "## Setup\n\n"
                "Run `make install`.\n\n"
            )
            old_body = (
                discovery.SENTINEL_START
                + "\n## Project context\n\nstale\n"
                + discovery.SENTINEL_END
            )
            tail = "\n\n## License\n\nMIT.\n"
            (target / "CLAUDE.md").write_text(
                preface + old_body + tail, encoding="utf-8"
            )

            result = discovery.write_discovery_breadcrumbs(target, "demo")
            self.assertEqual(result["claude_md"], "replaced")

            text = (target / "CLAUDE.md").read_text(encoding="utf-8")
            # Verbatim above.
            self.assertIn("Run `make install`.", text)
            self.assertTrue(text.startswith(preface))
            # Verbatim below.
            self.assertIn("MIT.", text)
            self.assertTrue(text.endswith(tail))

    def test_returns_status_dict_shape(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            # README.md exists without sentinels; CLAUDE.md does not exist.
            (target / "README.md").write_text("# Existing readme\n", encoding="utf-8")
            result = discovery.write_discovery_breadcrumbs(target, "demo")
            self.assertEqual(
                set(result.keys()), {"claude_md", "agents_md", "gemini_md", "readme_md", "mcp_json"}
            )
            self.assertEqual(result["claude_md"], "created")
            self.assertEqual(result["agents_md"], "created")
            self.assertEqual(result["readme_md"], "appended")
            # .mcp.json didn't exist either; should be created.
            self.assertEqual(result["mcp_json"], "created")

    def test_mcp_entry_points_at_this_install(self):
        # Agent-neutral: the server path is the script next to discovery.py,
        # not a Claude Code skills folder that other agents do not have.
        self.assertEqual(discovery.COMPATHY_QUERY_PATH,
                         Path(discovery.__file__).resolve().parent / "compathy_query.py")
        self.assertTrue(discovery.COMPATHY_QUERY_PATH.is_file())

    def test_federation_block_tells_any_agent_to_check_freshness(self):
        lineage = {"layers": [{"id": "acme/core", "role": "team", "pin": "a" * 40, "path": "context"}],
                   "persona": "acme/core/sales"}
        text = discovery.render_context_section("demo", lineage, None)
        self.assertIn("freshness --target . --format line", text)
        self.assertIn("persona.py", text)


# ---------------------------------------------------------------------------
# .mcp.json upsert (cross-lane integration with compathy-mcp)
# ---------------------------------------------------------------------------


class TestRenderMcpEntry(unittest.TestCase):
    def test_returns_valid_mcp_server_dict(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            entry = discovery.render_mcp_entry(target)
            self.assertEqual(entry["command"], "python3")
            self.assertEqual(entry["transport"], "stdio")
            self.assertIsInstance(entry["args"], list)
            self.assertIn("--target", entry["args"])
            target_idx = entry["args"].index("--target")
            self.assertEqual(entry["args"][target_idx + 1], str(target.resolve()))

    def test_args_reference_global_install_path(self):
        with tempfile.TemporaryDirectory() as td:
            entry = discovery.render_mcp_entry(Path(td))
            self.assertEqual(entry["args"][0], str(discovery.COMPATHY_QUERY_PATH))


class TestUpsertMcpJson(unittest.TestCase):
    def test_creates_file_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            self.assertEqual(discovery.upsert_mcp_json(target), "created")
            data = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertIn("mcpServers", data)
            self.assertIn("compathy-wiki", data["mcpServers"])
            self.assertEqual(
                data["mcpServers"]["compathy-wiki"]["transport"], "stdio"
            )

    def test_adds_to_existing_file_without_mcp_servers_key(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".mcp.json").write_text(
                json.dumps({"someOtherKey": "value"}), encoding="utf-8"
            )
            self.assertEqual(discovery.upsert_mcp_json(target), "added")
            data = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(data["someOtherKey"], "value")
            self.assertIn("compathy-wiki", data["mcpServers"])

    def test_preserves_other_servers_when_adding_compathy_wiki(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            existing = {
                "mcpServers": {
                    "other-server": {
                        "command": "node",
                        "args": ["/x/server.js"],
                        "transport": "stdio",
                    }
                }
            }
            (target / ".mcp.json").write_text(json.dumps(existing), encoding="utf-8")
            self.assertEqual(discovery.upsert_mcp_json(target), "added")
            data = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(
                data["mcpServers"]["other-server"],
                existing["mcpServers"]["other-server"],
            )
            self.assertIn("compathy-wiki", data["mcpServers"])

    def test_replaces_prior_compathy_wiki_entry(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            old = {
                "mcpServers": {
                    "compathy-wiki": {
                        "command": "old-command",
                        "args": ["--target", "/wrong/path"],
                        "transport": "stdio",
                    }
                }
            }
            (target / ".mcp.json").write_text(json.dumps(old), encoding="utf-8")
            self.assertEqual(discovery.upsert_mcp_json(target), "replaced")
            data = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(data["mcpServers"]["compathy-wiki"]["command"], "python3")

    def test_skips_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            malformed = "{not valid json at all"
            (target / ".mcp.json").write_text(malformed, encoding="utf-8")
            self.assertEqual(discovery.upsert_mcp_json(target), "skipped-malformed")
            self.assertEqual(
                (target / ".mcp.json").read_text(encoding="utf-8"), malformed
            )

    def test_skips_non_object_root(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / ".mcp.json").write_text("[]", encoding="utf-8")
            self.assertEqual(discovery.upsert_mcp_json(target), "skipped-malformed")
            self.assertEqual((target / ".mcp.json").read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
