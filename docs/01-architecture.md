# 01 — Architecture

## Design goals
1. **Automated & unattended** — runs in a quiet overnight window (e.g. ~04:00–10:30) with no interaction.
2. **Long-term + short-term** — durable knowledge *and* current in-flight work, kept separate.
3. **No pollution** — one-off/trivial detail must never enter long-term skills.
4. **No context bloat** — a thin always-on index; detail loads only on demand.
5. **Reusable everywhere** — the outputs are skills, readable by Copilot CLI *and* VS Code Copilot Chat.
6. **Extensible** — new input sources (VS Code chat, pull-request history, chat/email) plug in without redesign.
7. **Reviewable** — you review and correct; the system respects your edits.
8. **High-quality synthesis** — parallel, fresh-context sub-agents do the classification and editing, so no single agent degrades while wading through a whole heavy day.

## Control flow

```mermaid
flowchart TD
    subgraph Trigger["Trigger (nightly ~04:15)"]
      SCHED[Desktop scheduler<br/>optional] -->|shell step| RUN
      TASK[Windows Task Scheduler] --> RUN
    end

    RUN["run-dream.ps1<br/>(enforce model policy)"] --> HARVEST

    subgraph L1["Layer 1 — Harvest (deterministic, Python)"]
      HARVEST["harvest.py"]
      S1[(session-state events.jsonl<br/>prompts, answers, summaries)] --> HARVEST
      S2[git commits<br/>across your repos] --> HARVEST
      S3[inbox.md] --> HARVEST
      HARVEST --> SNAP["harvest/latest.json<br/>+ .md digest"]
    end

    SNAP --> SHARD["shard.py<br/>balanced, thread-grouped shards"]
    RUN -->|copilot -p, gpt-5.6-sol<br/>long_context + xhigh| BRAIN

    subgraph Brain["Layers 2-5 — Consolidation (lean orchestrator + parallel sub-agents)"]
      BRAIN["dream-consolidation.prompt.md<br/>(lean orchestrator)"]
      SHARD --> MAP["Layer 2 — MAP (parallel sub-agents)<br/>classify: importance / horizon / domain / confidence"]
      BRAIN -.orchestrates.-> MAP
      MAP --> REDUCE["reduce.py<br/>merge + dedup + plan"]
      REDUCE --> LEDGER[(ledger.db<br/>item registry)]
      LEDGER --> CONS["Layer 3 — APPLY (parallel sub-agents)<br/>edit each target skill in place"]
      LEDGER --> DECAY["Layer 4 — Decay & Promote"]
      CONS --> JOURNAL["Layer 5 — Journal + Review queue"]
      DECAY --> JOURNAL
    end

    CONS -->|long, high-confidence| REF["Reference skills<br/>team-resources, service-*, ..."]
    CONS -->|short / in-flight| ACTIVE["dream-active-work"]
    CONS -->|long, med/low-confidence| RQ["review-queue/*.md"]
    JOURNAL --> DIGEST["Chat digest / journal<br/>(your morning review)"]
    RUN -->|success| WM[(state.json<br/>watermark)]
```

## Why this structure

### Deterministic harvest, probabilistic consolidation
The **harvest is code** (Python over SQLite, the CLI's per-session event logs, and git) so it's cheap,
reproducible, and never hallucinates the inputs. It rebuilds each conversation — the user's prompts, the
agent's commentary and answers, compaction summaries — so the classifiers can learn from the back-and-forth
(questions, corrections, stated preferences), not just from final outcomes. The **consolidation is the model**
because classification/dedup/refinement need judgment. The model
never has to *find* the raw material — `harvest.py` hands it a compact snapshot.

### The ledger is what prevents pollution and enables decay
A plain LLM pass each night would re-derive everything and drift. The **ledger** (`ledger.db`) gives the
system memory *about its own memory*: every candidate is fingerprinted and counted. That yields three
properties a stateless pass can't have:
- **Idempotence** — re-running a night doesn't double-apply (fingerprints dedup).
- **Promotion** — a fact seen on ≥3 distinct days graduates from short-term to long-term (recurrence = durability).
- **Decay** — an in-flight thread untouched for 14 days is archived out of active context automatically.

### Two horizons, two homes
| Horizon | Home | Lifecycle |
|---|---|---|
| **Long-term** (architecture, topology, playbooks, repo map, conventions — anything that outlives the feature it was found in) | reference skills (`team-resources`, `service-architecture`, `deployment-runbook`, `telemetry-queries`, …); cross-cutting lessons go to `targets.general_skill` | refined in place, deduped, rarely removed; kept at each skill's level of generality and within its size budget |
| **Short-term** (the live status of an active feature, open PR, ongoing investigation) | `dream-active-work` | refreshed while active, archived on decay; compacted (removed text archived) when over budget |
| **Noise** (one-off bug, machine chatter, off-domain personal, automation transcripts, instance detail no skill is about) | *dropped* | never written |

A claim's `scope` (`feature` / `topic` / `cross-cutting`) decides its horizon, and `reduce.py` enforces it, so
learnings discovered during a feature still reach the reference skills instead of piling up in short-term memory.

### Every edit is auditable and revertible
The model edits skills in place, so the runner does not rely on its self-report: `skillaudit.py` snapshots the
whole skills folder into a local git history before and after each run and derives the change report from
the diff (sections added, rewritten or removed, exact lines). The Copilot CLI's per-session edit log tells the
Dream's own edits apart from anything else that changed while it ran, so a concurrent edit of yours is never
reported as, or reverted with, the Dream's. Any run's change to any skill can be undone with one command, and
the undo keeps later edits unless they touched the same lines. Skills you care most about
(`targets.watched_skills`) are itemized first and get a more conservative applier. A run lock keeps two runs,
or a run and a revert, from overlapping.

### Model policy
`config.model_policy` pins the cost-conscious model: `gpt-5.6-sol`, `long_context` (~1M tier),
and `xhigh` reasoning. Every sub-agent, nested worker and retry explicitly requests that same
configuration; `max`, `high`, and default context are not permitted. The large window
is the ceiling, not the operating point: map-reduce keeps each agent's actual input small.
`resolve-model-policy.ps1` validates the allow-list and reasoning/context floors before the runner
harvests anything. An unavailable configuration is a reported failure, not permission to downgrade.

### Map-reduce execution model (why parallel sub-agents)
A 1M context window is necessary but not sufficient. In practice an agent's output quality starts to
degrade well before the window is full — often around a third of it — as reasoning, tool output, and
partial edits accumulate. A single agent asked to read a whole heavy day *and* edit every skill would
spend its best tokens early and drift later. So the nightly run is a **lean map-reduce orchestrator**,
not a monolithic reader:

| Step | Who | Context it holds |
|---|---|---|
| **Shard** | `shard.py` (deterministic) | splits the harvest into balanced, thread-grouped shards (`map_reduce.target_tokens` each, ≤ `max_shards`) |
| **MAP** | one classifier **sub-agent per shard**, in parallel | only its own shard + the rubric |
| **REDUCE** | `reduce.py` (deterministic) + the orchestrator | compact JSON only (candidates, apply-plan) |
| **APPLY** | one editor **sub-agent per target skill**, in parallel (≤ `apply_max_parallel`) | only that one skill + its routed claims |
| **Journal** | the orchestrator | compact plan + one-line sub-agent summaries |

Two properties fall out of this:
- **The orchestrator stays lean.** It never reads a raw session transcript or a full skill body — only
  manifests, compact JSON, and one-line summaries. Its own context stays far below the degradation zone
  all night, even on a 300K-token day.
- **Every unit of judgment gets a fresh window.** Each shard is classified, and each skill edited, by an
  ephemeral sub-agent that starts clean. There is nothing to compact, because no sub-agent lives long
  enough to fill up. Threads are grouped so one classifier sees a whole feature/branch at once.

`shard.py` and `reduce.py` are deterministic code for the same reason `harvest.py` is: partitioning,
fingerprint-dedup, and routing are bookkeeping, not judgment — doing them in code keeps them reproducible
and keeps the orchestrator's context tiny. Set `map_reduce.enabled = false` to fall back to the classic
single-agent pass on light days.

## Prior art / inspiration
This design borrows two well-known ideas:
- **Sleep-time compute** (popularized by the Letta / MemGPT project): let an agent do useful background work —
  summarizing, reorganizing, and consolidating memory — while it is otherwise idle, so the "awake" path stays
  fast and uncluttered.
- **Generative Agents** (Stanford, Park et al. 2023): a memory stream plus periodic *reflection* that distills
  many low-level observations into higher-level, durable takeaways.

The nightly Dream is essentially that reflection pass: a deterministic harvest gathers the day's raw
observations, then a consolidation pass distills them — promoting what recurs, decaying what goes stale, and
dropping noise.

## Trigger model (why it works while you're logged off the keyboard)
Your machine stays **logged on but idle** overnight (sleep is disabled). Because the interactive session is
alive, your mapped drives, repo roots, and your Copilot auth token are all available at ~04:15 — so either a
**Windows Scheduled Task** ("run only when logged on") or a **Microsoft Scout (ClawPilot)** automation whose
shell step runs `run-dream.ps1` works. Scout is the recommended driver — it also posts a morning digest and an
interactive review thread; Windows Task Scheduler is the portable, no-Scout fallback. See
[05-install-and-schedule.md](05-install-and-schedule.md).

## Extensibility
New sources are added in `harvest.py` (`harvest_*` functions) and declared in `config.json → sources`.
The classifier and ledger are source-agnostic (each item carries a `source` tag). Candidates on the roadmap:
VS Code Copilot Chat transcripts, pull-request create/update history, incident tickets, chat/email threads.
