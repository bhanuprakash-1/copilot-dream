# 05 — Install & Schedule

## Prerequisites
- Copilot CLI on PATH (`copilot` / `copilot.exe`), authenticated (OAuth token stored in `~/.copilot`).
- Python 3 on PATH (stdlib only — no pip installs).
- Sleep disabled overnight (`powercfg` AC/DC standby = 0) so the machine is awake at ~04:15.
- The session stays **logged on but idle** overnight, so your mapped drives, repo roots, and Copilot auth
  are available to the run.

> **Platform note:** Windows + PowerShell is the primary target — the runner (`run-dream.ps1`), health check
> (`dream-status.ps1`), and scheduling use PowerShell and Windows Task Scheduler. The harvest and ledger
> (`harvest.py`, `ledger.py`) are stdlib-only Python and run cross-platform, so porting to a `cron`/shell
> trigger on macOS/Linux is mostly a matter of replacing the two `.ps1` wrappers.

## The headless command (what actually runs)
```
copilot -p "<bootstrap that points at dream-consolidation.prompt.md>" `
  --model gpt-5.6-sol `           # cost-conscious pin in config.model_policy
  --context long_context `        # long-context tier (~1M)
  --effort xhigh `                # exactly xhigh
  --allow-all-tools --allow-all-paths --no-ask-user `
  --add-dir <your-repo-root> --add-dir %USERPROFILE% `
  --log-dir <dream>\logs -C <dream>
```
`run-dream.ps1` resolves and validates `config.model_policy` before harvesting. The shared
`resolve-model-policy.ps1` rejects unlisted models, default context, and effort below `xhigh`.
The bootstrap and consolidation prompt require explicit model/effort/context on every sub-agent,
parallel worker and retry; no downgrade is permitted if this configuration is unavailable.

### Model flags reference
| Want | Flag |
|---|---|
| GPT-5.6 Sol | `--model gpt-5.6-sol` |
| 1M context | `--context long_context` |
| Xhigh reasoning | `--effort xhigh` |
| Non-interactive | `-p "<prompt>"` + `--allow-all-tools` + `--no-ask-user` |

You need exactly one nightly **trigger**. **Microsoft Scout (ClawPilot)** (Option B) is the recommended driver —
it schedules the run *and* gives you a morning digest with an interactive review thread; **Windows Task
Scheduler** (Option A) is the dependency-free **no-Scout fallback**. Pick one.

## Option A — Windows Task Scheduler (no-Scout fallback, portable)
No third-party dependency; runs in your logged-on session so mapped drives + auth work.
```powershell
# register (04:15 daily, runs only when logged on)
powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\.copilot\dream\triggers\install-scheduled-task.ps1

# optional explicit pin (must be permitted by config.model_policy)
... install-scheduled-task.ps1 -Model gpt-5.6-sol

# test immediately
Start-ScheduledTask -TaskName CopilotDream
Get-ScheduledTaskInfo -TaskName CopilotDream

# remove
... install-scheduled-task.ps1 -Unregister
```
This registers "run only when logged on" (no stored password; shares your interactive session). Use
`-RunWhenLoggedOff` only if you truly sign out overnight — but note mapped network drives may be absent in
session 0; the core sessions→skills path still works because those live under `%USERPROFILE%`.

## Option B — Microsoft Scout / ClawPilot (recommended: schedule + digest + interactive review)
**Microsoft Scout** (a.k.a. **ClawPilot**) is a Windows agentic-automation app that runs scheduled or on-demand
agent "automations" with shell auto-approve and posts their output to Teams. It's the author's recommended way
to drive the Dream, because it covers three jobs at once — trigger the nightly run, deliver a morning **digest**,
and give you an **interactive review thread** you drive in plain English — with no extra glue code.

**Trigger the nightly run:** create a scheduled Scout automation whose shell step runs
`powershell -File %USERPROFILE%\.copilot\dream\run-dream.ps1` (the same command as the
Task in Option A). Or keep Task Scheduler for the run itself and use Scout only for the digest + review below.

**Digest + review — two ready-to-import automations under `engine/triggers/`:**

| File | What it does |
|---|---|
| `scout-digest-automation.example.json` | The **morning digest**: runs `dream-status.ps1 -Json`, lists the pending review-queue (`dream-approve.ps1 -List`), reads today's journal, and posts a skimmable `Dream <verdict>` message to Teams. Its chat thread is **interactive** — reply `reject <slug>`, `approve <slug>`, or `track <note>` and the same automation carries it out via the helper scripts. |
| `scout-review-actions-automation.example.json` | *(optional)* An **on-demand** review-actions thread — open it any time (not only at digest time) to see what's pending and act on it in plain English. |

**Import them** (Scout UI): **Automations → Import**, pick each `*.example.json`, then edit the
`C:\Users\<you>\...` paths in the prompt to your own profile (and adjust the Teams target / schedule if you
like). The examples request **shell auto-approve** so the run is unattended; Scout executes the agent, posts to
Teams, and keeps the automation's chat thread live so your English replies become review actions. The review
workflow itself is documented in
[07-operations-and-maintenance.md](07-operations-and-maintenance.md#reviewing--approvingrejecting-knowledge).

> **Gotcha — keep `schedule.naturalLanguage` a plain phrase.** On import, Scout *parses* the automation's
> `schedule.naturalLanguage` (e.g. `"Daily at 10:20 am"`) and uses it as the real schedule — it overrides the
> `days`/`time`/`hour`/`minute` fields. If that phrase isn't parseable — e.g. it carries an extra parenthetical
> like `"Daily at 10:20 am (or run on demand)"` — the whole import fails with *"Couldn't import any
> automations…"*. Use a clean phrase; tweak the exact time/days in the Scout UI afterward.

> Any other desktop scheduler / automation runner that can run a Copilot prompt on a timer and post to a chat
> channel works too — adapt `engine/triggers/desktop-scheduler-digest.example.json` to it. Scout is simply the
> concrete tool the author uses.

> The default scheduled action reads the model pin from `config.json` on every run. Change it only
> when the user explicitly requests a different model, then align the Scout automation and explicit
> worker-launch instructions. Preserve long context and xhigh effort; do not rewrite history.

## Verifying a run
```powershell
# what the next run would harvest, no model spend
...\run-dream.ps1 -DryRun

# a real run now (foreground, ~10-30 min)
...\run-dream.ps1

# afterwards
python ...\ledger.py stats
Get-Content ...\dream\journal\<today>.md
Get-ChildItem ...\dream\review-queue\
Get-Content ...\dream\logs\run-<today>.log -Tail 40
```

## Cost / runtime notes
- All agents use the long-context tier and xhigh reasoning, with small inputs through map-reduce.
  Expect meaningful AI-credit use per night. Tune costs by shortening the window or running less often,
  not by weakening the required model, reasoning or context tier. The harvest itself is free (local Python).
- `run-dream.ps1` sets Copilot's background-agent drain to 3300 seconds and requires the orchestrator to
  keep its headless turn active while parallel agents run. It advances the watermark only after a fresh
  journal plus final completion marker; a clean Copilot exit by itself is not success.
- The headless run starts with every MCP server disabled (`runner.disable_mcp_servers`) — the Dream needs only
  local tools, and unattended OAuth-backed servers can make the CLI abort at startup. An attempt that fails
  before applying anything is retried once with a fresh session (`runner.max_attempts`).
- If REDUCE completed before an interruption, the unfinished work order is preserved under `pending/`.
  Resume only APPLY with `run-dream.ps1 -ReplayPlan <pending-apply-plan.json>`.

## Tuning knobs (config.json)
- `model_policy` — one authoritative model pin; `context` must be `long_context` and
  `effort` must be `xhigh`. Sub-agent launch arguments must explicitly match.
- `window.default_hours` / `max_hours` — how much history each run considers (168h max lets one good run
  catch up after several missed nights).
- `runner.*` — MCP servers for the headless run (`disable_mcp_servers`, `keep_mcp_servers`), retry
  attempts/backoff, and the total time budget.
- `thresholds.*` — promotion (hit_count/distinct_days), decay_days, auto-apply confidence, importance floor,
  and the anti-bloat size budgets (`skill_budget_chars`, `skill_budget_overrides`,
  `active_work_budget_chars`, `active_work_max_threads`).
- `sources.*` — enable/disable sources, add repo roots, conversation caps (`user_message_max_chars`,
  `assistant_final_max_chars`, `assistant_interim_max_chars`, `summary_max_chars`, `session_budget_chars`).
- `domain.relevant_keywords` — bias the classifier's domain-relevance.
- `read_only_context.*` — point `agent_instruction_globs` / `repo_skill_dirs` at your repos' agent guidance
  (`.github/copilot-instructions.md`, `AGENTS.md`, in-repo skills) so extracted knowledge aligns with each repo's
  conventions, and `personal_instruction_globs` at your global instructions so stated preferences aren't
  re-captured. Consulted **read-only**, non-existent paths skipped — your repos are never edited.
- `targets.long_term_skills` — the reference skills to refine; `targets.general_skill` names the one that
  receives cross-cutting learnings. **Leave it empty for a zero-config start:** the
  Dream seeds a single `knowledge-base` skill and *proposes* dedicated skills over time (you approve), so it's
  useful from night one; fill it in once you know your taxonomy. `seed.enabled` toggles this bootstrap/seeding.
