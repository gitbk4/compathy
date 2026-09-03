# Architecture

Compathy works in **Claude Code** and **Google Antigravity** — a single
SKILL.md package drives both, since the skill formats align on YAML
frontmatter (`name` + `description`). Only the install path differs.

## The concept (Karpathy, April 2026)

Traditional RAG retrieves raw chunks at query time via vector similarity.
The LLM synthesizes an answer from whatever chunks rank highest. This is
**retrieval-then-synthesis** — synthesis happens on every query.

Karpathy's alternative: **synthesis-then-retrieval**. The LLM reads raw sources
ONCE and writes a structured wiki: summaries, concept articles, entity pages,
all cross-linked. At query time, the LLM navigates the wiki's index and jumps
to relevant pages. Synthesis is pre-computed and compounds.

> "The tedious part of maintaining a knowledge base is not the reading or the
> thinking — it's the bookkeeping. LLMs handle bookkeeping efficiently: they
> don't get bored, they can touch 15 files in one pass."

## Three layers

| Layer | Owner | Mutability |
|---|---|---|
| `raw/` — raw sources | Human | Immutable (LLM reads, never writes) |
| `wiki/` — compiled artifact | LLM | LLM-owned (humans may edit; LLM reconciles) |
| `schema.md` — conventions | Co-evolved | Human + LLM update together |

## Split of concerns: Python vs Claude

```
┌─────────────────────────────────────────────────────────────┐
│  PYTHON (deterministic bookkeeping)                         │
│  ├── scaffold.py   creates dirs + renders templates         │
│  ├── bootstrap.py  emits git log + tree + READMEs + manifests│
│  ├── ingest.py     checksums, .ref resolution, state mgmt   │
│  └── lint.py       backlinks, orphans, schema, staleness    │
└──────────────────────┬──────────────────────────────────────┘
                       │ JSON outputs consumed by...
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  CLAUDE (synthesis + writing)                               │
│  ├── interviews the user                                    │
│  ├── reads raw sources                                      │
│  ├── writes summaries, concepts, entities                   │
│  ├── adds [[backlinks]] between related pages               │
│  ├── updates index.md (authoritative catalog)               │
│  ├── appends log.md (chronological record)                  │
│  ├── fixes lint errors                                      │
│  └── heals stale pages                                      │
└─────────────────────────────────────────────────────────────┘
```

## The compile loop

```
  [1] INGEST           [2] COMPILE          [3] LINT            [4] HEAL
  ─────────            ──────────           ────────            ────────
  User drops raw       Claude reads         lint.py validates:  Claude re-reads
  sources / adds       raw + bootstrap      • backlinks resolve related_paths,
  .ref files           emits compile        • no orphans        updates stale
       │               hints                • schema compliance pages, bumps
       │                   │                • staleness         updated: in
       ▼                   ▼                    │               frontmatter
  ingest.py           Claude writes              ▼
  detects changes     wiki pages with        Claude fixes
  via checksums       [[backlinks]],         errors (re-reads,
  (LF-normalized)     updates index.md,      re-writes)
       │              appends log.md             │
       │                   │                     │
       └───────────────────┴─────────┬───────────┘
                                     ▼
                        wiki/ is a compounding,
                        self-healing, git-versioned
                        compact knowledge graph
```

## Data flow: RECOMPILE

```
 user: /compathy
    │
    ▼
 Claude reads SKILL.md
    │
    ├─▶ python ingest.py --detect-changes
    │         │
    │         ├── walks context/raw/
    │         ├── computes sha256 per file (LF-normalized)
    │         ├── resolves .ref → content checksum of target
    │         ├── compares vs .compile-state.json
    │         └── emits {added, modified, deleted, errors}
    │
    ├─▶ Claude reads changed raw + related wiki pages
    ├─▶ Claude writes/updates wiki pages
    ├─▶ Claude updates index.md + log.md
    │
    ├─▶ python ingest.py --commit-state  (atomic tmp+rename)
    │
    └─▶ python lint.py --format json
              ├── parse_frontmatter (flat YAML)
              ├── parse_backlinks (strip code fences)
              ├── check_backlinks / check_orphans
              ├── check_schema_compliance
              └── check_staleness (batched git log)
         Claude fixes errors, reports summary
```

## Key design decisions

### 1. Stdlib only

No PyYAML, no click. Keep install zero-friction. Wrote a 50-line flat-YAML
parser in `lint.py` to avoid the dependency.

### 2. Flat YAML frontmatter

Markdown-native, ecosystem-compatible (Obsidian, MkDocs, GitHub previews all
parse it). Restricted to scalars + flat lists to keep the parser tiny and the
data model simple.

### 3. LF normalization before hashing

Windows + macOS would otherwise produce different checksums for the same
content, breaking idempotence. All checksums are computed on LF-normalized
bytes.

### 4. .ref files instead of copies

Projects already have `docs/`, `ADR/`, `CHANGELOG.md`. Copying into `raw/`
creates drift within a week. `.ref` files point at the real source. The
checksum of a `.ref` is the checksum of its target's content — so when the
target changes, the wiki knows to recompile.

Path sandboxing: `.ref` targets MUST be inside the repo root (computed via
`git rev-parse --show-toplevel`). Absolute paths and `..` segments are rejected.

### 5. Atomic state writes

`.compile-state.json` is written via tmp-file + `os.replace` so a crash mid-
write can never corrupt state. If the file IS corrupt (e.g. from an old crash),
ingest.py rebuilds from scratch with a warning — no data loss since `raw/` is
the source of truth.

### 6. Staleness via batched git log

Naïve implementation: one `git log -- <path>` call per page. At 100 pages = 100
git processes. Instead, one `git log --name-only --since=365.days` call emits
all commits with touched paths, then we map them to pages in-memory. O(1) git
calls regardless of page count.

### 7. Index is authoritative

The linter enforces bijection: every page file ↔ every index entry. This gives
Claude a single source of truth during compile ("if it's not in the index,
I forgot to add it") and prevents the index from silently drifting.

### 8. Append-only log

The log is a git-style chronological record. Every entry uses a greppable format
(`## [YYYY-MM-DD] <op> | <summary>`) so tools and humans can parse it with
simple regex.

## Failure modes (all handled, none silent)

| Scenario | Handling |
|---|---|
| No git repo | bootstrap runs without git data; warning printed |
| Git not installed | bootstrap exits with clear error |
| `.ref` path traversal | rejected before read; error message names the file |
| Layer pin or wiki tree sha mismatch | fetch aborts in a temp dir; nothing cached, nothing linked |
| Parent layer not cached | lint warns (`layer-not-cached`), unresolved links become warnings; `sync` fixes |
| Team page overrides `override: forbidden` org page | lint error `shadow-forbidden` |
| Team page claims `authority: org` | lint error `authority-claim` |
| Registry / persona JSON malformed | lint error; search skips with a warning |
| `.ref` target missing | clear error with target path |
| Corrupt state.json | warning + rebuild from scratch (no data loss) |
| Mid-write crash | atomic rename prevents partial state files |
| File > 5MB | skipped with warning |
| Malformed frontmatter | lint error with line number |
| Broken backlink | lint error with source + target |
| Orphan page | lint warning with hint |
| Stale page | lint warning with commit count; heal offered |

## Why not router-mode? (Killed 2026-05-22)

An earlier proposal partitioned `wiki/` into four cell-index files under
`wiki/cells/{concepts,entities,summaries,patterns}.md` so an agent could load
only the cell-index it needed. `scripts/router_dryrun.py` was built as the
kill-criterion gate: percentage savings vs flat ≥ 40%, absolute savings ≥
500 tokens, cross-section backlink ratio ≤ 25%, measured on a wiki of ≥ 80
pages.

On a 126-page real wiki (`career-ops`), the gate failed every condition:

| metric | observed | required |
|---|---|---|
| percentage savings | 3.3% | ≥ 40% |
| absolute savings | 75 tokens | ≥ 500 |
| cross-section backlink ratio | 63.7% | ≤ 25% |
| median session breadth | 3-of-4 cells | — |

The root cause is structural, not tunable. Pages link freely across the
four page-types (a summary references the concepts and entities it covers;
a pattern references its target entities), so a partition that mirrors page
types is fighting how knowledge actually clusters. With 3-of-4 cells touched
per typical session, router-mode wouldn't have shrunk the loaded context
even if the ratio were better.

The diagnostic stays in tree (`scripts/router_dryrun.py`) so future
partition-shaped proposals have to clear the same bar against the same
real wikis.

### What replaces it

The path compathy already ships — LLM reads `index.md` + summaries, picks
slugs, loads pages — *is* semantic search, with the LLM as the matcher.
Flat indexes at observed scale (2.3 K tokens on 126 pages) are not a
context-budget problem on modern models. When a real user wiki hits the
token wall (rough estimate: ≥ 500 pages, ≥ 10 K-token flat index), the
escape hatch is **opt-in embedding-based pre-filtering** via
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, with vectors cached in
`.compile-state.json` keyed by content hash. Not BM25 — keyword search is
the wrong tool when the consumer is an LLM.

Until that threshold is observed in the wild, no new partition, no new
search index, no new runtime dependency. The MCP server may add
`wiki_page(slug)` → body + 1-hop backlink neighbors so the LLM gets graph
context without re-querying; that's the only new surface area.

## Federation: layers, personas, registries (v0.3.0)

A company keeps a **high-order compathy** (the org layer). Teams keep
their own **specialized sub-compathy** that extends it. A member **joins
instantly** by importing the team's exported **persona**. Design decision
#4 (`.ref` over copies) applied across repos: parents are *pointed at*,
pinned, and cached; never vendored into the child's `wiki/`.

```
  ~/.compathy/layers/acme/<pin>/context/             org layer   (read-only cache)
  ~/.compathy/layers/acme--payments/<pin>/context/   team layer  (read-only cache)
  ./context/                                          project     (writable, today's wiki)
      ├── lineage.json   parents + pins + self {id, role}
      └── persona.json   the join token, verbatim
```

Any compathy can be a parent; org/team/project are labels on an ordered
list (depth cap 3). A team layer *is* a compathy whose `lineage.json`
points at the org; it declares `self: {id: acme/payments, role: team}`.

### Resolution

1. `[[slug]]` and `compathy_get_page` resolve **child to parent**; the
   result carries `layer` and `also_in`.
2. Backlinks may resolve **upward** only. A parent lints alone and knows
   nothing about children.
3. **Shadowing is opt-in per page.** Only a parent page that declares
   `authority:` carries an `override` policy (`forbidden` / `narrow` /
   `free`). A child may narrow a `narrow` page only with
   `extends: <parent id>`. Same-slug pages without `authority` (every
   layer's `technical-patterns`) are layer-local and lint says nothing.
4. A page may only claim the `authority` of the layer it lives in
   (`authority-claim` lint error otherwise).
5. If a parent is not cached, unresolved backlinks downgrade to
   `unverified-backlink` warnings so a fresh clone lints before `sync`.

### The persona (join token)

`context/personas/<role>.json`, exported by a team lead, committed, listed
in the generated `personas/index.json`:

- `layers[]`: id, role, git `source`, `path`, commit `pin`, and the git
  **tree sha of `wiki/`** at that pin (covers every page, verifiable
  offline, free via `git rev-parse <pin>:<path>/wiki`).
- `reads_first` (`<layer-id>/<slug>`), `responsibilities`, `toolkit`
  (ai-quickstart suggestion shape), `policy` (advisory locally, enforced
  upstream by git), `provenance`, and a reserved `signature` slot.

Two join paths, both idempotent:

- **import** a persona (file, https URL, or `org/team/role` via the org
  registry) into your own project;
- **clone** an already-linked repo and run `sync`; `lineage.json` is
  sufficient to rehydrate the caches. `.mcp.json` carries no per-layer
  arguments because the MCP server reads `lineage.json` itself.

### Fetch and verify

```
git clone --no-checkout --filter=blob:none <url>   (plain clone fallback)
git sparse-checkout set --no-cone <path>
git checkout <pin>
verify HEAD == pin && rev-parse HEAD:<path>/wiki == tree_sha
chmod -R a-w ; rename into ~/.compathy/layers/<id>/<pin>/
```

Clone happens in a sibling temp dir and is renamed in only after
verification, so a failed fetch never leaves a partial or unverified
cache entry. `GIT_TERMINAL_PROMPT=0`, no submodules, sources limited to
`git+https`, `git+ssh`, `git+file` (tests).

### Discovery

Org repo `context/registry.json` lists teams; each team's
`context/personas/index.json` lists personas. Search is a two-hop sparse
fetch cached for six hours. A team the org does not list is not
discoverable; that is the permission model applied to discovery.

### Permission control for the high-order source of truth

Principles:

1. **Git is the write-permission system; compathy does no auth.** Branch
   protection + CODEOWNERS on the org repo (or on `context/` in a
   monorepo) decide who changes org truth. Stdlib-only, serverless,
   reviewable in PRs.
2. **compathy enforces structure; CI enforces that structure holds.**
   `lint.py` in a child's CI fails the build when a team page violates
   `override: forbidden` or claims an authority it does not have.
3. **Read trust comes from pins and hashes.** Import verifies commit and
   wiki tree sha; updating is an explicit, diffed action.
4. **The org registry is the trust anchor**, not the file. A persona
   handed out-of-band is re-resolved against the registry; a
   byte-identical match lifts trust from 2 to 4. Signatures (v1.1) only
   add value for files that never touch the registry.
5. **Least privilege on join.** Three separate consents (fetch, link,
   toolkit); caches are read-only; `policy.may_edit` tells the agent to
   keep its hands off parents, which are not in the project's git tree
   anyway.
6. **Provenance survives layering.** Every resolved page carries `layer`;
   every persona carries `provenance`; every import is logged to
   `~/.compathy/import-log.jsonl` and to the wiki `log.md`.

| Actor | Org layer | Team layer | Project | Personas | Registry |
|---|---|---|---|---|---|
| Org maintainer (CODEOWNERS) | edit, set `authority/override` | read | read | export org-level | edit |
| Team lead (CODEOWNERS on team `context/`) | read; propose via PR | edit; narrow per `override` | read | export team personas | propose entry via PR |
| Team member | read | read; propose via PR | edit | import | read |
| CI | lint | lint + shadow/authority checks | lint | validate manifests | validate |

Upward flow in v1 is pull-model and already built: a parent's owner runs
`/compathy-augment <member-project>` to adopt strong patterns from below.
Only parent owners can write the parent, so this is permission-correct by
construction. A push-model `propose` helper is v2.

Trust score (deterministic, render-time): 4 = via org registry (or
byte-identical to it), pinned + tree-verified; 3 = direct URL; 2 = local
file; 1 = any layer unverified. 5 reserved for signed personas.

Threats considered: prompt injection via parent wiki text (pins mean you
read what was reviewed; the MCP persona search is local-only so page text
cannot make an agent fetch URLs); persona as supply chain (`toolkit` is
shown and consented, never auto-run); registry poisoning (only the org
registry is trusted; team indexes are generated and lint-validated); stale
org truth (the most likely failure: `status`, `update`, and the
`lineage:` summary line exist for it). Deliberately not built: accounts,
tokens, servers, encryption, automatic upward writes, mandatory signatures.

## Why not tokenize-and-vector-index?

- Vector DB adds a runtime dependency (Pinecone, Chroma, pgvector, etc.)
- Embeddings drift with model updates
- Queries can't be reviewed in PRs
- Synthesis happens at query time, not compile time (no compounding)
- The wiki is human-readable; a vector index is not

A compiled wiki + grep is enough up to ~100 pages / ~400k words, per
Karpathy's own measurements. Past that, you can still add vector search
on top — but the wiki remains the substrate.
