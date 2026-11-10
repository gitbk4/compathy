# compathy-respond: a Slack stand-in that answers from the compiled wiki

> Status: **APPROVED 2026-10-29 with the recommended defaults; Phase 0 built the same day**
> (`scripts/respond.py`, templates, `skills/compathy-respond/SKILL.md`, `tests/test_respond.py`,
> `init` run on the pilot project). Next: the owner's review of `policy.md` and `sources.md`,
> then Phase 1 (smoke test of the scheduled task and the escalation route).
> Pilot: one project's wiki, kept local until tested. Then the generic
> skill is pushed with the pilot's configuration left out (it lives in the
> project's `context/`, outside this repo). The pilot's full record, with
> its project specifics, stays in that project's
> `context/respond/pilot-plan.md`. Precedent: `PLAN.md` (federation, v0.3.0).

---

## Problem statement

When the owner is away or heads-down, coworkers ask the same things in Slack:
where the roadmap is, what a feature does, how a build is going, when the owner
is back. The answers already exist: in the compiled wiki, in Linear, on the
calendar. The stand-in answers on the owner's behalf during a window the owner opens,
from canonical sources only, and escalates whatever it is not sure about.

The bias is **precision over coverage**. A wrong or over-sharing answer from
the owner's own Slack account costs more than no answer. The design therefore
puts the guardrails in deterministic Python (what may be read, what may be
linked, what may be posted) and gives the model only the judgement calls
(what is being asked, is the evidence enough, how to phrase it).

## Vocabulary

| Term | Meaning |
|---|---|
| window | the bounded period the stand-in is active: end time, poll interval, mode (`dry`, `draft`, `live`), scope |
| tick | one poll: detect, filter, classify, retrieve, decide, compose, check, post, mark, record, escalate |
| corpus | the subset of wiki pages the policy allows as sources |
| registry | the list of shareable documents (title, URL, kind, audience, status, verified date) |
| interaction | one handled message, with an outcome: `answered`, `clarified`, `escalated`, `ignored`, `handed-over` |
| lesson | a graded, curated rule or house answer the stand-in reads before it composes |
| escalation | a notification to the owner plus a Slack acknowledgement to the asker |

## What exists and gets reused (don't rebuild)

- `scripts/compathy_query.py`: page iteration, frontmatter parsing, grep
  search, ad-hoc `--layer` directories, layered resolution. The corpus and
  the filtered search import these functions; nothing is re-implemented.
- `scripts/lint.py`, `scripts/paths.py`, `templates/`, `scripts/install.py`
  (the `SKILLS` map), `scripts/update.py` (self-update in Phase 0): extended.
- Connectors already in the session, checked read-only on 2026-10-28:
  - Slack: list channels (public, private, DMs, group DMs), read a channel or
    DM, read a thread, search with a `<@me>` keyword or `is:dm` and
    `after:` filters (both return results), send, draft, react, read a
    user's profile (email and org). The connector posts **as the owner's own
    account** and cannot post to Slack Connect channels, which matches the
    internal-only rule.
  - Linear: issues, projects, milestones, status updates. The pilot
    initiative's projects are visible.
  - Google Calendar: the primary calendar and a shared team PTO calendar;
    out-of-office and working-location event types are readable.
  - PushNotification (desktop; phone when Remote Control is connected) and
    scheduled-tasks (persistent cron, fresh session per run).
- The wiki pages the pilot leans on: the roadmap, the decision log's
  summary, the data requirements, the sales library, and the feature
  concept pages.

## Architecture

The compathy split: Python does deterministic bookkeeping and every
guardrail that must not depend on the model; the model does reading,
judgement, writing and the connector calls.

```
 /compathy-respond init | start | tick | stop | status | review
        │  one SKILL.md with subcommands (the /compathy-persona shape)
        ▼
 scripts/respond.py  (stdlib only, prints JSON)
   init      scaffold context/respond/ from templates, seed the registry
   corpus    the allowed pages after the policy filters
   search    filtered search (wraps compathy_query; never the log, never raw/)
   page      an allowed page's body, its links classified against the registry
   sources   registry entries, optionally matched to a query
   window    start | status | stop        (state file)
   check     validate a drafted reply BEFORE it is posted
   record    append an interaction to the ledger (refuses unregistered links or pages)
   review    ungraded interactions | grade | lessons export | stats
        ▲
        │ JSON
 The agent, per tick:
   Slack read -> filter -> classify -> retrieve -> decide
   -> compose -> check -> post -> react -> record -> escalate
```

**Transport.** Replies go out through the Slack connector, so they come
from the owner's account. Every reply carries a fixed header naming it as the
automated stand-in, and the model never writes in the owner's first person.

**Scheduling.** `start` creates a scheduled task (cron every N minutes,
prompt `/compathy-respond tick --target <project>`); it persists across
restarts, runs while the app is open, and each run is a fresh session that
reads its state from disk, so context never grows. `stop` deletes it. If
the Phase 1 smoke test shows task sessions do not carry the connectors, the
fallback is an in-session loop (`ScheduleWakeup`) bounded by the window.

**Layers.** The skill reads one project's `context/`. For questions that
span several of the owner's wikis, the others are
attached as ad-hoc layers (`compathy_query.py` already supports
`--layer <dir>`); the corpus filter applies per layer. Pilot: one wiki only.

## Data model: `context/respond/` (the companion)

```
context/
└── respond/
    ├── policy.md          human-owned; what may be answered, read, linked, posted
    ├── sources.md         human-owned; the registry of shareable documents
    ├── lessons.md         co-evolved; graded house answers, do-not-answer rules, gaps
    ├── ledger.md          append-only; one entry per interaction (local, never shared)
    ├── handoff-<date>.md  written at stop; what was answered, clarified, escalated
    └── .respond-state.json  window, last-seen ts per channel, handled keys, counters
```

Same conventions as the wiki: flat-YAML frontmatter, kebab-case, no em
dashes, greppable log lines. `schema.md.tmpl` gains a "Respond (optional)"
section the way it gained "Federation". `lint.py` validates the folder's
structure when it exists (warnings only; a wiki without `respond/` lints
exactly as today).

### policy.md (defaults the pilot starts from)

- **Who**: internal employees only. The asker's Slack profile email must end
  in the policy's `internal_domain`; bots and apps are skipped; Slack
  Connect channels are skipped; a `deny_channels` list for anything else.
- **Triggers**: an @mention of the owner in a channel the owner belongs to; any new
  message in a 1:1 DM; in group DMs, mentions only. Messages older than the
  window's start are never answered.
- **Sources**: wiki pages of type `concept`, `entity` and `patterns`, plus
  `summary` pages whose `sources:` are published documents. Denied by
  pattern in `sources:` (`slack`, `grain`, `thread`, `notes-`, `1on1`,
  `1-1`, `email`, `dm-`, `transcript`) and by an explicit `deny_slugs`
  list that `init` seeds with people pages and anything derived from
  conversations, for the owner to review. Never: `log.md`, `raw/`,
  `FOLLOWUPS.md`, the spine files directly, personas, `lineage.json`.
  The decision log's summary page is allowed on purpose.
- **Links**: only registry entries with `status: shared` and an audience
  that includes the asker (`internal` by default). Linear issue and project
  URLs count as internal. A document the wiki names but the registry does
  not mark shared is described, not linked, and flagged to the owner.
- **Never**: decide, approve, commit to a date, give the owner's opinion, make
  a promise, share calendar details beyond the return time, describe
  people, share 1:1 content, share raw or markdown files, offer or run a
  persona export, relay or forward messages, DM anyone the asker named,
  post anywhere but the thread or DM the question came from, change
  anything in Linear, Drive or the calendar, paste customer figures,
  prices or contract terms (the wiki's data-handling rules apply).
- **Confidence**: three tiers.
  `high` (the question maps to allowed pages that cover it directly, read
  in full) answers. `medium` asks one clarifying question, at most one per
  thread, then answers or escalates. `low`, denied topics, anything that
  asks the owner to decide, and anything about people escalate with an
  acknowledgement. A supported intent with a graded house answer in
  `lessons.md` is `high` by definition.
- **Intents supported in v1**: point to a source document; status of a
  feature build (Linear); when the owner is back (calendar); roadmap snapshot;
  what a feature is and why (wiki). Everything else escalates.
- **Rate limits**: one answer per thread per window plus follow-ups to a
  clarification; one clarification per thread; at most five posts per
  tick, the rest escalate as "high volume". After `tighten_after_negatives`
  (default 2) negative signals in a window, the stand-in answers only
  intents with a house answer until the next review.
- **Tone**: the wiki's writing rules; short; sources named; no em dashes.

### sources.md (the registry)

A table: title, URL, kind (`gdoc`, `gslides`, `gsheet`, `claude-doc`,
`claude-page`, `linear`, `notion`, `storybook`, `figma`, `public`),
audience, status (`shared`, `private`, `unknown`), verified (date), note.
`init` seeds it from every URL the allowed wiki pages cite, with
`status: unknown`; the owner flips the ones that are actually shared. Only
the owner edits it; the skill reads it.

### ledger.md and .respond-state.json

One ledger entry per interaction: timestamp, channel, thread key, asker
(Slack id), intent, confidence, outcome, pages used, links shared, the
reply's ts, the asker's reaction once harvested, the owner's grade and note.
The question is kept as a one-line paraphrase, not the message text. The
state file holds the window, the last-seen timestamp per channel, the set
of handled keys (`channel:thread_ts`, the idempotency guarantee), and
counters. Neither file is ever posted, shared or compiled into the wiki.

## The tick pipeline (what SKILL.md tells the model to do)

1. **Gate**: `window status`; exit if closed or past its end (then `stop`).
2. **Detect**: Slack search for `<@me>` since the last tick minus a ten
   minute overlap; list DMs and group DMs with activity since the last
   tick (search `is:dm`) and read only those.
3. **Filter**: drop own messages, bots, external askers (profile check),
   Slack Connect channels, denied channels, messages before the window,
   handled keys. If the owner has replied in a thread since the
   question (a message from the owner's id without the stand-in header), the
   thread is `handed-over`: the stand-in goes silent there.
4. **Classify** the intent; unsupported intents escalate without retrieval.
5. **Retrieve**: `lessons.md` first (house answers), then `search` across
   the corpus, then `page` on at most three pages, read in full.
   Linear and calendar intents call the connectors read-only.
6. **Decide** the tier under the policy.
7. **Compose** from the template:

   ```
   :robot_face: *Automated reply from the owner's stand-in (this is not the owner).*
   the owner is back <return, from the calendar> and will see this thread.

   <answer, at most eight lines>

   *Sources:* <title> (<registry link>) ...
   _Useful? React :+1: or :-1: so the owner can tune me._
   ```

8. **Check**: `respond.py check` validates the draft: header present,
   links are a subset of the registry's shared entries, pages used are in
   the corpus, length, no phrase from the policy's `never_say` list
   (customer names, figures), no first-person-as-the owner phrasing. A failed
   check means the reply is not posted; the interaction escalates instead.
9. **Post** in the thread (or the DM). `dry` mode writes the would-be reply
   to the ledger only; `draft` mode creates a Slack draft for the owner to send
   (one per channel, the connector's limit); `live` mode sends.
10. **Mark** the asker's message with a reaction: `robot_face` answered,
    `grey_question` clarifying, `rotating_light` escalated. Visible state
    for the owner and the room; the ledger stays the real record.
11. **Record** with `respond.py record` (which refuses anything the check
    would have refused, a second gate).
12. **Follow up**: threads with an open clarification are re-read; a clear
    reply is answered, anything else escalates. Reactions on earlier
    replies are harvested into the ledger.
13. **Escalate** per the chosen route (below), with the Slack
    acknowledgement to the asker.

## Reinforcement loop (how answers get better)

Signals, strongest first:

1. **The owner's grade** in `/compathy-respond review`: +1, 0 or -1 with a note
   or a correction. Authoritative.
2. **The asker's reaction** on the stand-in's reply (:+1: or :-1:),
   harvested at the next tick and at review.
3. **Hand-over**: the owner answered the thread directly. Recorded as "needed a
   human", with the owner's grade deciding whether the stand-in should have
   answered.
4. **Clarification outcome**: a clarifying question that led to a +1
   answer becomes a "clarify first" pattern.
5. **Escalations the owner later answered**: at review, the stand-in proposes a
   house answer drawn from the canonical pages, or records a **gap** when
   no page covers it. Nothing is copied from Slack into a lesson; the owner
   approves each lesson's text.

What the loop writes, all in `lessons.md`:

- **House answers**: question pattern, the approved answer, page slugs,
  registry links, grade history. Read before retrieval; take precedence
  when the pattern matches.
- **Do not answer**: patterns and why. Extend the policy without editing it.
- **Clarify first**: patterns where one question resolved the ambiguity.
- **Phrasing**: corrections the owner made to tone or wording.
- **Gaps**: asked, not covered. Each names the raw source that would close
  it; the next `/compathy` compile closes the loop once the owner drops it in
  `raw/`. The skill never writes `raw/` or the wiki.

`review` also prints the numbers that matter: answered, clarified,
escalated, handed-over; the share of answered interactions graded +1 by
the owner and by the asker. The pilot's bar before an unattended window: at
least twenty graded interactions and no -1 grade on an answered one.

## Escalation routes (the owner chooses; the recap below recommends)

The Slack connector cannot wake the owner's phone from the owner's own account
(Slack never notifies you of your own messages), so the phone needs
another path. The Slack side is still the durable record: every
escalation is acknowledged in the asker's thread and listed in a private
escalation channel the owner reads when back.

| Route | Reaches the phone | Setup | Trade-off |
|---|---|---|---|
| A. PushNotification | yes, when Remote Control is connected in the Claude app; desktop otherwise | none | needs the app open and Remote Control on; the mechanism we already have |
| B. iMessage to self via Messages.app (`osascript`) | yes, a real text on the phone | one Automation permission prompt on the Mac | Mac only; relies on Messages being signed in; to be smoke-tested |
| C. Twilio SMS from `respond.py` (stdlib `urllib`) | yes, SMS | a Twilio account and three env vars | the most reliable; a small monthly cost; credentials to keep |
| D. Calendar reminder (an event two minutes out with a popup) | yes, if calendar notifications are on | none | clutters the calendar; deleted at `stop`; a fallback, not a design |

Recommendation: A plus B for the pilot, C if B proves unreliable. The
escalation text names the asker, the channel, the paraphrased question and
the tier; never the message text.

## Commands

```
/compathy-respond init                       scaffold context/respond/, seed the registry, print what to review
/compathy-respond start --until <time> [--every 5m] [--mode dry|draft|live] [--scope mentions,dms,group-dms] [--channels ...]
/compathy-respond tick                       one poll (what the scheduled task runs)
/compathy-respond status                     window, counters, open clarifications, pending escalations
/compathy-respond stop                       delete the task, write handoff-<date>.md, notify the owner
/compathy-respond review [--since <date>]    grade interactions, update lessons.md, print the numbers
/compathy-respond replay --days 7            dry run against the last N days of mentions and DMs; ledger only
```

`start` always ends with a printed summary of the policy in force (who,
triggers, mode, window, escalation route) and one explicit go from the owner.
Nothing is posted at `start`. A window in `live` mode is the standing
authorization for that window and nothing beyond it.

## Built as planned, plus one knob

`allow_slugs` in `policy.md`: a page admitted despite a source-pattern
match (for a canonical feature page that also cites a private source);
`deny_slugs` still wins. Added when `init` on the pilot excluded a
canonical feature page because one of its sources is the owner's notes.

## Revision 2026-10-29: the registry fills itself

The owner asked that share status not be a manual chore as the wiki
grows. Checked against the connectors: Drive exposes a file's permission
list (exact); Notion exposes no permissions, but the owner's Private
section is listable and search results carry a `path` breadcrumb (a
proxy, with an ancestor check per link); the Docs connector states that no
tool shows who has access to a Claude doc, so Claude docs, Claude Design
and Figma stay manual. `sources.md` gained a `Via` column, the policy
gained `link_verify_max_age_days`, `kind_rules` and `notion_default`, and
the skill gained `verify`, run at `start` and per link before a reply
carries it. A row with `Via: manual` is the owner's and is never
overwritten. A `shared` row older than the max age is not linkable until
re-verified.

## Phase 1 smoke test (2026-10-29), results

Dry window on the pilot project, 16:59 to 20:09 Pacific. Nothing was
posted to Slack.

- **In-session tick, one day of real traffic**: 2 mentions, new messages
  in 4 DMs, all internal. 5 asks the owner had already answered
  (`handed-over`), 8 messages that asked nothing (`ignored`, reason
  `no-ask`), 0 answerable, 0 escalations. A day's traffic was requests
  for the owner's action, not questions the wiki answers; the replay over
  a week and the first live window will say more than this sample.
- **Gate self-test**: a synthetic roadmap reply carrying the
  domain-shared roadmap Google Doc passed `check`; the same reply carrying
  the private Claude doc link was refused.
- **Task session**: a one-time scheduled task ran `tick` in a fresh
  session in Auto mode. Connectors: Slack, Linear, Calendar and the
  compathy MCP all answered; no call was refused or permission-blocked;
  tick 2 recorded in the shared state with nothing new to handle.
- **Dispatch**: the task sat past its 17:11 time while this session was
  working and started at 17:56, as soon as this session paused. Ticks
  fire only while the app is otherwise idle. Acceptable for an
  out-of-office window (the app is idle then); not for a window run
  beside active work. The in-session loop has the same constraint.
- **Push route A**: mobile push needs two switches, Remote Control on in
  the sending session and `agentPushNotifEnabled` in Claude Code's
  `/config`. With both on, the push from this session was skipped because
  the owner was at the terminal (correct); the push from the unattended
  task session fell back to desktop because that session had Remote
  Control off. For task-session pushes to reach the phone, the app
  setting "Connect new sessions to Remote Control" must be on, or the
  window must run as the in-session loop in a session with Remote Control
  on.
- **Defects found and fixed**: the skill's scripts path broke under the
  symlinked install (now resolved with `realpath`); the handoff counted
  ignored messages as 0 and left the thread column blank; `window update
  --until` was missing (added to extend a window).

Ready for Phase 2 (the seven-day replay, graded in `review`) once the
owner picks the Remote Control default for task sessions.

## Phase 2 replay (2026-10-29 to 30), results

A dry window with a seven-day look-back over real mentions and DMs; nothing
posted. 19 asks, every one an escalation under the policy; 40 messages
ignored (no ask, a cc, the owner's own conversation, personal); 0 asks the
wiki could answer. The owner graded all 24 ledger entries: 21 at +1, and
three in a gray area at 0 with the answer the owner would have given. Those three
became the first house answers:

- a recurring document question, answered with the owner's rule and
  the documents' links;
- a recurring scoping question, answered with the owner's rule and the
  feature list to check it against, escalating anything outside it.

Also written: five do-not-answer rules the week confirmed (scheduling,
reviews, decisions, email relays, ownership), the acknowledgement phrasing,
and two gaps (a glossary intent with a raw source of customer-call
acronyms; a sibling project's wiki as a layer for build-status questions).

Two fixes from the replay: short queries now match whole words (the
acronym "HOD" had matched "method"); the stats' ignored count and the
unattended bar were corrected (the bar needs graded answers, not only
escalations, so a replay of escalations never meets it).

Finding: organic traffic exercises triage, not the answer path. Phase 3
needs the owner to ask the recurring questions in a test channel.

## Phase 3 supervised live hour (2026-11-03), results

A private test channel with the owner as its only member; the policy's
new `test_channels` list waives the owner-asker rule there. A one-hour
live window scoped to the channel; the owner asked seven questions; two
ticks (the five-post cap split them) answered all seven in threads, each
through the gate with registry links. The owner graded every reply:

- five at +1 (document requests, a value message, a roadmap question),
  two at 0 (an ROI estimate for a customer, a customer's data status).
- Lessons: two house answers that name the right contact and link for a
  recurring request (an ROI estimate; the sales slide assets), PH-002 (no
  timestamps in replies except an
  upcoming deadline, a ticket's completion or a source's last update),
  PH-003 (status answers: the target date, the milestone state, the
  tickets; no background). `max_reply_chars` lowered to 1200 on the pilot.
- Registry: two manual rows from the owner (a SharePoint model and a
  Claude artifact with a share key). Found and fixed: an unknown host was
  classed `public` and would have been auto-shared by the kind rule; it is
  now `other` (never auto-shared), `sharepoint` is a kind, and `sources
  set` takes `--kind`.
- The unattended bar is met on real answers (31 graded, five answered at
  +1, none negative). Next: an unattended half-day window on the real
  triggers, then review.

## Export to a ChatGPT workspace agent (2026-11-09)

The owner asked to run the stand-in from a ChatGPT agent. OpenAI's
workspace agents (Business and Enterprise) take instructions, an uploaded
skill, files, apps (Slack, Google Calendar), custom MCP servers (Linear's
read-only endpoint), per-action write approvals ("Never ask"), action
constraints and a schedule, and they run in the cloud, so the stand-in
works with the laptop closed. The ChatGPT Slack app reads DMs and threads
and creates reminders; it has no reactions scope, and its help page does
not list "send a message" by name (it requests `chat:write`), so the setup
tests posting in Preview first.

`respond.py export --out <dir>` builds the kit: the corpus pages, a
filtered index, the policy (link checks valid 30 days, the log channel),
the registry, the lessons, the five gate scripts, a stateless cloud skill
(the thread is the record, a Slackbot reminder is the escalation, a
private log channel is the ledger), and SETUP.md, agent-instructions.md
and schedule-instructions.md. It refuses to write inside `context/`, to
replace anything but an earlier export, or to finish if a private file or
excluded page would ride along. `record --imported` brings the log lines
back into the ledger at review; `sources seed` registers links the wiki
gained since `init`.

First export: 103 pages, 84 shareable links good until 2026-12-03, a
332 KB zip, in the pilot project's `exports/` folder. The Claude
scheduled task is paused so the two never run at once. Not verified: the
ChatGPT side itself (no workspace access from here).

## Core changes (existing files)

| File | Change |
|---|---|
| `skills/compathy-respond/SKILL.md` | new: the skill, subcommands, the tick pipeline, the rules |
| `scripts/respond.py` | new: the subcommands above, stdlib only |
| `scripts/paths.py` | `RESPOND_SUBDIR` and path helpers |
| `scripts/lint.py` | `check_respond(target)`: structure of policy, registry, lessons, ledger; warnings only |
| `templates/respond-*.md.tmpl` | policy, sources, lessons, ledger templates |
| `templates/schema.md.tmpl` | a "Respond (optional)" section |
| `scripts/install.py` | `respond` in `SKILLS`; `all` includes it |
| `README.md`, `ARCHITECTURE.md`, `release-notes.md` | the skill, the split of concerns, v0.4.0 notes |
| `tests/test_respond.py` | see Tests |

The main `/compathy` skill is untouched except for one optional line in
its summary (`respond: window open until ...`) when a window is open.

## Phasing

| Phase | What | Gate to the next |
|---|---|---|
| 0. Build | `respond.py`, templates, SKILL.md, tests; `init` on the pilot project; the owner reviews `policy.md`, `deny_slugs` and marks the registry | tests green; registry reviewed |
| 1. Smoke | the scheduled task runs `tick` in `dry` mode once (do task sessions see Slack, Linear, Calendar and the project MCP?); the escalation route fires to the phone | both confirmed, or the fallbacks chosen |
| 2. Replay | `replay --days 7`: would-be replies for last week's real mentions and DMs, ledger only; the owner grades them in `review` | the first lessons; no -1 on a would-be answer |
| 3. Supervised live | a one hour `live` window scoped to one private test channel plus DMs, the owner watching; then a one hour window on all triggers, the owner watching | review with no -1 grade |
| 4. Unattended | a half-day window; review; a full-day window; review | twenty graded interactions, no -1 on an answered one |
| 5. Generalize and push | strip nothing from the repo (the pilot's config never entered it); docs, installer, release notes v0.4.0; push to GitHub | The owner's go |

## Tests (unittest, stdlib, tempdirs)

- corpus: type allowlist, source-pattern denylist, `deny_slugs`, never the
  log or raw, layers respected
- registry: parse, status and audience filters, seeding from wiki URLs
- `check`: header required, unregistered link refused, page outside the
  corpus refused, `never_say` phrase refused, first-person-as-the owner refused
- `record`: ledger format, idempotency key refused twice, counters
- window: start refuses when open, stop writes the handoff, past-end detection
- review: grading, lessons export, the tighten-after-negatives counter
- lint: a wiki without `respond/` produces identical output to today

## Open decisions (need the owner)

1. **Target wiki**: one wiki for the pilot, with the owner's other wikis
   attached as layers in a second step. Agree?
2. **Escalation route**: A plus B as recommended, or C from the start?
3. **Scheduling**: scheduled task (persistent, app open) with the
   in-session loop as fallback. Agree?
4. **Group DMs**: mentions only (recommended), or every message?
5. **Companion shape**: the folder `context/respond/` with five files
   (recommended, so the ledger can be append-only and gitignored
   separately), or one `context/respond.md`?
6. **Slack status during a window** ("stand-in active until ..."): the
   skill asks the owner to set it by hand for now; automating it is an
   account-settings change we would add only on the owner's say-so.
7. **Name**: `compathy-respond`. Alternatives: `compathy-standin`,
   `compathy-concierge`.

## Risks

- **Replies come from the owner's account.** The header and footer are
  unmissable and the model never speaks as the owner; `check` enforces both.
  A reader can still skim past it; the pilot's test channel phase tells
  us whether the wording lands.
- **Unattended approval prompts.** A task session may stop on a permission
  prompt for a connector call. Phase 1 settles the permission mode and the
  project's allowlist (Slack read and send, Linear read, Calendar read,
  the compathy MCP) before any unattended window.
- **Hallucinated or private links.** Two gates: `check` before the post,
  `record` after it; both refuse any URL outside the registry's shared set.
- **Search lag.** Slack's search index can trail by a minute; the ten
  minute overlap plus idempotency keys cover it; DMs are read directly.
- **Volume.** Ninety-nine channels; mention search avoids reading them all;
  DM reads are limited to conversations with new activity.
- **Over-answering.** The tighten rule after two negative signals, the
  per-tick cap, and "unsupported intent escalates" keep the failure mode
  on the quiet side.
- **Stale truth.** The stand-in answers from the wiki as compiled; a
  recent decision not yet compiled is a gap. The reply's "as of" line and
  the gaps list make that visible; running `/compathy` before `start` is
  part of the checklist.

## Not in scope (v1)

Email and external counterparts; Slack Connect channels; acting on
anything (filing, approving, scheduling, editing Linear or Drive); writing
to the wiki or `raw/`; persona export; multi-person use (one stand-in, one
the owner); Notion, Grain or Google Drive content that is not already compiled
into an allowed page; learning from Slack history beyond the replay.
