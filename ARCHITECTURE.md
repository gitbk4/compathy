# Architecture

Compathy runs in any agent that loads SKILL.md packages and is tested in
**Claude Code** and **Google Antigravity**. One package drives every agent,
since the skill formats align on YAML frontmatter (`name` + `description`);
only the install path differs (`install.py --dir` covers other agents). The
wiki is plain markdown, and the `compathy-wiki` MCP server works with any MCP
client. Below, "the agent" is whatever LLM-driven tool runs the skill.

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

## Split of concerns: Python vs the agent

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
│  AGENT (synthesis + writing)                                │
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
  User drops raw       Agent reads          lint.py validates:  Agent re-reads
  sources / adds       raw + bootstrap      • backlinks resolve related_paths,
  .ref files           emits compile        • no orphans        updates stale
       │               hints                • schema compliance pages, bumps
       │                   │                • staleness         updated: in
       ▼                   ▼                    │               frontmatter
  ingest.py           Agent writes               ▼
  detects changes     wiki pages with        Agent fixes
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
 Agent reads SKILL.md
    │
    ├─▶ python ingest.py --detect-changes
    │         │
    │         ├── walks context/raw/
    │         ├── computes sha256 per file (LF-normalized)
    │         ├── resolves .ref → content checksum of target
    │         ├── compares vs .compile-state.json
    │         └── emits {added, modified, deleted, errors}
    │
    ├─▶ Agent reads changed raw + related wiki pages
    ├─▶ Agent writes/updates wiki pages
    ├─▶ Agent updates index.md + log.md
    │
    ├─▶ python ingest.py --commit-state  (atomic tmp+rename)
    │
    └─▶ python lint.py --format json
              ├── parse_frontmatter (flat YAML)
              ├── parse_backlinks (strip code fences)
              ├── check_backlinks / check_orphans
              ├── check_schema_compliance
              └── check_staleness (batched git log)
         Agent fixes errors, reports summary
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
the agent a single source of truth during compile ("if it's not in the index,
I forgot to add it") and prevents the index from silently drifting.

### 8. Append-only log

The log is a git-style chronological record. Every entry uses a greppable format
(`## [YYYY-MM-DD] <op> | <summary>`) so tools and humans can parse it with
simple regex.

## Failure modes (all handled, none silent)

| Scenario | Handling |
|---|---|
| No git repo | bootstrap runs without git data; warning printed |
| Git not installed | bootstrap, ingest and lint run without history; layer fetches stop with "git is not installed or not on PATH" |
| `.ref` path traversal | rejected before read; error message names the file |
| Layer pin or wiki tree sha mismatch | fetch aborts in a temp dir; nothing cached, nothing linked |
| Parent layer not cached | lint warns (`layer-not-cached`), unresolved links become warnings; `sync` fixes |
| Team page overrides `override: forbidden` org page | lint error `shadow-forbidden` |
| Team page claims `authority: org` | lint error `authority-claim` |
| Registry / persona JSON malformed | lint error; search skips with a warning |
| Publish: scan finding, bad private marker, invalid visibility, lint error in the build | plan lists blockers; apply refuses; the core repo is untouched |
| Publish: core repo has hand edits | apply and push refuse until the owner commits or discards them |
| Publish: remote is public, or cannot be checked | push refuses; `--confirm-private` only after the owner checks |
| Bundle folder missing (drive not synced) | push names the folder; nothing is written |
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
5. **Least privilege on join.** Separate consents (fetch, link, toolkit,
   and since v0.4.0 watch); caches are read-only; `policy.may_edit` tells
   the agent to keep its hands off parents, which are not in the project's
   git tree anyway.
6. **Provenance survives layering.** Every resolved page carries `layer`;
   every persona carries `provenance`; every import is logged to
   `~/.compathy/import-log.jsonl` and to the wiki `log.md`.
7. **One online source of truth (v0.4.1).** Personas pin only `git+https`
   or `git+ssh` sources. `export` and `publish` stop with
   `needs: online-source` and the skills ask the owner to create a private
   repository; `import` refuses a persona whose layers are local paths and
   names the owner to ask. Reads try the online source before a mirror.
   `--allow-local-source` is the explicit opt-out for tests and air-gapped
   setups.

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

## Publishing a core subset (v0.4.0)

Federation assumed every layer is a whole wiki in a shared repository. In
practice an owner's wiki mixes common ground (product facts, names, design
rules) with things that must stay private (customer terms, data notes,
plans). `/compathy-publish` turns the shareable part into a layer of its
own, so other functions can link to it the way a team links to an org.

```
  owner's project (private, any folder)          core repo (private remote, + bundle)
  context/wiki/  all pages ───── publish ─────▶  context/wiki/   pages with visibility >= audience
  context/publish.json  config, deny list        context/personas/<role>.json  pinned to the publish
                                                 README.md, .github/CODEOWNERS
                                                        │ pin + tree sha (verified)
                                                        ▼
  teammate's project: context/lineage.json ──▶ ~/.compathy/layers/<core>/<pin>/  (read-only)
```

Build order: select pages by `visibility`; cut `<!-- private -->` sections;
reduce frontmatter to an allowlist; turn links to unpublished pages into
plain text; generate index, schema, README, CODEOWNERS and the publish log;
scan every output file; lint the result in a temp dir; diff against the
core repo. `apply` commits the pages, then commits personas pinned to that
commit (a persona cannot pin the commit that contains it). `push` sends the
branch to the remote and writes the bundle.

Design decisions:

1. **Copies, but only one way.** Publishing is the one place compathy
   copies pages, because the owner's wiki is private and cannot be a layer
   itself. The core repo is written only by `apply`; teammates never copy
   from it, they pin it.
2. **Default deny, per page.** `visibility` lives in the page, so the
   decision travels with the content and shows up in diffs. Absent means
   private, so a new page never leaks.
3. **Fail closed.** An unbalanced private marker, an invalid visibility, a
   scan finding or a lint error in the built layer blocks the publish.
4. **Trees, not commits, mean change.** Freshness and `update` compare the
   wiki tree hash, so persona and README commits never read as news.
5. **Mirrors, not alternate sources.** A bundle in a shared drive is the
   same repository at another location. The mapping is per user, so
   `lineage.json` stays identical for everyone; pins and tree hashes are
   verified exactly as for the remote.
6. **The core repo instructs agents itself (v0.5.1).** Publish writes an
   `AGENTS.md` there, with `CLAUDE.md` and `GEMINI.md` importing it, saying
   the repo is read-only and pointing readers at their own projects, so no
   agent adds its own instructions file and blocks the next publish.

### Threat model for publishing

| Risk | Control |
|---|---|
| A private page is published | Default deny; the plan lists every published page; the owner approves marks |
| Private detail inside a shared page | `<!-- private -->` sections, fail closed on bad markers |
| Names of private pages leak through links | Links become the label or the page title; the plan lists every unlinked link |
| Source file names and local paths leak | Frontmatter allowlist drops `sources`, `related_paths` and custom fields; the scan flags home paths |
| Secrets, PII or code names in page text | Leak scan blocks; matches are never printed; allow entries need a reason |
| The core goes to a public repository | Anonymous `ls-remote` guard; `--confirm-private` only on the owner's word |
| Someone else edits the core | Only `apply` writes it (a dirty repo is refused); CODEOWNERS and branch protection on the remote; drive roles for the bundle |
| A swapped bundle or a forced push | Teammates verify pin and tree hash; updates are shown and consented before re-pinning. Signed publishes are the next step |
| Teammates read a stale core | `freshness` (cached six hours, optional SessionStart hook) and `update`'s page-level report |
| A leak is pushed anyway | Git keeps history: rewrite it, rotate any secret, ask teammates to re-sync. The diff review and the scan exist to stop this first |

### The Notion copy (v0.5.0)

Teammates without git cannot read a git layer, and on a Mac they usually
lack Python too: both come with Apple's Command Line Tools. A Drive bundle
is a git file, so it does not help them either. For them the core is
copied to Notion, which their agent reads through its Notion connector.

```
  core repo (committed publish) ── notion-plan ──▶ rendered pages + plan.json
                                                     │ owner's agent, Notion connector
                                                     ▼
  root page: how to use · latest publish · read first by role · index · publish log
  └── All pages: one sub-page per wiki page ([[links]] point at sub-pages)
```

Python renders every page from the committed core repo and keeps the
slug-to-page map in the owner's `context/notion-sync.json`; it never talks
to Notion and holds no token. Content hashes decide what to rewrite, so a
republish touches only changed pages and the root page. The scan runs on
the rendered copy too.

| Risk | Control |
|---|---|
| Someone edits the Notion copy | Share the root page view only; before overwriting, the skill reads Notion's version history since the last sync and asks when anyone else edited a page |
| A Notion reader acts on an old version | Readers always see the latest publish; the root page carries the publish date and log, and the reader's agent compares it with the date in their notes |
| The copy falls behind the core | `status` says `behind the core repo`; `push` reminds the owner to sync |
| A removed page lingers in Notion | The plan lists it; it goes to Notion's Trash only after the owner's yes |

Trade-off: Notion readers are not pinned or hash-verified; page
permissions are the control, and the core repo keeps every publish.

Deliberately not built: redaction by an LLM (non-deterministic and not
reviewable), encryption of the core (use repository and drive access),
and automatic pushes.

## Why not tokenize-and-vector-index?

- Vector DB adds a runtime dependency (Pinecone, Chroma, pgvector, etc.)
- Embeddings drift with model updates
- Queries can't be reviewed in PRs
- Synthesis happens at query time, not compile time (no compounding)
- The wiki is human-readable; a vector index is not

A compiled wiki + grep is enough up to ~100 pages / ~400k words, per
Karpathy's own measurements. Past that, you can still add vector search
on top — but the wiki remains the substrate.
