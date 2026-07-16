# 06 — Adopt / Adapt on Your Machine

The whole system is a handful of small files + 2 skills + docs. Nothing is secret; there are no credentials in
it. To adopt it, copy the engine, genericize identity/paths for your machine, and pick a trigger.

## What to copy
```
~/.copilot/dream/                     # engine (config, harvest.py, shard.py, reduce.py, ledger.py, prompt, runner, triggers)
~/.copilot/skills/dream/              # index skill
~/.copilot/skills/dream-active-work/  # short-term skill
docs/                                 # these docs (optional)
```
Do **not** copy: `ledger.db`, `state.json`, `harvest/`, `journal/`, `review-queue/` — those are personal run
state and will be regenerated (`python ledger.py init`).

## What to change (all in `config.json`)
| Field | Change to |
|---|---|
| `identity.alias` | your alias |
| `identity.git_emails` / `git_names` | your `git config user.email` / `user.name` (used to filter commits) |
| `sources.git_commits.roots_glob` | your repo roots (e.g. `D:/src/*`) |
| `targets.long_term_skills` | your reference skills (drop the examples you don't have) |
| `domain.relevant_keywords` | your services/domain terms |
| `paths.*` | usually unchanged (all under `~/.copilot/dream`), except `paths.docs` |

Then update the **hard-coded paths** in:
- `dream-consolidation.prompt.md` — the "Fixed paths" block (or replace the user-profile path).
- `run-dream.ps1` — the `--add-dir '<your-repo-root>'` (it already uses `$env:USERPROFILE`).
- `triggers/install-scheduled-task.ps1` (and any desktop-automation definition) — any profile / repo-root paths.
- `skills/dream/SKILL.md` — the routing table (your skills) and any absolute paths.

> Tip: most machine-specific values are the profile path and the repo root. A find/replace of your old
> `%USERPROFILE%` path and your repo root gets you 90% there.

## Model policy is portable
The two-model policy (`claude-opus-4.8` / `gpt-5.6-sol`, `long_context`, `max`) is enforced in
`run-dream.ps1`. If you prefer different models, edit the `ValidateSet` and the flags in one place.

## Minimal bring-up on a new machine
```powershell
# 1. edit config.json (identity, repo roots, target skills, keywords)
# 2. init state
python %USERPROFILE%\.copilot\dream\ledger.py init
# 3. dry-run to confirm harvest sees your sessions + commits
powershell -File %USERPROFILE%\.copilot\dream\run-dream.ps1 -DryRun
# 4. one real run
powershell -File %USERPROFILE%\.copilot\dream\run-dream.ps1
# 5. schedule (Windows Task Scheduler or a desktop automation app)
```

## Design principles worth keeping if you rebuild it
1. **Deterministic harvest, model-driven consolidation.** Don't make the model find the inputs.
2. **A ledger with fingerprints.** It's what buys idempotence, promotion, and decay — the anti-pollution core.
3. **Two horizons, two homes, one drop bucket.** Long-term in reference skills; short-term in one decaying
   skill; noise dropped (but recorded).
4. **Thin index + on-demand detail.** Keep the always-loaded surface tiny.
5. **Review-gated by default.** Auto-apply only high-confidence durable facts; queue the rest.
6. **Respect the human's edits.** Never silently delete their prose; the ledger won't re-add what they removed.

## Safety checklist before sharing
- [ ] No secrets/tokens/PII in `config.json` or the skills (there shouldn't be — the Dream is instructed
      never to write secrets).
- [ ] `ledger.db`, `journal/`, `review-queue/`, `harvest/` excluded from any copy/commit.
- [ ] Paths genericized; your own Copilot auth is used (never share tokens).
