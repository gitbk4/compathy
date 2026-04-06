---
name: compathy-compare
description: Compare two projects' architectures and produce a compatibility report with engineering hour estimates, redundancy analysis, Venn overlap diagram, and a proposed merged architecture diagram. Requires a compathy wiki in the current project.
---

# compathy-compare

You are orchestrating the `compathy-compare` skill. Your job is to compare the
current project against a target project and produce a structured report with
architecture diagrams.

**Invocation**: `/compathy-compare <path-to-other-project>`

The current project is implicitly "Project A". The path argument is "Project B".

`{skill_dir}` is the directory containing this SKILL.md. Scripts are at
`{skill_dir}/../../scripts/`.

---

## Phase 0 — Auto-update + Validate

First, pull the latest version from GitHub:

```bash
python3 {skill_dir}/../../scripts/update.py
```

Then validate:

1. Confirm the current project has a compathy wiki at `context/wiki/`.
   If not, tell the user: "Run `/compathy` first to create a wiki for this
   project." and stop.

2. Check if the target project has a compathy wiki. Note the result — you'll
   use this in Phase 1.

---

## Phase 1 — Gather data

Run the comparison script:

```bash
python3 {skill_dir}/../../scripts/compare.py --current . --target <path>
```

Read the JSON output. It contains:

- `current` / `target`: each project's entities, concepts, patterns, tech stack
- `target_wiki_available`: whether the target has a compathy wiki
- `overlap`: set intersections for entities, concepts, patterns, tech stack

**If `target_wiki_available` is false**, tell the user:
> "The target project doesn't have a compathy wiki. I'm using a bootstrap
> scan (file tree, manifests, READMEs) — the comparison will be less precise.
> For better results, run `/compathy` in the target project first."

---

## Phase 2 — Deep-read both projects

Read ALL concept, entity, pattern, and summary pages from the current wiki.

If the target has a wiki, read its pages too. If not, read:
- All READMEs from the bootstrap data
- The manifest files (package.json, pyproject.toml, etc.)
- A sample of key source files from the file tree (pick files that reveal
  architecture: routes, models, config, main entry points)

Your goal: build a mental model of both architectures deep enough to draw
them and assess compatibility.

---

## Phase 3 — Write the report

Create the reports directory if it doesn't exist:

```bash
mkdir -p context/compathy-reports
```

If `context/compathy-reports/.gitignore` doesn't exist, create it with:
```
*
!.gitignore
```

Write the report at:
`context/compathy-reports/compare-<target-project-name>-<YYYY-MM-DD>.md`

### Report structure

```markdown
# Compathy Compare: <Project A> x <Project B>

_Generated <date>. Target wiki: available | bootstrap-only._

---

## Compatibility Assessment

**Estimated engineering hours for full integration: <N> hours**

### Methodology

Hours are estimated by category:

| Category | Count | Hours each | Subtotal |
|---|---|---|---|
| Conflicting patterns to reconcile | X | 4h | Xh |
| Redundant entities to merge | X | 2h | Xh |
| Missing integrations to build | X | 8h | Xh |
| Tech stack gaps to bridge | X | 16h | Xh |
| Schema/data model alignment | X | 6h | Xh |
| **Total** | | | **Nh** |

Explain each line item briefly.

## Redundancies

List every entity, concept, or pattern that appears in both projects.
For each, note whether they're identical, overlapping, or conflicting.

## Tech Stack Comparison

| Technology | Project A | Project B |
|---|---|---|
| ... | Y | Y/N |

## Architecture: <Project A>

```
ASCII diagram showing Project A's architecture.
Derive from entities + patterns + related_paths.
Show: data stores, services, frontends, queues, external APIs.
Use box-drawing characters.
```

## Architecture: <Project B>

```
Same format for Project B.
```

## Overlap (Venn)

```
ASCII Venn diagram showing:
- Left only: unique to A
- Center: shared between A and B
- Right only: unique to B
Group by: tech stack, entities, concepts, patterns.
```

## Proposed Combined Architecture

```
ASCII diagram showing how A + B would merge.
Show: which services survive, which merge, new integration points,
shared data stores, unified API layer if applicable.
Mark new-work items with [NEW] and merge-work with [MERGE].
```

Explain the key decisions in the combined architecture.
```

---

## Phase 4 — Present summary

Print a compact summary:

```
compathy-compare: report complete
  projects: <A> x <B>
  target wiki: available | bootstrap-only
  engineering hours estimate: <N>h
  redundancies: <X> entities, <Y> concepts, <Z> patterns
  report: context/compathy-reports/compare-<name>-<date>.md
```

---

## Rules

1. **Never write to the target project.** Read-only.
2. **ASCII diagrams only.** No image generation, no graphviz.
3. **Hours estimates are rough.** State assumptions. Don't over-precision.
4. **Be specific about conflicts.** "Both have auth" is useless. "A uses JWT
   with refresh tokens, B uses session cookies — reconciliation needed" is useful.
5. **Reports are gitignored.** They contain internal analysis.
