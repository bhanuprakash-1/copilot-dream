# Contributing to Copilot Dream

Thanks for your interest! Copilot Dream is a small, dependency-light tool: plain
Markdown, standard-library Python, and Windows PowerShell. Please keep changes in
that spirit.

## Project layout
- `engine/` - the runtime that gets copied to `~/.copilot/dream/`:
  - `harvest.py` - deterministic collector (sessions + git + inbox -> snapshot).
  - `ledger.py` - SQLite item registry (importance, hit_count, promotion, decay).
  - `run-dream.ps1` - orchestrator (harvest -> headless Copilot run -> watermark).
  - `dream-status.ps1` - health check.
  - `config.example.json`, `inbox.example.md` - templates seeded on install.
  - `triggers/` - scheduled-task installer.
- `skills/dream`, `skills/dream-active-work` - the skill templates.
- `install/install.ps1` - bootstrap.
- `examples/` - a sample config and a sample journal.

## Extending: add a new harvest source
The ledger and the classifier are **source-agnostic** - they only care about the
normalized items they receive, not where those items came from. To teach the Dream
a new input:

1. Add a collector function in `engine/harvest.py` that returns records in the
   common shape the other sources already emit (an id, a timestamp, some text, and
   an origin label).
2. Declare and configure it under `sources.<your_source>` in `config.json` (add the
   same block to `examples/example-config.json` with an `enabled` flag so others can
   discover it).
3. Merge its records into the snapshot the harvester writes. The ledger
   (`ledger.py upsert`) and the consolidation prompt then pick them up unchanged.

No changes to `ledger.py` are needed for a new source - keep source-specific logic
in `harvest.py` and its configuration in `config.json`.

## Coding norms
- **PowerShell: ASCII-only.** No smart quotes, em dashes, arrows, or other non-ASCII
  characters in `.ps1` files. Use `-NoProfile -ExecutionPolicy Bypass` in examples.
  Prefer explicit, idempotent operations that never delete user data.
- **Python: standard library only.** No third-party packages and no `pip install`
  step. Target Python 3.8+. Keep subcommands deterministic and side-effect-light.
- **Markdown skills:** keep the index skill (`dream`) thin - it routes, it does not
  store detail. Detail belongs in the target skill it links to.

## Security: never commit personal data
This repo ships **templates only**. Your real knowledge and everything the Dream
generates stays on your machine and is **git-ignored**. Never commit:

- your personal skills (anything under `~/.copilot/skills/` that is yours),
- `config.json` (your identity, emails, repo paths),
- runtime state: `ledger.db`, `state.json`, `inbox.md`,
- generated output: `journal/`, `review-queue/`, `harvest/`, `logs/`.

Before opening a PR, run `git status` and confirm none of the above are staged. The
provided `.gitignore` already excludes them; please do not weaken those rules.

## Pull requests
- Keep PRs focused and small; describe the behavior change.
- Confirm `install/install.ps1` stays idempotent (safe to run twice) and that the
  Python stays standard-library only.
