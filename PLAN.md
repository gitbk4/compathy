# compathy — Federation Plan (org → team → member personas)

> Status: **SHIPPED in v0.3.0** (2026-09-03). Written 2026-09-01 as a plan,
> reviewed 2026-09-02, revised per the review, then built. The original plan
> follows the "Revisions applied" section unchanged, as the historical spec;
> where the two disagree, the revisions and the code win.
> Precedent: ai-quickstart's `PLAN.md` (locked spec + lanes + review report).

---

## Revisions applied (review of 2026-09-02)

| # | Change | Why |
|---|---|---|
| 1 | **One skill, subcommands**: `/compathy-persona export\|search\|import\|sync\|status\|update\|unlink\|whoami\|resolve\|registry\|config`, one `scripts/persona.py` dispatcher. `/compathy-layers` folded in. | ai-quickstart precedent; fewer files; the verb namespace disambiguates "persona". |
| 2 | **Shadowing is opt-in per page.** Only parent pages that declare `authority:` carry an `override` policy. Same-slug pages without it (every layer's `technical-patterns`, `builder`, `style`) are layer-local and lint says nothing. New rule: a page may only claim its own layer's `authority`. The "backlink to the parent page" warning was dropped (same slug would be a self-link); `extends:` is the explicit link. | The original default-on rule would have produced lint errors on every existing wiki the moment it linked. |
| 3 | **`lineage.json` is the join token for most people.** Clone an already-linked repo, run `sync`; the MCP server reads `lineage.json` itself, so `.mcp.json` carries no `--layer` args and stays portable. `context/persona.json` (manifest, verbatim) is committed next to it. | Most teammates join by cloning, not by importing a file. |
| 4 | **Git tree sha of `wiki/` at the pin** replaces `index_sha256`. | Free (`git rev-parse <pin>:<path>/wiki`), covers every page, verifiable offline. |
| 5 | **Sparse partial clone** for registries and layers; `git archive --remote` dropped. | GitHub does not support `git archive --remote`; clones ride on the user's existing git auth. |
| 6 | **Bundles, `visibility`, and signatures deferred to v1.1**; fields reserved. Added the cheaper v1 mechanism: **registry re-resolution** (a file handed out-of-band that is byte-identical to the registry copy gets trust 4). | The persona file itself holds URLs, pins, and slugs, not wiki content; the leak risk was bundles. The org registry is the trust anchor. |
| 7 | **Upward flow in v1 is `compathy-augment` run by the parent's owner** (pull model, permission-correct by construction). `propose` stays v2. | Already built; no new code. |
| 8 | **Import ends with a briefing**: `reads_first` pages are resolved to paths and the model briefs the new member. Import also seeds `entities/persona-<role>.md`. | The "instantly joined" moment. |
| 9 | Threat-model additions: sources limited to `git+https`/`git+ssh`/`git+file`; `GIT_TERMINAL_PROMPT=0`, no submodules; clone in a temp dir, verify, then rename; cache read-only. | Cheap now, embarrassing later. |
| 10 | Decisions: org = dedicated repo, team = whichever repo the team designates (any compathy can be a parent; roles are labels, depth cap 3); naming stays `persona`; ai-quickstart coupling is one-way (ai-quickstart reads compathy); `COMPATHY_STATE_HOME` (not `COMPATHY_HOME`, which ai-quickstart already uses for the skill root). | See review. |

Lanes as shipped: A lineage core (`lineage.py`, `layers.py`, `paths.py`),
B layered lint, C layered query, D+E+F in `persona.py` + one SKILL.md,
H docs (`SKILL.md`, `ARCHITECTURE.md`, `README.md`, `schema.md.tmpl`,
release notes). Lane G shrank to the trust score + threat notes (signing
is v1.1). Lane I (ai-quickstart `whoami` reads the active compathy persona)
is a separate ai-quickstart change.

Tests: `tests/test_lineage.py`, `tests/test_layers.py`,
`tests/test_federation.py` (org -> team -> export -> search -> import ->
layered lint/query/discovery -> clone + sync -> update -> unlink), plus the
existing suites. Ship gate scenario from the plan passes, including
"shadow of an `override: forbidden` page fails lint" and the added
"clone a linked repo, `sync` rehydrates".

---

# Original plan (2026-09-01), kept as written

---

## Problem statement

Today a compathy wiki is one `context/` per project, owned by whoever runs
`/compathy` there. There is no notion of *hierarchy* (a company truth that
teams specialize) and no way for a person to *join* an existing body of
compiled knowledge other than cloning a repo and reading `index.md`.

ai-quickstart already has the missing primitive: a **persona** — a small,
provenance-tagged, trust-scored, lockable profile that tells an agent "who
you are and how you work here", and that is read at scaffold time by
compathy (`persona_integration.py`).

**Goal.** Apply that concept one level up:

1. A company maintains a **high-order compathy** — the source of truth.
2. Teams maintain their own **specialized sub-compathy** that extends (and
   may narrow, never silently contradict) the org's.
3. Any team member **joins instantly** by importing an exported **persona**
   from their team: their agent then reads the right layers in the right
   order, adopts the team's patterns, and gets the team's toolkit.
4. New commands: **export**, **import**, **search** (find a team's compathy
   and the right persona to adopt).
5. **Permission control** for the high-order source of truth is designed
   here, not built yet.

The user story, end to end:

```
lead@payments:   /compathy-export --role backend-engineer
                 → context/personas/backend-engineer.json  (committed)
newhire:         /compathy-search "payments backend"
                 → 1. acme/payments/backend-engineer  [trust 4] …
                 /compathy-import acme/payments/backend-engineer
                 → layers pinned + cached, project linked, CLAUDE.md
                   breadcrumb says "read acme index, then payments index,
                   then this project's"; MCP server answers across all three
```

---

## Vocabulary

| Term | Meaning |
|---|---|
| **Layer** | One compathy `context/` directory (org, team, or project). |
| **Lineage** | Ordered chain of layers a project reads: `project → team → org`. Declared in `context/lineage.json`. |
| **Shadow** | A child page with the same slug as a parent page. Child wins at resolution; parent's `override:` policy says whether that's allowed. |
| **Authority** | Frontmatter marker on a page saying which layer owns the truth (`authority: org`). |
| **Persona** (compathy sense) | *A role within a lineage*, exported as a manifest: layer pins + role profile + toolkit + provenance. The join token. |
| **Registry** | Per-layer discovery list. Org registry lists teams; team registry lists personas. Each layer owns only its own list. |
| **Pin** | Commit SHA (plus index content hash) a persona locks a layer to. Mirrors ai-quickstart's `COMPATHY_VERSION` discipline: bumped on purpose, never pulled from HEAD silently. |
| **Layer cache** | `~/.compathy/layers/<layer-id>/<sha>/` — read-only clones import fetches into. |

**Naming collision, flagged.** ai-quickstart uses "persona" for two things
already (`persona.md` = user profile, `mappings/personas.yaml` = archetype
mapping). This plan adds a third: *team role*. The user asked for "persona",
so it stays, but every doc/command uses the two-word form **team persona**
and the manifest carries `"kind": "compathy-persona"`. Alternative if this
bites: rename to `role` (`/compathy-role-export`). Decide in review.

---

## What exists and gets reused (don't rebuild)

| Need | Reuse | Where |
|---|---|---|
| Flat-YAML frontmatter parse | `lint.parse_frontmatter` | compathy `scripts/lint.py` |
| Wiki reading | `compare.read_wiki_pages`, `read_project_data` | compathy `scripts/compare.py` |
| Idempotent breadcrumbs (CLAUDE.md / README / `.mcp.json`) | `discovery.upsert_section`, `upsert_mcp_json` | compathy `scripts/discovery.py` |
| MCP wiki server | `compathy_query.py` (gets multi-layer support) | compathy |
| Atomic tmp+rename writes | `ingest.save_state` pattern | compathy |
| Path sandboxing (`..` rejection, repo-root check) | `ingest.resolve_ref_file` | compathy |
| Persona JSON shape: `provenance`, `trust_score`, `locked`, paragraph IDs | ai-quickstart `persona_json.py` (read as plain JSON — **no code dependency**, same rule as `persona_integration.py`) | ai-quickstart |
| 1–5 deterministic trust scoring, render-time only | ai-quickstart `trust.py` idea (re-implemented, tiny) | ai-quickstart |
| Consent-before-install flow | ai-quickstart Phase 0f hook consent | ai-quickstart |
| SHA pin + explicit bump | ai-quickstart `COMPATHY_VERSION` | ai-quickstart |
| Adoption with provenance into `raw/` | `compathy-augment` | compathy |

Unchanged invariants: stdlib only; Python = deterministic bookkeeping,
Claude = synthesis; `raw/` immutable; index authoritative; append-only log;
soft-fail on anything non-load-bearing.

---

## Architecture

### Layer model (reference, never copy)

```
  ~/.compathy/layers/acme/<sha>/context/            ← org layer (read-only cache)
  ~/.compathy/layers/acme--payments/<sha>/context/  ← team layer (read-only cache)
  ./context/                                        ← project layer (writable, today's wiki)
        └── lineage.json   { layers: [project, team, org], persona: "acme/payments/backend-engineer" }
```

This is compathy design decision #4 (`.ref` over copies) applied across
repos: layers are *pointed at*, pinned, and cached — not vendored into the
child's `wiki/`. Drift is impossible by construction; freshness is an
explicit `update` with a diff.

**Why not compile org pages into the team wiki?** Same reason router-mode
died: the LLM reads indexes and jumps. Three indexes (org ~2k tokens, team
~1k, project ~1k) are well inside budget. A merged view is a *presentation*
concern (the MCP server / a generated `index.md` section), not a second
copy of the data.

### Resolution rules

1. **Slug lookup walks child → parent.** `get_page("go-service-patterns")`
   returns the project page if present, else team, else org. Result carries
   `layer: <id>` so provenance is never lost.
2. **Backlinks may resolve upward.** A team page may `[[acme-api-standards]]`
   an org page. Lint resolves against the full lineage. Backlinks may **not**
   resolve downward (org never depends on a team) — lint error.
3. **Shadowing is policy-checked.** If a child page's slug exists in a parent
   layer, lint reads the parent's `override:` field:
   - `override: forbidden` → child page is a lint **error**
   - `override: narrow` (default for `authority: org`) → allowed; child page
     must declare `extends: <layer-id>` and lint warns if it lacks a backlink
     to the parent page it narrows
   - `override: free` → allowed, no constraints
4. **Index is the union.** Merged index groups by layer, child first, with
   shadowed parent entries annotated `(shadowed by acme/payments)`.
5. **Max depth 3** (project, team, org). Deeper trees are a v2 question;
   cap now so lint and query stay simple.

### Persona as the join token

A team persona is a JSON manifest (flat where possible, one level of
nesting max — the same "tiny parser" constraint as everything else):

```json
{
  "schema_version": 1,
  "kind": "compathy-persona",
  "id": "acme/payments/backend-engineer",
  "title": "Backend engineer, Payments",
  "summary": "Owns ledger + settlement services. Go, Postgres, Temporal.",
  "tags": ["backend", "go", "payments", "ledger"],
  "layers": [
    {"id": "acme",          "role": "org",  "source": "git+https://github.com/acme/compathy-org.git", "path": "context", "pin": "3f2a…", "index_sha256": "…", "visibility": "org"},
    {"id": "acme/payments", "role": "team", "source": "git+https://github.com/acme/payments.git",      "path": "teams/payments/context", "pin": "9c1d…", "index_sha256": "…", "visibility": "team"}
  ],
  "reads_first": ["acme/patterns/api-standards", "acme/payments/patterns/go-service-patterns", "acme/payments/concepts/ledger-model"],
  "responsibilities": ["settlement pipeline", "ledger correctness", "on-call rota B"],
  "toolkit": {
    "claude_skills": [{"name": "compathy", "github": "gitbk4/compathy"}],
    "mcp_servers":   [{"id": "postgres", "description": "read-only analytics"}]
  },
  "policy": {"may_edit": ["project"], "may_propose_to": ["acme/payments"], "read_only": ["acme"]},
  "provenance": {"exported_by": "lead@acme.com", "exported_at": "2026-09-01T18:00:00Z", "from_commit": "9c1d…", "exporter_version": "0.3.0"},
  "signature": null
}
```

- `layers[]` is the load-bearing part: import can rebuild the whole reading
  environment from it.
- `toolkit` reuses ai-quickstart's suggestion object shape so `suggest` /
  trust badges can consume it unchanged later.
- `policy` is **advisory locally** (tells the agent what it should and
  shouldn't try to edit) and **enforced upstream** by git (see Permissions).
- `signature` is reserved for `ssh-keygen -Y sign` output (Phase 3).

### Discovery tree (each layer owns its own list)

```
org repo:   context/registry.json      → teams: [{id, source, path}]
team repo:  context/personas/index.json → personas: [{id, file, title, tags, summary}]
            context/personas/<role>.json
```

Search needs exactly one bootstrap: the org source URL (configured once in
`~/.compathy/config.json`, or passed `--org`). Org → teams → personas is a
two-hop fetch of three small JSON files. An org that doesn't list a team
makes that team undiscoverable — that *is* the permission model applied to
discovery. Teams cannot register themselves; they open a PR against the org
registry (helper in Phase 3).

---

## Data model

### `context/lineage.json` (child layers only; absent = standalone, today's behavior)

```json
{
  "schema_version": 1,
  "layers": [
    {"id": "acme/payments", "role": "team", "source": "git+https://…", "path": "teams/payments/context", "pin": "9c1d…"},
    {"id": "acme",          "role": "org",  "source": "git+https://…", "path": "context",                "pin": "3f2a…"}
  ],
  "persona": "acme/payments/backend-engineer",
  "imported_at": "2026-09-01T18:05:00Z",
  "imported_from": "context/personas/backend-engineer.json@9c1d…"
}
```

Committed to the project repo (it's config, not secrets — same reasoning as
`.mcp.json`). Source URLs are already public to anyone who can read the
repo; pins are commit SHAs.

### Page frontmatter additions (flat YAML, all optional, all backwards-compatible)

| Field | Values | Meaning |
|---|---|---|
| `authority` | `org` \| `team` \| `project` | Which layer owns this truth. Default: the layer the page lives in. |
| `override` | `forbidden` \| `narrow` \| `free` | What children may do with this slug. Default `narrow` when `authority: org`, else `free`. |
| `extends` | layer id | Required on a page that shadows a parent page. |
| `visibility` | `public` \| `org` \| `team` | Export gate (see Permissions). Default inherits layer visibility. |

`schema.md.tmpl` documents these; `lint.check_schema_compliance` validates
values. `schema_version` stays 1 — additive fields only.

### `~/.compathy/` (new home dir, mirrors `~/.ai-quickstart/`)

```
~/.compathy/
├── config.json                 { default_org: "git+https://…", consent: {…} }
├── layers/
│   └── <layer-id-slug>/<sha>/  read-only clone (git worktree or shallow clone at pin)
├── personas/
│   └── <id-slug>.json          imported manifests (verbatim) + import receipt
├── cache/
│   └── registry/<host>/<repo>.json   registry fetch cache (6h TTL, like ai-quickstart github cache)
└── import-log.jsonl            append-only, ≤4096-byte lines (what/when/from/verified)
```

`COMPATHY_HOME` env var overrides for tests (same pattern as
`AI_QUICKSTART_HOME`). Sync'd-filesystem warning reused from ai-quickstart's
`paths.py --detect` idea (layers cache on iCloud is slow; warn only).

### Registry files

```json
// org: context/registry.json
{"schema_version": 1, "org": "acme", "teams": [
  {"id": "payments", "source": "git+https://github.com/acme/payments.git", "path": "teams/payments/context", "visibility": "org"}
]}

// team: context/personas/index.json  (generated by export, never hand-edited)
{"schema_version": 1, "team": "acme/payments", "personas": [
  {"id": "acme/payments/backend-engineer", "file": "backend-engineer.json", "title": "…", "tags": ["…"], "summary": "…", "updated": "2026-09-01"}
]}
```

---

## Commands

All three follow the existing sub-skill pattern:
`skills/compathy-<name>/SKILL.md` + `scripts/<name>.py` + `tests/test_<name>.py`,
plus a `SKILLS` entry in `scripts/install.py`. Each SKILL.md opens with the
Phase 0 self-update call like `compathy-augment` does.

### `/compathy-export [--role <slug>] [--bundle]`  → `scripts/export_persona.py`

Run **inside a team (or org) compathy**. Produces a persona for one role.

| Step | Who | What |
|---|---|---|
| 0 | Python | Validate: `context/wiki/` exists, lint clean (errors block export), git clean tree on `context/` (dirty → refuse; pins must be reproducible), resolve own `lineage.json` (a team persona always embeds its parent org layer). |
| 1 | Claude | Interview: role title, one-line summary, tags. Propose `reads_first` by scanning `index.md` + `patterns/` (Claude picks 3–7 slugs, user confirms). Propose `responsibilities` from entity/concept pages mentioning the role. |
| 2 | Python | `export_persona.py write --role … <<JSON` — validate slugs exist in the lineage, compute pins (`git rev-parse HEAD` per layer) and `index_sha256`, check every referenced layer's `visibility` ≥ persona's declared visibility (refuse to export a `team`-visibility layer in a `public` persona), atomic-write `context/personas/<role>.json`, regenerate `context/personas/index.json`, append `log.md` entry (`## [date] export | persona <id>`). |
| 3 | Python (opt) | `--bundle` also writes `compathy-persona-<id>.tar.gz` = manifest + snapshot of each layer's `wiki/` at pin, for offline/air-gapped import. Bundles are never the source of truth; import prefers live pins and falls back to bundle contents with a `trust −1` badge. |
| 4 | Claude | Print summary; remind: "commit `context/personas/` so search can find it". |

Failure modes: not a compathy project → clear error; lint errors → list
them and stop; dirty tree → stop with `git status` excerpt; slug not found →
error naming the slug and layer searched.

### `/compathy-import <file | url | persona-id> [--dry-run] [--no-toolkit]`  → `scripts/import_persona.py`

Run **inside the project that should join** (or an empty dir — then it
scaffolds first via `scaffold.py --from-persona`).

| Step | Who | What |
|---|---|---|
| 0 | Python | Resolve argument: local file → read; `https://…json` → fetch (urllib); `org/team/role` id → search resolution (registry hops). Validate `kind`, `schema_version`, required fields. |
| 1 | Python | **Verify**: if `signature` present, `ssh-keygen -Y verify` against the org layer's `context/allowed_signers` (fetched at the *org* pin, not from the persona — the persona can't vouch for itself). Compute trust score (below). Emit JSON: manifest + trust + what will change. |
| 2 | Claude | Show the user: layers to fetch (source, pin, size), role summary, toolkit items, policy, trust badge, and any warnings (unsigned, pin older than 90 days, visibility mismatch). **Ask consent** in three separate yes/no's: (a) fetch layers, (b) link this project (write `lineage.json`, `.mcp.json`, CLAUDE.md), (c) install toolkit. Nothing happens on `--dry-run`. |
| 3 | Python | Fetch each layer into `~/.compathy/layers/<id>/<pin>/` (`git clone --depth 1` then `git fetch origin <pin>` + `checkout`, or `git worktree` if the repo is already cached). Verify `HEAD == pin` and `sha256(index.md) == index_sha256`; mismatch → abort, nothing linked. Clones are opened read-only by convention (`chmod -R a-w` on the cache dir; documented, not relied on for security). |
| 4 | Python | Link: write `context/lineage.json`; update `.mcp.json` `compathy-wiki` args to `--target . --layer <path> --layer <path>` (reuses `upsert_mcp_json`, only touches our key); upsert the CLAUDE.md/README sentinel section with a role paragraph + layer reading order (extend `render_context_section`); append `log.md` entry; append `~/.compathy/import-log.jsonl`. |
| 5 | Python (opt) | Toolkit: print install commands per item; **never auto-run** unless the user said yes in (c). Skills → `claude plugin install`/`git clone` lines; MCP → `.mcp.json` merge (our own upsert helper, one key at a time). |
| 6 | Claude | Summary + next step: "run `/compathy` to compile this project against its lineage". |

Idempotent: re-import of the same id + pins is a no-op with a "already
linked at these pins" message. Re-import with newer pins is an **update** and
prints a diff of each layer's `index.md` (old pin → new pin) before asking.

Failure modes: pin mismatch → abort + name the layer; unreachable source →
offer bundle fallback if `--bundle` file given, else abort; `lineage.json`
exists with a different persona → ask "replace or add?" (v1: replace only);
depth > 3 → error.

### `/compathy-search [query] [--org <source>] [--local]`  → `scripts/search.py`

| Step | Who | What |
|---|---|---|
| 0 | Python | Resolve org source: `--org` > `~/.compathy/config.json` > `lineage.json` of cwd (if already linked) > ask. `--local` skips network and lists only `~/.compathy/personas/` + layer cache. |
| 1 | Python | Fetch org `registry.json` (cache 6h), then each team's `personas/index.json` (cache 6h). Both via `git archive --remote` where supported, else a shallow clone of the registry paths into `cache/`. Soft-fail per team: unreachable team → warning line, keep going. |
| 2 | Python | Rank deterministically (no LLM, like `next-project`): token overlap of query vs `title+tags+summary` (+0.5), team name match (+0.2), persona `updated` within 90 days (+0.1), trust score normalized (+0.2). Emit JSON `{results:[{id, title, team, summary, trust, score, why}], warnings}`. |
| 3 | Claude | Render a ranked list with trust badges and the `why` signals; offer "import #N". If the user has an ai-quickstart persona (`~/.ai-quickstart/persona/persona.json` read as plain JSON), mention role/industry overlap as a soft hint — never auto-pick. |

Also exposed to agents via new MCP tools in `compathy_query.py`:
`compathy_layers()` (lineage + pins + cache status) and
`compathy_personas_search(query)` (local-only; the network path stays in the
CLI so an agent can't be tricked into fetching arbitrary URLs by page
content).

### Supporting command: `/compathy-layers [status | update [--layer id] | unlink]`

Small, but needed so pins aren't forever: `status` shows each layer's pin,
upstream HEAD, commits behind; `update` re-pins after showing the `index.md`
diff (same UX as persona heal's diff); `unlink` removes `lineage.json` and
the `--layer` args, leaving the local wiki intact. Could live inside
`/compathy-import` as flags; separate skill keeps SKILL.md files short.

---

## Core changes (existing files)

| File | Change |
|---|---|
| `scripts/paths.py` | `LINEAGE_FILE`, `PERSONAS_SUBDIR`, `REGISTRY_FILE`, `compathy_home()` (+`COMPATHY_HOME` override), `layers_cache_dir()`. |
| `scripts/lineage.py` (new, shared) | `load_lineage(target)`, `resolve_layers(target) -> [Path]` (child→parent), `resolve_slug(slug, layers)`, `merged_index(layers)`. Pure functions; everything else imports from here. |
| `scripts/lint.py` | `check_backlinks` resolves across `resolve_layers`; new `check_shadowing` (override policy, `extends` presence, no downward links); `check_schema_compliance` accepts the four new fields; new `check_persona_manifests` for `context/personas/*.json`. Standalone projects (no `lineage.json`) produce byte-identical output to today. |
| `scripts/compathy_query.py` | `--layer <path>` repeatable; `compathy_index` returns merged index with layer tags; `get_page` walks layers, adds `layer` + `shadowed` fields; `search` and `list_pages` accept `layer=` filter; new `compathy_layers`, `compathy_personas_search`. Bump `SERVER_VERSION`. |
| `scripts/discovery.py` | `render_context_section(project_name, lineage=None)` adds a "You are working as <role> on <team>" paragraph and reading order when linked. Sentinel unchanged → idempotent upsert still works. `upsert_mcp_json` learns to pass `--layer` args. |
| `scripts/scaffold.py` | `--from-persona <file|id>`: scaffold then call import's link step. This is what ai-quickstart Step 3 would pass through later. |
| `scripts/install.py` | `SKILLS` gains `export`, `import`, `search`, `layers`. |
| `templates/schema.md.tmpl` | Document layers, the four frontmatter fields, `personas/`, `registry.json`. |
| `templates/lineage.json.tmpl`, `templates/persona.json.tmpl` | New. |
| `SKILL.md` (main) | Phase 0 detects `lineage.json` and prints the lineage; Phase 1f/2b: "before writing a concept/entity, `resolve_slug` — if a parent already has it, **link, don't duplicate**; if you must narrow it, add `extends:`". Phase 3 reflect: also flag pages whose parent counterpart changed pin-to-pin. |
| `ARCHITECTURE.md` | New section "Federation: layers, personas, registries" + the permission model below. |

---

## Permission control for the high-order source of truth (thinking, not building)

### Principles

1. **Git is the write-permission system. compathy does not do auth.**
   Branch protection + CODEOWNERS on the org repo (or on `context/` in a
   monorepo) decide who can change org truth. Teams write only to their own
   repo/directory. This keeps compathy stdlib-only, serverless, and
   reviewable in PRs — which is the whole point of a markdown wiki.
2. **compathy enforces *structure*, CI enforces *that structure holds*.**
   `lint.py` runs in the child's CI; a team page that violates an org page's
   `override: forbidden` fails the build. The org can't be silently
   contradicted by a team; it can be *narrowed* with a visible `extends:`.
3. **Read trust comes from pins and hashes, not from trust in the network.**
   A persona names exact commits and index hashes. Import verifies both.
   Updating is an explicit, diffed action.
4. **Personas can't vouch for themselves.** Signature verification keys
   (`allowed_signers`) are fetched from the *org layer at its pin*, never
   from the persona or the team layer. A compromised team repo can't mint an
   org-trusted persona.
5. **Least privilege on join.** Import never installs anything without a
   per-category yes. Layers are cached read-only. `policy.may_edit` tells the
   agent to keep its hands off parent layers — and the parent layers aren't
   in the project's git tree anyway, so an agent editing them by accident
   affects only a local cache that the next `update` discards.
6. **Provenance survives layering.** Every resolved page carries `layer:`;
   every persona carries `provenance.exported_by/from_commit`; every import
   is logged. When a page is wrong, you can say which layer, which commit,
   who exported.
7. **Visibility is an export-time gate.** A `team`-visibility layer can't be
   embedded in a `public` persona; lint and export both refuse. This is the
   only place compathy tries to prevent *leakage* — everything else is the
   repo's access control.

### Who may do what (v1 matrix)

| Actor | Org layer | Team layer | Project layer | Personas | Registry |
|---|---|---|---|---|---|
| Org maintainer (CODEOWNERS on org `context/`) | edit, set `authority/override` | read | read | sign, export org-level personas | edit `registry.json` |
| Team lead (CODEOWNERS on team `context/`) | read; **propose** (PR) | edit; shadow per `override:` | read | export team personas | propose team entry (PR to org) |
| Team member | read | read; propose (PR) | edit | import | read |
| Automation (CI) | lint | lint + shadow check | lint | validate manifests | validate |

"Propose" = `compathy-propose` (v2): writes a provenance-tagged draft under
the *proposer's* `raw/upstream-proposals/` and, with `gh` available, opens a
PR against the parent. Until then: open the PR by hand; the wiki format is
markdown for exactly this reason.

### Mechanisms, ranked by how much they actually stop

| Mechanism | Stops | Doesn't stop | Cost |
|---|---|---|---|
| Branch protection + CODEOWNERS | unauthorized org edits | nothing about readers | 0 (repo config) |
| `override:` policy + lint in CI | teams silently contradicting org truth | teams that don't run lint in CI (document: required check) | small (lint.py) |
| Pins + index hash at import | MITM / moved tags / "HEAD drifted" surprises | a malicious commit that *is* the pin | small |
| `visibility` export gate | accidental embedding of team-private layers in public personas | someone copying files by hand | small |
| ssh signatures (Phase 3) | forged "official" personas | key compromise; unsigned personas still work with a lower badge | moderate (shell out to `ssh-keygen`) |
| Per-category consent on import | drive-by toolkit/MCP installs from a persona | a user clicking yes | small |
| Read-only layer cache | accidental edits to parents | deliberate `chmod` | trivial |

### Threat notes

- **Prompt injection via wiki content.** Parent layers are text the agent
  reads. Already true of any wiki; layering widens the blast radius to
  "anyone who can merge to the team repo". Mitigations: pins (you read what
  was reviewed), the MCP network path stays out of the agent's reach
  (`compathy_personas_search` is local-only), and `reads_first` is a
  suggestion, not an instruction channel.
- **Persona as supply chain.** `toolkit` can name repos. Import shows and
  asks; trust badge reflects signature/registry status; `warning_low_quality`
  from ai-quickstart's freshness idea can be reused when GitHub metadata is
  available (optional network).
- **Registry poisoning.** Only the org registry is trusted for team
  discovery, and it's protected by the org repo's rules. Team indexes are
  generated by export and validated by lint (ids must match file contents).
- **Stale org truth.** Not a security issue but the failure most likely to
  happen: teams pin an org SHA and never update. `compathy-layers status`
  + a lint *warning* when a pin is > N days behind upstream (opt-in network).

### Deliberately not built

- No accounts, tokens, roles, or servers inside compathy.
- No encryption of wiki content (use repo visibility).
- No automatic upward writes — every write to a parent is a PR.
- No signature *requirement* in v1; unsigned works with a visible badge.

---

## Trust score for personas (deterministic, render-time, 1–5)

| Score | Condition |
|---|---|
| 5 | Signed by a key in the org's `allowed_signers` at the org pin **and** all layers pinned + hash-verified |
| 4 | Reached via org registry → team index, pinned + hash-verified, unsigned |
| 3 | Reached via a team index or a direct URL, pinned + verified |
| 2 | Local file handed out-of-band, pinned + verified; or bundle fallback used |
| 1 | Any layer unpinned / hash mismatch tolerated with `--force` / depth or schema warnings |

Shown in search results and at import, ai-quickstart badge style
(`[trust 4]`). Never gates behavior on its own; it informs the consent
prompt.

---

## Phasing and lanes

Worktree-parallelizable like ai-quickstart's plan. Each lane = one PR.

| Lane | Deliverable | Depends on |
|---|---|---|
| **A: lineage core** | `scripts/lineage.py`, `paths.py` additions, `templates/lineage.json.tmpl`, tests. Local paths only, no network. | — |
| **B: layered lint** | `lint.py`: upward backlinks, `check_shadowing`, new frontmatter fields, manifest validation. Byte-identical output for standalone wikis (regression test). | A |
| **C: layered query** | `compathy_query.py --layer`, merged index, `get_page` layer field, `compathy_layers`, `compathy_personas_search`. | A |
| **D: export** | `export_persona.py`, `skills/compathy-export/SKILL.md`, `personas/index.json` generation, visibility gate, `--bundle`. | A, B |
| **E: import + layers** | `import_persona.py`, `layers.py`, both SKILL.md files, fetch/pin/verify, consent JSON, `discovery.py` + `scaffold.py --from-persona` hooks, import log. | A, C |
| **F: search** | `search.py`, SKILL.md, registry fetch + cache, ranking, `~/.compathy/config.json`. | E (shares fetch helper) |
| **G: permissions hardening** | ssh signing in export/import, `allowed_signers` at org pin, trust scoring module, pin-age warning, docs (`ARCHITECTURE.md` section, `schema.md.tmpl`). | D, E |
| **H: main SKILL.md + docs** | Phase 0/1f/2b/3 lineage awareness, README skills table, release notes v0.3.0. | B, C |
| **I (later): ai-quickstart bridge** | `whoami` shows active team persona; Step 3 passes `--from-persona`; team persona appended to `~/.ai-quickstart` persona as a `pinned`, `locked` paragraph on user consent. Lives in ai-quickstart, bumps `COMPATHY_VERSION`. | E, G |

Waves: **1** = A. **2** = B, C (parallel). **3** = D, E (parallel). **4** = F,
G, H (parallel). **5** = I.

Ship gate for v0.3.0: waves 1–4; `/compathy` on a standalone wiki unchanged;
end-to-end test: org fixture → team fixture → export → import into temp
project → `compathy_query` resolves an org slug through the lineage → lint
clean → shadow of `override: forbidden` page fails lint.

---

## Tests (unittest, stdlib, tempdirs, `tests/fixtures/`)

- `test_lineage.py` — load/resolve/merged_index; depth cap; missing layer path → clear error; no `lineage.json` → single-layer list.
- `test_lint.py` additions — upward backlink resolves; downward link errors; `override` matrix (forbidden/narrow/free × with/without `extends`); standalone golden-output regression.
- `test_compathy_query.py` additions — `--layer` ordering; shadowed page returns child + `shadowed_parent`; `compathy_layers`; local persona search.
- `test_export_persona.py` — refuses dirty tree; refuses lint errors; pins = HEAD; visibility gate; index regenerated; bundle contents.
- `test_import_persona.py` — file/url/id resolution (url via monkeypatched urllib); pin mismatch aborts atomically (no partial link); idempotent re-import; update path shows diff; consent JSON shape; `.mcp.json` only our key touched; CLAUDE.md sentinel replaced not duplicated.
- `test_search.py` — ranking determinism; per-team soft-fail; cache TTL; `--local`.
- `test_layers.py` — status/update/unlink; cache read-only.
- `test_trust_persona.py` — score table; signature verify mocked (`ssh-keygen` absent → unsigned path, never crash).
- `tests/e2e/test_federation_roundtrip.py` — the ship-gate scenario above.
- Fixtures: `fixtures/org-layer/`, `fixtures/team-layer/` (small wikis with `authority/override` variety), `fixtures/personas/*.json` (valid, unsigned, tampered hash, wrong kind).

---

## Open decisions (need the user)

1. **Repo topology.** One repo per layer (`compathy-org`, `payments`) vs a
   monorepo with `context/` + `teams/*/context/`. The layer spec supports
   both (`source` + `path`); CODEOWNERS granularity differs. Default assumed:
   **per-repo**, monorepo supported.
2. **Where personas live.** Inside the team's compathy repo
   (`context/personas/`, assumed) vs a separate org-wide personas repo.
   Assumed inside — keeps ownership with the team and discovery via the org
   registry.
3. **Naming.** Keep `persona` (assumed) vs `role`. See collision note.
4. **ai-quickstart coupling.** Should importing a team persona write into the
   user's `~/.ai-quickstart` persona (Lane I), or stay one-way (ai-quickstart
   reads compathy's active persona)? Assumed: opt-in, Lane I, later.
5. **Signing in v1 or v1.1.** Assumed v1.1 (Lane G ships after export/import
   work end to end unsigned with badges).
6. **Network in lint.** Pin-age warning needs `git ls-remote`. Assumed
   opt-in flag (`--check-upstream`), default off — lint stays offline.
7. **Bundle format.** `.tar.gz` of manifest + `wiki/` snapshots (assumed) vs
   a single concatenated markdown. Tar keeps slugs/paths intact for the same
   resolver code.

---

## Risks

1. **Pins rot.** Teams pin org once and never update → org truth diverges
   from what agents read. Mitigation: `layers status` in the summary line of
   every `/compathy` run when linked; opt-in staleness warning.
2. **Shadow policy is only as good as CI.** A team that skips lint can
   contradict org pages locally. Mitigation: document "lint as required
   check"; import prints a warning if a team layer's last commit has no
   passing lint marker (v2: teams commit a `.compathy-lint.json` receipt).
3. **Three "persona" meanings.** Mitigation: two-word form everywhere, `kind`
   field, and a rename escape hatch decided in review.
4. **Layer cache size.** Shallow clones at a pin are small; worktrees of a
   large monorepo are not. Mitigation: `git sparse-checkout` limited to
   `path` when available; fall back to full shallow clone with a size line
   in the consent prompt.
5. **Scope creep toward a platform.** Registry → someone wants a web UI,
   accounts, analytics. Guardrail: everything is files in git; if a feature
   needs a server, it's out.

---

## Not in scope (v0.3.0)

- `compathy-propose` (upward PR helper) — v2.
- Depth > 3, multiple parents (diamond lineage) — v2, needs a merge policy.
- Encrypted or private-content redaction on export — use repo visibility.
- Web dashboard / registry browser — no server.
- Live GitHub org scanning for undeclared team compathies — optional later;
  org registry is the truth on purpose.
