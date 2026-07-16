---
name: dream
description: Personal knowledge index and router. Use as the FIRST stop when a question is about "my" setup, my repos, my current/active work, where something lives, which skill to use, or how my personal knowledge is organized. Routes to the right detail skill instead of loading everything. Maintained nightly by the Dream consolidation.
applies-to: all
---

# Dream - Personal Knowledge Index

Thin router that keeps context small: it points to the right detail skill, which loads **on demand**.
Do not put detail here - put it in the target skill and link it. Maintained nightly by the Dream
(`~/.copilot/dream/`).

## Route by intent

<!--
  The rows below are EXAMPLES. Customize them for your own setup: rename the target
  skills, add rows for the domains you actually work in, and delete the ones you do
  not use. The Dream keeps these routing targets in sync with the skills it maintains.
  `team-resources` and `repo-memory` refer to your own personal / per-repo skills.
-->

| If the question is about... | Go to skill |
|---|---|
| **What I'm working on right now** - active features, in-flight PRs, ongoing investigations, current bugs | `dream-active-work` |
| My repos, paths, tools, pipelines, links | `team-resources` (your own) |
| Service / system architecture | `service-architecture` |
| Deployment steps / runbooks | `deployment-runbook` |
| Telemetry / logs / queries | `telemetry-queries` |
| Repo-specific coding patterns (per-repo) | `repo-memory` (per-repo history) |

## How my knowledge is maintained
- **Long-term** (durable architecture, playbooks, repo map, conventions) -> the reference skills above.
- **Short-term** (active threads, in-flight work) -> `dream-active-work`, with automatic decay.
- The **Dream** (a nightly headless Copilot run) harvests my recent sessions + git and refines these
  skills; it drops one-off noise and only promotes recurring facts to long-term.

## Feeding the Dream a note
Any of these land in `~/.copilot/dream/inbox.md` and get classified on the next nightly run:
1. **Command:** `dream-note "track the cache work as an active thread"` (tip: add a `dn` alias in `$PROFILE`).
2. **Just ask any Copilot session** (CLI or VS Code): *"add a dream note: <text>"* - see the agent rule below.
3. **Edit** `~/.copilot/dream/inbox.md` directly.

> **Agent rule:** when the user says "add a dream note: X", "dream note: X", or "note for the dream: X",
> append a line `- (YYYY-MM-DD HH:mm) X` under the marker in `~/.copilot/dream/inbox.md`, then confirm.
> Do not route it elsewhere - the nightly Dream classifies it (durable -> a skill; short-term -> active-work; noise -> dropped).

## Verify it's running
`powershell -File ~/.copilot/dream/dream-status.ps1` -> GREEN/YELLOW/RED (last run, next run, pending reviews).
Ops and recovery steps live in the repo `docs/`.
