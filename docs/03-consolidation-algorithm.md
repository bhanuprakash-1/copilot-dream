# 03 — Consolidation Algorithm

The nightly agent (Opus/GPT @ 1M/max) executes `~/.copilot/dream/dream-consolidation.prompt.md`. This doc
explains the logic; the prompt file is the source of truth.

## Phase 0 — Load
Read `config.json`, the harvest JSON (via `harvest/latest.json`), `ledger.py stats` + `dump --status active`,
and the **current content of every target skill** it might touch (so edits are in place, not blind appends).
It also loads your capture skill (`context-capture/SKILL.md`), whose KEEP/DROP table is the authoritative
durability filter.

## Phase 1 — Extract candidates
From each session (user messages = intent, assistant responses = findings) and each commit, extract atomic
**claims**. Each claim is scored on four axes:

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
  yields a durable *dev-workflow* lesson → `domain=dev-workflow`, `target=team-resources`.
- **LONG:** durable architecture, topology, naming, cluster/telemetry mapping, API-version quirks, deploy
  playbooks, repo map, permanent constraints, personal preferences. Apply the capture skill's KEEP filter
  strictly. → routed to the matching reference skill.
- **SHORT:** active feature, in-flight PR, ongoing investigation, current bug, still-live test result.
  → routed to `dream-active-work` **only**. In-flight/incident specifics never enter a reference skill.
- **Split rule:** a live incident often contains one durable lesson + lots of transient detail. The lesson
  goes LONG; the specifics stay SHORT. (Same rule the capture skill enforces manually.)

### Active-thread detection
A branch/feature appearing across multiple sessions in the window (e.g. `feature/checkout-retry`,
`fix/gateway-timeout`) is an **active thread** — one refreshed entry in `dream-active-work`
with title, repo/branch, goal, status, next/open, key files, `last_touched`.

## Phase 2 — Register in the ledger
All candidates (including drops) are upserted via `ledger.py upsert`. Repeats bump `hit_count` /
`distinct_days`. This is where cross-night memory accumulates.

## Phase 3 — Consolidate (apply), in precedence order
1. **SHORT → `dream-active-work`** — add/refresh active threads; remove decayed ones (Phase 4).
2. **LONG + high → reference skill** — in-place edit (refine existing entry if present; else slot under the
   best section). Dedup against current content first. Preserve the file's tone/tables/headers.
3. **LONG + med/low → review-queue** — write a proposal file (file, section, before/after) for approval;
   do **not** edit the skill.
4. **repo-memory** — repo-specific coding patterns are recorded in the journal's "for next in-repo session"
   section for the per-repo `.github/agent-history/_shared.md` (the Dream runs outside repos, so it records
   rather than commits).
5. **New skill needed?** Not auto-created. A `review-queue` proposal describes the new skill (name — no prefix
   if cross-repo, else a short repo-id prefix per your naming rule — description, outline).

Each applied/queued item is marked via `ledger.py set-status`.

## Phase 4 — Decay & Promote
- `ledger.py promotions` → genuinely-durable recurring shorts are promoted to their LONG target (high) or
  queued (med/low). This is how short-term facts *earn* long-term status.
- `ledger.py decays` → stale active shorts are removed from `dream-active-work` and marked `archived`
  (after any durable lesson is promoted). Keeps active context small.

## Phase 5 — Journal + record run
`journal/<YYYY-MM-DD>.md` contains: a summary line (harvested/dropped/updated/edited/promoted/decayed/queued),
applied changes per skill, the current active-work snapshot, review-queue links, repo-memory notes for next
in-repo session, and an audit sample of what was dropped and why. Then a run record via `ledger.py record-run`.

## Guardrails
- Never write secrets/tokens/PII — even if present in a session.
- Never delete your existing skill prose; only refine/append/dedup. "Archival" = move to review-queue or mark
  in the ledger, not silent deletion.
- Keep `dream` and `dream-active-work` **small** (they load often); detail lives in on-demand reference skills.
- Idempotent: fingerprints prevent double-applying the same night.
- Partial > none: if a source errors, continue with the rest.

## Worked example (illustrative)
Imagine a fictional service **acme-api** and a day of harvested signals:

| Raw signal | Classification | Destination |
|---|---|---|
| Branch `feature/checkout-retry` across 2 sessions + commit `a1b2c3d` | SHORT, acme-api, imp 7 | `dream-active-work` thread |
| "Gateway retries must be idempotent (durable rule learned while fixing a double-charge)" | SHORT now; durable design lesson → LONG candidate on recurrence | active thread + ledger (watch for promotion) |
| "Build my weekend hobby mobile app" session | off-domain | DROP |
| "Clean up C: drive disk space" session | machine chatter | DROP |
| Nightly scheduler run transcript | automation noise | DROP |
| A stable telemetry-cluster → region mapping learned while debugging | LONG, high | `telemetry-queries` (in place) |
| An endpoint → service mapping seen on 3 separate days | promoted SHORT → LONG | `service-architecture` (promotion) |
