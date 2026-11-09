---
name: compathy-persona
description: Federate compathy wikis across a company. Export a team persona (role + pinned org/team layers + toolkit), search an org registry for the team and persona to adopt, and import one so a new member's agent instantly reads the right layers in the right order. Also sync, status, update, unlink, whoami. Use when the user says "export a persona", "join a team", "import the payments persona", "search personas", "link this project to the org wiki", "update pins", or "who am I working as".
---

# compathy-persona

You are orchestrating `compathy-persona`, one skill with subcommands (the
same shape as `/ai-quickstart heal`). Every subcommand is a Python call
that prints JSON; you render it, ask the user the questions that need a
human, and run the next call. Python does pins, hashes, fetching,
verification and file writes. You do interviews, choices, and the
briefing.

**Invocation**: `/compathy-persona <subcommand> [args]`

| Subcommand | Who runs it | What it does |
|---|---|---|
| `export <role>` | a team lead (or org maintainer) inside their compathy | writes `context/personas/<role>.json` and regenerates `personas/index.json` |
| `search [query]` | anyone | ranks personas found via the org registry (or `--local`, or `--source <layer>`) |
| `import <file \| https-url \| org/team/role \| source#role \| path.bundle#role>` | a member, inside the project that should join | plan, consent per category, fetch + verify layers, link, toolkit, watch, briefing |
| `sync` | anyone who cloned an already-linked repo | fetches the layers `context/lineage.json` names |
| `status` | anyone in a linked project | pins, cache, verification, optional upstream check |
| `freshness` | anyone in a linked project | has a parent published new pages since the pin? (cached 6h) |
| `update [--layer id]` | project owner | re-pins after showing changed pages, publish notes, and this project's pages that link to them |
| `unlink` | project owner | removes `lineage.json` + `persona.json`, keeps the wiki |
| `whoami` | anyone | active persona + lineage for this project |

`{skill_dir}` is the directory containing this SKILL.md. Scripts are at
`{skill_dir}/../../scripts/`. Use `P="{skill_dir}/../../scripts/persona.py"`.

Vocabulary: a **layer** is one compathy `context/` (org, team, or
project). A child names its parents in `context/lineage.json`; parents
are pinned to a commit and cached read-only under `~/.compathy/layers/`,
never copied into the child. A **persona** is the join token: a JSON
manifest with the role, the pinned layers, what to read first, a toolkit,
and provenance.

---

## Phase 0 (every subcommand) - self-update, then route

```bash
python3 {skill_dir}/../../scripts/update.py
```

Then dispatch on the first argument. If it is missing or unknown, run
`whoami` and show the table above.

---

## `export <role>`

Run inside the team (or org) compathy. Precondition: the wiki is
committed, lint-clean, and `context/` has no uncommitted changes (the
pin must be reproducible).

### E1. Propose

```bash
python3 $P export propose --target . --role <role> [--layer-id acme/payments] [--source git+https://...]
```

- `layer.id` comes from `context/lineage.json` (`self.id`) when this repo
  joined an org with `import --as team --self-id`. A root org compathy
  has no lineage; pass `--layer-id acme` then.
- `source` is derived from the `origin` remote. Pass `--source` if the
  team's canonical URL differs.
- If `preconditions.git_clean` is false or `lint_errors > 0`, stop and
  tell the user exactly what to commit or fix. Do not pass
  `--allow-dirty` / `--allow-lint-errors` unless the user insists.
- If `needs` is `online-source`, this wiki has no online source of truth:
  no `origin`, or one that is a path on this machine. A persona must pin a
  repository teammates can reach, so tell the owner and ask whether to
  create a private one now. With their yes and the `gh` CLI:
  `gh repo create <org>/<name> --private --source . --remote origin --push`.
  Otherwise they create it in their git host, then `git remote add origin
  <url>` and `git push -u origin HEAD`. Run `export propose` again. Pass
  `--allow-local-source` only when the owner says this is a test or an
  air-gapped setup.

### E2. Interview (one question at a time)

1. **Title and summary**: propose `spec_template.title`; ask for a
   one-line summary of what this role owns.
2. **Read first**: from `candidates.patterns`, `candidates.authoritative`,
   and `candidates.mentioning_role`, propose 3-7 slugs a new member should
   read first. Include the parent org's key patterns page when one exists
   (write it as `acme/<slug>`). The user confirms or edits.
3. **Responsibilities and tags**: propose from entity/concept pages
   mentioning the role; the user confirms.
4. **Toolkit**: ask whether the team wants to ship skills (name + GitHub
   `owner/repo`) or MCP servers (`id`, optional `command`/`args`,
   `description`). Default is compathy itself.

Write the answers to a spec file, for example
`context/compathy-reports/persona-<role>.spec.json` (gitignored dir, see
`compathy-augment`), shaped like `spec_template`.

### E3. Write

```bash
python3 $P export write --target . --role <role> --spec <spec-file> [--layer-id ...] [--source ...]
```

Errors name the missing slug or the failed precondition; fix and re-run.
On success show the persona id, the pinned layers, and remind the user:

> commit `context/personas/` so `/compathy-persona search` can find it.
> If this is a new team, an org maintainer must also add it to the org
> `registry.json`: `persona.py registry add-team --id <team> --source <git url>`.

---

## `search [query]`

```bash
python3 $P search "<query>" [--org git+https://github.com/<org>/compathy-org.git] [--refresh]
```

The org source resolves from `--org`, then `~/.compathy/config.json`
(`persona.py config set-org <source>`), then the current project's
lineage. If none is known, ask the user for the org compathy repo URL and
offer to save it with `config set-org`. `--local` lists only personas
already on this machine (no network).

Render a ranked list:

```
 #  persona                               trust  why
 1  acme/payments/backend-engineer        [4]    matches backend, payments; tags backend
 2  acme/team-lead                        [4]    updated recently
```

Trust is deterministic: 4 = reached via the org registry and pinned; 3 =
direct URL; 2 = file handed out-of-band; 1 = a layer failed
verification. 5 is reserved for signed personas (v1.1). Show `warnings`
(unreachable teams) verbatim. Offer "import #N".

If the user has an ai-quickstart persona at
`~/.ai-quickstart/persona/persona.json`, you may mention role overlap as
a hint. Never auto-pick.

---

## `import <arg>`

Run inside the project that should join. An empty directory is fine: the
link step scaffolds `context/` first.

### I1. Plan (no side effects beyond the registry cache)

```bash
python3 $P import <file|https-url|org/team/role|source#role|path.bundle#role> --target . [--org ...]
```

A **layer source** is a published core (see `/compathy-publish`): the
private repo, `git+https://github.com/<org>/<core>.git#marketing`, or the
bundle file kept in a shared drive, `"<path>/<core>.bundle#marketing"`.
Without `#role` the error lists the roles the layer publishes
(`search --source <source>` lists them with summaries).

Show the user, compactly:

- persona title, summary, responsibilities, `reads_first`
- each layer: id, role, source, pin (12 chars), cached or not
- trust and `trust_reasons` (say "provisional" when
  `trust_provisional` is true)
- `toolkit` items (skills to install, MCP servers to register) and `policy`
- every entry in `warnings` verbatim
- if `already_linked_at_these_pins`: say so and stop unless the user wants
  `--force`
- if `pin_changes` is non-empty: this is an update of an existing link;
  the apply step will print an `index.md` diff per layer
- if `mirror` is set (an import from a bundle): the layer is read from the
  bundle on this machine, while `lineage.json` keeps the layer's canonical
  source, so the file stays the same for every teammate. Pins and tree
  hashes are verified either way, and later reads try the online
  repository first, falling back to the bundle.
- if `blockers` is not empty (`needs: online-source`): stop. The persona
  points at a path on its exporter's machine, so this project could not
  stay linked to the source of truth. Tell the user to ask `owner` to
  publish it to a private repository, then import from there.
  `--allow-local-source` is only for tests and air-gapped setups the user
  confirms.

### I2. Consent, four separate yes/no questions

Ask in this order, one at a time, with your harness's interactive prompt:

1. **fetch** the layers into `~/.compathy/layers/` (read-only cache)?
2. **link** this project (write `context/lineage.json`, `context/persona.json`,
   an `entities/persona-<role>.md` page, the "Federation" block in CLAUDE.md,
   AGENTS.md and README)?
3. **toolkit**: register the listed MCP servers in `.mcp.json` and print the
   skill install commands?
4. **watch** (Claude Code): add a SessionStart hook to
   `.claude/settings.local.json` that checks, at most every six hours,
   whether a parent layer was republished, and tells you when it was?

Never run the apply step with a consent the user did not give. If the
user declines everything, stop; nothing was written.

When the repo being linked is itself a **team** compathy joining the org
(the persona is org-level, for example `acme/team-lead`), pass
`--as team --self-id <org>/<team>` so lint knows this layer's authority
and later exports know their id.

### I3. Apply

```bash
python3 $P import <arg> --target . --apply --consent fetch,link,toolkit,watch [--as team --self-id acme/payments] [--project-name <name>]
```

Any subset of `fetch,link,toolkit,watch`. A fetch or verification failure
aborts before anything is linked; report the layer named in the error.

### I4. The briefing (this is the point of the whole feature)

The result's `briefing` lists the resolved `reads_first` pages with
absolute paths. Read them (or call `compathy_get_page` via the
`compathy-wiki` MCP server). Then brief the user in under 20 lines:

- what this team owns and how it fits the org (from the org + team index)
- the 3-5 conventions they must follow, each with its page slug
- who or what to ask (entity pages, on-call, owners) when known
- what the agent will and will not edit (`policy`)

End with:

```
compathy-persona: joined <persona id> [trust N]
  layers:   <org>@<pin> -> <team>@<pin> -> this project
  linked:   context/lineage.json, context/persona.json, CLAUDE.md, AGENTS.md
  toolkit:  <N mcp servers registered, M skill commands printed>
  next:     run /compathy to compile this project against its lineage
```

---

## `sync`

For someone who cloned an already-linked repo (`lineage.json` present,
`~/.compathy/layers` empty):

```bash
python3 $P sync --target .
```

Fetching is reading what a teammate already pinned, so tell the user what
will be fetched (ids, pins, sources) and proceed. If `ok` is false, show
the failing layer and its error; nothing else changes.

---

## `status` / `update` / `unlink` / `whoami`

```bash
python3 $P status --target . [--check-upstream]     # network only with the flag
python3 $P freshness --target . [--ttl 0] [--format line]  # one ls-remote per layer, cached 6h
python3 $P update --target . [--layer <id>] [--to <sha>]   # plan: what changed and what it touches here
python3 $P update --target . [--layer <id>] --apply         # re-pin after the user reads it
python3 $P unlink --target .
python3 $P whoami --target .
```

`freshness` compares the upstream's wiki tree with the pinned one, so a
commit that only touched personas or a README is not news. With
`--format line` it prints one sentence when a parent was republished and
nothing otherwise; that is what the watch hook runs. When you see that
sentence, tell the user and offer `update`.

`update` is two-step on purpose. For each layer that moved, show:

- `changed_pages`: added, modified and removed parent pages, with titles
- `publish_notes`: the parent's log entries since the old pin (the owner's
  publish notes)
- `affected`: this project's pages that link to a modified or removed
  parent page, or narrow a modified one
- `index_diff` when the user wants the detail

Ask before `--apply`. Pins are bumped on purpose, never pulled from HEAD
silently. `same-content` means the upstream moved without changing any
page; re-pinning is harmless. After applying, revise each `affected` page
against the parent pages it links to (read them with `compathy_get_page`),
replace links to removed pages, then run `/compathy` so lint is clean.

Bundle readers: `config set-mirror <canonical source> <path>.bundle` points
this machine at a different copy of a layer (import sets it for you);
`config unset-mirror <source>` removes it.

`whoami` renders in under 12 lines: persona id and title, self id/role,
each layer with pin and cached/verified flags, and `needs_sync`.

---

## Org maintainer helpers

```bash
python3 $P registry init --target . --org acme
python3 $P registry add-team --target . --id payments --source git+https://github.com/acme/payments.git [--path context]
python3 $P config set-org git+https://github.com/acme/compathy-org.git
```

The org registry is the only trusted path to discover teams. A team that
is not listed is not discoverable; that is the permission model applied
to discovery. Teams ask for a registry entry via PR to the org repo.

---

## Rules

1. **Never link without consent, never install without consent.** Four
   questions, four flags. No consent, no write.
2. **Pins are the truth.** Import and sync refuse a layer whose HEAD or
   wiki tree hash does not match. Do not suggest `--force` to get past a
   verification failure; it only re-fetches.
3. **Parents are read-only.** Never edit anything under
   `~/.compathy/layers/`. Propose changes upstream via PR (a team lead can
   pull strong patterns *up* with `/compathy-augment` run in the parent).
4. **Shadowing is opt-in.** Only parent pages that declare `authority:`
   carry an `override` policy. Everything else is layer-local; each layer
   keeps its own `technical-patterns`.
5. **Registry first.** For discovery and for cross-checking a file handed
   out-of-band, the org registry decides. A byte-identical match lifts
   trust to 4.
6. **Log everything.** Export, import, update, unlink each append to
   `log.md`; imports also append to `~/.compathy/import-log.jsonl`.
7. **No em dashes** in anything you write into the wiki.
