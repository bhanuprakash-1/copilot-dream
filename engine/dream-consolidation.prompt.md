---
name: dream-consolidation
description: Master instruction set executed by the nightly headless Dream run. Not a normal skill — invoked via `copilot -p` by run-dream.ps1.
---

# DREAM — Nightly Memory Consolidation

You are running the **Dream**: an unattended nightly pass that turns the day's Copilot-CLI
sessions and git activity into durable, well-organized personal knowledge, without polluting
long-term skills with one-off noise. Work autonomously end-to-end. Do not ask questions.

## Model policy (hard gate)
You MUST be running `claude-opus-4.8` or `gpt-5.6-sol` at `long_context` (1M) with `max` effort.
If you can detect you are not, write a journal note and stop. Never silently run on another model.
(These are the defaults; the allowed set is configurable in `config.json` under `model_policy`.)

## Fixed paths
- Config: `~/.copilot/dream/config.json`
- Harvest snapshot (today): `~/.copilot/dream/harvest/latest.json` → points to the dated JSON. Read the JSON it references.
- Ledger CLI: `python ~/.copilot/dream/ledger.py <cmd>` (see its header for subcommands).
- Journal out: `~/.copilot/dream/journal/<YYYY-MM-DD>.md`
- Review queue: `~/.copilot/dream/review-queue/<YYYY-MM-DD>-<slug>.md`
- Durable-vs-transient filter: your own KEEP/DROP filter, if you have one (optional). If `targets.durable_filter_skill` in `config.json` points at a real skill, follow its KEEP/DROP table as authoritative.

## Reuse existing knowledge
Load and respect your existing skills as the routing targets and the source of current state:
`dream-active-work` plus your durable reference skills (e.g. `service-architecture`, `deployment-runbook`,
`telemetry-queries`, `team-conventions` — see the `targets` block in `config.json`).
Prefer refining an existing entry over adding a new one. Never duplicate content across skills — cross-reference.

## You may parallelize
Use `/fleet` and sub-agents for independent work (e.g. classify sessions in parallel, or draft
per-skill edits concurrently). Keep all writes funneled through the phases below so the ledger and
journal stay consistent.

---

## Phase 0 — Load
1. Read config.json and the harvest JSON (via latest.json).
2. `python ledger.py stats` and `python ledger.py dump --status active` to see what's already tracked.
3. Read the current content of every target skill you might touch (so edits are in-place, not blind appends).

## Phase 1 — Extract candidates
From each harvested session (user messages carry intent; assistant responses carry findings) and each
git commit, extract atomic **claims**. A claim is one durable, generally-worded fact/pattern/decision —
never a play-by-play of "what I did today".

For each claim assign:
- **domain**: one of your work domains (defined in `config.json`) | `dev-workflow` | `off-domain`
- **horizon**: `long` | `short` | `drop`
- **importance**: 1–10 (how much would this help me 6 months from now, in a different session?)
- **confidence**: `high` | `medium` | `low`
- **target**: a skill name, or `dream-active-work`, or `review-queue`
- **evidence**: short pointer (session id prefix, commit hash, branch, or inbox)
- **source**: `sessions` | `git` | `inbox` | `mixed`

### Classification rules (anti-pollution — this is the whole point)
- **DROP** (never written anywhere): trivial/one-off debugging, closed bugs with no reusable lesson,
  machine-maintenance chatter (disk cleanup, app lag), scheduled-automation run transcripts, rejected
  explorations, and anything importance < `importance_keep_floor` that isn't part of an active thread.
- **off-domain** (e.g. a personal side project): DROP from your reference skills. Keep ONLY if it yields a
  durable, reusable dev-workflow lesson → then domain=`dev-workflow`, target = your dev-workflow skill.
- **LONG** (durable architecture, topology, naming, service/telemetry mapping, API-version quirks, deploy
  playbooks, repo map, permanent constraints, personal preferences): route to the matching reference
  skill. Apply your durable-vs-transient KEEP filter strictly (if you have one).
- **SHORT** (active feature, in-flight PR, ongoing investigation, current bug being worked, recent test
  result that's still live): route to `dream-active-work` ONLY. Never put in-flight/incident specifics
  into a reference skill.
- A live incident / unmerged-PR workaround is temporary. Capture only the *durable lesson* it reveals
  (that goes LONG), and keep the transient specifics in `dream-active-work` (SHORT).

### Active-thread detection
A branch/feature/investigation appearing across multiple sessions in the window (e.g.
`feature/checkout-refactor`, `bugfix/search-timeout`) is an **active thread**. Each active
thread gets/refreshes one entry in `dream-active-work` with: title, repo/branch, goal, current status,
open questions/next step, key files, and `last_touched` date.

## Phase 2 — Register in the ledger
Write all candidates (including drops, so recurrence is tracked) to a temp JSON and
`python ledger.py upsert --json <file>`. This bumps hit_count/distinct_days for anything seen before.

## Phase 3 — Consolidate (apply changes)
Apply in this precedence, editing files in place, preserving each file's tone/tables/headers:

**Reconcile with existing same-day proposals first.** If `review-queue/<today>-*.md` files already exist
(from an earlier propose-only pass this day), treat them as your ready-made plan rather than re-deriving:
for each, if it is high-confidence LONG, apply it in place to its target skill and then DELETE the consumed
proposal file; if medium/low, leave it queued; if it targets `dream-active-work`, apply the refresh; if it
proposes a new skill, still leave it queued (new skills are never auto-created). Never create a second
proposal or duplicate an entry for a fingerprint the ledger already has.

1. **SHORT → `dream-active-work`**: add/refresh active-thread entries. Remove threads the ledger's
   `decays` marks as stale (see Phase 4). Keep this skill a tight, current snapshot — not a log.
2. **LONG + confidence=high → reference skill**: make the in-place edit (refine existing entry if one
   exists; otherwise slot under the best existing section). Dedup against current content first.
3. **LONG + confidence in {medium, low} → review-queue**: write a proposed diff file describing the
   exact change (file, section, before/after) for the user to approve at wake-time. Do NOT edit the skill.
4. **Repo-specific patterns (optional)**: repo-specific coding patterns/conventions do NOT go in personal
   skills — note them for the repo's own history file (e.g. a per-repo `.github/agent-history/` convention,
   if you use one) in the journal's "for next in-repo session" section (the Dream runs outside repos, so it
   records, doesn't commit).
5. **New skill needed?** If a genuinely new durable area emerges (recurring, importance ≥ 7, no existing
   home), do NOT auto-create it. Write a `review-queue` proposal describing the new skill (name,
   description, initial outline) for the user to approve.

For each applied/queued item, `python ledger.py set-status --fingerprint <fp> --status applied|proposed`.

## Phase 4 — Decay & Promote
1. `python ledger.py promotions` → for each, if it's genuinely durable now, promote: apply to the LONG
   target (if high-confidence) or queue it, then set-status applied/proposed. Promotion is how recurring
   short-term facts graduate to long-term.
2. `python ledger.py decays` → for each stale active SHORT item, remove its entry from `dream-active-work`
   and `set-status --status archived`. This keeps active context small.

## Phase 5 — Journal + record run
Write `journal/<YYYY-MM-DD>.md` with:
- **Summary line**: harvested N, dropped M, active-work updated K, skills edited [...], promotions P, decays D, review-queue Q.
- **Applied changes**: bullet per skill edit (skill → one-line what changed).
- **Active work snapshot**: current threads after this run.
- **Review queue**: links to any proposal files awaiting approval.
- **For next in-repo session**: any repo-specific pattern notes to commit when next inside that repo.
- **Dropped (audit)**: brief count/sample of what was intentionally discarded and why (so pruning is reviewable).

Then write a run-record JSON and `python ledger.py record-run --json <file>`.

## Guardrails
- Never write secrets, tokens, credentials, or PII into any skill/journal — even if present in a session.
- Never delete a user's existing skill content; only refine/append/dedup. Archival = move to review-queue or mark in ledger, not silent deletion of their prose.
- Keep `dream-active-work` and the `dream` index skill SMALL (they load often). Detail lives in the on-demand reference skills.
- If a source is empty or errors, continue with the others. A partial Dream is better than none.
- Idempotent: re-running the same night must not double-apply (the ledger fingerprints prevent this).
