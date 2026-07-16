# 07 — Operations & Maintenance

Your day-to-day is intentionally tiny: **~2 minutes each morning to review**, and a **glanceable health check**
so you know it ran. The Dream is designed to fail *safe and loud*, not silent.

## The one command you run: `dream-status.ps1`
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File $env:USERPROFILE\.copilot\dream\dream-status.ps1
```
It prints a **GREEN / YELLOW / RED** verdict plus:
- newest journal + **how many hours ago** (did last night run?),
- last run status + model (from the ledger `runs` table),
- trigger state (Task Scheduler `CopilotDream`, or a desktop automation) + **next run time**,
- **review-queue count** (what's waiting for you),
- today's run-log tail on failure.

| Verdict | Meaning | Action |
|---|---|---|
| **GREEN** | ran recently, no pending review, trigger healthy | nothing |
| **YELLOW** | ran fine but items await your approval, or journal is 28–50 h old | review the queue / check tonight |
| **RED** | didn't run (journal > 50 h), last run failed, no trigger, or copilot/python missing | see "Recovery" below |

> Tip: wire `dream-status.ps1 -Json` into whatever morning automation you use (e.g. a desktop scheduler that
> posts to chat) to get this as a message before you log back in — then you don't even have to run the command.

## Daily routine (~2 min)
1. Glance at the digest / run `dream-status.ps1`.
2. If YELLOW for pending review: open `journal/<today>.md` (the summary + audit), skim `review-queue/*.md`.
3. Approve durable proposals (they auto-apply next full run once you keep them / or apply manually), delete
   ones you don't want. **The ledger won't re-propose what you delete** (fingerprints), so pruning sticks.
4. Drop notes for tonight — any of: `dream-note "..."`, tell any Copilot session *"add a dream note: ..."*, or edit `inbox.md`.

## Weekly (~5 min)
- Skim the week's journals — confirm promotions/decays look right (a short-term thread you finished should
  have faded; a recurring fact should have graduated to a reference skill).
- `python ledger.py dump --horizon short` — sanity-check active threads aren't stale.

## Monthly (~5 min)
- Skim reference-skill diffs (the Dream edits in place). If one is drifting or bloating, trim it — the Dream
  respects your edits.
- Retention is automatic: `run-dream.ps1` keeps the last ~40 harvest snapshots / run outputs and ~30 run
  logs; **journals are kept forever** (tiny, they're your audit trail). Nothing to clean manually.

## "Is it running and not stopped?" — failure modes & recovery
The Dream depends on: (a) the machine **awake** (sleep disabled), (b) your **session logged on** (locked is
fine; both trigger options need this), (c) `copilot` **authenticated**, (d) the **trigger** enabled.

| Symptom (from `dream-status.ps1`) | Likely cause | Fix |
|---|---|---|
| RED: journal > 50 h old | machine was off/asleep, or you were signed out overnight | run once now: `run-dream.ps1 -Model claude-opus-4.8`; confirm you stay logged on |
| RED: "no nightly trigger found" | task got removed | re-register: `triggers\install-scheduled-task.ps1` |
| WARN: Task 'CopilotDream' Disabled | someone disabled it | `Enable-ScheduledTask -TaskName CopilotDream` |
| WARN: last Task result non-zero / RED last run failed | run errored | read `logs\run-<date>.log` + newest `logs\dream-*-*.out.txt`; common: copilot auth expired -> run `copilot` once interactively to re-auth |
| Runner "hangs" after the journal is written | a copilot subagent/MCP process is slow to tear down | **handled automatically**: the runner judges success by the journal artifact, waits a 120 s grace for record-keeping, then force-kills only the stuck children and advances the watermark. Bounded by `-TimeoutMinutes` (default 45). Nothing for you to do. |
| Journal says harvest = 0 sessions | you didn't use Copilot that day | normal; the Dream still writes a short journal |

### Health-check the trigger directly
```powershell
Get-ScheduledTaskInfo -TaskName CopilotDream    # LastRunTime, LastTaskResult (0 = ok), NextRunTime
Start-ScheduledTask   -TaskName CopilotDream    # force a run now to test end-to-end
```

## Pause / resume / change model
```powershell
Disable-ScheduledTask -TaskName CopilotDream          # pause nightly
Enable-ScheduledTask  -TaskName CopilotDream          # resume
# switch the heavy model (only these two allowed):
triggers\install-scheduled-task.ps1 -Model gpt-5.6-sol
# ad-hoc safe run (proposes only, edits nothing):
run-dream.ps1 -ProposeOnly
```

## What "maintaining the knowledge" means (your part vs the Dream's)
- **Dream does:** harvest, classify, dedup, apply high-confidence durable facts, keep `dream-active-work`
  current with decay, queue anything uncertain, drop noise, journal everything.
- **You do:** a 2-min morning review — approve/prune the queue, and occasionally correct a skill edit. That
  correction *is* the training signal: the ledger remembers, so the system converges on your preferences.

## Safety recap
- Model policy enforced in the runner (`claude-opus-4.8` / `gpt-5.6-sol`, 1M/max only).
- Never writes secrets/PII; never deletes your skill prose (only refines/dedups; "archival" = queue/ledger).
- Watermark advances only on a successful **applying** run, so a failed or propose-only night is safely re-considered.
