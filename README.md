# compathy

> Jumpstart a Karpathy-style compiled knowledge base in any project.
> No RAG. No vector DB. Just markdown, backlinks, and a self-healing wiki.

## What it does

Reads raw project materials (specs, ADRs, meeting notes, external docs, git history)
and compiles a structured markdown wiki at `context/` with:

- **Summaries** — one per raw source
- **Concepts** — encyclopedia-style articles on cross-cutting ideas
- **Entities** — one page per person, tool, service, system
- **Index** — authoritative catalog with one-line summaries
- **Log** — append-only chronological record of every compile/lint pass
- **Backlinks** — wiki-style `[[slug]]` cross-references

The wiki compounds. Every future agent session reads the index first and
jumps to relevant pages — no re-grounding, no re-reading the whole codebase.

Works with **Claude Code** and **Google Antigravity** (same SKILL.md, same scripts).

Based on [Andrej Karpathy's llm-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
(April 2026).

## Skills included

| Skill | Command | Purpose |
|---|---|---|
| **compathy** | `/compathy` | Build/update the compiled wiki for the current project |
| **compathy-compare** | `/compathy-compare <path>` | Compare two projects' architectures + compatibility |
| **compathy-augment** | `/compathy-augment <path>` | Learn from another project's strengths and adopt them |
| **compathy-persona** | `/compathy-persona <export\|search\|import\|sync\|status\|update\|unlink\|whoami>` | Federate wikis across a company: org layer, team layers, and personas that let a member join instantly |

## Install

Clone once, then use the installer. By default it installs **all four
skills**. Pick `--claude` or `--antigravity` for your IDE.

```bash
git clone https://github.com/gitbk4/compathy.git ~/Code/compathy
cd ~/Code/compathy
```

### Claude Code

```bash
python3 scripts/install.py --claude              # all skills, global
python3 scripts/install.py --claude --workspace  # all skills, project-local
python3 scripts/install.py --claude --skill main # just the core skill
```

### Google Antigravity

```bash
python3 scripts/install.py --antigravity              # all skills, global
python3 scripts/install.py --antigravity --workspace  # all skills, project-local
```

Compathy installs into Antigravity's **Skills** slot (not Rules, not Workflows).
Its description field acts as the trigger phrase — Antigravity loads the skill
only when relevant, keeping your agent context clean.

### Update or uninstall

```bash
cd ~/Code/compathy && git pull          # update (symlink means all tools see it)
python3 scripts/install.py --claude --uninstall           # remove all skills
python3 scripts/install.py --claude --uninstall --skill compare  # remove one
```

### Windows users

The installer requires up-to-date Windows + Developer Mode for symlink
support. The installer will prompt you to confirm you've done this before
proceeding. If symlinks fail, it falls back to a one-time directory copy
(you'll need to re-run after each `git pull`).

## Use

In any project, invoke in your agentic IDE:

```
/compathy
```

**First run (INIT)**: scaffolds `context/`, interviews you, bootstraps from
git history, compiles initial wiki.

**Subsequent runs (RECOMPILE)**: detects what changed in `raw/`, updates only
affected wiki pages, runs lint, offers to heal stale pages.

## Output

After the first run, your project has:

```
context/
├── schema.md                   # conventions (human+LLM co-evolved)
├── raw/                        # you drop sources here; LLM never writes
│   ├── README.md
│   ├── augmented/              # adopted material from compathy-augment
│   └── <your sources + .ref files>
├── compathy-reports/           # gitignored: compare + augment reports
└── wiki/                       # LLM-owned, git-versioned, compounding
    ├── README.md
    ├── index.md                # authoritative catalog
    ├── log.md                  # append-only history
    ├── .compile-state.json     # checksums (for change detection)
    ├── concepts/               # cross-cutting articles
    ├── entities/               # people, tools, services, systems
    ├── patterns/               # technical conventions and coding patterns
    └── summaries/              # per-source summaries
```

## .ref files (avoid doc duplication)

If your project already has `docs/`, `ADR/`, or `CHANGELOG.md`, don't copy them
into `raw/`. Create a `.ref` file instead:

```
# context/raw/adr-0001.md.ref
docs/adr/0001-authentication.md
```

The skill resolves `.ref` files during compile. If the target moves, lint
flags it as broken. `.ref` paths are sandboxed to the repo root (no `..`,
no absolute paths).

## How it stays honest (lint + staleness)

`scripts/lint.py` runs on every compile:

- **Structural** — all `[[backlinks]]` resolve; no orphan pages; index is
  authoritative (bijection with page files); slug naming rules; required
  frontmatter fields present; schema version matches.
- **Staleness** — each page can declare `related_paths: [src/auth/]` in its
  frontmatter. Lint counts commits to those paths since the page's last
  update. If 10+ commits have touched the tracked paths, the page is flagged
  as stale and the skill offers to heal it.

## compathy-compare

Compare two projects and produce a compatibility report.

```
/compathy-compare ../other-project
```

The current project (must have a compathy wiki) is implicitly "Project A".
The target project can fall back to a bootstrap scan if it doesn't have a wiki
(less precise but always works).

The report includes:
- **Compatibility assessment** with an engineering-hours estimate
- **Redundancy list** — overlapping entities, concepts, patterns
- **Architecture diagram** for each project (ASCII)
- **Venn overlap diagram** (shared vs unique)
- **Proposed combined architecture** (how a merge would look)

Reports are written to `context/compathy-reports/` and gitignored by default
(they're internal analysis, not shipped artifacts).

### Engineering hours methodology

The compatibility assessment produces a rough estimate of the total
engineering hours required for full integration. Hours are computed by
category:

| Category | Per item | What it counts |
|---|---|---|
| Conflicting patterns | 4h | Conventions that directly contradict (e.g. different naming, different auth approach) |
| Redundant entities | 2h | Entities doing the same job that must be merged or deduplicated |
| Missing integrations | 8h | Features/services in one project that the other lacks |
| Tech stack gaps | 16h | Fundamental tech differences (different language, different DB, different framework) |
| Schema/data model alignment | 6h | Overlapping data models that need migration or adapter logic |

The estimate is an order-of-magnitude guide, not a project plan. It helps
answer "is this a weekend project or a quarter?" before diving in.

## compathy-persona (federation: org, teams, members)

A company keeps one high-order compathy (the **org layer**). Each team
keeps its own compathy that **extends** the org's. Any member **joins
instantly** by importing the team's exported **persona**: their agent then
reads org, team, and project wikis in that order, follows the team's
patterns, and gets the team's toolkit.

Parents are never copied. They are pinned to a commit, verified by the git
tree hash of their `wiki/`, and cached read-only under `~/.compathy/layers/`
(the `.ref` idea applied across repos).

```
# team lead, inside the team's compathy
/compathy-persona export backend-engineer      # -> context/personas/backend-engineer.json (commit it)

# new member, in an empty dir or an existing project
/compathy-persona search "payments backend"    # ranks personas via the org registry
/compathy-persona import acme/payments/backend-engineer
#   plan -> three consents (fetch, link, toolkit) -> verified fetch -> link -> briefing

# a teammate who cloned an already-linked repo
/compathy-persona sync                          # lineage.json is enough to rehydrate
```

What linking writes: `context/lineage.json` (parents + pins + this layer's
role), `context/persona.json` (the manifest, verbatim), a
`entities/persona-<role>.md` page, and a "Federation" block in the
CLAUDE.md/README context section. The `compathy-wiki` MCP server reads
`lineage.json` itself, so every tool answers across layers
(`compathy_get_page` returns `layer` and `also_in`).

**Shadowing is opt-in.** A parent page that declares `authority: org` (or
`team`) carries an `override` policy: `forbidden` (link only), `narrow`
(a child may specialize it with `extends: <parent>`), or `free`. Every
other same-slug collision, such as each layer's own `technical-patterns`,
is layer-local and lint says nothing. Lint also rejects pages that claim an
authority their layer does not have.

**Permission control is git.** Branch protection and CODEOWNERS decide who
writes the org layer; `lint.py` in CI decides whether a team respects it;
pins and tree hashes decide what an importer trusts; the org's
`context/registry.json` is the only path to discover teams. compathy has
no accounts, tokens, or servers. Trust is shown as a 1-5 badge (4 = via the
registry, pinned and verified; 5 reserved for signed personas in v1.1).

Org maintainers: `persona.py registry init --org acme` and
`registry add-team --id payments --source git+https://...`. Members:
`persona.py config set-org git+https://github.com/acme/compathy-org.git`
once, then search by name. Full flow in `skills/compathy-persona/SKILL.md`;
design and threat model in `ARCHITECTURE.md`.

To clear the read-only cache: `chmod -R u+w ~/.compathy/layers && rm -rf ~/.compathy/layers`.

## compathy-augment

Learn from another project's strengths and selectively adopt them.

```
/compathy-augment ../admired-project
```

Flow:
1. **Ask your intent** — what quality are you trying to improve?
2. **Choose entry** — you tell compathy what you like, OR compathy analyzes
   the target and shows you its strengths
3. **Pick what to adopt** — select from the strength list
4. **Adopt with provenance** — selected items are written to
   `context/raw/augmented/<target-name>/` as raw sources with full attribution
5. **Compile** — your next `/compathy` run integrates adopted material into
   your wiki with backlinks and index entries

The target project is **always read-only**. Compathy-augment never writes to it.

Adopted material lives in `raw/augmented/` with frontmatter citing
`source_project`, `source_paths`, and `adopted_reason` — so provenance
is never lost across compiles.

## Design philosophy

- **Python = bookkeeping.** Deterministic file I/O, checksums, graph validation.
- **Claude = synthesis.** Reading, writing, backlinks, healing.
- **Stdlib only.** No dependencies. Works with Python 3.10+.
- **Git-versioned.** The wiki is text. Diff it. Review it in PRs.
- **Human-editable.** Claude reconciles manual edits rather than clobbering.

## Requirements

- Python 3.10+
- Git (optional, but unlocks history-bootstrap and staleness detection)
- An agentic IDE that loads SKILL.md packages (Claude Code, Antigravity)

## License

MIT. See [LICENSE](LICENSE).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for deep-dive on the design.
