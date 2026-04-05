---
name: compathy
description: Jumpstart a Karpathy-style compiled knowledge base in any project. Reads raw sources, writes a structured markdown wiki with backlinks, index, log, and technical patterns. The wiki becomes compact, persistent context for all future agent sessions.
---

# compathy

You are orchestrating the `compathy` skill. Your job is to build (or
update) a compiled markdown wiki at `context/` in the current project.

**Concept** (Karpathy, April 2026): instead of RAG over raw chunks, you read
sources and WRITE a structured wiki with summaries, cross-linked concept pages,
and an index. The wiki is a compounding, git-versioned, human-readable artifact.
Future sessions skim the index and jump to relevant pages — no re-grounding.

---

## Phase 0 — Detect Mode

Run the mode detector:

```bash
python3 {skill_dir}/scripts/scaffold.py --target . --check
```

Output is `INIT` or `RECOMPILE`. Branch accordingly.

`{skill_dir}` is the directory containing this SKILL.md. You can resolve it
from the path of this file.

---

## Phase 1 — INIT Mode (first run)

### 1a. Scaffold the structure

```bash
python3 {skill_dir}/scripts/scaffold.py --target . --project-name "<name>"
```

This creates `context/{raw/, wiki/{concepts,entities,summaries,patterns}/,
schema.md, wiki/{index.md, log.md, README.md}, raw/README.md}`. It refuses
to clobber an existing `context/`.

### 1b. Interview the user

Ask three questions, one at a time (use your harness's interactive-prompt
mechanism — e.g. AskUserQuestion in Claude Code, or a plain question in
Antigravity):

1. **Project purpose** — "In one sentence, what does this project do?"
2. **Context sources** — "What should I compile into the wiki? Pick any that apply:
   existing docs (README, ADRs, specs) / meeting notes / external materials you'll paste /
   auto-derive from git history and code only."
3. **Depth** — "Quick (5-10 pages, ~20k token budget) or deep (20-40 pages, ~80k budget)?"

### 1c. Gather raw materials

Based on interview answers:

- If user said "existing docs": create `.ref` files in `context/raw/` pointing
  to each doc. Example: `context/raw/readme.md.ref` with content `README.md`.
- If user pastes URLs/files: save them as `.md` into `context/raw/`.
- If user said "auto-derive": skip this step; the bootstrap fills the gap.

### 1d. Bootstrap from the project itself (CP1)

Always run this on first compile, regardless of raw/ contents:

```bash
python3 {skill_dir}/scripts/bootstrap.py --target .
```

You get JSON with: `git_log`, `file_tree`, `readmes`, `manifests`. Use this
to seed initial entity and concept pages even if `raw/` is sparse.

### 1e. Detect raw changes (baseline)

```bash
python3 {skill_dir}/scripts/ingest.py --target . --detect-changes
```

All current raw entries appear in `added`. Read them (resolving any `.ref`
files yourself by reading the target path).

### 1f. Compile the initial wiki

Write pages using the `Write` tool. For each source:

1. **One summary page** in `context/wiki/summaries/<source-slug>.md`.
2. **Concept pages** in `context/wiki/concepts/` for cross-cutting ideas
   (aim for 3-8 concept pages in quick mode, 10-25 in deep mode).
3. **Entity pages** in `context/wiki/entities/` for people, tools, services,
   systems (aim for 2-5 in quick, 5-15 in deep).

**Do NOT write patterns pages yet.** Patterns are written LAST (see 1f-bis).

Every page needs flat-YAML frontmatter (see `context/schema.md`):

```markdown
---
type: concept
schema_version: 1
created: 2026-04-04
updated: 2026-04-04
sources: [source-slug-a, source-slug-b]
related_paths: [src/auth/, docs/adr/]
---

# Page Title

Body with [[backlinks]] to related pages.
```

Backlink rules:
- Use `[[slug]]` or `[[slug|display text]]`
- Every backlink MUST point to a page that exists (or will exist by end of compile)
- Aim for 2-5 backlinks per concept page

### 1f-bis. Write technical patterns (LAST, after all other pages)

Patterns describe **how code is written in this project**. They are the
payoff of compathy: future agent sessions read them first and match
the existing style on the first try.

Write AFTER concepts/entities/summaries are done — patterns synthesize
them plus the actual code.

**Branch on whether code exists:**

```bash
# Heuristic: any source files outside context/, docs/, node_modules/, .git/
```

Use `bootstrap.py` output (file_tree) to decide. If you see `.ts`, `.tsx`,
`.py`, `.go`, `.rs`, `.rb`, `.java`, `.kt`, `.swift` files (not just configs,
READMEs, and docs), treat as **code-exists**.

#### Mode A — Code exists: derive from the codebase

Read 5-15 representative files spanning the main directories (auth, api,
components, models, tests, etc.). Synthesize technical patterns into
`context/wiki/patterns/technical-patterns.md`. Cover at minimum:

- Language / framework / runtime (what's actually imported)
- Project structure (top-level dirs and their roles)
- Naming conventions (files, functions, types, constants)
- State management / data layer (how data flows)
- Error handling style
- Testing conventions (framework, file location, structure)
- Any project-specific idioms you notice (e.g. "always use `Result<T>`",
  "all routes go through `middleware/auth.ts`")

Cite specific files in `sources:` (e.g. `sources: [src/auth/login.ts, src/routes/api.ts]`).
Also set `related_paths:` so staleness lint tracks these over time.

#### Mode B — No code yet: ask the user

Ask the user interactively whether they want to lock in patterns
now. Offer 3-5 options derived from the tech stack you observed in the
wiki's concepts/entities pages or the project's manifests. Example:

> "I see this is a React + Vite + Tailwind project with Prisma on the backend.
> Want to lock in coding patterns now? Pick any that apply:
>
> - **Functional components only** (no class components)
> - **Container/presentational split** for React
> - **RTK Query for data** (vs SWR, React Query, plain fetch)
> - **Prisma repositories** wrapped behind a service layer
> - **Yup for form validation**
> - **Defer** — I'll establish patterns after the first real code lands"

If they pick options, write `patterns/technical-patterns.md` documenting
their choices (cite sources as `[user-interview]`). If they defer, skip —
leave `patterns/` empty and note in the log: "patterns deferred until code exists".

### 1g. Update the index (authoritative catalog)

Edit `context/wiki/index.md`. Under each category (Concepts / Entities /
Summaries / Patterns), add one bullet per page:

```markdown
## Concepts
- [[retrieval-augmented-generation]] — classic chunk-and-search pattern
- [[knowledge-compilation]] — LLM as compiler, not retriever
```

Every page you wrote MUST appear here.

### 1h. Append to the log (with wiki diff — CP7)

Edit `context/wiki/log.md` — append a new entry:

```markdown
## [YYYY-MM-DD] compile | initial wiki from N sources

- added: summaries/(...), concepts/(...), entities/(...)
- sources: N raw files
- backlinks: M
```

### 1i. Commit the state

```bash
python3 {skill_dir}/scripts/ingest.py --target . --commit-state
```

### 1j. Run lint

```bash
python3 {skill_dir}/scripts/lint.py --target . --format json
```

Fix any errors by editing affected pages, then re-run lint. Report the final
summary to the user (page counts, backlinks created, any warnings).

---

## Phase 2 — RECOMPILE Mode (subsequent runs)

### 2a. Detect changes

```bash
python3 {skill_dir}/scripts/ingest.py --target . --detect-changes
```

Read the JSON output: `added`, `modified`, `deleted`, `errors`.

If all lists are empty AND no errors: tell the user "no raw/ changes since last
compile — nothing to do" and skip to lint. If there are `.ref` errors, surface
them immediately — the user needs to fix them before proceeding.

### 2b. Update affected pages

For each changed raw source:

- **Added**: create a new summary page. Update any concept/entity pages the new
  source touches. Add [[backlinks]].
- **Modified**: re-read the source. Update the summary page and any concept/
  entity pages that depend on it. Check backlinks still resolve.
- **Deleted**: remove the summary page. Scan concept/entity pages that cited
  this source (check `sources:` frontmatter) and update them.

Touch 10-15 pages per change is normal. You are doing bookkeeping.

### 2b-bis. Refresh patterns (last, as always)

If `patterns/` is empty and the codebase now exists (wasn't true at INIT),
this is your chance to create `patterns/technical-patterns.md` — derive
from the code (Mode A in 1f-bis).

If `patterns/` has pages and recent commits touched tracked `related_paths`
(staleness check will catch this), refresh the patterns page to reflect the
current idioms. Patterns drift faster than concepts.

### 2c. Update index and log

- Add/remove entries in `index.md` to match wiki/ state.
- Append log entry summarizing the diff (CP7 — be specific about which files
  changed).

### 2d. Commit state + lint

```bash
python3 {skill_dir}/scripts/ingest.py --target . --commit-state
python3 {skill_dir}/scripts/lint.py --target . --format json
```

Fix errors. Show the user a summary of changes and any warnings (including
staleness warnings — these mean code has drifted from docs).

### 2e. Staleness healing (CP3)

If lint reports `stale-page` warnings, offer to the user: "N pages look stale
vs. recent code activity. Want me to re-read the related code paths and
refresh them?" If yes, for each stale page:

1. Read files/dirs from `related_paths`
2. Update the page content to reflect current reality
3. Bump `updated:` in frontmatter
4. Append a log entry: `## [date] heal | refreshed page-slug`

---

## Rules You Follow

1. **raw/ is immutable.** Never write to `context/raw/`.
2. **wiki/ is yours.** Humans may edit pages; on the next pass, reconcile their
   edits into your work rather than clobbering.
3. **Every page in the index.** No orphans. lint enforces this.
4. **Flat-YAML frontmatter only.** No nested maps or nested lists.
5. **kebab-case slugs.** ASCII only.
6. **Log everything.** Every compile/ingest/lint/heal gets an entry.
7. **Fix lint errors before finishing.** Report warnings but don't require fixes.
8. **Token budgets are guidance.** If the user asked for quick mode, don't
   write 40 pages.
9. **Patterns are written LAST.** They synthesize the rest of the wiki plus
   the actual code. Don't write them before concepts/entities/summaries.

## When You're Done

Print a compact summary:

```
compathy: <INIT|RECOMPILE> complete
  pages: <N> (concepts: X, entities: Y, summaries: Z, patterns: P)
  backlinks: <M>
  lint: <E> errors, <W> warnings
  next: run `/compathy` again whenever you add to raw/
```
