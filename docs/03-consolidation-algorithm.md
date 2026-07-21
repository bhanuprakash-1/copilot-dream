# 03 — Consolidation Algorithm

The nightly agent (Opus/GPT @ 1M/max) executes `~/.copilot/dream/dream-consolidation.prompt.md` as a
**lean map-reduce orchestrator** — it shards the harvest, fans classification out to parallel sub-agents,
merges their output deterministically, then fans the edits out to parallel per-skill sub-agents. This doc
explains the logic; the prompt file is the source of truth. See
[01-architecture § Map-reduce execution model](01-architecture.md) for *why* it is structured this way.

Throughout, the orchestrator stays **lean**: it holds only compact JSON (manifests, candidates, the
apply-plan) and one-line sub-agent summaries — never a raw session transcript or a full skill body.

## Phase 0 — Load & Shard  (orchestrator)
Read `config.json`, `ledger.py stats` + `dump --status active`, then run `shard.py` to split today's
harvest into balanced, thread-grouped shards. The orchestrator reads only the shard **manifest**
(`harvest/shards/latest.json` → `manifest.json`) — file, kind, counts, est_tokens, branches per shard —
not the shard bodies. An empty day (0 shards) writes a short journal and stops.

Before sharding, a **bootstrap** step guarantees the scaffolding exists so later phases can always write: the
`dream` index and `dream-active-work` short-term skills are created from a minimal template if missing, and — if
`config.targets.long_term_skills` is empty and `config.seed.enabled` — the seed skill (`config.seed.general_skill`,
default `knowledge-base`) is created and used as the **sole** long-term routing target for this run. This is what
lets the Dream run usefully with zero long-term skills configured (see [cold-start seed](02-data-model.md#cold-start-seed-configjson--seed)).

## Phase 1 — MAP / Classify  (one sub-agent per shard, in parallel)
Each MAP sub-agent reads its own `shard-NN.json` (plus the classification rubric and, if configured, your
KEEP/DROP filter skill). It also consults, **read-only,** any `config.read_only_context.agent_instruction_globs`
files whose path matches its shard's repo — that repo's `.github/copilot-instructions.md`, `AGENTS.md`, etc. —
treating them as the repo's authoritative conventions, and **defers repo-owned knowledge to the repo** (its
agent-history and in-repo skills under `config.read_only_context.repo_skill_dirs`) instead of copying it into a
personal skill. It then extracts atomic **claims** from every session turn (user = intent, assistant =
findings) and each commit, scores each, and writes a compact `claims-NN.json`. Each claim is scored on
four axes:

| Axis | Values | Question |
|---|---|---|
| `domain` | your project/area tags (e.g. acme-api / platform / dev-workflow / off-domain) | Which area? |
| `horizon` | long / short / drop | Durable, in-flight, or noise? |
| `importance` | 1–10 | Would this help me in 6 months, elsewhere? |
| `confidence` | high / med / low | How sure am I this is a correct, general fact? |

### Anti-pollution classification rules (the core requirement)
- **DROP (written nowhere):**
  - trivial/one-off debugging; closed bugs with no reusable lesson;
  - machine-maintenance chatter (disk cleanup, app lag); scheduled-automation run transcripts;
  - rejected explorations; anything `importance < importance_keep_floor` not tied to an active thread.
- **off-domain** (e.g. a personal weekend side-project): dropped from your service skills. Kept **only** if it
  yields a durable *dev-workflow* lesson → `domain=dev-workflow`, `target = your dev-workflow skill`.
- **LONG:** durable architecture, topology, naming, cluster/telemetry mapping, API-version quirks, deploy
  playbooks, repo map, permanent constraints, personal preferences. Apply your KEEP/DROP filter
  (`config.targets.durable_filter_skill`, if set) strictly. → routed to the matching reference skill. If you
  have **no** long-term skills yet (empty `config.targets.long_term_skills`), every LONG claim is routed to the
  seed skill (`config.seed.general_skill.name`) instead. Repo-owned conventions documented in
  `config.read_only_context` are **not** personal knowledge — reference them, never copy them in.
- **SHORT:** active feature, in-flight PR, ongoing investigation, current bug, still-live test result.
  → routed to `dream-active-work` **only**. In-flight/incident specifics never enter a reference skill.
- **Split rule:** a live incident often contains one durable lesson + lots of transient detail. The lesson
  goes LONG; the specifics stay SHORT. (Same rule the capture skill enforces manually.)

### Active-thread detection
A branch/feature appearing across multiple sessions in the window (e.g. `feature/checkout-retry`,
`fix/gateway-timeout`) is an **active thread** — one refreshed entry in `dream-active-work`
with title, repo/branch, goal, status, next/open, key files, `last_touched`. (The sharder groups a
thread's sessions into the same shard, so one MAP sub-agent sees the whole thread at once.)

## Phase 2 — REDUCE / Register  (orchestrator + reduce.py)
`reduce.py merge` concatenates every `claims-NN.json`, dedups by fingerprint, and **conservatively
resolves cross-shard disagreement** (a claim two shards score differently is never auto-elevated to
long/high — it is demoted toward active-work or review). The merged `candidates.json` is upserted via
`ledger.py upsert` (all candidates, including drops; repeats bump `hit_count` / `distinct_days` — this is
where cross-night memory accumulates). Then `reduce.py plan` builds `apply-plan.json`: the per-skill APPLY
buckets, the active-work add/remove lists, the review-queue, and the drop count — folding in ledger
`promotions` and `decays` (see Phase 4).

## Phase 3 — APPLY  (one sub-agent per target, in parallel)
From `apply-plan.json` the orchestrator launches one editor sub-agent per bucket (each edits a different
file, so parallel is safe):
1. **per reference skill** (`by_skill`) — LONG + high-confidence claims. In-place edit: refine an existing
   entry if present, else slot under the best section; dedup first; preserve tone/tables/headers; a
   `promoted` claim is phrased as a now-durable fact. Also reconciles any same-day review-queue proposal
   for that skill (applies high-confidence ones, deletes the consumed file).
2. **`dream-active-work`** — adds/refreshes active threads and removes decayed ones. Kept a tight snapshot.
3. **review-queue** — writes a proposal (file, section, before/after; or a new-skill name+outline) for
   every LONG med/low-confidence, unroutable, or new-area item. Does **not** edit a skill.

New skills are **never auto-created** — they arrive as a review-queue proposal you approve. When a claim's
`target` is unroutable (matches no configured or seeded skill) and its `importance ≥ max(7, importance_keep_floor)`,
`reduce.py plan` marks it `new_skill: true` and the review-queue sub-agent writes a `new-skill:<name>` proposal
(description + section outline) instead of editing anything; lower-importance unroutable claims are queued as
ordinary proposals. `repo-memory` (repo-specific coding patterns) is recorded in the journal's "for next in-repo
session" section, not a personal skill (the Dream runs outside repos, so it records rather than commits).

## Phase 4 — Decay & Promote  (computed in REDUCE, executed in APPLY + status)
- **Promotion** — `ledger.py promotions` surfaces recurring shorts (≥ `promote_hit_count` over ≥
  `promote_distinct_days` distinct days); `reduce.py plan` routes each to its LONG target (applied by the
  Phase-3 skill sub-agent) or to the review-queue. This is how short-term facts *earn* long-term status.
- **Decay** — `ledger.py decays` surfaces stale active shorts; the active-work sub-agent removes them from
  `dream-active-work` and they are marked `archived`. Keeps active context small.
Finally the orchestrator marks each fingerprint via `ledger.py set-status` (applied / proposed / archived),
using the fingerprints already in `apply-plan.json`.

## Phase 5 — Journal + record run  (orchestrator)
`journal/<YYYY-MM-DD>.md` is written from the compact plan + one-line sub-agent summaries (never raw
sessions): a summary line (harvested/shards/dropped/active±/edited/promoted/queued), applied changes per
skill, the current active-work snapshot, review-queue links, repo-memory notes for the next in-repo
session, and an audit sample of what was dropped and why. Then a run record via `ledger.py record-run`.

## Guardrails
- Never write secrets/tokens/PII — even if present in a session.
- Never delete your existing skill prose; only refine/append/dedup. "Archival" = move to review-queue or mark
  in the ledger, not silent deletion.
- Keep `dream` and `dream-active-work` **small** (they load often); detail lives in on-demand reference skills.
- Idempotent: fingerprints prevent double-applying the same night.
- Partial > none: if a source errors, continue with the rest.

## Worked example (illustrative)
Imagine a fictional service **acme-api** and a day of harvested signals. In a real run these are spread
across shards and classified by parallel MAP sub-agents, then merged by `reduce.py` — the classification
below is what matters:

| Raw signal | Classification | Destination |
|---|---|---|
| Branch `feature/checkout-retry` across 2 sessions + commit `a1b2c3d` | SHORT, acme-api, imp 7 | `dream-active-work` thread |
| "Gateway retries must be idempotent (durable rule learned while fixing a double-charge)" | SHORT now; durable design lesson → LONG candidate on recurrence | active thread + ledger (watch for promotion) |
| "Build my weekend hobby mobile app" session | off-domain | DROP |
| "Clean up C: drive disk space" session | machine chatter | DROP |
| Nightly scheduler run transcript | automation noise | DROP |
| A stable telemetry-cluster → region mapping learned while debugging | LONG, high | `telemetry-queries` (in place) |
| An endpoint → service mapping seen on 3 separate days | promoted SHORT → LONG | `service-architecture` (promotion) |
