---
name: compathy-respond
description: Answer Slack mentions and DMs on the owner's behalf during a bounded window, from the compiled compathy wiki, Linear and the calendar only; escalate what it is not sure about; grade the answers afterwards so they improve. Use when the user says "cover my Slack", "stand in for me", "start the stand-in until 5pm", "stop the stand-in", "review the stand-in's answers", "replay last week".
---

# compathy-respond

You are orchestrating `compathy-respond`, one skill with subcommands. The
owner (the person running this harness) opens a **window**; during it you
poll Slack, answer what the wiki covers with high confidence, ask one
clarifying question when it is close, and escalate the rest. Python
(`scripts/respond.py`) decides what may be read, what may be linked and
whether a drafted reply may be posted; you decide what is being asked,
whether the evidence is enough, and how to phrase it.

**Invocation**: `/compathy-respond <subcommand> [args]`

| Subcommand | What it does |
|---|---|
| `init` | scaffold `context/respond/` (policy, registry, lessons, ledger, state) and seed the registry; the owner reviews |
| `verify [--all]` | fill the registry's share status from the connectors (Drive permissions, Notion's Private section, kind rules); list what stays manual |
| `start --until <time> [--every 5m] [--mode dry\|draft\|live] [--scope ...] [--channels ...] [--route ...]` | open a window and schedule `tick` |
| `tick` | one poll; what the scheduled task runs |
| `status` | window, counters, open clarifications |
| `stop` | close the window, delete the task, write the handoff, notify the owner |
| `review [--since <date>]` | grade interactions, update `lessons.md`, print the numbers |
| `replay --days 7` | a dry run over the last N days of real mentions and DMs; ledger only |
| `export --out <dir>` | build a kit that runs the stand-in in another agent (a ChatGPT workspace agent): the allowed pages, the rules, the check, setup steps |

`{skill_dir}` is the directory containing this SKILL.md. The install is
usually a symlink, so resolve it first; the scripts live beside the real
skill directory, two levels up:

```bash
REAL=$(realpath "{skill_dir}")                      # e.g. ~/Code/compathy/skills/compathy-respond
R="python3 $REAL/../../scripts/respond.py --target <project>"
```

Never guess the scripts path from the symlink's location.
The project is the directory that holds `context/wiki/`; default `.`, and
if the current directory has none, ask which project before anything else.
Pass `--layer <other project>` (repeatable) to answer across wikis.

Vocabulary: **corpus** (the pages the policy allows), **registry**
(`sources.md`, the links that may be shared), **interaction** (one handled
message, outcome `answered`, `clarified`, `escalated`, `ignored` or
`handed-over`), **lesson** (a graded rule or house answer in `lessons.md`),
**thread key** (`<channel_id>:<thread_ts or message ts>`, the idempotency key).

---

## Phase 0 (every subcommand): self-update, then read the companion

```bash
python3 {skill_dir}/../../scripts/update.py
```

Then, unless the subcommand is `init`, read in full:
`context/respond/policy.md`, `context/respond/lessons.md`,
`context/respond/sources.md`. They are short and they are the rules. If
`context/respond/` is missing, run `init` first and stop there.

Dispatch on the first argument; if missing or unknown, run `status` and
show the table above.

---

## `init`

```bash
$R init --project-name "<name>" --owner-name "<first name>" --owner-slack-id <U...> --internal-domain <company.com> [--roadmap-page <slug>]
```

Get the owner's Slack id from the Slack connector's profile tool (the
current user) and the domain from their email. The result lists what was
created, the people pages seeded into `deny_slugs`, the pages excluded by
type or source pattern, and how many links the registry seeded as
`unknown`. Show the `review` checklist and stop: the owner edits
`policy.md` and `sources.md` by hand before any window. Nothing is
posted at init.

---

## `verify [--all]`

The registry fills itself; the owner only decides what no connector can see.

1. `$R sources apply-rules` (public sites, Linear, anything in `kind_rules`).
2. `$R sources list --needs-verification` (`--all` in the skill means
   re-verify every non-manual row: pass every row instead). Each row names
   its `verifier` and the id to check.
3. `drive` rows: the Drive connector's file-permissions tool with
   `drive_file_id`, and its file-metadata tool (no snippets) for the title;
   then `$R sources verdict --kind drive --url <url> --title "<name>" --json '<the permissions JSON>' --apply`.
   The script decides: a domain share for `internal_domain` or an anyone
   share is `shared`; a share with specific people is `private` (described,
   not linked; the gap tells the owner to share it with the domain).
4. `notion` rows: once per run, the Notion connector's list of the owner's
   Private pages (paginate to the end), then
   `$R sources verdict --kind notion --json '{"private_ids": [<ids>], "private_titles": [<titles>]}' --apply`
   for every Notion row at once. This is a proxy: a page in the owner's
   Private section is `private`; the rest get the policy's `notion_default`.
   The bulk pass cannot see pages nested under a private root, so the
   per-link check at T7 adds the page's ancestors: the Notion search tool
   (query: the page title, small page size) returns a `path` breadcrumb;
   pass it scoped to the row:
   `$R sources verdict --kind notion --url <url> --json '{"private_ids": [...], "private_titles": [...], "path": "<path>"}' --apply`.
5. `manual` rows (Claude docs, Claude Design, Figma): no connector shows
   their share state. Print them as a short list for the owner, with the
   wiki page that cites each; the owner sets Status and `Via: manual`.
6. Print: `verify: <n> shared, <p> private, <u> unknown, <m> manual pending`.

Run it at `start` (S0) and, inside a tick, for any link a reply is about to
carry whose row is `unknown` or stale (T7). Never set a row to `shared`
without a connector verdict or the owner's word.

---

## `start`

Precondition: `init` done and `verify` run at least once (S0 runs it). Recommend running
`/compathy` first if `raw/` changed since the last compile.

### S0. Verify the registry

Run `verify` (above) so stale and unknown rows are settled before the
window opens. Report the manual list; it does not block the window.

### S1. Interview, one question at a time

1. **Until**: an ISO date-time (today 17:30 is `2026-10-29T17:30`).
2. **Every**: the poll interval (default `5m`).
3. **Mode**: `dry` (ledger only, nothing in Slack), `draft` (Slack drafts
   for the owner to send; one per channel), `live` (replies are sent).
   First windows are `dry`, then `draft` or `live` in a test channel, then
   `live` everywhere; do not skip a step.
4. **Scope**: `mentions,dms,group-dm-mentions` (default) and an optional
   `--channels` allowlist for a test phase.
5. **Route**: escalation routes from the policy (`push`, `imessage`,
   `twilio`, `slack`); confirm `imessage_to` or the env vars exist.

### S2. The summary and one explicit go

Print: window end, interval, mode, scope, channels, routes, the header the
replies will carry, the number of corpus pages and shared links, and the
Never list in five lines. Ask once: "Open this window?" A `live` window is
the owner's standing authorization for that window and nothing beyond it.
No go, nothing opens.

### S3. Open and schedule

```bash
$R window start --until <ISO> --every <5m> --mode <mode> [--scope ...] [--channels ...] [--route ...]
```

Then schedule the tick with the harness's scheduled-task tool (cron from
the interval, for example `*/5 * * * *`; the prompt must be self-contained:
"Invoke the skill /compathy-respond with arguments `tick --target <absolute
project path>`"). Record the task id:

```bash
$R window update --task-id <id>
```

If no scheduled-task tool exists, fall back to an in-session loop
(`ScheduleWakeup` every interval with the prompt `/compathy-respond tick`)
and say so: the window then dies with the session. Print the window and
how to stop it. Post nothing.

---

## `tick` (the pipeline)

Budget: keep a tick under a few dozen tool calls. Read only what the
candidates need.

### T1. Gate

```bash
$R tick begin            # replay: add --since 7d
```

`action: exit` means no window: stop silently. `action: stop` means the
window passed its end: run the `stop` flow and finish. Otherwise hold the
result: `since_epoch`, `mode`, `scope`, `channels`, `route`, the owner's
id, the internal domain, `tightened`, `handled_keys`,
`open_clarifications`, `max_posts_per_tick`.

### T2. Detect

- **Mentions**: the Slack search tool with keywords `["<@OWNER_ID>"]`,
  `after` = `since_epoch`, sorted by timestamp, bots excluded. Paginate
  while results are newer than `since_epoch`.
- **DMs** (scope `dms`): the Slack search tool with `is:dm`, channel type
  `im`, `after` = `since_epoch`; then read each active DM with the read
  tool (`oldest` = `since_epoch`) for the full message.
- **Group DMs** (scope `group-dm-mentions`): covered by the mention search.
- A candidate in a thread: read the thread to see the context and whether
  the owner already replied.
- `--channels` set: drop candidates outside the allowlist.
- **Test channels** (policy `test_channels`): read the channel directly
  (`oldest` = `since_epoch`); every top-level message is a candidate, the
  owner's own included; no mention is needed. The owner-asker rule is
  waived there and nowhere else.

### T3. Filter

Drop, and record as `ignored` with a reason (`$R record --json
'{"outcome":"ignored","thread_key":"...","reason":"..."}'`), any candidate
that is: from the owner; from a bot or app; from a user whose profile
email is not in the internal domain (read the profile once per user per
tick); in a Slack Connect channel; in a `deny_channels` channel; older
than the window; already in `handled_keys` and not an open clarification.
If the owner has replied in the thread since the question, record
`handed-over` and go silent there.

### T4. Classify

First, does the message ask the owner anything? A statement, a shared
link, an acknowledgement, an emoji, a "be right there": record `ignored`
with reason `no-ask` and move on. Chit-chat is never escalated.

One intent per ask: `source-document`, `build-status`, `availability`,
`roadmap`, `feature`, or `other`. `other`, a request for a decision, an
approval, an opinion, a review, a calendar change, a meeting slot (the
calendar intent answers only "when is the owner back"), anything about a
person, anything that asks you to relay or to share files: escalate (T8)
without retrieval.

In a 1:1 DM the DM is the thread: an ask the owner has already replied to
is `handed-over`; later chit-chat in the same DM is `no-ask`. A message
that answers or follows up the owner's own last message (the owner asked,
the other person is replying) is the owner's conversation: `ignored`,
reason `owner-driven`. A mention that only copies the owner on an ask
directed at someone else is `ignored`, reason `cc`.

### T5. Retrieve

1. `lessons.md` house answers: a matching pattern is the answer
   (`house_answer: true`, confidence `high`).
2. `$R search --query <term> --query <term>` (two or three terms), then
   `$R page --slug <slug>` on at most three pages, read in full. A page
   outside the corpus is refused by the script; do not read it another way.
3. `build-status`: the Linear tools, read only (issues by project or
   query, milestones, status updates). Report states, counts, milestone
   and the issue or project URL; never paste descriptions.
4. `availability`: the calendar tools, read only: out-of-office events on
   the primary calendar and the shared PTO calendar for the owner. Answer
   with the return date or time only; no meetings, no locations. With no
   out-of-office event, say the owner will see the thread when next
   online, and give `working_hours` if the policy has them. Never invent a
   time.
5. `roadmap`: `$R page --slug <roadmap_page>` and the registry's roadmap
   link; a five-line snapshot: where we are, customers and dates, this
   quarter, what moves the dates, as-of date.

### T6. Decide

`high`: the pages read cover the question directly, or a house answer
matches. `medium`: close, one ambiguity a single question would settle,
and the thread has no clarification yet. Else `low`. `tightened: true`
means only house answers may be answered.

### T7. Compose

Before composing, for each link the reply would carry: if `$R sources
list --match <url>` shows it `unknown` or `stale`, verify that one row
(the `verify` steps 3 or 4 for its kind, `--url` scoped). A row that ends
`private` or stays `unknown` means the document is described, not linked.

From the policy's template: the header line, the return line, at most
eight lines of answer, `*Sources:*` with registry titles and links, the
footer. Plain statements. Dates only for an upcoming deadline, a ticket's
completion, or a source's last update; never when something was decided,
built, exported or added. A status answer is the target date, the
milestone state and the ticket links, with no process background. When a
house answer names a person as the contact for a document or a process,
name them; otherwise never describe people. Never the owner's first
person; never a commitment. A clarification is the
header plus one question. An escalation acknowledgement is the header plus
"I've flagged this for <owner>; back <return>." When a registry document
clearly bears on the ask (a source-document question the corpus only
partly covers), the acknowledgement may add one line pointing at it, with
the owner still to confirm; it passes `check` like any link.

### T8. Check, post, mark, record

```bash
$R check --json '{"outcome":"answered","intent":"feature","confidence":"high","thread_key":"C123:1761.0001","asker":"U456","asker_email":"a@company.com","message":"...","pages":["slug"],"links":["https://..."],"house_answer":false}'
```

`ok: false` lists the problems. An unregistered link: remove it and
describe the document instead, then check again. Anything else: escalate.
Then, by mode:

- `dry`: post nothing.
- `draft`: the Slack draft tool, in the thread (`thread_ts`).
- `live`: the Slack send tool, in the thread (`thread_ts`; a DM has none).
  Keep the reply's `ts`. Add the reaction to the asker's message:
  `robot_face` answered, `grey_question` clarifying, `rotating_light`
  escalated.

```bash
$R record --json '{...the same fields..., "channel":"C123 (#name)", "question":"<one-line paraphrase>", "reply_ts":"...", "mode":"live", "escalated_via":[...]}'
```

`record` re-runs the check and refuses what the check would refuse.
Follow-ups to an open clarification pass `--follow-up`.

### T9. Escalate

Text: `<owner>'s stand-in: <asker name> in <#channel or DM> asked
<paraphrase> (<tier>, <reason>). Thread: <permalink>`. Never the message
text. Routes, from `route`:

- `push`: the harness's push-notification tool (proactive).
- `imessage` or `twilio`: `$R escalate --text "..." --route imessage,twilio`.
- `slack`: post the same text in the policy's `escalation_channel`
  (`live` mode only).

Then the acknowledgement in the thread (T7), checked and recorded as
`escalated`. In `dry` mode, `$R escalate --dry-run` and no push.

### T10. Follow-ups and reactions

For each open clarification: read the thread. A clear reply: answer it
(`--follow-up`). An unclear reply: escalate (`--follow-up`). No reply:
leave it. For ledger entries with a `reply_ts` in the last two days and no
reaction: read the thread, and for a `+1` or `-1` reaction by the asker on
the reply, `$R review reaction --id R-NNNN --value +1|-1`.

### T11. End

```bash
$R tick end
```

Print one line: `respond: tick N, <a> answered, <c> clarified, <e>
escalated, <i> ignored, <h> handed over`.

---

## `stop`

```bash
$R window stop
```

Delete the scheduled task named in the result (`task_id`) with the
harness's scheduled-task tool, or end the in-session loop. Read the
handoff file the result names and give the owner its numbers and the
escalated list in under twelve lines. Send one push notification: `stand-in
window closed: <a> answered, <e> escalated, <c> clarifications open`.

## `status`

```bash
$R window status
```

Render in under ten lines: open or not, until, mode, scope, counters,
tightened, open clarifications, last tick.

---

## `review`

The reinforcement loop. Signals, strongest first: the owner's grade; the
asker's reaction; a hand-over; a clarification that led to a +1; an
escalation the owner later answered.

0. **Import a remote stand-in's log first**, when the stand-in ran from an
   export (a ChatGPT workspace agent). Read the log channel (the exported
   policy's `escalation_channel`, for example `#<owner>-standin-log`) since
   the last import. For each `respond-log | <outcome> | <intent> |
   <confidence> | thread <key> | reply <ts> | pages <...> | links <...> |
   asker <id> | q <paraphrase>` line, record it as it happened:
   `$R record --imported --json '{"outcome": ..., "intent": ..., "confidence": ..., "thread_key": ..., "reply_ts": ..., "pages": [...], "links": [...], "asker": ..., "question": ..., "channel": ..., "mode": "chatgpt"}'`
   (`"mode": "dry"` for lines followed by `DRY` text). `--imported` skips
   the check, since the remote stand-in checked before posting, and skips
   a thread already in the ledger, so an import can be repeated.
1. `$R review list` (ungraded; `--all` for everything, `--since` to bound).
2. For each entry, show the paraphrased question, the reply (read it from
   Slack by `reply_ts` when it exists), the pages and links, the outcome,
   the asker's reaction. Ask the owner for `+1`, `0` or `-1` and an
   optional note or correction; then
   `$R review grade --id R-NNNN --score <s> --note "<note>"`.
3. `$R review export --graded-only`. Propose edits to `lessons.md`, one
   section at a time: a `+1` answer that is likely to recur becomes a
   **House answer** (pattern, the approved answer, pages, links, grade);
   a `-1` or a hand-over becomes a **Do not answer** rule or a **Phrasing**
   correction; a resolved clarification becomes a **Clarify first**
   pattern; an escalation the wiki could not cover becomes a **Gap** with
   the raw source that would close it. The owner approves each lesson's
   text; then you edit `lessons.md` (bump `updated:`). Nothing is copied
   from Slack into a lesson.
4. `$R review stats`. Print the numbers and whether the unattended bar is
   met (twenty graded interactions, no `-1` on an answered one).

A `-1` reaction during a window counts toward `tighten_after_negatives`;
`window start` resets it.

---

## `export --out <dir>`

A kit for running the stand-in in another agent, built from this project:
the corpus pages only, a filtered index, the policy, the registry, the
lessons, the five gate scripts, a cloud version of this skill (stateless:
the thread is the record, a Slack reminder is the escalation, a private
log channel is the ledger), and `SETUP.md`, `agent-instructions.md` and
`schedule-instructions.md` for a ChatGPT workspace agent.

1. Run `verify` first; the kit freezes the link verdicts.
2. `$R export --out <dir> [--force] [--link-max-age-days 30] [--log-channel "#<owner>-standin-log"] [--test-channel-name "#<test channel>"]`.
   `--force` replaces only an earlier export (one with its `MANIFEST.json`);
   the kit never lives inside `context/`. The export stops, and leaves
   nothing behind, if a ledger, state, handoff, log, raw source or excluded
   page would ride along, or if the kit's own check would see different
   pages than were exported.
3. Report the kit's `SETUP.md` path, the page count, the shareable link
   count, and the date its links stop being shareable
   (`links_valid_until`). Pause any local stand-in before the remote one
   goes live: two stand-ins reply twice.

After the owner is back, `review` imports the remote log (step 0), and a
fresh `export --force` carries the new lessons back.

## `replay --days N`

A quality read before any live window, on last week's real traffic:

1. If no window is open: `$R window start --until <now + 1h> --mode dry`.
2. `$R tick begin --since <N>d`, then T2 to T11 in `dry` mode (records,
   no posts, no reactions, no pushes). The hand-over rule is suspended in
   a replay: an ask the owner answered during the week is still treated
   as unanswered, so its would-be reply can be graded. Candidates already
   in `handled_keys` from earlier ticks stay skipped.
3. `$R tick end`, then `$R window stop` if you opened the window.
4. Print the would-be replies from `$R review list`, then run `review`.

---

## Rules

1. **The script's refusals are final.** A page outside the corpus is not
   read another way; a link outside the registry is not posted; a draft
   that fails `check` is not sent. Escalate instead.
2. **Only canonical sources.** The corpus, Linear read-only, the calendar's
   out-of-office events. Never the wiki log, `raw/`, follow-up files,
   project documents read directly, Slack history, email, Notion, meeting
   notes.
3. **Never speak as the owner.** The header on every post; no decisions,
   approvals, promises, opinions or dates of the owner's; no first person.
4. **Internal only.** The profile check, no Slack Connect, no external
   email. Email is out of scope entirely.
5. **One thread, one answer**, plus follow-ups to a clarification; one
   clarification per thread; the per-tick cap; the tightened rule.
6. **Escalate when in doubt**, with the acknowledgement. Waiting for the
   owner is a good outcome.
7. **Nothing personal.** No people pages, no 1:1 content, no calendar
   details beyond the return time, no message text in the ledger or in
   an escalation.
8. **Never write the wiki or `raw/`.** The skill writes only
   `context/respond/` (ledger, state, handoff; `lessons.md` at review).
9. **No side effects beyond the reply.** No persona export, no file
   sharing, no edits in Linear, Drive or the calendar, no DMs to third
   parties, no posts outside the asking thread except the escalation
   channel.
10. **Log everything** in the ledger, grade it in review, and keep the
    numbers honest. No em dashes in anything you write.
