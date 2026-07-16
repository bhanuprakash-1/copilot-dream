#!/usr/bin/env python3
"""
Dream sharder - deterministic split of the day's harvest into balanced shards so the
consolidation can classify them with PARALLEL sub-agents (the MAP step of map-reduce).

Why this exists:
  A single agent that reads a full heavy day of sessions degrades in quality well before it
  runs out of context window. Splitting the raw material into balanced shards lets the Dream
  fan out one fresh-context classifier sub-agent per shard, then reduce their compact JSON
  outputs. This file is deterministic code (not the model) so sharding is cheap + reproducible
  and the orchestrator never has to load raw session bodies into its own context.

Design:
  - Sessions are grouped by (repository, branch) - or by cwd when those are absent - so all the
    sessions belonging to one active thread land in the SAME shard (better thread detection).
  - Groups are greedily bin-packed (largest first) into the least-loaded shard, capped at
    map_reduce.max_shards, targeting map_reduce.target_tokens per shard.
  - Git commits are compact and cross-cutting -> they get one dedicated shard.
  - Inbox notes attach to the least-loaded session shard (or their own shard if no sessions).

Outputs (under <harvest_dir>/shards/<YYYYMMDD-HHMMSS>/):
  - shard-NN.json : a mini-snapshot the MAP sub-agent reads (sessions/git/inbox subset).
  - manifest.json : the ONLY file the orchestrator reads - compact shard index (counts, ids,
                    branches, est tokens, file paths). No session bodies.

Usage:
  python shard.py --config ~/.copilot/dream/config.json
  python shard.py --config <cfg> --harvest <harvest-*.json|latest.json> --target-tokens 120000 --max-shards 6

stdlib only. Windows-friendly. Never splits a single thread across shards.
"""
import argparse, json, math, os, sys
from datetime import datetime, timezone


def expand(p):
    return os.path.expanduser(p)


def load_config(path):
    with open(expand(path), "r", encoding="utf-8") as f:
        return json.load(f)


def utcnow():
    return datetime.now(timezone.utc)


def resolve_harvest(cfg, harvest_arg):
    """Return the path to the dated harvest JSON (following latest.json if given a pointer)."""
    if harvest_arg:
        p = expand(harvest_arg)
    else:
        p = os.path.join(expand(cfg["paths"]["harvest_dir"]), "latest.json")
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    # latest.json is a pointer: {"json": "<path>", ...}. A real snapshot has "sessions".
    if isinstance(data, dict) and "json" in data and "sessions" not in data:
        real = data["json"]
        with open(real, "r", encoding="utf-8") as f:
            return real, json.load(f)
    return p, data


def est_tokens_text(s):
    return max(0, len(s or "")) // 4  # ~4 chars/token heuristic


def est_session_tokens(se):
    t = est_tokens_text(se.get("summary"))
    for tn in se.get("turns", []):
        t += est_tokens_text(tn.get("user")) + est_tokens_text(tn.get("assistant"))
    return t


def group_key(se):
    repo = (se.get("repository") or "").strip()
    branch = (se.get("branch") or "").strip()
    if repo or branch:
        return "%s@@%s" % (repo, branch)
    cwd = (se.get("cwd") or "").strip()
    return "cwd::%s" % cwd if cwd else "misc::"


def branch_label(se):
    b = (se.get("branch") or "").strip()
    r = (se.get("repository") or "").strip()
    if b:
        return b
    if r:
        return r
    return "(none)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.copilot/dream/config.json")
    ap.add_argument("--harvest", default=None, help="path to a harvest-*.json or latest.json (default: latest.json)")
    ap.add_argument("--target-tokens", type=int, default=None)
    ap.add_argument("--max-shards", type=int, default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    mr = cfg.get("map_reduce", {})
    target_tokens = args.target_tokens or mr.get("target_tokens", 120000)
    max_shards = args.max_shards or mr.get("max_shards", 6)

    src_path, snap = resolve_harvest(cfg, args.harvest)
    sessions = snap.get("sessions", []) or []
    gitc = snap.get("git", []) or []
    inbox = (snap.get("inbox") or "").strip()

    # --- group sessions by thread so a thread never splits across shards ---
    groups = {}
    for se in sessions:
        groups.setdefault(group_key(se), []).append(se)
    grouplist = []
    for k, ss in groups.items():
        grouplist.append({"key": k, "sessions": ss,
                          "tokens": sum(est_session_tokens(s) for s in ss)})
    grouplist.sort(key=lambda g: g["tokens"], reverse=True)
    total = sum(g["tokens"] for g in grouplist)

    if grouplist:
        n = max(1, min(max_shards, math.ceil(total / max(1, target_tokens)), len(grouplist)))
    else:
        n = 0
    shard_sessions = [[] for _ in range(n)]
    shard_tokens = [0] * n
    for g in grouplist:
        idx = min(range(n), key=lambda i: shard_tokens[i]) if n else 0
        shard_sessions[idx].extend(g["sessions"])
        shard_tokens[idx] += g["tokens"]

    # attach inbox to the least-loaded session shard (or its own shard if no sessions)
    inbox_shard = None
    if inbox and n:
        inbox_shard = min(range(n), key=lambda i: shard_tokens[i])

    # --- write shard files + manifest ---
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    out_dir = expand(args.out_dir) if args.out_dir else os.path.join(
        expand(cfg["paths"]["harvest_dir"]), "shards", stamp)
    os.makedirs(out_dir, exist_ok=True)

    manifest_shards = []
    sid = 0

    def write_shard(kind, sessions_subset, git_subset, inbox_text):
        nonlocal sid
        sid += 1
        name = "shard-%02d.json" % sid
        fp = os.path.join(out_dir, name)
        body = {
            "shard_id": "%02d" % sid,
            "kind": kind,
            "since_utc": snap.get("since_utc"),
            "window_hours": snap.get("window_hours"),
            "sessions": sessions_subset,
            "git": git_subset,
            "inbox": inbox_text or "",
        }
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
        est = sum(est_session_tokens(s) for s in sessions_subset) + est_tokens_text(inbox_text)
        manifest_shards.append({
            "shard_id": "%02d" % sid,
            "file": fp,
            "kind": kind,
            "n_sessions": len(sessions_subset),
            "n_commits": sum(len(g.get("commits", [])) for g in git_subset),
            "est_tokens": est,
            "session_ids": [s.get("id", "")[:8] for s in sessions_subset],
            "branches": sorted({branch_label(s) for s in sessions_subset}),
        })

    for i in range(n):
        write_shard("sessions", shard_sessions[i], [],
                    inbox if inbox_shard == i else "")
    if gitc:
        write_shard("git", [], gitc, "")
    if inbox and not n:
        write_shard("inbox", [], [], inbox)

    manifest = {
        "generated_utc": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_json": src_path,
        "target_tokens": target_tokens,
        "max_shards": max_shards,
        "shards": manifest_shards,
        "totals": {
            "sessions": len(sessions),
            "commits": sum(g.get("commit_count", len(g.get("commits", []))) for g in gitc),
            "shards": len(manifest_shards),
            "est_tokens": total,
        },
    }
    manifest_fp = os.path.join(out_dir, "manifest.json")
    with open(manifest_fp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    # stable pointer for the orchestrator
    latest_fp = os.path.join(expand(cfg["paths"]["harvest_dir"]), "shards", "latest.json")
    os.makedirs(os.path.dirname(latest_fp), exist_ok=True)
    with open(latest_fp, "w", encoding="utf-8") as f:
        json.dump({"manifest": manifest_fp, "dir": out_dir,
                   "generated_utc": manifest["generated_utc"],
                   "totals": manifest["totals"]}, f, indent=2)

    if not args.quiet:
        print("SHARD OK  shards=%d  sessions=%d  commits=%d  est_tokens=%d  target=%d/shard  max=%d"
              % (len(manifest_shards), len(sessions), manifest["totals"]["commits"],
                 total, target_tokens, max_shards))
        print("MANIFEST: %s" % manifest_fp)
        for s in manifest_shards:
            print("  shard %s [%s] sessions=%d commits=%d est_tokens=%d branches=%s"
                  % (s["shard_id"], s["kind"], s["n_sessions"], s["n_commits"],
                     s["est_tokens"], ",".join(s["branches"][:4])))


if __name__ == "__main__":
    main()
