---
name: compathy-augment
description: Learn from another project's strengths and adopt selected patterns, conventions, or architectural ideas into your own compathy wiki. Analyzes the target, surfaces what's strong, lets you pick what to bring over. Requires a compathy wiki in the current project.
---

# compathy-augment

You are orchestrating the `compathy-augment` skill. Your job is to help the
user learn from a target project and selectively adopt its best ideas into
their own compathy wiki.

**Invocation**: `/compathy-augment <path-to-target-project>`

The current project is the one being improved. The target is read-only.

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

2. Check if the target project exists and is readable.

---

## Phase 1 — Ask the user's intent

Ask the user two questions (use your harness's interactive-prompt mechanism):

**Question 1 — Quality goal:**
> "What quality are you trying to improve in your project? (e.g., error
> handling, test coverage, API design, documentation structure, deployment
> patterns, code organization)"

**Question 2 — Entry path:**
> "Do you already know what you like about the target project, or would you
> like me to analyze it and show you its strengths?
>
> A) I'll tell you what I like about it
> B) Analyze it for me"

If user picks **A**: collect their observations (free text). Skip to Phase 3,
using their observations as the strength list.

If user picks **B**: proceed to Phase 2.

---

## Phase 2 — Analyze the target

Run the analysis script:

```bash
python3 {skill_dir}/../../scripts/augment.py --current . --target <path>
```

Read the JSON output. It contains:
- `target`: the target project's data (wiki pages or bootstrap scan)
- `target_wiki_available`: whether the target has a compathy wiki
- `existing_augments`: any previous augmentations from this target

**If `target_wiki_available` is false**, tell the user:
> "The target project doesn't have a compathy wiki. I'm analyzing from its
> file structure, manifests, and READMEs — results will be less precise."

### Deep-read the target

If the target has a wiki: read all its concept, entity, pattern, and summary
pages.

If not: read its READMEs, manifests, and sample source files (routes, models,
config, tests — pick files that reveal architecture and conventions).

### Characterize strengths

Freely identify what the target project does well. Focus on:
- **Patterns & conventions** — naming, file organization, error handling,
  testing approach, API design
- **Architecture** — clean boundaries, separation of concerns, service design
- **Documentation** — clear specs, ADRs, inline comments, type coverage
- **Tooling** — CI/CD, linting, formatting, pre-commit hooks
- **Domain modeling** — entity design, data flow clarity

Filter your findings by the user's stated quality goal from Phase 1.

Present a ranked list:

```
Strengths I found in <target> (filtered by: <quality goal>):

1. **<strength title>** — <1-2 sentence description>
   Source: <file path or wiki slug in target>

2. **<strength title>** — ...

3. ...
```

---

## Phase 3 — User selects

Ask the user:
> "Which of these would you like to bring into your project?
> (List numbers, e.g. '1, 3' — or 'all')"

---

## Phase 4 — Adopt with provenance

For each selected strength:

1. Create the augmented-sources directory:
   ```
   context/raw/augmented/<target-project-name>/
   ```

2. Write a `.md` file for each adoption:
   `context/raw/augmented/<target-name>/<strength-slug>.md`

   Format:
   ```markdown
   ---
   source_project: <target project name>
   source_paths: [<list of files in target this came from>]
   adopted_reason: <why the user chose this>
   quality_goal: <from Phase 1>
   date: <YYYY-MM-DD>
   ---

   # <Strength Title>

   <Detailed description of the pattern/convention/approach.>

   ## How it works in <target project>

   <Concrete examples from the target, with code snippets if relevant.>

   ## Suggested adaptation for <current project>

   <How this could be applied here, noting any adjustments needed for
   the current project's stack and conventions.>
   ```

3. These files are now raw sources. The next `/compathy` compile will pull
   them into the wiki with full attribution.

---

## Phase 5 — Write adoption report

Create the reports directory if it doesn't exist:

```bash
mkdir -p context/compathy-reports
```

If `context/compathy-reports/.gitignore` doesn't exist, create it with:
```
*
!.gitignore
```

Write:
`context/compathy-reports/augment-<target-name>-<YYYY-MM-DD>.md`

Include:
- Quality goal
- Strengths identified (all, not just selected)
- What was adopted and why
- Suggested next steps ("run `/compathy` to compile these into your wiki")

---

## Phase 6 — Summary

Print:

```
compathy-augment: complete
  target: <name> (wiki: available | bootstrap-only)
  quality goal: <goal>
  strengths found: <N>
  adopted: <M>
  raw sources written to: context/raw/augmented/<target-name>/
  next: run `/compathy` to compile augmented sources into your wiki
  report: context/compathy-reports/augment-<target-name>-<date>.md
```

---

## Rules

1. **Never write to the target project.** Read-only, always.
2. **Augmentations go into `raw/`, not `wiki/`.** The next `/compathy` compile
   integrates them properly — with backlinks, index entries, and log.
3. **Provenance is mandatory.** Every adopted source must cite `source_project`
   and `source_paths` in frontmatter.
4. **Don't duplicate what's already there.** Check `existing_augments` — if
   the user already adopted something from this target, skip it (or offer to
   refresh it instead).
5. **Respect the quality goal.** Don't dump everything — filter strengths by
   what the user actually asked to improve.
6. **Reports are gitignored.** They contain internal analysis.
