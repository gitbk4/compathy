#!/usr/bin/env python3
"""v0 diagnostic: measure router-vs-flat-index token savings and
cross-section backlink ratio against a real wiki. Read-only. No changes
to wiki, lint, SKILL, or MCP code paths.

Gate verdict (kill criterion) for v1.0 to ship: on a wiki of >= 80 pages,
  - percentage savings on router + median-N sections vs flat index >= 40%
  - absolute savings per session >= 500 tokens
  - cross-section backlink ratio <= 25%

If any condition fails, v1.0+ is dead. Document and stop.

Requires tiktoken to evaluate the gate (per A1 from /plan-eng-review:
a kill-criterion gate has to be reproducible across machines, and a
naive len(text)//4 heuristic can disagree with real BPE tokens by
20-30%). Without tiktoken, --len-heuristic mode prints informational
numbers but the gate verdict is suppressed (passed=null).

Usage:
  python scripts/router_dryrun.py --target .
  python scripts/router_dryrun.py --target . --format json
  python scripts/router_dryrun.py --target . --out router_metrics.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import median_high

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
import lint  # noqa: E402  (reuse parse_backlinks, iter_wiki_pages, read_page)
from paths import WIKI_SUBDIRS, index_path, wiki_dir  # noqa: E402

# The four-cell partition this diagnostic measured. Mirrors the
# WIKI_SUBDIRS compathy already ships under wiki/. Defined locally so
# the diagnostic stays self-contained even after router-mode scaffolding
# is removed from paths.py.
CELL_NAMES = WIKI_SUBDIRS

GATE_PERCENTAGE_SAVINGS = 0.40
GATE_ABSOLUTE_SAVINGS = 500
GATE_CROSS_SECTION_RATIO = 0.25
GATE_PAGE_COUNT_FLOOR = 80

# Canonical H2 section names in index.md, in order. Lowercased for
# matching; the canonical title-case version is used in router output.
CANONICAL_SECTIONS = ("Concepts", "Entities", "Summaries", "Patterns")

# Match an H2 heading. Captures the heading text after "## ".
H2_RE = re.compile(r"^##\s+(.+?)\s*$")

# Match a wiki-style backlink slug in an index bullet (used to extract
# the page slug from "- [[my-slug]] — one-line summary"). The same regex
# lint.parse_backlinks uses internally.
BACKLINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def parse_index_sections(index_text: str) -> tuple[dict, list]:
    """Parse index.md into a dict of section_name -> list[page_slug].

    Returns (sections, non_canonical_warnings). sections keys are the
    canonical lowercased names that match CELL_NAMES. Non-canonical
    H2 sections (renamed, extra) are recorded as warnings but their
    bullets are ignored. The body text under "## How to use this wiki"
    or any other non-canonical heading is treated as commentary, not
    pages.

    Bullets that fail to contain a [[slug]] are skipped silently (e.g.
    "_(no entries yet)_" placeholders from a freshly-scaffolded wiki).
    """
    sections: dict[str, list[str]] = {name: [] for name in CELL_NAMES}
    warnings: list[str] = []
    current_section: str | None = None

    for raw_line in index_text.splitlines():
        line = raw_line.rstrip()
        h2 = H2_RE.match(line)
        if h2:
            heading = h2.group(1).strip()
            normalized = heading.lower()
            if heading in CANONICAL_SECTIONS or normalized in CELL_NAMES:
                current_section = normalized
            else:
                warnings.append(
                    f"non-canonical H2 section ignored: {heading!r}"
                )
                current_section = None
            continue

        if current_section is None:
            continue

        # Find the first [[slug]] in this line (bullet or otherwise).
        m = BACKLINK_RE.search(line)
        if not m:
            continue
        token = m.group(1).strip()
        slug = token.split("|", 1)[0].strip()
        if slug:
            sections[current_section].append(slug)

    return sections, warnings


def build_section_membership(sections: dict) -> dict:
    """Return dict[slug -> section_name] for every slug appearing in index."""
    membership: dict[str, str] = {}
    for section, slugs in sections.items():
        for slug in slugs:
            membership[slug] = section
    return membership


def compute_per_page_breadth(wiki_root: Path, membership: dict) -> list:
    """For each page on disk, compute (distinct outbound sections + 1).

    The +1 counts the page's own section (since a session that touches
    this page necessarily loads its own section index too). Backlink
    targets that are not in the index (broken links) are skipped, not
    counted.

    Pages outside the canonical four subdirs are ignored.
    """
    breadths = []
    for slug, page_path in lint.iter_wiki_pages(wiki_root):
        own_section = membership.get(slug)
        if own_section is None:
            # Page exists on disk but isn't in any index section. Lint
            # would flag this as an orphan-page; we conservatively count
            # it as a single-section session.
            continue

        _fm, body, err = lint.read_page(page_path)
        if err is not None or body is None:
            continue

        distinct_sections = {own_section}
        for target_slug in lint.parse_backlinks(body):
            tgt_section = membership.get(target_slug)
            if tgt_section is not None:
                distinct_sections.add(tgt_section)
        breadths.append(len(distinct_sections))
    return breadths


def compute_cross_section_ratio(wiki_root: Path, membership: dict) -> tuple:
    """Compute (cross_section_count, total_count, ratio).

    Walks every page; for each outbound [[slug]] backlink, look up the
    section the target lives in. If the target's section differs from
    the source page's section, it's a cross-section link. Backlink
    targets not in the index are skipped (they would be broken links
    that lint catches separately).

    Returns (0, 0, 0.0) for a wiki with no resolvable backlinks.
    """
    cross = 0
    total = 0
    for slug, page_path in lint.iter_wiki_pages(wiki_root):
        own_section = membership.get(slug)
        if own_section is None:
            continue
        _fm, body, err = lint.read_page(page_path)
        if err is not None or body is None:
            continue
        for target_slug in lint.parse_backlinks(body):
            tgt_section = membership.get(target_slug)
            if tgt_section is None:
                continue
            total += 1
            if tgt_section != own_section:
                cross += 1
    ratio = (cross / total) if total > 0 else 0.0
    return cross, total, ratio


def compute_median_n(breadths: list) -> int:
    """Compute the integer median session breadth, capped at the number
    of canonical cells.

    Uses statistics.median_high so the result is one of the actual
    integer data points (no rounding ambiguity). Returns 1 for an empty
    or absent input (a wiki with no resolvable cross-section links
    behaves like every session is single-cell)."""
    if not breadths:
        return 1
    n = median_high(breadths)
    return min(n, len(CELL_NAMES))


def format_synthetic_router(sections: dict) -> str:
    """Build the synthetic router body that would replace index.md in
    router mode. Four Markdown links, one per cell, with page counts.

    This is the router shape v1.0 actually ships, so the v0 measurement
    is honest about what's compared.
    """
    lines = []
    for name in CELL_NAMES:
        count = len(sections.get(name, []))
        title = name.capitalize()
        lines.append(
            f"- [{title}](cells/{name}.md) "
            f"— {count} page{'s' if count != 1 else ''}"
        )
    return "\n".join(lines) + "\n"


def extract_section_block(index_text: str, target_section: str) -> str:
    """Return the slice of index_text under the H2 heading for the
    target section (the bullets only, no heading).

    Used to estimate "router + this section's index" token cost.
    """
    canonical_title = next(
        (s for s in CANONICAL_SECTIONS if s.lower() == target_section),
        None,
    )
    if canonical_title is None:
        return ""

    lines = index_text.splitlines(keepends=True)
    capturing = False
    out: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        h2 = H2_RE.match(stripped)
        if h2:
            heading = h2.group(1).strip()
            if heading == canonical_title:
                capturing = True
                continue
            if capturing:
                break
            continue
        if capturing:
            out.append(line)
    return "".join(out)


def _get_tiktoken_encoder():
    """Return a tiktoken encoder or None if tiktoken is unavailable."""
    try:
        import tiktoken  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # pylint: disable=broad-except
        return None


def count_tokens(text: str, encoder) -> int:
    """Count tokens via tiktoken if encoder is provided, else use the
    len(text)//4 heuristic. The heuristic is informational only and is
    NOT acceptable for gate evaluation (per A1)."""
    if encoder is not None:
        return len(encoder.encode(text))
    return max(1, len(text) // 4)


def compute_metrics(
    index_text: str,
    sections: dict,
    median_n: int,
    encoder,
) -> dict:
    """Compute token costs across all four scenarios."""
    flat_tokens = count_tokens(index_text, encoder)
    router_body = format_synthetic_router(sections)
    router_tokens = count_tokens(router_body, encoder)

    # Sort sections by page count, descending. "router + one most
    # relevant" picks the largest; "router + median-N" picks the N
    # largest (conservative — worst case for measured savings).
    section_sizes = sorted(
        sections.items(), key=lambda kv: len(kv[1]), reverse=True
    )

    largest_section_name = section_sizes[0][0] if section_sizes else None
    if largest_section_name:
        block = extract_section_block(index_text, largest_section_name)
        router_plus_one_tokens = router_tokens + count_tokens(block, encoder)
    else:
        router_plus_one_tokens = router_tokens

    median_n_sections = [
        name for name, _slugs in section_sizes[:median_n]
    ]
    median_block_tokens = sum(
        count_tokens(extract_section_block(index_text, s), encoder)
        for s in median_n_sections
    )
    router_plus_median_tokens = router_tokens + median_block_tokens

    absolute_savings = flat_tokens - router_plus_median_tokens
    percentage_savings = (
        (absolute_savings / flat_tokens) if flat_tokens > 0 else 0.0
    )

    return {
        "flat_tokens": flat_tokens,
        "router_tokens": router_tokens,
        "router_plus_one_tokens": router_plus_one_tokens,
        "router_plus_median_tokens": router_plus_median_tokens,
        "absolute_savings": absolute_savings,
        "percentage_savings": percentage_savings,
        "median_n_sections": median_n_sections,
    }


def evaluate_gate(
    metrics: dict,
    page_count: int,
    cross_section_ratio: float,
    encoder,
) -> dict:
    """Return {evaluated, passed, reasons}.

    The gate is evaluated only when tiktoken is available (per A1) and
    page_count >= GATE_PAGE_COUNT_FLOOR. Otherwise evaluated=False and
    passed=None (no verdict).
    """
    reasons: list[str] = []
    if encoder is None:
        reasons.append(
            "tiktoken not installed; gate verdict suppressed. Install "
            "tiktoken to evaluate."
        )
        return {"evaluated": False, "passed": None, "reasons": reasons}
    if page_count < GATE_PAGE_COUNT_FLOOR:
        reasons.append(
            f"wiki has {page_count} pages; gate requires "
            f">= {GATE_PAGE_COUNT_FLOOR}. Numbers above are smoke-test "
            "only, not a gate verdict."
        )
        return {"evaluated": False, "passed": None, "reasons": reasons}

    pct = metrics["percentage_savings"]
    absv = metrics["absolute_savings"]

    failures = []
    if pct < GATE_PERCENTAGE_SAVINGS:
        failures.append(
            f"percentage savings {pct:.1%} < required "
            f"{GATE_PERCENTAGE_SAVINGS:.0%}"
        )
    if absv < GATE_ABSOLUTE_SAVINGS:
        failures.append(
            f"absolute savings {absv} tokens < required "
            f"{GATE_ABSOLUTE_SAVINGS}"
        )
    if cross_section_ratio > GATE_CROSS_SECTION_RATIO:
        failures.append(
            f"cross-section backlink ratio {cross_section_ratio:.1%} > "
            f"max {GATE_CROSS_SECTION_RATIO:.0%}"
        )

    if failures:
        return {"evaluated": True, "passed": False, "reasons": failures}
    return {
        "evaluated": True,
        "passed": True,
        "reasons": [
            f"percentage savings {pct:.1%} >= {GATE_PERCENTAGE_SAVINGS:.0%}",
            f"absolute savings {absv} >= {GATE_ABSOLUTE_SAVINGS}",
            f"cross-section ratio {cross_section_ratio:.1%} <= "
            f"{GATE_CROSS_SECTION_RATIO:.0%}",
        ],
    }


def build_report(target: Path) -> dict:
    """Run the full dry-run measurement against the wiki at target and
    return the structured result. Does not write any files."""
    idx_path = index_path(target)
    if not idx_path.exists():
        raise FileNotFoundError(
            f"index.md not found at {idx_path}. Run /compathy first to "
            "scaffold and compile a wiki."
        )
    index_text = idx_path.read_text(encoding="utf-8")
    sections, non_canonical_warnings = parse_index_sections(index_text)
    membership = build_section_membership(sections)

    wiki_root = wiki_dir(target)
    breadths = compute_per_page_breadth(wiki_root, membership)
    median_n = compute_median_n(breadths)
    cross, total, ratio = compute_cross_section_ratio(wiki_root, membership)

    encoder = _get_tiktoken_encoder()
    metrics = compute_metrics(index_text, sections, median_n, encoder)

    # Page count = pages on disk in canonical subdirs (matches lint's
    # iter_wiki_pages). NOT the count from index.md, since the goal is
    # to evaluate against actual wiki size.
    page_count = sum(1 for _slug, _p in lint.iter_wiki_pages(wiki_root))

    gate = evaluate_gate(metrics, page_count, ratio, encoder)

    return {
        "token_counter": "tiktoken" if encoder is not None else "len-heuristic",
        "page_count": page_count,
        "sections": {name: len(slugs) for name, slugs in sections.items()},
        "median_n": median_n,
        "cross_section_count": cross,
        "total_backlink_count": total,
        "cross_section_ratio": ratio,
        "flat_tokens": metrics["flat_tokens"],
        "router_tokens": metrics["router_tokens"],
        "router_plus_one_tokens": metrics["router_plus_one_tokens"],
        "router_plus_median_tokens": metrics["router_plus_median_tokens"],
        "median_n_sections": metrics["median_n_sections"],
        "absolute_savings": metrics["absolute_savings"],
        "percentage_savings": metrics["percentage_savings"],
        "gate": gate,
        "non_canonical_warnings": non_canonical_warnings,
    }


def format_human(report: dict) -> str:
    """Render the report as a human-readable markdown string."""
    out = []
    out.append("# router_dryrun report\n")
    out.append(f"token counter: **{report['token_counter']}**\n")
    out.append(f"page count: **{report['page_count']}**\n")

    out.append("\n## Section sizes (pages indexed)\n")
    for name in CELL_NAMES:
        out.append(f"- {name}: {report['sections'][name]}")

    out.append("\n\n## Median session breadth\n")
    out.append(f"median_n = **{report['median_n']}** sections per session")
    out.append(
        f" (cells: {', '.join(report['median_n_sections']) or '(none)'})"
    )

    out.append("\n\n## Cross-section backlinks\n")
    out.append(
        f"- cross-section: {report['cross_section_count']} / "
        f"{report['total_backlink_count']} backlinks"
    )
    out.append(
        f"- ratio: **{report['cross_section_ratio']:.1%}** "
        f"(gate max: {GATE_CROSS_SECTION_RATIO:.0%})"
    )

    out.append("\n\n## Token costs\n")
    out.append(f"- flat index: {report['flat_tokens']} tokens")
    out.append(f"- router alone: {report['router_tokens']} tokens")
    out.append(
        f"- router + 1 largest section: {report['router_plus_one_tokens']} tokens"
    )
    out.append(
        f"- router + median-N sections: "
        f"{report['router_plus_median_tokens']} tokens"
    )

    out.append("\n\n## Savings (router + median-N vs flat)\n")
    out.append(
        f"- absolute: **{report['absolute_savings']} tokens** "
        f"(gate min: {GATE_ABSOLUTE_SAVINGS})"
    )
    out.append(
        f"- percentage: **{report['percentage_savings']:.1%}** "
        f"(gate min: {GATE_PERCENTAGE_SAVINGS:.0%})"
    )

    out.append("\n\n## Gate verdict\n")
    g = report["gate"]
    if not g["evaluated"]:
        out.append("**NOT EVALUATED.**")
    elif g["passed"]:
        out.append("**PASS.** v1.0 has evidence to proceed.")
    else:
        out.append("**FAIL.** v1.0+ plan should be killed.")
    for reason in g["reasons"]:
        out.append(f"- {reason}")

    if report["non_canonical_warnings"]:
        out.append("\n\n## Non-canonical sections (ignored)\n")
        for w in report["non_canonical_warnings"]:
            out.append(f"- {w}")

    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=(
            "v0 diagnostic: measure router-vs-flat token savings and "
            "cross-section backlink ratio."
        )
    )
    ap.add_argument(
        "--target",
        default=".",
        help="project root (default: cwd; must contain context/wiki/index.md)",
    )
    ap.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format on stdout (default: text)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "write the structured JSON report to this path "
            "(stdout still gets --format output)"
        ),
    )
    args = ap.parse_args(argv)

    target = Path(args.target).resolve()
    try:
        report = build_report(target)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(format_human(report))

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    # Exit code reflects gate verdict for CI-friendliness:
    #   0 = gate passed OR gate not evaluated (smoke test / sub-threshold)
    #   2 = gate evaluated and failed
    if report["gate"]["evaluated"] and not report["gate"]["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
