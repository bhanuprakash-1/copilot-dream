#!/usr/bin/env python3
"""
Dream harvester — deterministic collection of the day's raw material.

Reads (per config.json):
  1. Copilot-CLI sessions updated within the harvest window. Each session's main-thread
     conversation is rebuilt from its event log (~/.copilot/session-state/<id>/events.jsonl):
     every user prompt, the agent's commentary and final answers (with the tools used in each
     turn), compaction summaries, delegated sub-agent tasks and the skills that were loaded.
     Sub-agent internals, injected skill bodies, system notifications and bare slash commands are
     excluded. Only material newer than the window start is harvested, so a long-running session
     is not re-classified in full every night; a short prior-context note keeps it intelligible.
     Sessions without an event log fall back to the session-store `turns` table.
  2. Git commits authored by the user across the configured repo roots within the window.
  3. Manual notes from inbox.md.

Writes a compact JSON snapshot + a human-readable .md digest into the harvest dir, and prints a
one-line summary. The Dream consolidation agent consumes the JSON; the agent does NOT need to
re-query anything to get the raw material.

stdlib only. Windows-friendly. Never fails the whole run on a single bad source.
"""
import argparse, fnmatch, glob, json, os, re, sqlite3, subprocess, sys
from collections import Counter
from datetime import datetime, timedelta, timezone

def expand(p): return os.path.expanduser(p)

def load_config(path):
    with open(expand(path), "r", encoding="utf-8") as f:
        return json.load(f)

def utcnow():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def compute_since(cfg, state, cutoff, override_hours=None):
    """Window start. After a successful run the window resumes at that run's harvest cutoff minus a
    small overlap (events can reach disk shortly after their timestamp); default_hours applies only
    to a first run, and max_hours bounds the catch-up after an outage."""
    win = cfg["window"]
    default_h, max_h = float(win["default_hours"]), float(win["max_hours"])
    overlap_h = float(win.get("overlap_minutes", 15)) / 60.0
    if override_hours:
        hours = min(float(override_hours), max_h)
    else:
        hours = default_h
        last = (state or {}).get("last_run_utc")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                hours = max(0.0, min((cutoff - last_dt).total_seconds() / 3600.0 + overlap_h, max_h))
            except ValueError:
                hours = default_h
    return cutoff - timedelta(hours=hours), hours

def rows_as_dicts(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

EVENT_TYPES = ('"type":"user.message"', '"type":"assistant.message"',
               '"type":"session.compaction_complete"', '"type":"skill.invoked"')
# user.message sources the CLI generates itself (sub-agent traffic, scheduled ticks, injected skill,
# retrieval and instruction context); prompts the user typed carry no source or source "user".
SYNTHETIC_SOURCES = ("agent-", "system", "schedule-", "skill-", "embedding-", "instruction-")
# Text the CLI records in the user role although the user did not type it.
NOISE_PREFIXES = ("<skill-context", "<system_notification", "<system-notification", "<system_reminder",
                  "<available_skills>", "[Scheduled prompt",
                  "The main agent received the following user request")
SLASH_COMMAND = re.compile(r"^/[\w:.-]+$")
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}"), "[REDACTED TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED JWT]"),
    (re.compile(r"(?i)\b(AccountKey|SharedAccessKey|client_secret|password|pwd)(\s*[=:]\s*[\"']?)[^\s;,\"']{8,}"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)([?&]sig=)[A-Za-z0-9%/+=]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "Bearer [REDACTED]"),
]

def redact(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

def clip(text, limit):
    """Trim to about `limit` chars keeping head and tail (answers often conclude at the end)."""
    text = (text or "").strip()
    if not limit or len(text) <= limit:
        return text, False
    head = int(limit * 0.7)
    tail = limit - head
    return ("%s\n...[%d chars elided]...\n%s"
            % (text[:head].rstrip(), len(text) - head - tail, text[-tail:].lstrip())), True

def dialogue_caps(src):
    return {
        "user": int(src.get("user_message_max_chars", 6000)),
        "final": int(src.get("assistant_final_max_chars", 8000)),
        "interim": int(src.get("assistant_interim_max_chars", 600)),
        "summary": int(src.get("summary_max_chars", 8000)),
        "prior": int(src.get("prior_context_max_chars", 3000)),
        "budget": int(src.get("session_budget_chars", 80000)),
    }

def new_conversation():
    return {"entries": [], "prior_summary": None, "first_user": None, "first_ts": None,
            "skills": [], "delegations": [], "tools": Counter()}

def conversation_from_events(path, since_s):
    """Rebuild one session's main-thread conversation from its event log.

    Sub-agent traffic (events carrying agentId or parentToolCallId, and CLI-generated prompts such as
    agent messages or scheduled ticks) is skipped. Each assistant text is `final` (the answer that closed
    a user turn) or `interim` (commentary between tool calls); the CLI's phase marker decides when present.
    """
    conv = new_conversation()
    entries = conv["entries"]
    turn = {"texts": [], "tools": Counter()}

    def close_turn():
        texts = turn["texts"]
        if texts:
            if not any(entries[i]["kind"] == "final" for i in texts):
                entries[texts[-1]]["kind"] = "final"
            if turn["tools"]:
                last_final = [i for i in texts if entries[i]["kind"] == "final"][-1]
                entries[last_final]["tools"] = dict(turn["tools"].most_common(8))
        turn["texts"], turn["tools"] = [], Counter()

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            head = line[:96]
            if not any(t in head for t in EVENT_TYPES):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("agentId"):
                continue
            kind, ts, d = ev.get("type"), ev.get("timestamp") or "", ev.get("data") or {}
            if conv["first_ts"] is None and ts:
                conv["first_ts"] = ts
            in_window = ts >= since_s
            if kind == "user.message":
                text = (d.get("content") or "").strip()
                source = str(d.get("source") or "")
                if source.startswith(SYNTHETIC_SOURCES) or not text or text.startswith(NOISE_PREFIXES) \
                        or SLASH_COMMAND.match(text):
                    continue
                close_turn()
                if conv["first_user"] is None:
                    conv["first_user"] = text
                entries.append({"role": "user", "ts": ts, "text": text})
            elif kind == "assistant.message":
                if d.get("parentToolCallId"):
                    continue
                for req in d.get("toolRequests") or []:
                    if not in_window:
                        break
                    name = req.get("name") or "?"
                    args = req.get("arguments") if isinstance(req.get("arguments"), dict) else {}
                    turn["tools"][name] += 1
                    conv["tools"][name] += 1
                    if name == "task":
                        label = " - ".join(str(args[k]) for k in ("name", "description") if args.get(k))
                        if label:
                            conv["delegations"].append(label[:160])
                    elif name == "skill" and args.get("skill"):
                        conv["skills"].append(str(args["skill"]))
                text = (d.get("content") or "").strip()
                if text:
                    entries.append({"role": "assistant", "ts": ts,
                                    "kind": "final" if d.get("phase") == "final_answer" else "interim",
                                    "text": text})
                    turn["texts"].append(len(entries) - 1)
            elif kind == "session.compaction_complete":
                text = (d.get("summaryContent") or "").strip()
                if text and in_window:
                    entries.append({"role": "summary", "ts": ts, "text": text})
                elif text:
                    conv["prior_summary"] = text
            elif kind == "skill.invoked" and in_window and d.get("name"):
                conv["skills"].append(str(d["name"]))
    close_turn()
    return conv

def conversation_from_turns(c, sid, since_s):
    """Fallback for sessions without an event log: session-store turns + checkpoint overviews."""
    conv = new_conversation()
    rows = c.execute("SELECT user_message, assistant_response, timestamp FROM turns "
                     "WHERE session_id=? ORDER BY turn_index", (sid,)).fetchall()
    for um, ar, ts in rows:
        ts = ts or ""
        if conv["first_ts"] is None and ts:
            conv["first_ts"] = ts
        um, ar = (um or "").strip(), (ar or "").strip()
        user_ok = bool(um) and not um.startswith(NOISE_PREFIXES) and not SLASH_COMMAND.match(um)
        if user_ok and conv["first_user"] is None:
            conv["first_user"] = um
        if user_ok:
            conv["entries"].append({"role": "user", "ts": ts, "text": um})
        if ar:
            conv["entries"].append({"role": "assistant", "ts": ts, "kind": "final", "text": ar})
    try:
        for title, overview, created in c.execute(
                "SELECT title, overview, created_at FROM checkpoints WHERE session_id=? "
                "ORDER BY checkpoint_number", (sid,)):
            text = "\n".join(x for x in (title, overview) if x).strip()
            if not text:
                continue
            if (created or "") >= since_s:
                conv["entries"].append({"role": "summary", "ts": created, "text": text})
            else:
                conv["prior_summary"] = text
    except sqlite3.Error:
        pass
    conv["entries"].sort(key=lambda e: e["ts"])
    return conv

def enforce_budget(dialogue, budget):
    """Fit one session's dialogue into its char budget: shrink then drop commentary, then shorten
    answers and summaries, then long prompts. Returns (dialogue, dropped_commentary_count)."""
    def total():
        return sum(len(e["text"]) for e in dialogue)
    if budget <= 0 or total() <= budget:
        return dialogue, 0
    for e in dialogue:
        if e.get("kind") == "interim":
            e["text"], cut = clip(e["text"], 240)
            e["truncated"] = e.get("truncated", False) or cut
    dropped = 0
    if total() > budget:
        kept = [e for e in dialogue if e.get("kind") != "interim"]
        dropped = len(dialogue) - len(kept)
        dialogue = kept
    for floor, roles in ((1500, ("assistant", "summary")), (800, ("user",))):
        over = total() - budget
        if over <= 0:
            break
        pool = [e for e in dialogue if e["role"] in roles and len(e["text"]) > floor]
        size = sum(len(e["text"]) for e in pool)
        if not size:
            continue
        ratio = max(0.0, 1.0 - over / float(size))
        for e in pool:
            e["text"], cut = clip(e["text"], max(floor, int(len(e["text"]) * ratio)))
            e["truncated"] = e.get("truncated", False) or cut
    return dialogue, dropped

def finish_dialogue(conv, since_s, caps):
    """Window-filter, redact, cap and budget one session's conversation."""
    dialogue = []
    for e in conv["entries"]:
        if e["ts"] < since_s:
            continue
        if e["role"] == "user":
            limit = caps["user"]
        elif e["role"] == "summary":
            limit = caps["summary"]
        else:
            limit = caps["final"] if e["kind"] == "final" else caps["interim"]
        item = dict(e)
        item["text"], cut = clip(redact(e["text"]), limit)
        if cut:
            item["truncated"] = True
        dialogue.append(item)
    dialogue, dropped = enforce_budget(dialogue, caps["budget"])
    prior = None
    if dialogue and conv["first_ts"] and conv["first_ts"] < since_s:
        if conv["prior_summary"]:
            prior = clip(redact(conv["prior_summary"]), caps["prior"])[0]
        elif conv["first_user"]:
            prior = "Session opened with: " + clip(redact(conv["first_user"]), min(1500, caps["prior"]))[0]
    return dialogue, prior, dropped

def harvest_sessions(cfg, since):
    src = cfg["sources"]["cli_sessions"]
    if not src.get("enabled"): return []
    db = expand(src["db"])
    if not os.path.exists(db): return []
    caps = dialogue_caps(src)
    skip_empty = src.get("skip_empty_sessions", True)
    use_events = src.get("use_event_log", True)
    state_dir = expand(src.get("session_state_dir", "~/.copilot/session-state"))
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
        conv, source = None, "turns"
        events_path = os.path.join(state_dir, sid, "events.jsonl")
        if use_events and os.path.isfile(events_path):
            try:
                conv, source = conversation_from_events(events_path, since_s), "events"
            except OSError as e:
                sys.stderr.write("WARN could not read %s: %s\n" % (events_path, e))
        if conv is None:
            conv = conversation_from_turns(c, sid, since_s)
        dialogue, prior, dropped = finish_dialogue(conv, since_s, caps)
        if skip_empty and not dialogue:
            continue
        kinds = Counter(e.get("kind") or e["role"] for e in dialogue)
        entry = {"id": sid, "cwd": cwd, "repository": repo, "branch": branch,
                 "summary": summary, "created_at": created, "updated_at": updated,
                 "conversation_source": source,
                 "counts": {"user": kinds.get("user", 0), "final": kinds.get("final", 0),
                            "interim": kinds.get("interim", 0), "summary": kinds.get("summary", 0),
                            "interim_dropped": dropped},
                 "skills_used": sorted(set(conv["skills"])),
                 "delegations": conv["delegations"][:20],
                 "tools": dict(conv["tools"].most_common(12)),
                 "prior_context": prior,
                 "dialogue": dialogue}
        for aux, key, tcol in (("session_files", "files", "first_seen_at"),
                               ("session_refs", "refs", "created_at")):
            try:
                rows = rows_as_dicts(c.execute("SELECT * FROM %s WHERE session_id=?" % aux, (sid,)))
                entry[key] = [r for r in rows if (r.get(tcol) or since_s) >= since_s][:60]
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
    gc = cfg["sources"]["git_commits"]
    excl = gc.get("roots_exclude", [])
    def excluded(ap):
        return any(fnmatch.fnmatch(ap, e) or e.lower() in ap.lower() for e in excl)
    roots = set()
    for pat in gc["roots_glob"]:
        for p in glob.glob(pat):
            if not os.path.isdir(os.path.join(p, ".git")):
                continue
            ap = os.path.abspath(p)
            if excluded(ap):
                continue
            roots.add(ap)
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
    L.append("Sessions: **%d** (%d prompts, %d answers, %d summaries; %d from event logs) | "
             "Git repos with commits: **%d** (%d commits) | Inbox notes: **%s**"
             % (s["sessions"], s["user_prompts"], s["agent_answers"], s["summaries"],
                s["event_log_sessions"], s["git_repos"], s["git_commits"],
                "yes" if s["inbox_chars"] else "no"))
    L.append("")
    L.append("## Sessions")
    for se in snapshot["sessions"]:
        cnt = se.get("counts", {})
        skills = se.get("skills_used") or []
        L.append("- `%s` | repo=%s branch=%s | %s | prompts=%d answers=%d summaries=%d via %s%s"
                 % (se["id"][:8], se.get("repository"), se.get("branch"),
                    (se.get("summary") or "(no summary)"), cnt.get("user", 0), cnt.get("final", 0),
                    cnt.get("summary", 0), se.get("conversation_source"),
                    (" | skills: " + ", ".join(skills[:6])) if skills else ""))
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
        try:
            state = json.load(open(state_path, encoding="utf-8-sig"))
        except Exception as e:
            raise RuntimeError("could not read Dream watermark %s: %s" % (state_path, e))

    # Taken before any source is read so it can serve as the next watermark without a gap.
    cutoff = utcnow()
    since, hours = compute_since(cfg, state, cutoff, args.hours)
    sessions = harvest_sessions(cfg, since)
    gitc = harvest_git(cfg, since)
    inbox = harvest_inbox(cfg)

    snapshot = {
        "generated_utc": iso(utcnow()),
        "cutoff_utc": iso(cutoff),
        "since_utc": iso(since),
        "window_hours": round(hours, 2),
        "config_path": expand(args.config),
        "identity": cfg["identity"]["alias"],
        "sessions": sessions,
        "git": gitc,
        "inbox": inbox,
        "stats": {
            "sessions": len(sessions),
            "user_prompts": sum(se["counts"]["user"] for se in sessions),
            "agent_answers": sum(se["counts"]["final"] for se in sessions),
            "summaries": sum(se["counts"]["summary"] for se in sessions),
            "event_log_sessions": sum(1 for se in sessions if se["conversation_source"] == "events"),
            "dialogue_chars": sum(len(e["text"]) for se in sessions for e in se["dialogue"]),
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
                   "cutoff_utc": snapshot["cutoff_utc"], "stats": snapshot["stats"]}, f, indent=2)

    if not args.quiet:
        s = snapshot["stats"]
        print("HARVEST OK  window=%.1fh  sessions=%d  prompts=%d  answers=%d  summaries=%d  "
              "git_repos=%d  git_commits=%d  inbox_chars=%d"
              % (hours, s["sessions"], s["user_prompts"], s["agent_answers"], s["summaries"],
                 s["git_repos"], s["git_commits"], s["inbox_chars"]))
        print("JSON: %s" % json_path)
        print("MD:   %s" % md_path)

if __name__ == "__main__":
    main()
