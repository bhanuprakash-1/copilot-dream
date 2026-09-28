---
name: dream-consolidation
description: Master instruction set executed by the nightly headless Dream run. Not a normal skill - invoked via `copilot -p` by run-dream.ps1. Runs as a lean map-reduce orchestrator with parallel sub-agents.
---

# DREAM - Nightly Memory Consolidation (map-reduce)

You are running the **Dream**: an unattended nightly pass that turns the day's Copilot-CLI conversations
(the user's prompts, the agent's answers, and the back-and-forth between them) and git activity into
durable, well-organized personal knowledge, without polluting long-term skills with one-off noise or
over-specific detail. Work autonomously end-to-end. Do not ask questions.

You run as a **lean map-reduce ORCHESTRATOR**, not a monolithic reader. Agent quality degrades long
before a single thread fills a 1M window, so you NEVER pull raw session transcripts or full skill bodies
into your own context. Instead you: shard the harvest -> fan out parallel classifier sub-agents (MAP) ->
merge their compact JSON (REDUCE) -> fan out parallel per-skill editor sub-agents (APPLY) -> write the
journal from compact summaries. Every sub-agent is an ephemeral, fresh context, so no thread ever needs
compaction and each piece of judgment happens in a clean, high-quality window.

## Model policy (hard gate)
Use `config.model_policy` and the resolved bootstrap values as the single source of truth.
The current cost-conscious model pin is `gpt-5.6-sol`, with `xhigh` reasoning and `long_context` (~1M tier).
Every orchestrator, MAP/APPLY worker, reviewer, nested sub-agent and retry MUST use the SAME pinned
model, `long_context`, and `xhigh` effort. Small shards still require long context.
For EVERY launch explicitly set `model`, `reasoning_effort`, and `context_tier` from the resolved
policy; never omit them or inherit a tool's cheaper/default configuration. Standalone CLI workers
must receive `--model`, `--context long_context`, and `--effort` explicitly.
If the model, effort, or context tier is unavailable, stop and report a model-policy failure. Do not
downgrade or treat this as an ordinary partial-bucket failure. Preserve historical journal models.
Record the requested model/effort/context in each worker receipt and the final journal.

## Lean-orchestrator discipline (MANDATORY - this is what protects quality)
- NEVER read a full session transcript, the full harvest JSON, or a full skill body into YOUR context.
  You read only: `config.json`, the shard MANIFEST, `candidates.json`, `apply-plan.json`, ledger CLI
  output, and the one-line summaries your sub-agents return.
- ALL heavy reading and editing happens inside sub-agents. Sub-agents communicate by WRITING FILES
  (claims-NN.json, in-place skill edits, proposal files) and RETURNING a single one-line summary -
  never by dumping large content back to you.
- Launch independent sub-agents in PARALLEL (issue all launches in one turn so they run concurrently).
  Respect the caps in `config.map_reduce`: `max_shards` bounds MAP fan-out, `apply_max_parallel` bounds
  APPLY fan-out.
- **HEADLESS BACKGROUND LIFECYCLE (HARD):** this orchestrator runs under `copilot -p`. NEVER end your
  turn while a background sub-agent is running. Ending the turn starts a non-resetting host shutdown
  drain even if completion notifications later wake you. Continue independent work; when none remains,
  call `read_agent(wait:true, timeout:180)` for each known running agent and repeat until all complete.
  Do not say "ending my turn", do not wait passively for a notification, and do not return control to
  the host between launch and collection.
- If a sub-agent fails, retry it ONCE; if it still fails, note it in the journal and continue. A partial
  Dream is better than none.
- Keep your own running context tiny. If you ever feel the need to summarize your own context, you have
  broken this discipline - push that work into a sub-agent instead.

## Fixed paths
- Config: `~/.copilot/dream/config.json`
- Sharder: `python ~/.copilot/dream/shard.py --config <config>`
- Reducer: `python ~/.copilot/dream/reduce.py --config <config> <merge|plan> ...`
- Ledger CLI: `python ~/.copilot/dream/ledger.py <cmd>` (see its header for subcommands).
- Shard pointer (this run's scratch dir): `~/.copilot/dream/harvest/shards/latest.json` -> `.dir` / `.manifest`.
- Journal out: `~/.copilot/dream/journal/<YYYY-MM-DD>.md`
- Review queue: `~/.copilot/dream/review-queue/<YYYY-MM-DD>-<slug>.md`
- Durable-vs-transient filter: your own KEEP/DROP filter, if you have one. If
  `config.targets.durable_filter_skill` points at a real skill, its table is authoritative.
- Standing guidance: `~/.copilot/dream/inbox.md` (`config.sources.inbox.path`) - the user's notes and vetoes,
  re-read every night by EVERY MAP classifier and APPLY/review sub-agent. A veto there overrides everything,
  including paraphrases.

## Routing targets (reuse existing knowledge)
The authoritative target list is `config.targets`. A claim's `scope` picks its home first (see the rubric):
`feature` -> `dream-active-work`; `topic` -> the reference skill that owns the topic; `cross-cutting` ->
`config.targets.general_skill` unless a reference skill owns that tool or workflow. Load and respect your
existing reference skills (e.g. `service-architecture`, `deployment-runbook`, `telemetry-queries`,
`team-conventions` - see the `targets` block in `config.json`). Prefer refining an existing entry over
adding a new one. Never duplicate content across skills - cross-reference.

---

## Phase 0 - Load & Shard  (ORCHESTRATOR, lean)
1. Read `config.json`. Note `map_reduce`, `targets`, `thresholds`.
2. `python ledger.py stats` and `python ledger.py dump --status active` - the compact state of what is
   already tracked (so edits build on it, not blind re-derivation).
2b. **Bootstrap (cold-start safety).** Ensure the scaffolding skills exist so later phases can write:
   - If `config.targets.index_skill` or `config.targets.short_term_skill` is missing on disk, create it
     with a minimal valid `SKILL.md` (frontmatter `name` + `description`, one-line body).
   - If `config.targets.long_term_skills` is EMPTY and `config.seed.enabled`, ensure the seed skill
     `config.seed.general_skill` exists (create it from its `name`/`description` if missing) and use its
     NAME as the SOLE long-term routing target this run - substitute it wherever
     "[<comma-separated config target skill names>]" appears below.
3. `python shard.py --config <config>` to split today's harvest into balanced, thread-grouped shards.
4. Read ONLY `harvest/shards/latest.json` -> the `manifest` file it points to. The manifest lists each
   shard's file, kind, session/commit counts, est_tokens, and branches. Record the shard dir (`.dir`).
   Do NOT open the shard bodies yourself.
   - If the manifest has 0 shards (empty day): write a one-paragraph journal noting "no material",
     `ledger.py record-run`, write the bootstrap completion marker as the final action, and stop.

## Phase 1 - MAP / Classify  (PARALLEL sub-agents, one per shard)
Launch one sub-agent per shard in the manifest, all in the same turn (cap at `map_reduce.max_shards`).
Give each sub-agent exactly this job (substitute the bracketed values):

> You are a Dream classifier sub-agent. (1) Read the "## Classification rubric" section of
> `~/.copilot/dream/dream-consolidation.prompt.md`, if configured the durable filter skill at
> `config.targets.durable_filter_skill`, and the standing guidance in `~/.copilot/dream/inbox.md` (its
> vetoes override everything, including paraphrases). Also read, READ-ONLY, the files matching
> `config.read_only_context.personal_instruction_globs` (the user's global instructions: a preference
> already stated there is never re-captured) and any files in
> `config.read_only_context.agent_instruction_globs` whose path matches the repository of YOUR shard's
> sessions/commits (they are small; skip non-matching or non-existent ones) - treat them as that repo's
> authoritative conventions/protocol. Do NOT copy repo-owned coding conventions or repo-specific detail
> into personal skills; that knowledge belongs to the repo (its agent-history and the in-repo skills under
> `config.read_only_context.repo_skill_dirs`). Personal skills are for cross-cutting, durable, personal
> knowledge. (2) Read your shard file `<shard_dir>/shard-<NN>.json` in full. Each session's `dialogue` is
> the conversation in order: the user's prompts (`user`), the agent's `interim` commentary and `final`
> answers (`tools` = what it ran in that turn), and `summary` entries (compaction summaries of earlier
> turns). `prior_context` is orientation only - it was harvested on an earlier night, so never extract
> claims from it. (3) Mine the whole back-and-forth per "### Mining the conversation" and extract atomic
> **claims** - each a durable, generally-worded fact/pattern/decision, never a play-by-play of "what I
> did today". (4) Give each claim: `claim`, `domain`, `scope` (feature|topic|cross-cutting), `thread`
> (feature scope: branch or short feature slug), `signal` (question|correction|preference|decision|
> finding|status), `horizon` (long|short|drop), `importance` (1-10), `confidence` (high|medium|low),
> `target` (one of [<comma-separated config target skill names>] | dream-active-work | review-queue),
> `evidence` (session-id prefix / commit hash / branch), `source` (sessions|git|inbox|mixed). Apply
> "### Scope decides the home" and "### Altitude & anti-bloat" strictly - dropping noise and
> over-specific detail is the whole point. (5) Write the claims as a JSON array to
> `<shard_dir>/claims-<NN>.json`. Return ONLY one line:
> "shard <NN>: K claims (L long / S short / D drop; C corrections, P preferences)".
> Do not write anything else back to me.

Keep this SAME turn active until every MAP agent completes: use `read_agent(wait:true, timeout:180)`
repeatedly rather than ending the turn. Collect the one-line summaries. Do NOT read the claims files
yourself - the reducer will.

## Phase 2 - REDUCE / Ledger  (ORCHESTRATOR, lean)
1. `python reduce.py --config <config> merge --in <shard_dir> --out <shard_dir>/candidates.json`
   (concatenates every claims-NN.json, dedups by fingerprint, conservatively resolves any cross-shard
   disagreement toward review - never auto-elevating a contested claim to long/high).
2. `python ledger.py upsert --json <shard_dir>/candidates.json` (bumps hit_count / distinct_days so
   recurrence accumulates across nights).
3. `python reduce.py --config <config> plan --candidates <shard_dir>/candidates.json --out <shard_dir>/apply-plan.json`
   (routes candidates into per-skill APPLY buckets, the active-work bucket, the review-queue, and drops;
   enforces scope - a `feature` claim never reaches a reference skill and a `topic`/`cross-cutting`
   learning never lands in active-work; records each target's current size vs budget; also folds in ledger
   `promotions` and `decays`, and force-drops any fingerprint the user has
   previously rejected — status `rejected` — so a discarded proposal never resurfaces).
4. Read `apply-plan.json` - it is compact (one-line claims). This is your work order for Phase 3.

## Phase 3 - APPLY  (PARALLEL sub-agents, one per target)
Launch these in parallel (respect `apply_max_parallel`). Each edits a DIFFERENT file, so parallel is
safe; never point two sub-agents at the same file. In replay mode, SKIP any bucket marked
`receipt_complete: true`; its exact failed-run receipt proves that bucket already finished. For the
list-valued review queue, skip it when top-level `review_queue_receipt_complete` is true.

a) For EACH entry in `apply-plan.by_skill` -> one editor sub-agent:
> You are a Dream applier for skill `<name>` (`<skill_file>`). If that file does NOT exist yet (a freshly
> seeded skill, or a configured skill not yet created), CREATE it first with a valid `SKILL.md` frontmatter
> (`name: <name>` + a one-line `description` of its scope) and a short intro heading; otherwise read the
> CURRENT file in full. Read the standing guidance in `~/.copilot/dream/inbox.md` and the
> "### Altitude & anti-bloat" rules in `~/.copilot/dream/dream-consolidation.prompt.md`. Read ONLY your
> bucket from `<apply_plan_path>` at `by_skill["<name>"]` (its `claims`, `current_chars`, `budget_chars`,
> `over_budget`, `watched`); do not ask the orchestrator to paste them. Skip a claim if an inbox veto covers it, if
> the skill already says it, or if it is too specific for this skill's scope. For each remaining claim,
> refine the existing line that covers the topic, or add ONE concise bullet (1-2 sentences, at the
> skill's level of generality) under the best existing section. Preserve the file's tone/tables/headers.
> NEVER delete a fact; cross-reference instead of duplicating. If `over_budget` is true, the edit must not
> grow the file: make room by merging duplicate or overlapping lines and tightening verbose wording in the
> sections you touch, without losing any fact. If `watched` is true, the user values this skill highly and
> reviews every change to it: prefer adding a line or refining one in place over merging, moving or
> restructuring, and never remove a topic.
> If a claim is marked `"promoted": true`, phrase it as a now-durable fact (it graduated from short-term).
> Do NOT consume or delete any review-queue proposal; those remain human-gated. After the skill edit
> succeeds, write `<receipt_dir>/skill-<name>.json` with `bucket="skill"`, `name="<name>"`,
> `status="complete"`, `completed_utc`, every claim `fingerprint` from this bucket in `fingerprints`, the
> skipped ones in `skipped_fingerprints`, your one-line result in `summary`, and a `changes` array with
> one entry per edit: `{"section": "<heading>", "action": "added|refined|merged|tightened|moved|removed",
> "note": "<what and why>"}`. Every merge, tightening, move or removal MUST appear there with its reason;
> the run's change report quotes it next to the exact diff. Then return ONLY one line:
> "<name>: <what changed, naming any section tightened, merged or removed>; skipped K; <chars before> -> <chars after>".

b) One active-work sub-agent (if `apply-plan.active_work` has `add` or `remove_decayed`, or is `over_budget`):
> You maintain `dream-active-work` (`<short_term_skill file>`): short-term memory of IN-FLIGHT work, not a
> knowledge base. Read it in full, then read ONLY `active_work` from `<apply_plan_path>`. For each thread in
> `active_work.add` (group claims by `thread`), add or refresh ONE entry of at most ~8 lines: **title** -
> repo/branch - PR/issue link; goal (1 line); current status (1 line); next step / open question (1-2
> lines); key files (at most 3); `last_touched = <today>`. Design details, findings and learnings do not
> belong here - link the design doc or PR instead; the plan routes durable learnings to reference skills.
> Remove the entries named in `remove_decayed`, except a thread that also appears in `active_work.add`:
> fresh activity wins, so refresh it and drop only its stale details. Keep this a tight CURRENT snapshot, not a log; merge
> duplicate threads. If the file exceeds `budget_chars` or `max_threads`, compact it: shrink verbose
> entries to the template, collapse threads whose `last_touched` is older than
> `config.thresholds.decay_days` into one-line bullets under "Recently completed", and drop the oldest
> one-liners beyond the cap. Before removing or shortening any text, append it verbatim to
> `active_work.archive_file` (create it with a dated heading if missing) so nothing is lost. After the
> edit succeeds, write `<receipt_dir>/active-work.json` with `bucket="active-work"`,
> `status="complete"`, `completed_utc`, all add/remove fingerprints in `fingerprints`, your one-line result
> in `summary`, and a `changes` array (`section`, `action`, `note`) naming every thread added, refreshed,
> collapsed or removed. Then return ONLY one line
> summarizing adds/removals/compaction and `<chars before> -> <chars after>`.

c) One review-queue sub-agent (if `apply-plan.review_queue` is non-empty):
> Read ONLY `review_queue` from `<apply_plan_path>`, the standing guidance in `~/.copilot/dream/inbox.md`,
> and the "### Altitude & anti-bloat" rules in `~/.copilot/dream/dream-consolidation.prompt.md`. Skip any
> item an inbox veto covers or that cannot be written at its target skill's level of generality - a
> proposal the user would reject is noise. For items with `needs_target: true`, pick the existing
> reference skill that owns the topic (`config.targets.general_skill` for cross-cutting ones). Items with
> `existing_skill_file` target an installed skill that is not auto-applied: propose a normal Before/After
> against that file. For each remaining item, write a proposal file `review-queue/<today>-<slug>.md`.
> It MUST begin with this YAML frontmatter (the approve/reject helpers parse `fingerprint` and
> `target`), followed by the human-readable change:
> ```
> ---
> fingerprint: <the claim fingerprint>
> slug: <slug>
> target: <target skill name, or new-skill:<proposed-name>>
> horizon: long|short
> confidence: high|medium|low
> importance: <1-10>
> source: sessions|git|inbox|mixed
> date: <today>
> ---
> # <short title>
> **Target:** `~/.copilot/skills/<name>/SKILL.md` - section "<section>"
> **Proposes:** <one line: what to add and why it is durable>
>
> ## Before
> <the exact current text, or "(new section)">
>
> ## After
> <the exact proposed text>
> ```
> For items marked `"new_skill": true`, set `target: new-skill:<name>` and use the body to propose the new
> skill (description + initial section outline) instead of a Before/After. The `## After` text must be
> the concise, general wording that would actually go in the skill. Do NOT edit any skill in place.
> If a same-day proposal for that fingerprint already exists, skip it. After every proposal is written
> or confirmed present, write `<receipt_dir>/review-queue.json` with `bucket="review-queue"`,
> `status="complete"`, `completed_utc`, all item fingerprints in `fingerprints`, and the skipped ones in
> `skipped_fingerprints`. Then return ONLY one line: "queued N proposals; skipped K".

Keep this SAME turn active until every APPLY agent completes: use `read_agent(wait:true, timeout:180)`
repeatedly rather than ending the turn. Collect the one-line summaries.

## Phase 4 - Ledger status  (ORCHESTRATOR, lean)
Using the fingerprints already present in `apply-plan.json` and the exact-run receipt files:
- Each `by_skill` + `active_work.add` fingerprint whose bucket has either `receipt_complete: true`
  (replay) or a fresh receipt from this run
  -> `python ledger.py set-status --fingerprint <fp> --status applied`.
- Each `review_queue` fingerprint whose bucket has a replay/fresh receipt -> `--status proposed`.
- Each `active_work.remove_decayed` fingerprint whose bucket has a replay/fresh receipt
  -> `--status archived`.
- Any fingerprint listed in a receipt's `skipped_fingerprints` -> `--status dropped` instead (the applier
  judged it vetoed, already covered, or too specific for its target).
- Drops were registered by the upsert; leave them (horizon=drop).
If an APPLY sub-agent FAILED for a skill even after one retry, mark those fingerprints `proposed`
instead of `applied`, so nothing is silently lost. Never infer per-plan completion from the ledger's
global item status; only this plan's receipt can suppress replay.

## Phase 5 - Journal + record run  (ORCHESTRATOR, lean)
Write the exact journal path provided by the bootstrap prompt (normally
`journal/<YYYY-MM-DD>.md`; replay mode uses a suffixed recovery journal) from the COMPACT plan totals +
your collected one-line summaries (NOT from raw sessions):
- **Summary line**: harvested N sessions (P prompts, A answers), shards S, dropped M, rerouted-to-reference R, active-work +A/-D, skills edited [...], promotions P, review-queue Q.
- **Applied changes**: one bullet per skill edit, `- <skill>: <its APPLY summary line>` (the report
  generator matches these bullets by skill name).
- **Conversation signals**: `apply-plan.totals.signals` (questions answered, corrections, preferences,
  decisions) and where notable corrections/preferences went.
- **Size check**: chars before -> after for each edited skill and `dream-active-work` vs its budget; flag
  any still over budget.
- **Active work snapshot**: the current threads after this run.
- **Review queue**: links to any proposal files awaiting approval.
- **For next in-repo session**: any repo-specific patterns to commit when next inside that repo.
- **Dropped (audit)**: the drop count + a few representative samples and why (so pruning stays reviewable).
Do not write a section listing file-level skill changes: after the run, `run-dream.ps1` appends
"## Skill changes (verified by diff)" (every skill edited, sections rewritten or removed, revert commands),
generated from a snapshot of the skills folder taken before and after the run.
Then write a run-record JSON and `python ledger.py record-run --json <file>`.
After the journal and `record-run` both succeed, write the completion-marker JSON at the exact path
provided by the bootstrap prompt. This marker is the FINAL filesystem action. Do not finish your turn
or emit a final response before it exists.

---

## Classification rubric  (READ BY EACH MAP SUB-AGENT - single source of truth)
A **claim** is one durable, generally-worded fact / pattern / decision - never "what I did today".
Assign `domain` (your project/area tags | `dev-workflow` | `off-domain`), `scope`, `horizon`, `importance`
(1-10: how much would this help me 6 months from now, in a different session?), `confidence`, `target`,
and `signal`.

### Mining the conversation (the back-and-forth is primary evidence)
Read each session as a conversation, not a log: the user's prompts say what they needed to know or do, the
agent's answers say what was found, and the exchange between them shows what survived scrutiny.
- **Questions -> settled answers** (`signal=question`). When the user asks something and an answer survives -
  backed by source, logs or tests, not contradicted later, built on by follow-ups - its durable core is a
  claim. A question asked repeatedly, or one that needed a long investigation, marks a knowledge gap: raise
  importance and target the skill a future session would load to answer it.
- **Corrections** (`signal=correction`). When the user corrects the agent ("no, it is X", "you missed Y")
  or later evidence overturns an answer, keep the corrected fact, plus a one-line pitfall if the wrong
  assumption is a natural one to make. Never record the superseded version.
- **Preferences and working rules** (`signal=preference`) stated in instructions or pushback ("always",
  "never", "I prefer", "don't"). They are `cross-cutting` only when they apply beyond the task at hand;
  task-local instructions ("don't touch that file", "keep the PR description brief") are drops. Skip
  anything the personal instruction files already say.
- **Decisions with rationale** (`signal=decision`, "X over Y because Z"): `topic` when the reason is a
  lasting property of the system, `feature` when it only concerns the change in flight.
- **Findings** (`signal=finding`) and in-flight **status** (`signal=status`, always `feature`).
- **Confidence comes from the dialogue**: verified by source/logs/tests or confirmed by the user -> high;
  plausible but unverified agent reasoning -> medium at most; doubted or revised later -> low or drop.
- `summary` entries condense earlier turns; use them as evidence under the same rules. Ignore pleasantries,
  formatting/tone requests, progress chatter, and tool noise.

### Scope decides the home (learnings leave the Dream skills)
| `scope` | What it is | `horizon` | Home |
|---|---|---|---|
| `feature` | Only meaningful while one feature, PR, investigation, incident or rollout is in flight: status, next steps, PR/bug numbers, the design being drafted, per-change decisions | short | `dream-active-work`, one entry per `thread` |
| `topic` | Durable knowledge about one system/component/area that stays true after the current work ends: architecture, behavior, constraints, debugging recipes | long | the reference skill that owns the topic |
| `cross-cutting` | Durable and not about one system: tools, workflows, engineering lessons, working preferences | long | `config.targets.general_skill`, or the reference skill that owns that tool/workflow |

The test: *"Would this still be true and useful after this feature / PR / investigation is finished?"* If
yes, it is NOT `feature` - even though it was discovered while working on one - so it never goes to
`dream-active-work`, which holds only the in-flight status of each thread. Split a mixed observation into a
`feature` claim (the status) and a `topic`/`cross-cutting` claim (the lesson). The reducer enforces this:
a durable scope aimed at active-work is rerouted to a reference skill or review, and a `feature` claim can
never enter a reference skill.

### Altitude & anti-bloat (specific detail only in a skill about that thing)
A skill holds knowledge at its own level of generality. Before targeting a skill, ask: *is this skill about
the thing this detail describes?*
- Write each LONG claim as a general rule, pattern or fact in one or two sentences, with at most one short
  example. Strip instance detail - resource/cluster/RG names, subscription IDs, PR/bug/incident numbers,
  commit SHAs, dates, build numbers, exact counts, one-off error text - unless that detail IS the durable
  fact (a stable endpoint, a naming pattern, a canonical command) and the target skill is specifically
  about it.
- Code-level specifics (one method's behavior, one handler's bug, a line-level mechanism, a telemetry
  table's cadence found in one investigation) belong to the repo (its docs, PR, agent-history), not a
  personal skill - unless the skill exists to document exactly that component. If generalizing leaves
  nothing useful, it is not skill material: `horizon=drop` (or `feature` if it belongs to an active thread).
- A claim that restates or marginally extends what the target skill already says is a drop, not an addition.
- Importance reflects breadth: 7+ only if it will help across many future sessions; knowledge that only
  matters when working on one narrow component is 5 or less.

### Inbox notes
`inbox.md` notes are the user's directives and are re-read every night. "Track X" / "stop tracking X"
produce the matching `feature` claim. Vetoes and placement rules ("do not reintroduce ...", "keep <skill>
lean") produce NO claim - obey them: drop every claim they cover, including paraphrases and new wordings.

### Anti-pollution rules (this is the whole point)
- **DROP** (written nowhere, but still emit it so recurrence is tracked): trivial/one-off debugging,
  closed bugs with no reusable lesson, machine-maintenance chatter (disk cleanup, app lag),
  scheduled-automation run transcripts, rejected explorations, and anything with
  `importance < config.thresholds.importance_keep_floor` that is not part of an active thread.
- **off-domain** (e.g. a personal side project): DROP from your reference skills. Keep ONLY if it yields
  a durable, reusable dev-workflow lesson -> then `domain=dev-workflow`, `scope=cross-cutting`,
  `target` = `config.targets.general_skill`.
- **LONG** (`topic` / `cross-cutting`: durable architecture, topology, naming, service/telemetry mapping,
  API-version quirks, deploy playbooks, repo map, permanent constraints, personal preferences): route to the
  matching reference skill. Apply your durable-vs-transient KEEP filter strictly (if you have one). If the
  user has NO long-term skills yet (empty `config.targets.long_term_skills`), route every LONG claim to the
  seed skill (`config.seed.general_skill.name`). Repo-owned conventions documented in a repo's own agent
  guidance (`config.read_only_context`) are NOT personal knowledge - reference them, do not copy them in.
- **SHORT** (`feature`: active feature, in-flight PR, ongoing investigation, current bug being worked,
  recent test result still live): `target=dream-active-work` ONLY. Never put in-flight/incident specifics
  into a reference skill.
- **Split rule**: a live incident / unmerged-PR workaround is temporary. Capture only the *durable
  lesson* it reveals as a LONG claim, and keep the transient specifics as a SHORT claim.

### Confidence gate (drives auto-apply vs review)
- `high` LONG claims are applied in place by an APPLY sub-agent.
- `medium`/`low` LONG claims become review-queue proposals (the user approves at wake-time).
- When unsure, choose the lower confidence - a false auto-apply pollutes; a queued proposal does not.

### Active-thread detection
A branch/feature/investigation appearing across multiple sessions in the window (e.g.
`feature/checkout-refactor`, `bugfix/search-timeout`) is an **active thread** -> one refreshed entry in
`dream-active-work` with title, repo/branch, goal, status, next/open, key files, `last_touched`.
Give every `feature` claim of one thread the same `thread` value.
(The sharder groups a thread's sessions into the same shard, so one MAP sub-agent sees the whole thread.)

## Guardrails
- Never write secrets, tokens, credentials, or PII into any skill/journal/proposal - even if present in a session.
- Never delete a user's existing skill prose; only refine/append/dedup. Merging duplicate lines or tightening
  wording to respect a skill's budget is refinement, provided no fact is lost. Archival = review-queue or a
  ledger status change, not silent deletion. `dream-active-work` is Dream-owned short-term memory: its
  compaction may remove text only after appending it to `active_work.archive_file`.
- Keep `dream-active-work` and the `dream` index skill SMALL (they load often). Detail lives in the
  on-demand reference skills, and only at the level of generality of the skill that holds it.
- If a source is empty or a sub-agent errors after one retry, continue with the rest. Partial > none.
- Idempotent: re-running the same night must not double-apply (ledger fingerprints + reducer dedup prevent it).

## Single-agent fallback
If `config.map_reduce.enabled` is `false`, skip sharding and run the classic single pass: read the harvest
JSON directly, classify inline per the rubric above (scope decides SHORT vs LONG), `ledger.py upsert`, then
apply per the same precedence (SHORT -> dream-active-work; LONG+high -> reference skill in place;
LONG+med/low -> review-queue; new area -> review-queue proposal) and the same altitude and budget rules.
Use this only on explicitly light days; the map-reduce path is the default and is preferred for quality.
