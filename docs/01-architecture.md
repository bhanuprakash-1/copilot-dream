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
      S1[(session-store.db<br/>sessions + turns)] --> HARVEST
      S2[git commits<br/>across your repos] --> HARVEST
      S3[inbox.md] --> HARVEST
      HARVEST --> SNAP["harvest/latest.json<br/>+ .md digest"]
    end

    SNAP --> SHARD["shard.py<br/>balanced, thread-grouped shards"]
    RUN -->|copilot -p, opus-4.8/gpt-5.6-sol<br/>long_context + max| BRAIN

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
The **harvest is code** (Python over SQLite + git) so it's cheap, reproducible, and never hallucinates the
inputs. The **consolidation is the model** because classification/dedup/refinement need judgment. The model
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
| **Long-term** (architecture, topology, playbooks, repo map, conventions) | reference skills (`team-resources`, `service-architecture`, `deployment-runbook`, `telemetry-queries`, …) | refined in place, deduped, rarely removed |
| **Short-term** (active feature, open PR, ongoing investigation, live finding) | `dream-active-work` | refreshed while active, archived on decay |
| **Noise** (one-off bug, machine chatter, off-domain personal, automation transcripts) | *dropped* | never written |

### Model policy
Only `claude-opus-4.8` or `gpt-5.6-sol`, both `--context long_context` (1M) `--effort max` — and every
sub-agent runs on that same model. The 1M window is the ceiling, not the operating point: the map-reduce
structure below keeps each agent working in a small, clean slice of it. Max reasoning is worth it for the
judgment-heavy classification and in-place editing. `run-dream.ps1` refuses any other model (PowerShell
`ValidateSet`); `config.model_policy` is the single source of truth for the allowed set.

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
