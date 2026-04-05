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

## Install

Clone once, then use the installer to symlink into whichever agentic IDE
you use. One target per invocation — pick `--claude` or `--antigravity`.

```bash
git clone https://github.com/gitbk4/compathy.git ~/Code/compathy
cd ~/Code/compathy
```

### Claude Code

```bash
python3 scripts/install.py --claude              # global (~/.claude/skills/compathy)
python3 scripts/install.py --claude --workspace  # current project (.claude/skills/compathy)
```

Invoke with `/compathy`.

### Google Antigravity

```bash
python3 scripts/install.py --antigravity              # global (~/.gemini/antigravity/skills/compathy)
python3 scripts/install.py --antigravity --workspace  # current project (.agent/skills/compathy)
```

Compathy installs into Antigravity's **Skills** slot (not Rules, not Workflows).
Its description field acts as the trigger phrase — Antigravity loads the skill
only when relevant, keeping your agent context clean.

### Update or uninstall

```bash
cd ~/Code/compathy && git pull          # update (symlink means all tools see it)
python3 scripts/install.py --claude --uninstall
python3 scripts/install.py --antigravity --uninstall
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
- An agentic IDE that loads SKILL.md packages (Claude Code, Antigravity)

## License

MIT. See [LICENSE](LICENSE).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for deep-dive on the design.
