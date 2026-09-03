# v0.3.0: Federation (org layer, team layers, personas)

A company can now keep one high-order compathy, teams can keep their own
specialized sub-compathy that extends it, and any member joins instantly by
importing the team's exported persona. Parents are pinned, verified, and
cached read-only; they are never copied into a child wiki.

Stdlib-only. No new dependencies. Standalone wikis are unaffected: every
new check, field, and tool is additive, and `lint.py` output for an
unlinked project is unchanged (one pre-existing false positive fixed, see
below).

## What's new

- **`/compathy-persona`**: one skill, subcommands `export`, `search`,
  `import`, `sync`, `status`, `update`, `unlink`, `whoami`, `resolve`,
  plus `registry` and `config` helpers for org maintainers. Python does
  pins, hashes, fetching and writes; the model interviews, asks consent,
  and briefs the new member from the persona's `reads_first` pages.
- **Layers and lineage.** `context/lineage.json` names a project's parent
  layers (id, role, git source, path, commit pin, wiki tree sha) and the
  project's own role. Caches live under `~/.compathy/layers/<id>/<pin>/`
  (override with `COMPATHY_STATE_HOME`). Depth cap 3.
- **Persona manifest** (`context/personas/<role>.json`, committed): the
  exporting layer plus its parents at exact pins, `reads_first`,
  `responsibilities`, a `toolkit` in ai-quickstart's suggestion shape, an
  advisory `policy`, provenance, and a reserved `signature` slot.
  `personas/index.json` is generated.
- **Two join paths.** Import a persona (file, https URL, or
  `org/team/role` resolved through the org registry), or clone an
  already-linked repo and run `sync`. `.mcp.json` carries no per-layer
  arguments; the MCP server reads `lineage.json` itself.
- **Layered lint.** Backlinks resolve upward into cached parents; uncached
  parents downgrade unresolved links to warnings. Shadowing is opt-in: only
  parent pages with `authority:` carry an `override` policy (`forbidden`,
  `narrow` with required `extends:`, `free`); every other same-slug
  collision is layer-local. A page may only claim its own layer's
  authority. Persona manifests, `personas/index.json`, and `registry.json`
  are validated.
- **Layered MCP server** (`compathy_query.py` 0.2.0). `compathy_get_page`
  resolves nearest layer first and returns `layer` and `also_in`; `search`
  and `list_pages` span layers and accept a `layer` filter; `compathy_index`
  carries parent indexes; new `compathy_layers` and local-only
  `compathy_personas_search`. `--layer <dir>` adds ad-hoc layers.
- **Verification.** Layers are fetched by partial clone + sparse checkout
  at the pin into a temp dir, verified (HEAD == pin, `git rev-parse
  HEAD:<path>/wiki` == tree sha), made read-only, then renamed into the
  cache. A mismatch caches nothing and links nothing.
- **Discovery.** The org's `context/registry.json` lists teams; search is
  a two-hop sparse fetch with a 6h cache revalidated by one `ls-remote`.
  Personas handed out-of-band are cross-checked against the registry; a
  byte-identical match lifts trust from 2 to 4.
- **Breadcrumbs.** The CLAUDE.md/README context section gains a
  "Federation" block (persona, reading order org -> team -> project, cache
  paths, read-first pages). `scaffold.py --from-persona` scaffolds and links
  in one step. The main `/compathy` skill detects lineage in Phase 0, syncs
  missing caches, and resolves slugs before creating pages.
- **Trust badge** (1-5, deterministic): 4 via registry or byte-identical
  to it; 3 direct URL; 2 local file; 1 any layer unverified. 5 is reserved
  for signed personas (v1.1).

## Permission model (designed and enforced where compathy can)

Git decides who writes a layer (branch protection, CODEOWNERS). Lint in
CI decides whether a child respects `authority`/`override`. Pins and tree
hashes decide what an importer trusts. The org registry is the only path
to discover teams. Three separate consents on import (fetch, link,
toolkit). Every import is logged to the wiki `log.md` and to
`~/.compathy/import-log.jsonl`. No accounts, tokens, or servers. Upward
flow in v1 is pull-model via `/compathy-augment` run by the parent's owner.
Full matrix and threat notes in `ARCHITECTURE.md`.

## Fixes

- `lint.py` no longer reports `index-stale: log` on a fresh scaffold (the
  index template's `[[log]]` link was counted as a missing page).

## Compatibility

- Backwards-compatible with v0.2.x scaffolds. No `schema_version` bump; the
  three new frontmatter fields are optional.
- Python 3.9+, git 2.25+ (sparse-checkout). Partial clone falls back to a
  plain clone when a server refuses filters.
- New skill: `python3 scripts/install.py --claude --skill persona` (or
  re-run the installer; `all` now includes it).

## Known limits

- Signatures, offline bundles, and a `visibility` export gate are deferred
  to v1.1; the manifest reserves the fields.
- One persona per project; depth 3; single parent chain (no diamonds).
- `status --check-upstream` reports whether the remote HEAD differs from
  the pin, not how many commits behind.
- Clearing the cache needs `chmod -R u+w ~/.compathy/layers` first (it is
  deliberately read-only).
