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
  --model claude-opus-4.8 `      # or gpt-5.6-sol — ONLY these two
  --context long_context `        # 1,000,000-token tier
  --effort max `                  # max reasoning
  --allow-all-tools --allow-all-paths --no-ask-user `
  --add-dir <your-repo-root> --add-dir %USERPROFILE% `
  --log-dir <dream>\logs -C <dream>
```
`run-dream.ps1` builds this, after first running `harvest.py`. It **refuses any model except the two allowed**
(PowerShell `ValidateSet`) and passes `long_context` + `max` unconditionally.

### Model flags reference
| Want | Flag |
|---|---|
| Claude Opus 4.8 | `--model claude-opus-4.8` |
| GPT-5.6 Sol | `--model gpt-5.6-sol` |
| 1M context | `--context long_context` |
| Max reasoning | `--effort max` |
| Non-interactive | `-p "<prompt>"` + `--allow-all-tools` + `--no-ask-user` |

You need exactly one nightly **trigger**. **Microsoft Scout (ClawPilot)** (Option B) is the recommended driver —
it schedules the run *and* gives you a morning digest with an interactive review thread; **Windows Task
Scheduler** (Option A) is the dependency-free **no-Scout fallback**. Pick one.

## Option A — Windows Task Scheduler (no-Scout fallback, portable)
No third-party dependency; runs in your logged-on session so mapped drives + auth work.
```powershell
# register (04:15 daily, runs only when logged on)
powershell -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\.copilot\dream\triggers\install-scheduled-task.ps1

# with GPT-5.6 Sol instead
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
`powershell -File %USERPROFILE%\.copilot\dream\run-dream.ps1 -Model claude-opus-4.8` (the same command as the
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

> Any other desktop scheduler / automation runner that can run a Copilot prompt on a timer and post to a chat
> channel works too — adapt `engine/triggers/desktop-scheduler-digest.example.json` to it. Scout is simply the
> concrete tool the author uses.

> To switch the heavy model to GPT-5.6 Sol, change `-Model gpt-5.6-sol` in the run step.

## Verifying a run
```powershell
# what the next run would harvest, no model spend
...\run-dream.ps1 -DryRun

# a real run now (foreground, ~10-30 min)
...\run-dream.ps1 -Model claude-opus-4.8

# afterwards
python ...\ledger.py stats
Get-Content ...\dream\journal\<today>.md
Get-ChildItem ...\dream\review-queue\
Get-Content ...\dream\logs\run-<today>.log -Tail 40
```

## Cost / runtime notes
- One run reads a day of sessions + all target skills in a single 1M-context pass at max effort — expect
  meaningful AI-credit use per night. Tune by: shortening the window, lowering `--effort` for light days, or
  running every other night. The harvest itself is free (local Python).
- `run-dream.ps1` advances the watermark **only on success**, so a failed/cancelled night is safely retried.

## Tuning knobs (config.json)
- `window.default_hours` / `max_hours` — how much history each run considers.
- `thresholds.*` — promotion (hit_count/distinct_days), decay_days, auto-apply confidence, importance floor.
- `sources.*` — enable/disable sources, add repo roots, truncation sizes.
- `domain.relevant_keywords` — bias the classifier's domain-relevance.
