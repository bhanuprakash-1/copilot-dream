#!/usr/bin/env python3
"""
Dream reducer - the REDUCE step of map-reduce. Turns the parallel classifier sub-agents' compact
JSON outputs into (a) one deduped candidate list for the ledger, and (b) an apply-plan grouped by
target skill so the orchestrator can fan out one fresh-context APPLY sub-agent per skill.

Why this exists:
  Merging, fingerprint-dedup, and routing are deterministic bookkeeping - doing them in code keeps
  the orchestrator's context tiny (it reads a compact plan, never raw session bodies or skill
  bodies) and makes the pipeline reproducible.

Subcommands:
  merge  --config <cfg> --in <dir-with-claims-*.json | glob> --out <candidates.json>
         Concatenate every MAP output (claims-*.json), dedup by fingerprint (conservatively
         resolving any cross-shard disagreement toward human review), write candidates.json.

  plan   --config <cfg> --candidates <candidates.json> --out <apply-plan.json>
         Query the ledger for promotions + decays (run AFTER `ledger.py upsert candidates.json`),
         then route every candidate to: a per-skill APPLY bucket (LONG + high-confidence), the
         active-work bucket (SHORT), the review-queue (LONG + med/low, or new-skill), or drop.
         `scope` decides the home first: `feature` claims are in-flight status and only ever reach
         active-work; `topic` / `cross-cutting` learnings never do, even if a classifier marked them
         short. Each bucket carries its skill's current size and budget so appliers avoid bloat.

  replay --config <cfg> --in <apply-plan.json> --out <annotated-apply-plan.json>
         Annotate a preserved plan with completed APPLY buckets from receipt files. Recovery skips
         only buckets that have a receipt from that exact failed run; global ledger status is not
         used as a proxy for per-plan completion.

MAP output item shape (what each classifier sub-agent writes; same as ledger upsert):
  { "claim","domain","scope","thread","signal","horizon","importance","confidence","target",
    "source","evidence","notes" }

stdlib only. Windows-friendly.
"""
import argparse, glob as globmod, hashlib, json, os, re, subprocess, sys
from collections import Counter
from datetime import datetime, timezone


def expand(p):
    return os.path.expanduser(p)


def load_config(path):
    with open(expand(path), "r", encoding="utf-8") as f:
        return json.load(f)


def fingerprint(claim):
    norm = re.sub(r"\s+", " ", (claim or "").strip().lower())
    norm = re.sub(r"[^a-z0-9 ]", "", norm)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


CONF_RANK = {"high": 3, "medium": 2, "low": 1}
CONF_BY_RANK = {3: "high", 2: "medium", 1: "low"}
HORIZON_RANK = {"drop": 0, "short": 1, "long": 2}
DURABLE_SCOPES = ("topic", "cross-cutting")
SCOPE_ALIASES = {"feature": "feature", "thread": "feature", "in-flight": "feature",
                 "topic": "topic", "system": "topic", "component": "topic",
                 "cross-cutting": "cross-cutting", "crosscutting": "cross-cutting",
                 "general": "cross-cutting", "workflow": "cross-cutting"}


def normalize_scope(value):
    key = re.sub(r"[\s_]+", "-", (value or "").strip().lower())
    return SCOPE_ALIASES.get(key)


def cap_confidence(item, ceiling="medium"):
    if CONF_RANK.get(item.get("confidence") or "low", 1) > CONF_RANK[ceiling]:
        item["confidence"] = ceiling


def merge_dupes(items):
    """Merge duplicate claims (same fingerprint) deterministically, biasing disagreement toward
    review (never auto-elevate a contested claim to LONG/high)."""
    d = max(items, key=lambda x: (x.get("importance") or 0))  # base = highest-importance instance
    out = dict(d)
    out["importance"] = max((x.get("importance") or 0) for x in items)
    # confidence: most conservative (min rank) across duplicates
    ranks = [CONF_RANK.get((x.get("confidence") or "low"), 1) for x in items]
    out["confidence"] = CONF_BY_RANK[min(ranks)]
    scopes = {normalize_scope(x.get("scope")) for x in items} - {None}
    if len(scopes) == 1:
        out["scope"] = scopes.pop()
    elif scopes:
        # Shards disagree on whether this is in-flight status or durable knowledge: keep the durable
        # reading so it cannot hide in active-work, but leave the final call to review.
        out["scope"] = "topic" if "topic" in scopes else "cross-cutting"
        cap_confidence(out)
    horizons = {(x.get("horizon") or "drop") for x in items}
    if len(horizons) == 1:
        out["horizon"] = horizons.pop()
    elif horizons == {"long", "short"}:
        if out.get("scope") in DURABLE_SCOPES:
            out["horizon"] = "long"
            cap_confidence(out)
        else:
            out["horizon"] = "short"  # do not auto-elevate to long on single-night disagreement
    else:
        # a mix involving 'drop' -> keep the strongest non-drop but force review (cap conf medium)
        non_drop = [h for h in horizons if h != "drop"]
        out["horizon"] = max(non_drop, key=lambda h: HORIZON_RANK[h]) if non_drop else "drop"
        if out["horizon"] != "drop":
            cap_confidence(out)
    # an in-flight claim lives in active-work; a durable one keeps its reference-skill target
    if out["horizon"] == "short" and out.get("scope") not in DURABLE_SCOPES:
        out["target"] = "dream-active-work"
    ev = sorted({(x.get("evidence") or "").strip() for x in items if x.get("evidence")})
    out["evidence"] = "; ".join(ev)[:400]
    srcs = {(x.get("source") or "") for x in items if x.get("source")}
    out["source"] = "mixed" if len(srcs) > 1 else (srcs.pop() if srcs else None)
    out["fingerprint"] = d.get("fingerprint") or fingerprint(d.get("claim", ""))
    return out


def cmd_merge(cfg, args):
    paths = []
    if os.path.isdir(expand(args.inp)):
        paths = sorted(globmod.glob(os.path.join(expand(args.inp), "claims-*.json")))
    else:
        paths = sorted(globmod.glob(expand(args.inp)))
    raw = []
    for p in paths:
        try:
            data = json.load(open(p, encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("claims", [data])
            for it in data:
                if it.get("claim", "").strip():
                    it["fingerprint"] = it.get("fingerprint") or fingerprint(it["claim"])
                    raw.append(it)
        except Exception as e:
            sys.stderr.write("WARN could not read %s: %s\n" % (p, e))
    by_fp = {}
    for it in raw:
        by_fp.setdefault(it["fingerprint"], []).append(it)
    merged = [merge_dupes(v) if len(v) > 1 else v[0] for v in by_fp.values()]
    merged.sort(key=lambda x: (x.get("importance") or 0), reverse=True)
    with open(expand(args.out), "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print("MERGE OK  files=%d  raw_claims=%d  unique=%d  -> %s"
          % (len(paths), len(raw), len(merged), expand(args.out)))


def ledger_query(cfg_path, *sub):
    """Shell out to ledger.py; return parsed JSON (or []). Pass a subcommand plus any args, e.g.
    ledger_query(cfg, "promotions") or ledger_query(cfg, "dump", "--status", "rejected")."""
    ledger = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([sys.executable, ledger, "--config", cfg_path, *sub],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        sys.stderr.write("WARN ledger %s failed: %s\n" % (" ".join(sub), e))
    return []


def ledger_query_strict(cfg_path, *sub):
    """Shell out to ledger.py and fail if a safety-critical query cannot be completed."""
    ledger = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, ledger, "--config", cfg_path, *sub],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("ledger %s failed (exit %s): %s"
                           % (" ".join(sub), r.returncode, r.stderr.strip()))
    return json.loads(r.stdout) if r.stdout.strip() else []


def build_target_map(cfg):
    t = cfg.get("targets", {})
    m = {}
    for name, path in (t.get("long_term_skills", {}) or {}).items():
        m[name] = expand(path)
    if t.get("short_term_skill"):
        m["dream-active-work"] = expand(t["short_term_skill"])
    if t.get("index_skill"):
        m["dream"] = expand(t["index_skill"])
    return m


def skill_on_disk(cfg, name):
    """Path of an installed skill that is not configured as a routing target, or None."""
    if not name or not re.match(r"^[\w.-]+$", name):
        return None
    skills_dir = expand((cfg.get("paths", {}) or {}).get("skills_dir", "~/.copilot/skills"))
    path = os.path.join(skills_dir, name, "SKILL.md")
    return path if os.path.isfile(path) else None


def text_chars(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return len(f.read())
    except (OSError, TypeError):
        return 0


def cmd_plan(cfg, args):
    cands = json.load(open(expand(args.candidates), encoding="utf-8"))
    tmap = build_target_map(cfg)
    targets_cfg = cfg.get("targets", {}) or {}
    long_skills = set((targets_cfg.get("long_term_skills", {}) or {}).keys())
    # Cold-start seed: with no long-term skills configured yet, route durable facts to one auto-seeded
    # general skill (the orchestrator's bootstrap creates the file) instead of proposing every LONG claim.
    seed = cfg.get("seed", {}) or {}
    seed_name = None
    if not long_skills and seed.get("enabled", True):
        gs = seed.get("general_skill", {}) or {}
        seed_name = gs.get("name")
        if seed_name:
            long_skills = {seed_name}
            tmap[seed_name] = expand(gs.get("path") or ("~/.copilot/skills/%s/SKILL.md" % seed_name))
    # Home for durable learnings that are not about one system (tools, workflows, preferences).
    general_skill = targets_cfg.get("general_skill") or seed_name
    if general_skill not in long_skills:
        general_skill = None
    th = cfg.get("thresholds", {}) or {}
    watched = set(targets_cfg.get("watched_skills") or [])
    keep_floor = th.get("importance_keep_floor", 4)
    skill_budget = int(th.get("skill_budget_chars", 60000))
    budget_overrides = th.get("skill_budget_overrides", {}) or {}
    archive_dir = expand((cfg.get("paths", {}) or {}).get("archive_dir", "~/.copilot/dream/archive"))
    today = datetime.now().strftime("%Y-%m-%d")
    # Hard user veto: fingerprints the user has explicitly rejected (via dream-reject.ps1 ->
    # ledger status='rejected') are force-dropped here, so a rejected proposal never resurfaces
    # even if its source session/commit is still in the harvest window and gets re-classified.
    rejected = {r.get("fingerprint") for r in ledger_query_strict(args.config, "dump", "--status", "rejected")
                if r.get("fingerprint")}

    aw_file = tmap.get("dream-active-work")
    aw_chars = text_chars(aw_file)
    aw_budget = int(th.get("active_work_budget_chars", 20000))
    plan = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by_skill": {},          # skill_name -> {skill_file, claims:[...], size/budget}
        "active_work": {"skill_file": aw_file, "add": [], "remove_decayed": [],
                        "current_chars": aw_chars, "budget_chars": aw_budget,
                        "max_threads": int(th.get("active_work_max_threads", 20)),
                        "over_budget": aw_chars > aw_budget,
                        "archive_file": os.path.join(archive_dir, "active-work-%s.md" % today)},
        "review_queue": [],      # claims needing a proposal file (med/low LONG, new-skill, unroutable)
        "drops_count": 0,
        "rejected_denied": 0,    # candidates force-dropped because the user rejected them before
        "rerouted_to_reference": 0,  # durable learnings a classifier had aimed at active-work
        "totals": {},
    }

    def route_to_skill(name, claim, extra=None):
        if name not in plan["by_skill"]:
            chars = text_chars(tmap.get(name))
            budget = int(budget_overrides.get(name, skill_budget))
            plan["by_skill"][name] = {"skill_file": tmap.get(name), "claims": [],
                                      "current_chars": chars, "budget_chars": budget,
                                      "over_budget": chars > budget, "watched": name in watched}
        c = dict(claim)
        if extra:
            c.update(extra)
        plan["by_skill"][name]["claims"].append(c)

    for c in cands:
        fp = c.get("fingerprint")
        if fp and fp in rejected:
            plan["drops_count"] += 1
            plan["rejected_denied"] += 1
            continue
        horizon = c.get("horizon") or "drop"
        conf = c.get("confidence") or "low"
        target = c.get("target") or ""
        imp = c.get("importance") or 0
        scope = normalize_scope(c.get("scope"))
        if horizon == "drop":
            plan["drops_count"] += 1
            continue
        c = dict(c)
        if scope:
            c["scope"] = scope
        if scope == "feature" and horizon == "long":
            # in-flight specifics never enter a reference skill
            horizon = c["horizon"] = "short"
        elif scope in DURABLE_SCOPES and (horizon == "short" or target == "dream-active-work"):
            # a learning that outlives the feature belongs in a reference skill, not active-work
            horizon = c["horizon"] = "long"
            if target in ("", "dream-active-work"):
                if scope == "cross-cutting" and general_skill:
                    target = general_skill
                else:
                    target = "review-queue"
                    c["needs_target"] = True
                c["target"] = target
            c["rerouted_from_active_work"] = True
            plan["rerouted_to_reference"] += 1
        if horizon == "short" or target == "dream-active-work":
            plan["active_work"]["add"].append(c)
            continue
        if target == "review-queue":
            plan["review_queue"].append(c)
            continue
        if horizon == "long" and conf == "high" and target in long_skills:
            route_to_skill(target, c)
            continue
        if horizon == "long" and conf in ("medium", "low") and target in long_skills:
            plan["review_queue"].append(c)
            continue
        # target is not a configured routing target: an installed skill gets an ordinary proposal;
        # an unknown one becomes a new-skill proposal if important enough, else a plain proposal
        item = dict(c)
        existing = skill_on_disk(cfg, target)
        if existing:
            item["existing_skill_file"] = existing
        elif imp >= max(7, keep_floor):
            item["new_skill"] = True
        plan["review_queue"].append(item)

    # promotions: recurring SHORT items that have earned LONG status
    for p in ledger_query(args.config, "promotions"):
        if p.get("fingerprint") in rejected:
            continue  # user vetoed this claim; never promote it
        tgt = p.get("target") or ""
        pc = {"claim": p.get("claim"), "domain": p.get("domain"),
              "horizon": "long", "confidence": "high", "importance": p.get("importance") or 6,
              "target": tgt, "fingerprint": p.get("fingerprint"),
              "source": "ledger-promotion", "evidence": "promoted (hit=%s days=%s)"
              % (p.get("hit_count"), p.get("distinct_days"))}
        if tgt in long_skills:
            route_to_skill(tgt, pc, {"promoted": True})
        else:
            pc["promoted"] = True
            if tgt in ("", "dream-active-work"):
                pc["needs_target"] = True
            plan["review_queue"].append(pc)

    # decays: stale active SHORT threads to remove from active-work
    for dcy in ledger_query(args.config, "decays"):
        plan["active_work"]["remove_decayed"].append(
            {"fingerprint": dcy.get("fingerprint"), "claim": dcy.get("claim")})

    plan["totals"] = {
        "candidates": len(cands),
        "skills_to_edit": len(plan["by_skill"]),
        "apply_claims": sum(len(v["claims"]) for v in plan["by_skill"].values()),
        "active_add": len(plan["active_work"]["add"]),
        "active_remove": len(plan["active_work"]["remove_decayed"]),
        "review_queue": len(plan["review_queue"]),
        "drops": plan["drops_count"],
        "rejected_denied": plan["rejected_denied"],
        "rerouted_to_reference": plan["rerouted_to_reference"],
        "over_budget_skills": sorted(n for n, v in plan["by_skill"].items() if v.get("over_budget")),
        # what the conversations yielded (question / correction / preference / decision / finding / status)
        "signals": dict(Counter((c.get("signal") or "unspecified") for c in cands)),
    }
    with open(expand(args.out), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    print("PLAN OK  " + "  ".join("%s=%s" % (k, v) for k, v in plan["totals"].items()
                                  if not isinstance(v, (dict, list))))
    for name, v in plan["by_skill"].items():
        print("  APPLY %-32s claims=%d size=%d/%d%s%s -> %s"
              % (name, len(v["claims"]), v["current_chars"], v["budget_chars"],
                 " OVER-BUDGET" if v["over_budget"] else "", " WATCHED" if v.get("watched") else "",
                 v["skill_file"]))
    aw = plan["active_work"]
    print("  ACTIVE-WORK add=%d remove=%d size=%d/%d%s"
          % (plan["totals"]["active_add"], plan["totals"]["active_remove"], aw["current_chars"],
             aw["budget_chars"], " OVER-BUDGET" if aw["over_budget"] else ""))
    print("  REVIEW-QUEUE items=%d   DROPS=%d (rejected-denied=%d)   REROUTED-TO-REFERENCE=%d"
          % (plan["totals"]["review_queue"], plan["totals"]["drops"], plan["rejected_denied"],
             plan["rerouted_to_reference"]))
    print("  SIGNALS %s" % json.dumps(plan["totals"]["signals"], sort_keys=True))
    print("PLAN FILE: %s" % expand(args.out))


def cmd_replay(cfg, args):
    plan = json.load(open(expand(args.inp), encoding="utf-8-sig"))
    rejected = {
        r.get("fingerprint")
        for r in ledger_query_strict(args.config, "dump", "--status", "rejected")
        if r.get("fingerprint")
    }
    replayable_fps = {
        c.get("fingerprint")
        for entry in plan.get("by_skill", {}).values()
        for c in entry.get("claims", [])
        if c.get("fingerprint")
    }
    replayable_fps.update(
        c.get("fingerprint")
        for c in plan.get("active_work", {}).get("add", [])
        if c.get("fingerprint")
    )
    replayable_fps.update(
        c.get("fingerprint")
        for c in plan.get("review_queue", [])
        if c.get("fingerprint")
    )
    rejected_denied = len(replayable_fps.intersection(rejected))

    def not_rejected(item):
        return item.get("fingerprint") not in rejected

    for entry in plan.get("by_skill", {}).values():
        entry["claims"] = [c for c in entry.get("claims", []) if not_rejected(c)]
    plan["by_skill"] = {
        name: entry for name, entry in plan.get("by_skill", {}).items()
        if entry.get("claims")
    }
    active = plan.setdefault("active_work", {"skill_file": None, "add": [], "remove_decayed": []})
    active["add"] = [c for c in active.get("add", []) if not_rejected(c)]
    plan["review_queue"] = [c for c in plan.get("review_queue", []) if not_rejected(c)]

    receipts_dir = expand(args.receipts) if args.receipts else None
    completed = set()
    completed_skills = {}
    completed_active = set()
    completed_review = set()
    receipt_files = []
    receipt_warnings = []
    if receipts_dir and os.path.isdir(receipts_dir):
        receipt_files = sorted(globmod.glob(os.path.join(receipts_dir, "*.json")))
    for path in receipt_files:
        try:
            receipt = json.load(open(path, encoding="utf-8-sig"))
        except Exception as e:
            receipt_warnings.append("%s: %s" % (path, e))
            continue
        if receipt.get("status") == "complete":
            fps = {fp for fp in receipt.get("fingerprints", []) if fp}
            completed.update(fps)
            bucket = receipt.get("bucket")
            if bucket == "skill" and receipt.get("name"):
                completed_skills.setdefault(receipt["name"], set()).update(fps)
            elif bucket == "active-work":
                completed_active.update(fps)
            elif bucket == "review-queue":
                completed_review.update(fps)

    completed_buckets = 0
    for name, entry in plan.get("by_skill", {}).items():
        fps = {c.get("fingerprint") for c in entry.get("claims", []) if c.get("fingerprint")}
        entry["receipt_complete"] = bool(fps) and fps.issubset(completed_skills.get(name, set()))
        if entry["receipt_complete"]:
            completed_buckets += 1
    active_fps = {
        c.get("fingerprint")
        for c in active.get("add", []) + active.get("remove_decayed", [])
        if c.get("fingerprint")
    }
    active["receipt_complete"] = bool(active_fps) and active_fps.issubset(completed_active)
    if active["receipt_complete"]:
        completed_buckets += 1
    review_fps = {
        c.get("fingerprint") for c in plan.get("review_queue", []) if c.get("fingerprint")
    }
    plan["review_queue_receipt_complete"] = bool(review_fps) and review_fps.issubset(completed_review)
    if plan["review_queue_receipt_complete"]:
        completed_buckets += 1

    plan["replay"] = {
        "source_plan": os.path.abspath(expand(args.inp)),
        "filtered_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "receipts_dir": os.path.abspath(receipts_dir) if receipts_dir else None,
        "receipt_files": len(receipt_files),
        "receipt_warnings": receipt_warnings,
        "completed_fingerprints": sorted(completed),
        "completed_buckets": completed_buckets,
        "rejected_denied": rejected_denied,
    }
    totals = plan.setdefault("totals", {})
    totals.update({
        "skills_to_edit": len(plan.get("by_skill", {})),
        "apply_claims": sum(len(v.get("claims", [])) for v in plan.get("by_skill", {}).values()),
        "active_add": len(active.get("add", [])),
        "active_remove": len(active.get("remove_decayed", [])),
        "review_queue": len(plan.get("review_queue", [])),
        "replay_completed_buckets": completed_buckets,
        "replay_completed_fingerprints": len(completed),
        "replay_rejected_denied": rejected_denied,
    })

    with open(expand(args.out), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    print("REPLAY OK  apply=%d  active_add=%d  active_remove=%d  review=%d  completed_buckets=%d  -> %s"
          % (totals["apply_claims"], totals["active_add"], totals["active_remove"],
             totals["review_queue"], totals["replay_completed_buckets"], expand(args.out)))
    for warning in receipt_warnings:
        print("REPLAY WARN malformed receipt ignored: %s" % warning)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.copilot/dream/config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--in", dest="inp", required=True)
    m.add_argument("--out", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--candidates", required=True)
    p.add_argument("--out", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--in", dest="inp", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--receipts")
    args = ap.parse_args()
    cfg = load_config(args.config)
    {"merge": cmd_merge, "plan": cmd_plan, "replay": cmd_replay}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
