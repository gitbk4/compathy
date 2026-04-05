# context-compounder

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

The wiki compounds. Every future Claude Code session reads the index first and
jumps to relevant pages — no re-grounding, no re-reading the whole codebase.

Based on [Andrej Karpathy's llm-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
(April 2026).

## Install

```bash
# Global install (works in every project)
git clone https://github.com/gitbk4/context-compounder.git \
  ~/.claude/skills/context-compounder

# Or per-project
git clone https://github.com/gitbk4/context-compounder.git \
  .claude/skills/context-compounder
```

Update later with `git pull`.

## Use

In any project, ask Claude Code:

```
/context-compounder
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
│   └── <your sources + .ref files>
└── wiki/                       # LLM-owned, git-versioned, compounding
    ├── README.md
    ├── index.md                # authoritative catalog
    ├── log.md                  # append-only history
    ├── .compile-state.json     # checksums (for change detection)
    ├── concepts/               # cross-cutting articles
    ├── entities/               # people, tools, services, systems
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

## Design philosophy

- **Python = bookkeeping.** Deterministic file I/O, checksums, graph validation.
- **Claude = synthesis.** Reading, writing, backlinks, healing.
- **Stdlib only.** No dependencies. Works with Python 3.10+.
- **Git-versioned.** The wiki is text. Diff it. Review it in PRs.
- **Human-editable.** Claude reconciles manual edits rather than clobbering.

## Requirements

- Python 3.10+
- Git (optional, but unlocks history-bootstrap and staleness detection)
- Claude Code

## License

MIT. See [LICENSE](LICENSE).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for deep-dive on the design.
