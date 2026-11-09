---
name: compathy-publish
description: Publish a core subset of this compathy wiki so teammates in other functions (marketing, sales, design) can link their own wikis to it and stay current. Marks pages by visibility, cuts private sections, scans for leaks, builds a read-only core layer in a private git repo and an optional shared-drive bundle, and writes one persona per audience. Use when the user says "publish my wiki", "share my context with the team", "publish the core", "what would I publish", "republish", or "who can see my wiki".
---

# compathy-publish

You are running `compathy-publish` for the **owner** of a wiki. The owner
keeps one full, private compathy. Pages marked `visibility: org` (or
`team`, `public`) form the shared core. Publishing copies only those pages
into a separate git repository, the **core repo**, which teammates link to
as a pinned, read-only parent layer with `/compathy-persona import`. Their
own wikis stay theirs to edit.

Python does selection, transforms, the leak scan, git and the bundle. You
choose pages with the owner, explain the plan, and ask before anything
leaves the machine.

`{skill_dir}` is the directory containing this SKILL.md. Scripts are at
`{skill_dir}/../../scripts/`. Use `P="{skill_dir}/../../scripts/publish.py"`
and `Q="{skill_dir}/../../scripts/persona.py"`.

| Subcommand | What it does |
|---|---|
| `init` | write `context/publish.json` and create (or clone) the core repo |
| `mark <slugs> --visibility V` | set or clear `visibility` on pages |
| `plan [--diff] [--note T]` | build in a temp dir: pages, unlinked links, private sections, scan, lint, diff |
| `apply --note T` | write the core repo and commit content, then personas pinned to it |
| `push [--confirm-private]` | push to the remote (refused if it is public) and refresh the bundle |
| `remote <url> [--confirm-private]` | set the core's online source of truth (config and origin; refuses a public repo) |
| `status` | what is marked, the core repo's HEAD, the last publish, the bundle's state |

Every call prints JSON. Exit 1 means `error` holds a message for the owner.

---

## Phase 0 - self-update, then route

```bash
python3 {skill_dir}/../../scripts/update.py
```

If `context/publish.json` exists, run `status` and show it in under 10
lines; otherwise start at **Init**. Then do what the owner asked.

---

## Init (once per wiki)

Ask one question at a time; propose answers from the wiki.

1. **Layer id and title.** `<org>/<area>` (for example `acme/pricing`) and a
   title such as "Acme pricing core".
2. **Audience.** `org` (everyone in the company) unless the owner says
   `team`. Pages publish when their visibility reaches the audience:
   private < team < org < public.
3. **Core repo and its online source of truth.** A local folder outside
   this project (for example `~/Code/<area>-core`) and a **private** remote
   in the company's git host. The remote is required: personas pin it and
   teammates' `lineage.json` records it, so a bundle alone is not enough.
   If the owner has none, ask whether to create one now; with their yes and
   the `gh` CLI, `gh repo create <org>/<name> --private`. Never create a
   public one. `allow_local_source` in `publish.json` is only for tests and
   air-gapped setups the owner names.
4. **Bundle (optional).** For readers without access to the git host: a
   `.bundle` path inside a synced folder limited to the company, such as a
   Google shared drive mounted by Drive for desktop
   (`~/Library/CloudStorage/GoogleDrive-<email>/Shared drives/<drive>/<name>.bundle`).
5. **Owner and email domains.** Name, email, git host handle (it becomes
   CODEOWNERS) and the company email domains the scan should allow.

```bash
python3 $P init --target . --layer-id <id> --title "<title>" --repo <path> \
  [--remote git+https://github.com/<org>/<name>.git] [--bundle "<path>.bundle"] \
  --owner-name "<name>" --owner-email <email> [--owner-github <handle>] --email-domain <domain>
```

If the result says `needs: online-source`, create the repository as in
step 3 and set it (the command refuses a public repository):

```bash
python3 $P remote <url> --target .
```

Then add two things to `context/publish.json` with the owner:

- `scan.deny`: regular expressions for terms that must never leave (client
  code names, the source of a sample dataset, internal project names).
  Matching is case-insensitive. The list stays in the owner's project.
- `personas`: one per audience role, for example `marketing`, `sales`,
  `design`, each with `title`, `summary`, `tags`, `reads_first` (3 to 7
  published slugs a newcomer should read first) and `responsibilities`.

## Choose the pages

Read `index.md` and propose a core set. Good candidates: what the product
does, features and their names, categories, value claims the owner stands
behind, naming and writing rules, design rules, the shareable roadmap.
Leave out unless the owner insists: customer and contract specifics,
people pages, notes on raw or patient-level data, internal planning and
financials, engineering internals, anything under a data handling rule.

Show the proposal as a short table (slug, why), get the owner's yes, then:

```bash
python3 $P mark <slug> <slug> ... --visibility org --target .
```

For a page that is mostly shareable, wrap the internal part:

```markdown
<!-- private -->
Internal detail that stays in the owner's wiki.
<!-- /private -->
```

Unmarked pages stay private. New pages are private until marked.

## Plan

```bash
python3 $P plan --target . --diff --note "<one line on what changed>"
```

Render, compactly:

- pages published and private; added, changed, removed since the last publish
- `unlinked_total` and the pages with the most unlinked links (links to
  private pages become plain text: the page title or the link label). If
  a sentence reads badly in the diff, give the link a label in the source
  page, `[[slug|words that fit the sentence]]`
- private sections cut, frontmatter fields dropped (`sources`,
  `related_paths` and anything not on the allowlist never leave)
- every scan finding as `rule  source.file:source.line  context`, where
  `source` is the place to fix it in this wiki (`file:line` is the
  published copy); the context masks the match, and you never print or
  guess the matched text
- lint errors in the built layer, and `blockers`
- the diff file path (`context/compathy-reports/publish-<date>.diff`);
  summarize the changed pages from it in a few lines

A `no online source of truth` blocker means the core has no private remote
yet: offer to create one and run `remote <url>` as in Init.

Fixing findings, in order of preference: edit the source page, wrap the
passage in a private section, or unmark the page. An allow entry
(`scan.allow`: `rule`, optional `file` and `fingerprint`, and a `reason`) is
for false positives only, and only with the owner's explicit yes.

## Apply (owner's yes)

Ask for the one-line note (draft it from the diff), then:

```bash
python3 $P apply --target . --note "<note>"
```

It writes the core repo, commits the pages (`publish: <note>`), then
commits the personas pinned to that commit. Nothing leaves the machine.

## Push (a separate yes)

```bash
python3 $P push --target .
```

- The remote must refuse anonymous reads. If `push` reports that the
  repository answers without credentials, stop: it is public. If it cannot
  tell (an unknown host), ask the owner to confirm the repository is
  private before passing `--confirm-private`.
- The bundle is written to a temp file, verified, then renamed into place.

After the first push, give the owner what to send teammates (from the core
repo's README):

```
/compathy-persona import <remote>#<role>
/compathy-persona import "<path to the bundle on your machine>#<role>"   # Drive readers
```

and the access checklist:

- teammates get read access only: a team with Read on the private repo,
  Viewer on the shared drive
- only the owner can write: protect the default branch, keep the drive's
  Manager and Content manager roles to the owner
- each teammate has git, compathy and, for the bundle, Drive for desktop

## Republish

The same loop: edit the wiki as usual, `plan`, `apply`, `push`. Teammates'
`/compathy-persona freshness` (or their session-start hook) sees the new
publish within six hours, and their `update` lists the pages that changed
and which of their own pages link to them.

---

## Rules

1. **Nothing leaves without a yes.** Marking pages, allow entries, `apply`
   and `push` each need the owner's explicit yes.
2. **Never print a scan match.** Refer to findings by rule, file, line and
   fingerprint.
3. **The core repo is written only by `apply`.** Never edit it by hand;
   corrections go into the owner's wiki and the next publish.
4. **Private by default.** Never mark a page the owner did not approve, and
   never widen an audience without asking.
5. **Never push to a public repository**, and never pass
   `--confirm-private` on your own judgment.
6. **No em dashes** in anything you write into the wiki.
