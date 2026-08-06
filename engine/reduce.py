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

  replay --config <cfg> --in <apply-plan.json> --out <annotated-apply-plan.json>
         Annotate a preserved plan with completed APPLY buckets from receipt files. Recovery skips
         only buckets that have a receipt from that exact failed run; global ledger status is not
         used as a proxy for per-plan completion.

MAP output item shape (what each classifier sub-agent writes; same as ledger upsert):
  { "claim","domain","horizon","importance","confidence","target","source","evidence","notes" }

stdlib only. Windows-friendly.
"""
import argparse, glob as globmod, hashlib, json, os, re, subprocess, sys
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


def merge_dupes(items):
    """Merge duplicate claims (same fingerprint) deterministically, biasing disagreement toward
    review (never auto-elevate a contested claim to LONG/high)."""
    d = max(items, key=lambda x: (x.get("importance") or 0))  # base = highest-importance instance
    out = dict(d)
    out["importance"] = max((x.get("importance") or 0) for x in items)
    # confidence: most conservative (min rank) across duplicates
    ranks = [CONF_RANK.get((x.get("confidence") or "low"), 1) for x in items]
    out["confidence"] = CONF_BY_RANK[min(ranks)]
    horizons = {(x.get("horizon") or "drop") for x in items}
    if len(horizons) == 1:
        out["horizon"] = horizons.pop()
    elif horizons == {"long", "short"}:
        out["horizon"] = "short"  # do not auto-elevate to long on single-night disagreement
    else:
        # a mix involving 'drop' -> keep the strongest non-drop but force review (cap conf medium)
        non_drop = [h for h in horizons if h != "drop"]
        out["horizon"] = max(non_drop, key=lambda h: HORIZON_RANK[h]) if non_drop else "drop"
        if out["horizon"] != "drop" and CONF_RANK[out["confidence"]] > 2:
            out["confidence"] = "medium"
    # if resolved to short, home is active-work
    if out["horizon"] == "short":
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


def cmd_plan(cfg, args):
    cands = json.load(open(expand(args.candidates), encoding="utf-8"))
    tmap = build_target_map(cfg)
    long_skills = set((cfg.get("targets", {}).get("long_term_skills", {}) or {}).keys())
    # Cold-start seed: with no long-term skills configured yet, route durable facts to one auto-seeded
    # general skill (the orchestrator's bootstrap creates the file) instead of proposing every LONG claim.
    seed = cfg.get("seed", {}) or {}
    if not long_skills and seed.get("enabled", True):
        gs = seed.get("general_skill", {}) or {}
        seed_name = gs.get("name")
        if seed_name:
            long_skills = {seed_name}
            tmap[seed_name] = expand(gs.get("path") or ("~/.copilot/skills/%s/SKILL.md" % seed_name))
    keep_floor = cfg.get("thresholds", {}).get("importance_keep_floor", 4)
    # Hard user veto: fingerprints the user has explicitly rejected (via dream-reject.ps1 ->
    # ledger status='rejected') are force-dropped here, so a rejected proposal never resurfaces
    # even if its source session/commit is still in the harvest window and gets re-classified.
    rejected = {r.get("fingerprint") for r in ledger_query_strict(args.config, "dump", "--status", "rejected")
                if r.get("fingerprint")}

    plan = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by_skill": {},          # skill_name -> {skill_file, claims:[...]}
        "active_work": {"skill_file": tmap.get("dream-active-work"), "add": [], "remove_decayed": []},
        "review_queue": [],      # claims needing a proposal file (med/low LONG, new-skill, unroutable)
        "drops_count": 0,
        "rejected_denied": 0,    # candidates force-dropped because the user rejected them before
        "totals": {},
    }

    def route_to_skill(name, claim, extra=None):
        entry = plan["by_skill"].setdefault(name, {"skill_file": tmap.get(name), "claims": []})
        c = dict(claim)
        if extra:
            c.update(extra)
        entry["claims"].append(c)

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
        if horizon == "drop":
            plan["drops_count"] += 1
            continue
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
        # unknown/unresolvable target: propose as new skill if important + recurring-worthy, else queue
        item = dict(c)
        if target not in long_skills and imp >= max(7, keep_floor):
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
    }
    with open(expand(args.out), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    print("PLAN OK  " + "  ".join("%s=%s" % (k, v) for k, v in plan["totals"].items()))
    for name, v in plan["by_skill"].items():
        print("  APPLY %-32s claims=%d -> %s" % (name, len(v["claims"]), v["skill_file"]))
    print("  ACTIVE-WORK add=%d remove=%d" % (plan["totals"]["active_add"], plan["totals"]["active_remove"]))
    print("  REVIEW-QUEUE items=%d   DROPS=%d (rejected-denied=%d)"
          % (plan["totals"]["review_queue"], plan["totals"]["drops"], plan["rejected_denied"]))
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
