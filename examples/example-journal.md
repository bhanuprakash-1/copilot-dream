# Dream Journal - 2026-07-16

_Synthetic example journal for the fictional service `acme-api`. It illustrates the
output the nightly Dream writes to `~/.copilot/dream/journal/<date>.md`. Every name,
repo, branch, and id below is made up._

**Run:** 2026-07-16 04:15 -> 04:39 (24m) | model `gpt-5` | window 30h | harvested 42 candidates (18 sessions, 21 commits, 3 inbox notes)

## Summary
Consolidated 42 candidates: 2 durable facts applied to reference skills, 2 active
threads refreshed, 1 item queued for review, 37 dropped as one-off noise. The ledger
now tracks 128 items (61 long / 44 short / 23 applied).

## Applied changes (high-confidence, auto-applied)
- **service-architecture** - Recorded that `acme-api` loads its feature flags from the
  `acme-config` service at startup and caches them for 5 minutes, so a flag flip takes
  up to 5 minutes to take effect. (seen 3x over 3 days, importance 7)
- **deployment-runbook** - Recorded that the `acme-api` canary stage must stay green for
  15 minutes before the pipeline promotes to the broad ring; promoting earlier trips the
  automated rollback guard. (seen 4x over 3 days, importance 8)

## Active work snapshot (dream-active-work refreshed)
- **Cut p99 latency on GET /v1/orders** - repo `acme/acme-api` @ `feature/orders-cache`.
  Goal: add a read-through cache for the hot order-lookup path. Status: cache wired behind
  a flag; load test shows p99 1.8s -> 620ms; still validating cache invalidation on order
  update. Next: add the invalidation hook and soak for 24h. last_touched 2026-07-16.
- **De-flake test_order_refund_flow** - repo `acme/acme-api` @ `main`. Goal: stabilize the
  refund end-to-end test. Status: root-caused to a race between the refund worker and the
  test fixture teardown. Next: wait for worker drain before asserting. last_touched 2026-07-15.

## Review queue (needs your approval)
- `review-queue/2026-07-16-new-skill-acme-oncall.md` - proposal to split the on-call and
  incident playbooks out of `deployment-runbook` into a new `acme-oncall` skill (medium
  confidence, spans 6 items). Approve to create the skill, or reject to keep them inline.

## Dropped (audit - not written to any skill)
- One-off `500` from a scratch local environment while testing - no durable lesson. (importance 2)
- Weekend side-project ("garden-sensor" hobby repo) - off-domain for the acme skills, kept out. (importance 1)
- Machine-maintenance chatter (disk cleanup, driver update, VPN reconnect) - noise. (importance 1)
- Duplicate of an already-applied fact about canary timing (same fingerprint) - merged, not re-added.
