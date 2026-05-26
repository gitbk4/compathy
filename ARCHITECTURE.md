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

## Why not tokenize-and-vector-index?

- Vector DB adds a runtime dependency (Pinecone, Chroma, pgvector, etc.)
- Embeddings drift with model updates
- Queries can't be reviewed in PRs
- Synthesis happens at query time, not compile time (no compounding)
- The wiki is human-readable; a vector index is not

A compiled wiki + grep is enough up to ~100 pages / ~400k words, per
Karpathy's own measurements. Past that, you can still add vector search
on top — but the wiki remains the substrate.
