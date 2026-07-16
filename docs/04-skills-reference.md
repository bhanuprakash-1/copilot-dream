# 04 — Skills Reference (and the no-bloat design)

## The lazy-loading contract
Copilot (CLI and VS Code) loads a skill's **body into context only when the skill's `description` frontmatter
matches the current work**. So the way to "have a lot of knowledge available without filling context" is:

- one **thin, broadly-matching index skill** (`dream`) that is small and points elsewhere, and
- many **narrowly-matching detail skills** whose bodies stay out of context until they're relevant.

The Dream maintains that discipline: it keeps `dream` and `dream-active-work` small and pushes detail into
the reference skills.

```
Question ──► matches `dream` (broad description) ──► routing table ──► names the right detail skill
                                                                        │
                              detail skill's own description matches ───┘──► its body loads on demand
```

## Skills produced by this system

### `dream` — always-on index / router  (`~/.copilot/skills/dream/SKILL.md`)
- **Role:** first stop for "my setup / current work / where does X live / which skill". A compact routing
  table + a one-glance domain map. **No detail** — only pointers.
- **Why:** keeps the always-relevant footprint tiny while still guiding the agent to the right place.

### `dream-active-work` — short-term memory  (`~/.copilot/skills/dream-active-work/SKILL.md`)
- **Role:** the current in-flight threads (feature, PR, investigation) with `last_touched` and decay.
- **Schema per entry:** Title · repo/branch · goal · status · next/open · key files · `last_touched`.
- **Lifecycle:** refreshed while active; archived after 14 days untouched; durable lessons promoted out first.

## Long-term targets (reference skills — examples)
These are the skills the Dream *refines*. The set below is **illustrative** — configure your own in
`config.json → targets.long_term_skills`. The Dream refines whichever skills you list; it assumes no
particular service.

| Skill | Owns |
|---|---|
| `team-resources` | repos, paths, pipelines, on-call queues, subscriptions, identity, tools, links |
| `service-architecture` | service topology, environment URLs, data-store layout, debugging recipes |
| `deployment-runbook` | change→deploy planning, deploy templates, post-deploy live validation |
| `telemetry-queries` | which telemetry cluster/DB/table, correlation, ready-made queries |
| `repo-memory` | repo-specific coding patterns via per-repo `.github/agent-history/*` |
| `context-capture` | the manual "capture this" path + the authoritative KEEP/DROP filter |

The Dream does **not** duplicate content across these — it refines the right one and cross-references.

## Relationship to a manual capture skill
A manual, confirm-first "capture what we learned" skill (referred to here as `context-capture`) is the
hand-driven counterpart of this system. The Dream is its **automated, nightly** version. They share the same
durability filter (the Dream reads that skill's KEEP/DROP table). Use the manual capture skill mid-session
when you want something recorded *now*; let the Dream handle the routine nightly curation.

## Works in both Copilot CLI and VS Code Copilot Chat
Both clients read `~/.copilot/skills/`. These are plain markdown skills with standard frontmatter, so the
same knowledge is available in:
- **Copilot CLI** — `/skills` lists them; they load by description match.
- **VS Code GitHub Copilot Chat** — the same user-level skills directory is honored.

Nothing here is CLI-only. The engine (`~/.copilot/dream/`) is what's CLI-driven (it runs `copilot -p`), but
its **outputs are client-agnostic skills**.

## Naming rule
Prefix **repo-scoped** assets with a short repo id (e.g. `myrepo-`); leave **genuinely cross-repo** assets
unprefixed. `dream` and `dream-active-work` are cross-cutting (they span every project + general dev), so
they are **unprefixed** by design. If the Dream proposes a new repo-specific skill, it uses your repo prefix.
