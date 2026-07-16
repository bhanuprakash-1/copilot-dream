#!/usr/bin/env python3
"""
Dream harvester — deterministic collection of the day's raw material.

Reads (per config.json):
  1. Copilot-CLI sessions from ~/.copilot/session-store.db updated within the harvest window.
  2. Git commits authored by the user across the configured repo roots within the window.
  3. Manual notes from inbox.md.

Writes a compact JSON snapshot + a human-readable .md digest into the harvest dir, and prints a
one-line summary. The Dream consolidation agent consumes the JSON; the agent does NOT need to
re-query anything to get the raw material (though it may, for depth).

stdlib only. Windows-friendly. Never fails the whole run on a single bad source.
"""
import argparse, glob, json, os, re, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone

def expand(p): return os.path.expanduser(p)

def load_config(path):
    with open(expand(path), "r", encoding="utf-8") as f:
        return json.load(f)

def utcnow():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def compute_since(cfg, state, override_hours=None):
    win = cfg["window"]
    default_h, max_h = win["default_hours"], win["max_hours"]
    if override_hours:
        hours = min(int(override_hours), max_h)
    else:
        last = (state or {}).get("last_run_utc")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                hours = (utcnow() - last_dt).total_seconds() / 3600.0
                hours = max(default_h, min(hours + 1, max_h))  # +1h margin
            except Exception:
                hours = default_h
        else:
            hours = default_h
    return utcnow() - timedelta(hours=hours), hours

def rows_as_dicts(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def harvest_sessions(cfg, since):
    src = cfg["sources"]["cli_sessions"]
    if not src.get("enabled"): return []
    db = expand(src["db"])
    if not os.path.exists(db): return []
    a_trunc = src.get("assistant_truncate_chars", 1600)
    u_trunc = src.get("user_truncate_chars", 4000)
    skip_empty = src.get("skip_empty_sessions", True)
    exclude_cwd = [s.lower() for s in src.get("exclude_cwd_substrings", [])]
    uri = "file:{}?mode=ro".format(db.replace("\\", "/"))
    c = sqlite3.connect(uri, uri=True)
    out = []
    since_s = iso(since)
    sess = c.execute(
        "SELECT id,cwd,repository,branch,summary,created_at,updated_at "
        "FROM sessions WHERE updated_at > ? ORDER BY updated_at DESC", (since_s,)).fetchall()
    for sid, cwd, repo, branch, summary, created, updated in sess:
        if cwd and any(sub in cwd.lower() for sub in exclude_cwd):
            continue  # self-exclude Dream's own runs
        turns = c.execute(
            "SELECT turn_index,user_message,assistant_response,timestamp "
            "FROM turns WHERE session_id=? ORDER BY turn_index", (sid,)).fetchall()
        if skip_empty and not turns:
            continue
        t_out = []
        for ti, um, ar, ts in turns:
            t_out.append({
                "i": ti,
                "user": (um or "")[:u_trunc],
                "assistant": (ar or "")[:a_trunc],
                "assistant_truncated": bool(ar and len(ar) > a_trunc),
                "ts": ts,
            })
        entry = {"id": sid, "cwd": cwd, "repository": repo, "branch": branch,
                 "summary": summary, "created_at": created, "updated_at": updated,
                 "turns": t_out}
        for aux, key in (("session_files", "files"), ("session_refs", "refs")):
            try:
                cur = c.execute("SELECT * FROM %s WHERE session_id=?" % aux, (sid,))
                entry[key] = rows_as_dicts(cur)
            except Exception:
                entry[key] = []
        out.append(entry)
    c.close()
    return out

def git(root, args):
    try:
        r = subprocess.run(["git", "-C", root] + args, capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""

def find_git_roots(cfg):
    roots = set()
    for pat in cfg["sources"]["git_commits"]["roots_glob"]:
        for p in glob.glob(pat):
            if os.path.isdir(os.path.join(p, ".git")):
                roots.add(os.path.abspath(p))
    return sorted(roots)

def harvest_git(cfg, since):
    src = cfg["sources"]["git_commits"]
    if not src.get("enabled"): return []
    emails = cfg["identity"].get("git_emails", [])
    names = cfg["identity"].get("git_names", [])
    since_s = since.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    SEP = "\x1e"; FLD = "\x1f"
    for root in find_git_roots(cfg):
        commits = []
        seen = set()
        authors = emails + names
        for who in authors:
            fmt = FLD.join(["%H", "%an", "%ae", "%cI", "%s"]) + SEP
            log = git(root, ["--no-pager", "log", "--no-merges",
                             "--author=%s" % who, "--since=%s" % since_s,
                             "--pretty=format:" + fmt, "--name-only"])
            if not log.strip():
                continue
            for block in log.split(SEP):
                block = block.strip("\n")
                if not block.strip():
                    continue
                head, _, files_blob = block.partition("\n")
                parts = head.split(FLD)
                if len(parts) < 5:
                    continue
                h, an, ae, ci, subj = parts[:5]
                if h in seen:
                    continue
                seen.add(h)
                files = [ln for ln in files_blob.splitlines() if ln.strip()]
                commits.append({"hash": h[:12], "author": an, "email": ae,
                                "date": ci, "subject": subj, "files": files[:40]})
        if commits:
            commits.sort(key=lambda x: x["date"], reverse=True)
            out.append({"repo": root, "commit_count": len(commits), "commits": commits})
    return out

def harvest_inbox(cfg):
    src = cfg["sources"]["inbox"]
    if not src.get("enabled"): return ""
    path = expand(src["path"])
    if not os.path.exists(path): return ""
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    marker = "<!-- Add notes below this line -->"
    body = txt.split(marker, 1)[1] if marker in txt else txt
    body = body.strip()
    return body

def to_md(snapshot):
    L = []
    L.append("# Dream harvest — %s" % snapshot["generated_utc"])
    L.append("")
    L.append("Window: last **%.1f h** (since %s)" % (snapshot["window_hours"], snapshot["since_utc"]))
    s = snapshot["stats"]
    L.append("Sessions: **%d** | Git repos with commits: **%d** (%d commits) | Inbox notes: **%s**"
             % (s["sessions"], s["git_repos"], s["git_commits"], "yes" if s["inbox_chars"] else "no"))
    L.append("")
    L.append("## Sessions")
    for se in snapshot["sessions"]:
        L.append("- `%s` | repo=%s branch=%s | %s | turns=%d"
                 % (se["id"][:8], se.get("repository"), se.get("branch"),
                    (se.get("summary") or "(no summary)"), len(se.get("turns", []))))
    L.append("")
    L.append("## Git commits (authored by me)")
    for g in snapshot["git"]:
        L.append("### %s (%d)" % (g["repo"], g["commit_count"]))
        for c in g["commits"][:20]:
            L.append("- `%s` %s  _(%s)_" % (c["hash"], c["subject"], c["date"][:10]))
    L.append("")
    if snapshot["inbox"]:
        L.append("## Inbox notes")
        L.append(snapshot["inbox"])
    return "\n".join(L) + "\n"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.copilot/dream/config.json")
    ap.add_argument("--hours", type=float, default=None, help="override window hours")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state_path = expand(cfg["paths"]["state_file"])
    state = {}
    if os.path.exists(state_path):
        try: state = json.load(open(state_path, encoding="utf-8"))
        except Exception: state = {}

    since, hours = compute_since(cfg, state, args.hours)
    sessions = harvest_sessions(cfg, since)
    gitc = harvest_git(cfg, since)
    inbox = harvest_inbox(cfg)

    snapshot = {
        "generated_utc": iso(utcnow()),
        "since_utc": iso(since),
        "window_hours": round(hours, 2),
        "config_path": expand(args.config),
        "identity": cfg["identity"]["alias"],
        "sessions": sessions,
        "git": gitc,
        "inbox": inbox,
        "stats": {
            "sessions": len(sessions),
            "git_repos": len(gitc),
            "git_commits": sum(g["commit_count"] for g in gitc),
            "inbox_chars": len(inbox),
        },
    }

    out_dir = expand(args.out_dir or cfg["paths"]["harvest_dir"])
    os.makedirs(out_dir, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(out_dir, "harvest-%s.json" % stamp)
    md_path = os.path.join(out_dir, "harvest-%s.md" % stamp)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(to_md(snapshot))
    # stable "latest" pointers
    latest = os.path.join(out_dir, "latest.json")
    with open(latest, "w", encoding="utf-8") as f:
        json.dump({"json": json_path, "md": md_path, "generated_utc": snapshot["generated_utc"],
                   "stats": snapshot["stats"]}, f, indent=2)

    if not args.quiet:
        s = snapshot["stats"]
        print("HARVEST OK  window=%.1fh  sessions=%d  git_repos=%d  git_commits=%d  inbox_chars=%d"
              % (hours, s["sessions"], s["git_repos"], s["git_commits"], s["inbox_chars"]))
        print("JSON: %s" % json_path)
        print("MD:   %s" % md_path)

if __name__ == "__main__":
    main()
