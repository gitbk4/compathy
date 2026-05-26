"""Tests for router_dryrun.py — v0 diagnostic for the router migration gate.

Pure-function tests cover parsing, median computation, ratio computation,
synthetic router rendering, and gate evaluation logic. Integration tests
exercise build_report against small synthetic wikis built with scaffold.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import router_dryrun  # noqa: E402
import scaffold  # noqa: E402
from paths import index_path, wiki_dir  # noqa: E402
from router_dryrun import (  # noqa: E402
    CELL_NAMES,
    GATE_ABSOLUTE_SAVINGS,
    GATE_CROSS_SECTION_RATIO,
    GATE_PAGE_COUNT_FLOOR,
    GATE_PERCENTAGE_SAVINGS,
    build_section_membership,
    compute_median_n,
    evaluate_gate,
    extract_section_block,
    format_synthetic_router,
    parse_index_sections,
)


def _git_init(root):
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(root), check=True, capture_output=True,
    )


def _write_page(wiki_root: Path, section: str, slug: str, body: str = ""):
    """Write a minimal valid wiki page at wiki/<section>/<slug>.md."""
    page = wiki_root / section / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"type: {'concept' if section == 'concepts' else section[:-1]}\n"
        "schema_version: 1\n"
        "---\n"
    )
    page.write_text(frontmatter + body, encoding="utf-8")


def _write_index(wiki_root: Path, sections: dict):
    """Write an index.md with the given canonical sections.

    sections is a dict like {"Concepts": ["slug-a", "slug-b"], ...}.
    """
    parts = [
        "---\ntype: index\nschema_version: 1\n---\n\n",
        "# Wiki Index\n\n",
    ]
    for heading in ("Concepts", "Entities", "Summaries", "Patterns"):
        parts.append(f"## {heading}\n\n")
        for slug in sections.get(heading, []):
            parts.append(f"- [[{slug}]] — summary\n")
        parts.append("\n")
    (wiki_root / "index.md").write_text("".join(parts), encoding="utf-8")


# ---------------- Pure-function tests ----------------


class TestParseIndexSections(unittest.TestCase):
    def test_canonical_four_sections(self):
        text = (
            "# Wiki Index\n\n"
            "## Concepts\n- [[concept-a]] — x\n- [[concept-b]] — y\n\n"
            "## Entities\n- [[entity-a]] — z\n\n"
            "## Summaries\n- [[summary-a]] — w\n\n"
            "## Patterns\n- [[pattern-a]] — v\n"
        )
        sections, warnings = parse_index_sections(text)
        self.assertEqual(sections["concepts"], ["concept-a", "concept-b"])
        self.assertEqual(sections["entities"], ["entity-a"])
        self.assertEqual(sections["summaries"], ["summary-a"])
        self.assertEqual(sections["patterns"], ["pattern-a"])
        self.assertEqual(warnings, [])

    def test_alias_with_pipe(self):
        text = "## Concepts\n- [[my-slug|My Display Name]] — x\n"
        sections, _ = parse_index_sections(text)
        self.assertEqual(sections["concepts"], ["my-slug"])

    def test_missing_section(self):
        """A wiki with only Concepts and Patterns (no Entities/Summaries)
        parses cleanly; the missing sections are empty lists."""
        text = (
            "## Concepts\n- [[a]] — x\n\n"
            "## Patterns\n- [[b]] — y\n"
        )
        sections, warnings = parse_index_sections(text)
        self.assertEqual(sections["concepts"], ["a"])
        self.assertEqual(sections["entities"], [])
        self.assertEqual(sections["summaries"], [])
        self.assertEqual(sections["patterns"], ["b"])
        self.assertEqual(warnings, [])

    def test_extra_non_canonical_section_warned(self):
        text = (
            "## Concepts\n- [[a]] — x\n\n"
            "## Glossary\n- [[g]] — extra\n\n"
            "## Patterns\n- [[p]] — y\n"
        )
        sections, warnings = parse_index_sections(text)
        self.assertEqual(sections["concepts"], ["a"])
        self.assertEqual(sections["patterns"], ["p"])
        # "Glossary" is not canonical; its bullets are ignored and
        # a warning records the heading.
        self.assertTrue(
            any("Glossary" in w for w in warnings),
            f"expected Glossary warning, got: {warnings}",
        )

    def test_placeholder_lines_skipped(self):
        text = (
            "## Concepts\n_(no entries yet)_\n\n"
            "## Patterns\n- [[p]] — y\n"
        )
        sections, _ = parse_index_sections(text)
        self.assertEqual(sections["concepts"], [])
        self.assertEqual(sections["patterns"], ["p"])

    def test_empty_index(self):
        sections, warnings = parse_index_sections("")
        for name in CELL_NAMES:
            self.assertEqual(sections[name], [])
        self.assertEqual(warnings, [])


class TestSyntheticRouter(unittest.TestCase):
    def test_router_lists_all_four_cells(self):
        sections = {
            "concepts": ["a", "b"],
            "entities": ["c"],
            "summaries": [],
            "patterns": ["d"],
        }
        router = format_synthetic_router(sections)
        self.assertIn("[Concepts](cells/concepts.md)", router)
        self.assertIn("[Entities](cells/entities.md)", router)
        self.assertIn("[Summaries](cells/summaries.md)", router)
        self.assertIn("[Patterns](cells/patterns.md)", router)
        self.assertIn("— 2 pages", router)
        self.assertIn("— 1 page", router)
        self.assertIn("— 0 pages", router)

    def test_router_uses_markdown_links_not_wiki_links(self):
        """Verifies the reframe is honored: router uses [Title](path)
        Markdown links, NOT [[wiki-link]] syntax. The reframe explicitly
        dropped [[cell:X]] syntax in favor of plain Markdown links."""
        router = format_synthetic_router(
            {name: [] for name in CELL_NAMES}
        )
        self.assertNotIn("[[cell:", router)
        self.assertNotIn("[[concepts]]", router)


class TestExtractSectionBlock(unittest.TestCase):
    def test_extracts_only_target_section(self):
        text = (
            "## Concepts\n- a\n- b\n\n"
            "## Entities\n- c\n- d\n"
        )
        concepts_block = extract_section_block(text, "concepts")
        self.assertIn("- a", concepts_block)
        self.assertIn("- b", concepts_block)
        self.assertNotIn("- c", concepts_block)
        self.assertNotIn("- d", concepts_block)

    def test_missing_section_returns_empty(self):
        text = "## Concepts\n- a\n"
        self.assertEqual(extract_section_block(text, "patterns"), "")


class TestComputeMedianN(unittest.TestCase):
    def test_empty_input_defaults_to_one(self):
        self.assertEqual(compute_median_n([]), 1)

    def test_typical_input(self):
        # Median of [1, 1, 2, 2, 3] is 2 (median_high returns 2)
        self.assertEqual(compute_median_n([1, 1, 2, 2, 3]), 2)

    def test_cap_at_number_of_cells(self):
        """If every page references all four cells, median_n is capped
        at len(CELL_NAMES). Prevents 'router + 5 sections' from being
        proposed when only 4 exist."""
        breadths = [10, 10, 10, 10, 10]  # impossible in practice but cap
        self.assertEqual(compute_median_n(breadths), len(CELL_NAMES))


class TestEvaluateGate(unittest.TestCase):
    def _make_metrics(self, pct=0.5, absv=1000):
        return {
            "percentage_savings": pct,
            "absolute_savings": absv,
        }

    def test_no_encoder_suppresses_verdict(self):
        gate = evaluate_gate(
            self._make_metrics(), 100, 0.1, encoder=None
        )
        self.assertFalse(gate["evaluated"])
        self.assertIsNone(gate["passed"])
        self.assertTrue(any("tiktoken" in r for r in gate["reasons"]))

    def test_below_page_floor_suppresses_verdict(self):
        gate = evaluate_gate(
            self._make_metrics(), 30, 0.1, encoder="fake-encoder"
        )
        self.assertFalse(gate["evaluated"])
        self.assertIsNone(gate["passed"])
        self.assertTrue(any("80" in r for r in gate["reasons"]))

    def test_passing_gate(self):
        gate = evaluate_gate(
            self._make_metrics(pct=0.5, absv=1000),
            page_count=100,
            cross_section_ratio=0.2,
            encoder="fake",
        )
        self.assertTrue(gate["evaluated"])
        self.assertTrue(gate["passed"])

    def test_fail_on_percentage(self):
        gate = evaluate_gate(
            self._make_metrics(pct=0.3, absv=1000),  # below 40%
            page_count=100,
            cross_section_ratio=0.2,
            encoder="fake",
        )
        self.assertTrue(gate["evaluated"])
        self.assertFalse(gate["passed"])
        self.assertTrue(
            any("percentage" in r for r in gate["reasons"])
        )

    def test_fail_on_absolute_savings(self):
        gate = evaluate_gate(
            self._make_metrics(pct=0.5, absv=400),  # below 500
            page_count=100,
            cross_section_ratio=0.2,
            encoder="fake",
        )
        self.assertFalse(gate["passed"])
        self.assertTrue(any("absolute" in r for r in gate["reasons"]))

    def test_fail_on_cross_section_ratio(self):
        gate = evaluate_gate(
            self._make_metrics(pct=0.5, absv=1000),
            page_count=100,
            cross_section_ratio=0.4,  # above 25%
            encoder="fake",
        )
        self.assertFalse(gate["passed"])
        self.assertTrue(any("cross-section" in r for r in gate["reasons"]))

    def test_gate_constants_match_design(self):
        """Sanity check: if any of these change, the design doc gate
        criteria must change in lockstep."""
        self.assertEqual(GATE_PERCENTAGE_SAVINGS, 0.40)
        self.assertEqual(GATE_ABSOLUTE_SAVINGS, 500)
        self.assertEqual(GATE_CROSS_SECTION_RATIO, 0.25)
        self.assertEqual(GATE_PAGE_COUNT_FLOOR, 80)


# ---------------- Integration tests against synthetic wikis ----------------


class TestBuildReportIntegration(unittest.TestCase):
    def test_small_wiki_gate_not_evaluated(self):
        """A 4-page wiki is below the 80-page floor. Numbers print but
        the gate does not produce a verdict."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="test")
            wiki = wiki_dir(root)
            _write_page(wiki, "concepts", "concept-a")
            _write_page(wiki, "concepts", "concept-b")
            _write_page(wiki, "entities", "entity-a", "see [[concept-a]]")
            _write_page(wiki, "patterns", "pattern-a")
            _write_index(wiki, {
                "Concepts": ["concept-a", "concept-b"],
                "Entities": ["entity-a"],
                "Patterns": ["pattern-a"],
            })
            report = router_dryrun.build_report(root)
            self.assertEqual(report["page_count"], 4)
            self.assertEqual(report["sections"]["concepts"], 2)
            self.assertEqual(report["sections"]["entities"], 1)
            self.assertEqual(report["sections"]["summaries"], 0)
            self.assertEqual(report["sections"]["patterns"], 1)
            self.assertFalse(report["gate"]["evaluated"])
            self.assertIsNone(report["gate"]["passed"])

    def test_cross_section_ratio_computed(self):
        """One page in 'entities' links to one page in 'concepts' →
        1 cross-section out of 1 total = 1.0 ratio."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="test")
            wiki = wiki_dir(root)
            _write_page(wiki, "concepts", "concept-a")
            _write_page(wiki, "entities", "entity-a", "see [[concept-a]]")
            _write_index(wiki, {
                "Concepts": ["concept-a"],
                "Entities": ["entity-a"],
            })
            report = router_dryrun.build_report(root)
            self.assertEqual(report["cross_section_count"], 1)
            self.assertEqual(report["total_backlink_count"], 1)
            self.assertEqual(report["cross_section_ratio"], 1.0)

    def test_intra_section_links_not_counted_as_cross(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="test")
            wiki = wiki_dir(root)
            _write_page(wiki, "concepts", "concept-a", "see [[concept-b]]")
            _write_page(wiki, "concepts", "concept-b")
            _write_index(wiki, {
                "Concepts": ["concept-a", "concept-b"],
            })
            report = router_dryrun.build_report(root)
            self.assertEqual(report["cross_section_count"], 0)
            self.assertEqual(report["total_backlink_count"], 1)
            self.assertEqual(report["cross_section_ratio"], 0.0)

    def test_missing_index_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            # No scaffold — no context/wiki/index.md
            with self.assertRaises(FileNotFoundError):
                router_dryrun.build_report(root)

    def test_token_counter_field_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="test")
            wiki = wiki_dir(root)
            _write_page(wiki, "concepts", "concept-a")
            _write_index(wiki, {"Concepts": ["concept-a"]})
            report = router_dryrun.build_report(root)
            self.assertIn(report["token_counter"], ("tiktoken", "len-heuristic"))

    def test_json_output_via_main(self):
        """Smoke-test the CLI: --format json --target X produces parseable JSON."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _git_init(root)
            scaffold.create_structure(root, project_name="test")
            wiki = wiki_dir(root)
            _write_page(wiki, "concepts", "concept-a")
            _write_index(wiki, {"Concepts": ["concept-a"]})

            # Capture stdout via subprocess so we exercise the real CLI path
            r = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "router_dryrun.py"),
                    "--target", str(root),
                    "--format", "json",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(
                r.returncode, 0,
                f"stderr: {r.stderr}\nstdout: {r.stdout[:500]}",
            )
            data = json.loads(r.stdout)
            self.assertIn("token_counter", data)
            self.assertIn("gate", data)
            self.assertIn("flat_tokens", data)


class TestBuildSectionMembership(unittest.TestCase):
    def test_round_trips(self):
        sections = {
            "concepts": ["a", "b"],
            "entities": ["c"],
            "summaries": [],
            "patterns": ["d"],
        }
        membership = build_section_membership(sections)
        self.assertEqual(membership["a"], "concepts")
        self.assertEqual(membership["c"], "entities")
        self.assertEqual(membership["d"], "patterns")
        self.assertNotIn("nonexistent", membership)


if __name__ == "__main__":
    unittest.main()
