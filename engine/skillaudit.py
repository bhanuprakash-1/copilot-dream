#!/usr/bin/env python3
"""
Dream skill history - a local, never-pushed git history of the skills folder, so every change the
Dream makes is itemized line by line and can be undone per skill.

The history is its own git directory (config history.dir, default ~/.copilot/dream/skills-history.git)
with the skills folder as its work tree, so nothing is added inside ~/.copilot/skills.

Each Dream run is bracketed by snapshots tagged dream-<date>-<run8>-pre and -post. The Dream's own edits
are told apart from anything else that changed while it ran by the Copilot CLI's per-session edit log
(session-state/<run>/rewind-file-snapshots): the run's base (tag -base) is the post-run tree with every
file the Dream edited put back to its content from just before the Dream first touched it. Reports and
reverts work on base..post, so edits made by you or other tools during the run are listed separately and
are never reverted with the run. Changes made between runs (approvals, manual edits, installs) are
committed by the next snapshot, so the history covers every change to a skill.

Subcommands:
  run-begin  --run ID --date D [--receipts DIR]
                                     snapshot and tag the state before a run
  run-end    --run ID --date D [--receipts DIR] [--journal FILE] [--status ok|failed]
                                     snapshot and tag the state after it, separate the Dream's edits,
                                     write changes/<date>-<run8>.md + .json and add a summary to the journal
  report     [--run REF] [--journal FILE]
                                     rebuild a run's report
  snapshot   --message TEXT          commit whatever changed (used after approvals)
  runs       [--limit N]             recorded runs and what each one changed
  show       --run REF [--skill NAME] [--stat] [--all]
                                     a run's diff (the Dream's edits; --all adds edits made by others)
  log        [--skill NAME] [--limit N]
                                     every recorded change, newest first
  revert     --run REF --skill NAME [--check] [--restore] [--veto]
                                     undo one run's change to one skill
  undo-revert [--id ID]              undo a revert (files and ledger statuses); default: the latest
  restore    --skill NAME --to REV [--check]
                                     put a skill back to any recorded state

REF is a run id or unique prefix, a date (YYYY-MM-DD: the latest run that day) or "last".
REV is a commit, a tag, or REF:pre / REF:base / REF:post.

stdlib only; requires git on PATH.
"""
import argparse, fnmatch, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

EXCLUDES = ["__pycache__/", "*.pyc", "node_modules/", ".DS_Store", "Thumbs.db", "desktop.ini",
            "*.tmp", "*.swp", "~$*"]
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
JOURNAL_HEADING = "## Skill changes (verified by diff)"
TOOL = "python ~/.copilot/dream/skillaudit.py"
RUN_LOCK_MAX_AGE_SECONDS = 4 * 3600      # longer than any Dream run (the scheduled task stops at 3h)
STALE_INDEX_LOCK_SECONDS = 300           # only skillaudit uses this repository


class HistoryError(RuntimeError):
    pass


def expand(p):
    return os.path.expanduser(p)


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(path):
    with open(expand(path), encoding="utf-8") as f:
        return json.load(f)


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def parse_name_status(out):
    """`git diff --name-status -z` output -> [(status letter, path)]."""
    parts = out.split("\0")
    result, i = [], 0
    while i + 1 < len(parts):
        status = parts[i].strip()
        if status:
            result.append((status[0], parts[i + 1]))
            i += 2
        else:
            i += 1
    return result


def skill_of(path):
    return path.split("/", 1)[0]


def check_skill(name):
    name = (name or "").strip().strip("/\\")
    if not SKILL_NAME.match(name) or name in (".", ".."):
        raise HistoryError("'%s' is not a skill folder name" % name)
    return name


def spec(skill):
    return ":(literal)%s/" % skill


def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: the process exists
        try:
            code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_excluded(rel):
    parts = rel.split("/")
    for pattern in EXCLUDES:
        if pattern.endswith("/"):
            if pattern[:-1] in parts[:-1]:
                return True
        elif fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


def short_section(path):
    return " > ".join(path.split(" > ")[-2:])


def outline(text):
    """Split a markdown file into lines, the heading path each line sits under, and all heading paths.
    A single level-1 heading is the document title and is left out of the paths."""
    lines = text.splitlines()
    stack, paths, heads = [], [], []
    in_fence = False
    in_front = bool(lines) and lines[0].strip() == "---"
    titles = sum(1 for line in lines if re.match(r"^#\s+\S", line))
    for i, line in enumerate(lines):
        if in_front:
            paths.append("(frontmatter)")
            if i > 0 and line.strip() == "---":
                in_front = False
            continue
        if FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match:
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            if level > 1 or titles > 1:
                heads.append(" > ".join(t for lvl, t in stack if lvl > 1 or titles > 1))
        visible = [t for lvl, t in stack if lvl > 1 or titles > 1]
        paths.append(" > ".join(visible) if visible else "(top of file)")
    return lines, paths, heads


class History:
    def __init__(self, cfg):
        self.cfg = cfg
        hist = cfg.get("history") or {}
        paths = cfg.get("paths") or {}
        targets = cfg.get("targets") or {}
        sessions = (cfg.get("sources") or {}).get("cli_sessions") or {}
        self.enabled = hist.get("enabled", True)
        self.engine = os.path.abspath(expand(paths.get("engine", "~/.copilot/dream")))
        self.git_dir = os.path.abspath(expand(hist.get("dir", "~/.copilot/dream/skills-history.git")))
        self.work = os.path.abspath(expand(paths.get("skills_dir", "~/.copilot/skills")))
        self.changes_dir = os.path.abspath(expand(hist.get("changes_dir", "~/.copilot/dream/changes")))
        self.reverts_dir = os.path.join(self.changes_dir, "reverts")
        self.max_lines = int(hist.get("report_max_lines_per_skill", 120))
        self.archive_dir = os.path.abspath(expand(paths.get("archive_dir", "~/.copilot/dream/archive")))
        self.session_state = os.path.abspath(expand(sessions.get("session_state_dir", "~/.copilot/session-state")))
        self.receipt_root = os.path.join(self.engine, "completion", "receipts")
        self.lock_path = os.path.join(self.engine, "run.lock")
        self.watched = set(targets.get("watched_skills") or [])
        short = targets.get("short_term_skill") or ""
        self.short_term = os.path.basename(os.path.dirname(expand(short))) if short else "dream-active-work"

    # ---- git plumbing -------------------------------------------------------------------------
    def git_run(self, *args, data=None, env=None):
        if not os.path.isdir(self.work):
            raise HistoryError("skills folder not found: %s" % self.work)
        cmd = ["git", "--git-dir=" + self.git_dir, "--work-tree=" + self.work,
               "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false",
               "-c", "core.longpaths=true"] + list(args)
        full_env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        full_env.update(env or {})
        lock = os.path.join(self.git_dir, "index.lock")
        for attempt in range(8):
            try:
                if os.path.isfile(lock) and time.time() - os.path.getmtime(lock) > STALE_INDEX_LOCK_SECONDS:
                    os.remove(lock)  # left behind by a git process that died
            except OSError:
                pass
            try:
                result = subprocess.run(cmd, input=data, capture_output=True, env=full_env, cwd=self.work)
            except FileNotFoundError:
                raise HistoryError("git is not on PATH")
            err = result.stderr.decode("utf-8", "replace")
            if result.returncode != 0 and "index.lock" in err and attempt < 7:
                time.sleep(1.5)
                continue
            return result
        return result

    def git(self, *args, data=None, env=None):
        result = self.git_run(*args, data=data, env=env)
        if result.returncode != 0:
            raise HistoryError("git %s failed: %s"
                               % (" ".join(args[:2]), result.stderr.decode("utf-8", "replace").strip()))
        return result.stdout.decode("utf-8", "replace")

    def ensure(self):
        if not os.path.isdir(self.work):
            raise HistoryError("skills folder not found: %s" % self.work)
        if os.path.normcase(self.git_dir).startswith(os.path.normcase(self.work.rstrip("\\/") + os.sep)):
            raise HistoryError("history.dir must be outside the skills folder")
        if os.path.isfile(os.path.join(self.git_dir, "HEAD")):
            return False
        os.makedirs(os.path.dirname(self.git_dir), exist_ok=True)
        try:
            subprocess.run(["git", "init", "--quiet", "--bare", self.git_dir], check=True, capture_output=True)
            for key, value in (("core.bare", "false"), ("core.autocrlf", "false"), ("core.safecrlf", "false"),
                               ("core.quotepath", "false"), ("core.longpaths", "true"),
                               ("core.filemode", "false"), ("core.symlinks", "false"),
                               ("user.name", "Copilot Dream"), ("user.email", "dream@localhost"),
                               ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
                               ("gc.autoDetach", "false")):
                subprocess.run(["git", "--git-dir=" + self.git_dir, "config", key, value],
                               check=True, capture_output=True)
        except FileNotFoundError:
            raise HistoryError("git is not on PATH")
        except subprocess.CalledProcessError as e:
            raise HistoryError("could not create %s: %s" % (self.git_dir, e.stderr.decode("utf-8", "replace")))
        info = os.path.join(self.git_dir, "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "exclude"), "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(EXCLUDES) + "\n")
        return True

    def head(self):
        result = self.git_run("rev-parse", "--verify", "--quiet", "HEAD")
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def rev(self, ref):
        result = self.git_run("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if result.returncode != 0:
            raise HistoryError("unknown revision '%s'" % ref)
        return result.stdout.decode().strip()

    def snapshot(self, message, trailers=None):
        """Commit every pending change in the skills folder. Returns (commit, [(status, path)]).
        `message` may be a function of the staged changes."""
        self.ensure()
        self.git("add", "--all", "--", ".")
        had_head = self.head()
        changed = parse_name_status(self.git("diff", "--cached", "--name-status", "--no-renames", "-z"))
        if had_head and not changed:
            return had_head, []
        body = message(changed) if callable(message) else message
        if trailers:
            body += "\n\n" + "\n".join("%s: %s" % (k, v) for k, v in trailers.items())
        self.git("commit", "--quiet", "--no-verify", "--allow-empty", "-F", "-", data=body.encode("utf-8"))
        return self.head(), changed

    def show_bytes(self, rev, path):
        result = self.git_run("show", "%s:%s" % (rev, path))
        return result.stdout if result.returncode == 0 else None

    def show_text(self, rev, path):
        data = self.show_bytes(rev, path)
        return None if data is None else data.decode("utf-8", "replace")

    def changed_paths(self, a, b, skill=None):
        args = ["diff", "--name-status", "--no-renames", "-z", a, b]
        if skill:
            args += ["--", spec(skill)]
        return parse_name_status(self.git(*args))

    def files_at(self, rev, skill):
        out = self.git("ls-tree", "-r", "--name-only", "-z", rev, "--", spec(skill))
        return [p for p in out.split("\0") if p]

    def disk_files(self, skill):
        root = os.path.join(self.work, skill)
        found = []
        for folder, dirs, names in os.walk(root):
            for name in names:
                rel = os.path.relpath(os.path.join(folder, name), self.work).replace("\\", "/")
                if not is_excluded(rel):
                    found.append(rel)
        return found

    def read_disk(self, rel):
        path = os.path.join(self.work, *rel.split("/"))
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def write_disk(self, rel, data):
        path = os.path.join(self.work, *rel.split("/"))
        if data is None:
            if os.path.isfile(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def merge3(self, ours, base, theirs):
        """Apply the base->theirs change onto ours (3-way). Returns (bytes or None, conflict count)."""
        if ours == base:
            return theirs, 0
        if ours == theirs:
            return ours, 0
        tmp = tempfile.mkdtemp(prefix="skillaudit-")
        try:
            files = []
            for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
                path = os.path.join(tmp, name)
                with open(path, "wb") as f:
                    f.write(data)
                files.append(path)
            result = subprocess.run(["git", "merge-file", "-p", "-q"] + files, capture_output=True)
            if result.returncode == 0:
                return result.stdout, 0
            return None, result.returncode if 0 < result.returncode < 128 else -1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def plan_revert(self, before, after, files, restore=False):
        """What undoing before->after for these files would write: ([(path, bytes|None)], [(path, why)])."""
        plan, conflicts = [], []
        for _, path in files:
            old, new, current = self.show_bytes(before, path), self.show_bytes(after, path), self.read_disk(path)
            if restore or current == new:
                plan.append((path, old))
            elif current == old:
                continue
            elif old is None:
                conflicts.append((path, "the run created it and it was edited afterwards"))
            elif new is None:
                conflicts.append((path, "the run deleted it and it exists again"))
            elif current is None:
                conflicts.append((path, "it was deleted after the run"))
            else:
                merged, count = self.merge3(current, new, old)
                if merged is None:
                    conflicts.append((path, "later edits overlap the lines this run changed (%s region(s))"
                                      % (count if count > 0 else "?")))
                else:
                    plan.append((path, merged))
        return plan, conflicts

    # ---- run records ------------------------------------------------------------------------
    def record_path(self, date, run8, ext="json"):
        return os.path.join(self.changes_dir, "%s-%s.%s" % (date, run8, ext))

    def runs(self):
        records = []
        for path in glob.glob(os.path.join(self.changes_dir, "*.json")):
            rec = read_json(path)
            if rec and rec.get("run") and ("pre" in rec or "post" in rec):
                rec["_path"] = path
                records.append(rec)
        records.sort(key=lambda r: r.get("started_utc") or "", reverse=True)
        return records

    def resolve_run(self, ref, finished=True):
        runs = [r for r in self.runs() if r.get("post")] if finished else self.runs()
        if not runs:
            raise HistoryError("no finished Dream run has been recorded yet")
        key = (ref or "last").strip().lower()
        if key in ("last", "latest"):
            matches = runs[:1]
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", key):
            matches = [r for r in runs if r.get("date") == key][:1]
        else:
            matches = [r for r in runs if r["run"].lower().startswith(key)]
        if not matches:
            raise HistoryError("no recorded run matches '%s' (list them with: %s runs)" % (ref, TOOL))
        if len(matches) > 1:
            raise HistoryError("'%s' matches %d runs; use more of the run id" % (ref, len(matches)))
        return matches[0]

    def resolve_rev(self, ref):
        if ":" in ref and not re.match(r"^[A-Za-z]:[\\/]", ref):
            run_ref, _, side = ref.rpartition(":")
            if side in ("pre", "base", "post"):
                rec = self.resolve_run(run_ref, finished=(side != "pre"))
                value = rec.get(side) or (rec.get("pre") if side == "base" else None)
                if not value:
                    raise HistoryError("run %s has no %s snapshot" % (rec["run8"], side))
                return value
        return self.rev(ref)

    def receipt(self, rec, skill):
        folder = rec.get("receipts") or os.path.join(self.receipt_root, rec["run"])
        name = "active-work.json" if skill == self.short_term else "skill-%s.json" % skill
        return read_json(os.path.join(folder, name)) or {}

    def active_run(self):
        """The runner's lock record while a Dream run is in progress, else None."""
        lock = read_json(self.lock_path)
        if not lock:
            return None
        try:
            pid = int(lock.get("pid"))
            started = datetime.fromisoformat(str(lock.get("started_utc")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if (datetime.now(timezone.utc) - started).total_seconds() > RUN_LOCK_MAX_AGE_SECONDS:
            return None
        return lock if pid_alive(pid) else None

    def refuse_during_run(self, what):
        lock = self.active_run()
        if lock:
            raise HistoryError("a Dream run is in progress (pid %s, started %s); %s after it finishes"
                               % (lock.get("pid"), lock.get("started_utc"), what))

    # ---- attribution --------------------------------------------------------------------------
    def rewind_edits(self, run_id):
        """Files under the skills folder that the Copilot session edited, from its rewind log:
        {rel: {"before": bytes|None, "after_hash": str|None, "after_known": bool}}. None if unusable."""
        folder = os.path.join(self.session_state, run_id, "rewind-file-snapshots")
        index = read_json(os.path.join(folder, "index.json"))
        if not index or not isinstance(index.get("snapshots"), list):
            return None
        root = os.path.normcase(self.work.rstrip("\\/")) + os.sep
        edits = {}
        for snap in index["snapshots"]:
            for entry in (snap.get("files") or {}).values():
                path = os.path.abspath(entry.get("path") or "")
                if not os.path.normcase(path).startswith(root):
                    continue
                rel = os.path.relpath(path, self.work).replace("\\", "/")
                if is_excluded(rel):
                    continue
                pre, post = entry.get("preimage") or {}, entry.get("postimage") or {}
                if rel not in edits:
                    if pre.get("kind") == "absent":
                        before = None
                    elif pre.get("kind") == "content":
                        backup = os.path.join(folder, "backups", pre.get("backupFile") or "")
                        try:
                            with open(backup, "rb") as f:
                                before = f.read()
                        except OSError:
                            return None
                        if pre.get("contentHash") and sha256(before) != pre["contentHash"]:
                            return None
                    else:
                        return None
                    edits[rel] = {"before": before, "after_known": False, "after_hash": None}
                if post.get("kind") in ("content", "absent"):
                    edits[rel]["after_known"] = True
                    edits[rel]["after_hash"] = post.get("contentHash") if post.get("kind") == "content" else None
        return edits

    def build_base(self, rec, edits):
        """Commit the post-run tree with each Dream-edited file set back to its pre-edit content."""
        tmp = tempfile.mkdtemp(prefix="skillaudit-index-")
        try:
            env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
            self.git("read-tree", rec["post"], env=env)
            for rel, edit in sorted(edits.items()):
                if edit["before"] is None:
                    self.git("update-index", "--force-remove", "--", rel, env=env)
                else:
                    blob = self.git("hash-object", "-w", "--no-filters", "--stdin", data=edit["before"]).strip()
                    self.git("update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, rel), env=env)
            tree = self.git("write-tree", env=env).strip()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        parent = rec.get("pre") or rec["post"]
        message = "Dream run %s %s: skills just before the Dream's own edits" % (rec["date"], rec["run8"])
        return self.git("commit-tree", tree, "-p", parent, "-m", message).strip()

    def attribute(self, rec):
        """Set rec base/mixed/others from the CLI edit log; fall back to the pre-run snapshot."""
        edits = self.rewind_edits(rec["run"])
        if edits is not None:
            base = self.build_base(rec, edits)
            tag = "dream-%s-%s-base" % (rec["date"], rec["run8"])
            self.git("tag", "-f", tag, base)
            mixed = []
            for rel, edit in edits.items():
                if edit["after_known"]:
                    now = self.show_bytes(rec["post"], rel)
                    if (sha256(now) if now is not None else None) != edit["after_hash"]:
                        mixed.append(rel)
            rec.update({"base": base, "base_tag": tag, "attribution": "cli-edit-log", "mixed": sorted(mixed),
                        "dream_files": sorted(edits)})
            rec["others"] = [p for _, p in self.changed_paths(rec["pre"], base)] if rec.get("pre") else None
        else:
            rec.update({"base": rec.get("pre"), "base_tag": rec.get("pre_tag"),
                        "attribution": "snapshots-only" if rec.get("pre") else "unavailable",
                        "mixed": [], "dream_files": None, "others": None})
        return rec

    # ---- analysis ---------------------------------------------------------------------------
    def analyze_file(self, a, b, status, path):
        old = self.show_text(a, path) if status != "A" else ""
        new = self.show_text(b, path) if status != "D" else ""
        old, new = old or "", new or ""
        _, a_paths, a_heads = outline(old)
        _, b_paths, b_heads = outline(new)
        diff = self.git("diff", "--no-color", "--no-ext-diff", "--histogram", "-U0", a, b, "--", ":(literal)" + path)
        hunks, current = [], None
        for line in diff.splitlines():
            match = HUNK.match(line)
            if match:
                current = {"old_start": int(match.group(1)), "new_start": int(match.group(3)), "old": [], "new": []}
                hunks.append(current)
            elif current is not None and line[:1] == "-":
                current["old"].append(line[1:].rstrip("\r"))
            elif current is not None and line[:1] == "+":
                current["new"].append(line[1:].rstrip("\r"))
        binary = not hunks and "Binary files" in diff
        for h in hunks:
            h["kind"] = "rewritten" if h["old"] and h["new"] else ("removed" if h["old"] else "added")
            if h["new"] and 0 < h["new_start"] <= len(b_paths):
                h["section"] = b_paths[h["new_start"] - 1]
            elif h["old"] and 0 < h["old_start"] <= len(a_paths):
                h["section"] = a_paths[h["old_start"] - 1]
            else:
                h["section"] = "(top of file)"
        old_heads, new_heads = set(a_heads), set(b_heads)
        return {
            "binary": binary, "chars_before": len(old), "chars_after": len(new),
            "added": sum(len(h["new"]) for h in hunks if h["kind"] == "added"),
            "removed": sum(len(h["old"]) for h in hunks if h["kind"] == "removed"),
            "rewritten": sum(len(h["old"]) for h in hunks if h["kind"] == "rewritten"),
            "rewritten_into": sum(len(h["new"]) for h in hunks if h["kind"] == "rewritten"),
            "sections_added": [s for s in b_heads if s not in old_heads],
            "sections_removed": [s for s in a_heads if s not in new_heads],
            "hunks": hunks,
        }

    def analyze(self, a, b, rec=None, skills=None):
        by_skill = {}
        for status, path in self.changed_paths(a, b):
            if skills is None or skill_of(path) in skills:
                by_skill.setdefault(skill_of(path), []).append((status, path))
        mixed = set((rec or {}).get("mixed") or [])
        result = []
        for name, files in by_skill.items():
            entry = {"name": name, "watched": name in self.watched, "files": [], "mixed_files": [],
                     "chars_before": 0, "chars_after": 0, "added": 0, "removed": 0, "rewritten": 0,
                     "rewritten_into": 0, "sections_added": [], "sections_removed": [],
                     "sections_rewritten": [], "sections_extended": [], "hunks": []}
            for status, path in sorted(files, key=lambda f: (not f[1].endswith("/SKILL.md"), f[1])):
                fa = self.analyze_file(a, b, status, path)
                entry["files"].append({"path": path, "status": status, "binary": fa["binary"]})
                if path in mixed:
                    entry["mixed_files"].append(path)
                for key in ("chars_before", "chars_after", "added", "removed", "rewritten", "rewritten_into"):
                    entry[key] += fa[key]
                entry["sections_added"] += fa["sections_added"]
                entry["sections_removed"] += fa["sections_removed"]
                entry["hunks"] += [dict(h, path=path) for h in fa["hunks"]]
            gone = set(entry["sections_added"] + entry["sections_removed"])
            for h in entry["hunks"]:
                if h["section"] in gone:
                    continue
                bucket = "sections_extended" if h["kind"] == "added" else "sections_rewritten"
                if h["section"] not in entry[bucket]:
                    entry[bucket].append(h["section"])
            entry["sections_extended"] = [s for s in entry["sections_extended"] if s not in entry["sections_rewritten"]]
            if rec:
                receipt = self.receipt(rec, name)
                entry["applier_summary"] = receipt.get("summary")
                entry["applier_changes"] = receipt.get("changes") or []
            result.append(entry)
        result.sort(key=lambda s: (not s["watched"], -(s["removed"] + s["rewritten"]), -s["added"], s["name"]))
        return result


# ---- rendering ----------------------------------------------------------------------------------
def fmt_chars(before, after):
    return "{:,} → {:,} ({:+,})".format(before, after, after - before)


def fmt_lines(s):
    return "+%d / −%d / ~%d→%d" % (s["added"], s["removed"], s["rewritten"], s.get("rewritten_into", s["rewritten"]))


def names(sections, limit=6):
    if not sections:
        return "—"
    shown = [short_section(s) for s in sections[:limit]]
    more = len(sections) - limit
    return "; ".join(shown) + (" (+%d more)" % more if more > 0 else "")


def journal_account(journal_text, skill):
    if not journal_text:
        return None
    match = re.search(r"^\s*[-*]\s+\**%s\**\s*:\s*(.+)$" % re.escape(skill), journal_text, re.M)
    return match.group(1).strip() if match else None


ATTRIBUTION_NOTES = {
    "snapshots-only": "The Copilot CLI edit log for this run was not available, so everything that changed while "
                      "it ran is shown as the run's change, including any edit made by someone else meanwhile.",
    "unavailable": "No snapshot was taken before this run and the CLI edit log was not available, so its changes "
                   "cannot be itemized.",
}


def render_report(h, rec, journal_text=None):
    run8, date = rec["run8"], rec["date"]
    L = ["# Skill changes — Dream run %s (%s)" % (date, run8), ""]
    L.append("Status: **%s** · before the Dream's edits `%s` → after `%s`"
             % (rec.get("status", "?"), rec.get("base_tag") or (rec.get("base") or "?")[:7], rec.get("post_tag")))
    if rec.get("attribution") in ATTRIBUTION_NOTES:
        L += ["", "> " + ATTRIBUTION_NOTES[rec["attribution"]]]
    outside = rec.get("outside_changes") or []
    if outside:
        L += ["", "Committed just before this run: %d file(s) changed outside the Dream (manual edits, approvals, "
              "installs) in %s." % (len(outside), ", ".join("`%s`" % s for s in sorted({skill_of(p) for p in outside})))]
    others = rec.get("others") or []
    if others:
        L += ["", "**Changed during the run by something other than the Dream** (not part of this report, not undone "
              "by `revert`): %s. Inspect with `%s show --run %s --all`."
              % (", ".join("`%s`" % p for p in others), TOOL, run8)]
    L += ["",
          "- **Undo one skill:** `%s revert --run %s --skill <name>` (add `--check` to preview)" % (TOOL, run8),
          "- **Full diff:** `%s show --run %s --skill <name>`" % (TOOL, run8),
          "- **History of a skill:** `%s log --skill <name>`" % TOOL, ""]
    skills = rec.get("skills") or []
    if not skills:
        L.append("The Dream changed no skill files in this run.")
        return "\n".join(L) + "\n"
    L += ["★ = watched skill (listed first). Lines: +added / −removed / ~rewritten→into.", "",
          "| Skill | Chars | Lines | Sections rewritten or trimmed | Sections removed | Sections added |",
          "|---|---|---|---|---|---|"]
    for s in skills:
        L.append("| %s%s%s | %s | %s | %s | %s | %s |"
                 % (s["name"], " ★" if s["watched"] else "", " ⚠" if s.get("mixed_files") else "",
                    fmt_chars(s["chars_before"], s["chars_after"]), fmt_lines(s), names(s["sections_rewritten"], 4),
                    names(s["sections_removed"], 4), names(s["sections_added"], 4)))
    if any(s.get("mixed_files") for s in skills):
        L += ["", "⚠ also edited by something else after the Dream's last edit during the run; `revert` refuses "
                  "these files unless you pass `--restore`."]
    for s in skills:
        L += ["", "## %s%s" % (s["name"], " ★" if s["watched"] else ""), ""]
        account = s.get("applier_summary") or journal_account(journal_text, s["name"])
        if account:
            L.append("**Applier's account:** %s" % account)
        for change in s.get("applier_changes") or []:
            L.append("- %s — %s: %s" % (change.get("action", "?"), short_section(change.get("section", "?")),
                                        change.get("note", "")))
        L.append("- **Rewritten or trimmed (%d):** %s" % (len(s["sections_rewritten"]), names(s["sections_rewritten"], 50)))
        L.append("- **Removed (%d):** %s" % (len(s["sections_removed"]), names(s["sections_removed"], 60)))
        L.append("- **Added (%d):** %s" % (len(s["sections_added"]), names(s["sections_added"], 50)))
        if s["sections_extended"]:
            L.append("- **Extended with new lines:** %s" % names(s["sections_extended"], 50))
        if s.get("mixed_files"):
            L.append("- ⚠ Also edited by something else during the run: %s" % ", ".join("`%s`" % p for p in s["mixed_files"]))
        if s["name"] == h.short_term:
            archived = os.path.join(h.archive_dir, "active-work-%s.md" % rec["date"])
            if os.path.isfile(archived):
                L.append("- Removed text was archived verbatim in `%s`." % archived)
        extra = [f for f in s["files"] if not f["path"].endswith("/SKILL.md")]
        if extra:
            L.append("- Other files: " + ", ".join("`%s` (%s)" % (f["path"], f["status"]) for f in extra))
        L += ["", "```diff"]
        shown, current_section, hidden = 0, None, 0
        for hunk in s["hunks"]:
            lines = ["-" + x for x in hunk["old"]] + ["+" + x for x in hunk["new"]]
            if shown >= h.max_lines:
                hidden += len(lines)
                continue
            if hunk["section"] != current_section:
                L.append("@@ %s" % short_section(hunk["section"]))
                current_section = hunk["section"]
            room = h.max_lines - shown
            L += lines[:room]
            hidden += max(0, len(lines) - room)
            shown += min(len(lines), room)
        L.append("```")
        if hidden:
            L.append("… %d more changed line(s) not shown. Full diff: `%s show --run %s --skill %s`"
                     % (hidden, TOOL, run8, s["name"]))
        L.append("Undo: `%s revert --run %s --skill %s`" % (TOOL, run8, s["name"]))
    return "\n".join(L) + "\n"


def render_journal_section(h, rec, report_path):
    run8 = rec["run8"]
    L = [JOURNAL_HEADING, "",
         "Snapshots: before the Dream's edits `%s` → after `%s`. Line-by-line report: `%s`"
         % (rec.get("base_tag") or "?", rec.get("post_tag"), report_path), ""]
    if rec.get("attribution") in ATTRIBUTION_NOTES:
        L += ["> " + ATTRIBUTION_NOTES[rec["attribution"]], ""]
    skills = rec.get("skills") or []
    if not skills:
        L.append("The Dream changed no skill files.")
    else:
        L += ["| Skill | Chars | Lines +/−/~ | Rewritten or trimmed | Removed sections |", "|---|---|---|---|---|"]
        for s in skills:
            L.append("| %s%s%s | %s | %s | %s | %s |"
                     % (s["name"], " ★" if s["watched"] else "", " ⚠" if s.get("mixed_files") else "",
                        fmt_chars(s["chars_before"], s["chars_after"]), fmt_lines(s), names(s["sections_rewritten"], 3),
                        "%d: %s" % (len(s["sections_removed"]), names(s["sections_removed"], 3)) if s["sections_removed"] else "—"))
        L += ["", "★ watched. Undo one skill: `%s revert --run %s --skill <name>` (`--check` previews)." % (TOOL, run8)]
    if rec.get("others"):
        L += ["", "Also changed during the run by something other than the Dream (not undone by `revert`): %s."
              % ", ".join("`%s`" % p for p in rec["others"])]
    return "\n".join(L) + "\n"


def upsert_journal_section(path, section, keep_mtime=False):
    stat = os.stat(path)
    with open(path, "rb") as f:
        text = f.read().decode("utf-8-sig", "replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")
    start = text.find(JOURNAL_HEADING)
    if start >= 0:
        nxt = text.find("\n## ", start + len(JOURNAL_HEADING))
        text = text[:start].rstrip("\n") + "\n\n" + section + ("\n" + text[nxt + 1:] if nxt >= 0 else "")
    else:
        text = text.rstrip("\n") + "\n\n" + section
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", newline))
    if keep_mtime:
        # regenerating an old report must not make an old journal look like the newest one
        os.utime(path, (stat.st_atime, stat.st_mtime))


def summary_line(rec):
    skills = rec.get("skills") or []
    touched = [s for s in skills if s["removed"] or s["rewritten"] or s["sections_removed"]]
    watched = [s["name"] for s in touched if s["watched"]]
    line = "%d skill(s) changed by the Dream; %d with text rewritten or removed%s" % (
        len(skills), len(touched), (" (watched: %s)" % ", ".join(watched)) if watched else "")
    if rec.get("others"):
        line += "; %d file(s) changed by others during the run" % len(rec["others"])
    if rec.get("attribution") in ATTRIBUTION_NOTES:
        line += "; attribution: %s" % rec["attribution"]
    return line


def public_record(rec):
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    out["skills"] = [{k: v for k, v in s.items() if k != "hunks"} for s in rec.get("skills") or []]
    return out


def write_report(h, rec, keep_journal_mtime=False):
    if not rec.get("base") and rec.get("attribution") != "unavailable":
        h.attribute(rec)
    rec["skills"] = h.analyze(rec["base"], rec["post"], rec) if rec.get("base") else []
    journal = rec.get("journal")
    journal_text = None
    if journal and os.path.isfile(journal):
        with open(journal, encoding="utf-8-sig", errors="replace") as f:
            journal_text = f.read()
    report_path = h.record_path(rec["date"], rec["run8"], "md")
    rec["report"] = report_path
    os.makedirs(h.changes_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(render_report(h, rec, journal_text))
    write_json(h.record_path(rec["date"], rec["run8"]), public_record(rec))
    if journal_text is not None:
        upsert_journal_section(journal, render_journal_section(h, rec, report_path), keep_mtime=keep_journal_mtime)
    return rec


# ---- ledger ---------------------------------------------------------------------------------------
def ledger_conn(h):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ledger as ledger_db
    conn = ledger_db.connect(h.cfg)
    ledger_db.ensure_schema(conn)
    return conn


def ledger_statuses(h, fingerprints):
    conn = ledger_conn(h)
    try:
        return {fp: row[0] for fp in fingerprints
                for row in [conn.execute("SELECT status FROM items WHERE fingerprint=?", (fp,)).fetchone()] if row}
    finally:
        conn.close()


def set_ledger(h, statuses):
    """{fingerprint: status} -> number of ledger rows updated."""
    conn = ledger_conn(h)
    try:
        now, done = utcnow(), 0
        for fp, status in statuses.items():
            done += conn.execute("UPDATE items SET status=?, updated_at=? WHERE fingerprint=?", (status, now, fp)).rowcount
        conn.commit()
        return done
    finally:
        conn.close()


# ---- commands -----------------------------------------------------------------------------------
def cmd_run_begin(h, args):
    run8 = args.run[:8]
    created = h.ensure()
    message = ("Baseline: skills before Dream run %s %s" if created
               else "Changes made outside the Dream before run %s %s") % (args.date, run8)
    commit, changed = h.snapshot(message, {"Dream-Run": args.run, "Dream-Phase": "pre"})
    tag = "dream-%s-%s-pre" % (args.date, run8)
    h.git("tag", "-f", tag, commit)
    rec = {"run": args.run, "run8": run8, "date": args.date, "started_utc": utcnow(), "status": "running",
           "pre": commit, "pre_tag": tag, "outside_changes": [] if created else [p for _, p in changed],
           "receipts": args.receipts or os.path.join(h.receipt_root, args.run)}
    write_json(h.record_path(args.date, run8), rec)
    print("HISTORY pre %s %s (%s)" % (tag, commit[:10], "new baseline" if created
                                      else "%d file(s) changed outside the Dream since the last run" % len(changed)))


def cmd_run_end(h, args):
    run8 = args.run[:8]
    rec = read_json(h.record_path(args.date, run8))
    if not rec or rec.get("run") != args.run:
        rec = {"run": args.run, "run8": run8, "date": args.date, "started_utc": utcnow(),
               "pre": None, "pre_tag": None, "outside_changes": [],
               "note": "no snapshot was taken before this run (history unavailable when it started)"}
    if args.receipts:
        rec["receipts"] = args.receipts
    run_date = rec["date"]

    def message(changed):
        skills = sorted({skill_of(p) for _, p in changed})
        return "Dream run %s %s (%s)%s" % (run_date, run8, args.status, (": " + ", ".join(skills)) if skills else "")

    commit, _ = h.snapshot(message, {"Dream-Run": args.run, "Dream-Phase": "post"})
    tag = "dream-%s-%s-post" % (run_date, run8)
    h.git("tag", "-f", tag, commit)
    rec.update({"post": commit, "post_tag": tag, "ended_utc": utcnow(), "status": args.status, "base": None})
    if args.journal:
        rec["journal"] = args.journal
    h.attribute(rec)
    rec = write_report(h, rec)
    print("HISTORY post %s %s: %s; report: %s" % (tag, commit[:10], summary_line(rec), rec["report"]))


def cmd_report(h, args):
    rec = h.resolve_run(args.run)
    if args.journal:
        rec["journal"] = args.journal
    rec = write_report(h, rec, keep_journal_mtime=True)
    print("REPORT %s: %s -> %s" % (rec["run8"], summary_line(rec), rec["report"]))


def cmd_snapshot(h, args):
    lock = h.active_run()
    if lock:
        print("HISTORY deferred: a Dream run is in progress (pid %s); this change is recorded when it finishes"
              % lock.get("pid"))
        return 0
    commit, changed = h.snapshot(args.message, {"Dream-Phase": "manual"})
    print("HISTORY snapshot %s (%d file(s) changed)" % (commit[:10], len(changed)))


def cmd_runs(h, args):
    runs = h.runs()[:args.limit]
    if not runs:
        print("No Dream runs recorded yet.")
        return
    for r in runs:
        parts = ["%s%s +%d/-%d/~%d->%d" % (s["name"], "*" if s.get("watched") else "", s["added"], s["removed"],
                                           s["rewritten"], s.get("rewritten_into", s["rewritten"]))
                 for s in r.get("skills") or []]
        state = ", ".join(parts) if parts else ("(no skill changes)" if r.get("post") else "(not finished)")
        if r.get("others"):
            state += "  [+%d changed by others]" % len(r["others"])
        print("%s  %s  %-8s  %s" % (r.get("date"), r["run8"], r.get("status", "?"), state))
    print("\n* = watched. Details: %s show --run <run8> [--skill <name>]; reports in %s" % (TOOL, h.changes_dir))


def cmd_show(h, args):
    rec = h.resolve_run(args.run)
    before = rec.get("pre") if args.all else (rec.get("base") or rec.get("pre"))
    if not before:
        raise HistoryError("run %s has no snapshot from before it; nothing to compare" % rec["run8"])
    paths = ["--", spec(check_skill(args.skill))] if args.skill else []
    mode = ["--stat"] if args.stat else ["--histogram"]
    out = h.git("diff", "--no-color", "--no-ext-diff", *mode, before, rec["post"], *paths)
    print("Run %s %s (%s → %s)%s%s" % (rec["date"], rec["run8"], before[:7], rec["post"][:7],
                                       " — " + args.skill if args.skill else "",
                                       " — including changes made by others during the run" if args.all else ""))
    print(out.replace("\r\n", "\n").rstrip() or "(no changes)")


def cmd_log(h, args):
    h.ensure()
    paths = ["--", spec(check_skill(args.skill))] if args.skill else []
    out = h.git("log", "-n", str(args.limit), "--date=format:%Y-%m-%d %H:%M", "--format=%h  %ad  %s", *paths)
    print(out.rstrip() or "(no history yet)")


def cmd_revert(h, args):
    skill = check_skill(args.skill)
    rec = h.resolve_run(args.run)
    run8, before = rec["run8"], rec.get("base") or rec.get("pre")
    if not before:
        raise HistoryError("run %s has no snapshot from before it, so it cannot be reverted" % run8)
    files = h.changed_paths(before, rec["post"], skill)
    if not files:
        others = [p for p in rec.get("others") or [] if skill_of(p) == skill]
        if others:
            print("%s changed during run %s, but not by the Dream (e.g. your own edit), so the run's revert does not "
                  "touch it. To undo that change use: %s restore --skill %s --to %s:pre"
                  % (skill, run8, TOOL, skill, run8))
        else:
            print("Run %s (%s) did not change %s; nothing to revert." % (run8, rec["date"], skill))
        return 0
    mixed = [p for _, p in files if p in set(rec.get("mixed") or [])]
    if mixed and not args.restore:
        print("REFUSED: %s was also edited by something else after the Dream's last edit during run %s (%s). Undoing "
              "only the Dream's part is not possible automatically. Inspect with: %s show --run %s --skill %s\n"
              "--restore puts the file(s) back to their state before the Dream's edits, dropping that other edit too "
              "(it stays in history)." % (skill, run8, ", ".join(mixed), TOOL, run8, skill))
        return 2
    plan, conflicts = h.plan_revert(before, rec["post"], files, restore=args.restore)
    why = "\n".join("  - %s: %s" % (p, reason) for p, reason in conflicts)
    later = [r for r in h.runs() if r.get("post") and (r.get("started_utc") or "") > (rec.get("started_utc") or "")
             and any(s.get("name") == skill for s in r.get("skills") or [])]
    if later:
        why += ("\n  Later run(s) also changed %s: %s. Revert those first (newest first), then this one."
                % (skill, ", ".join("%s (%s)" % (r["run8"], r.get("date")) for r in later)))
    if conflicts:
        print("CONFLICT: run %s's change to %s cannot be undone on its own:\n%s\n%s --restore to reset the file(s) "
              "to their state before the Dream's edits (later edits to them are dropped but stay in history)."
              % (run8, skill, why, "Nothing changed. Re-run with" if not args.check else "Use"))
        return 2
    if args.check:
        print("OK: %s can be reverted to its state before run %s (%s): %d file(s)%s."
              % (skill, run8, rec["date"], len(plan), "" if plan else " already match it"))
        return 0
    if not plan:
        print("%s already matches its state before run %s; nothing to do." % (skill, run8))
        return 0
    h.refuse_during_run("revert")
    receipt = h.receipt(rec, skill)
    skipped = set(receipt.get("skipped_fingerprints") or [])
    fingerprints = [fp for fp in receipt.get("fingerprints") or [] if fp not in skipped]
    if skill == h.short_term and not args.veto:
        fingerprints = []  # active-work items are re-evaluated by the next run
    new_status = "rejected" if args.veto else "reverted"
    try:
        prior = ledger_statuses(h, fingerprints) if fingerprints else {}
    except Exception as e:
        if args.veto:
            raise HistoryError("cannot read the ledger to veto the reverted claims (%s); nothing changed" % e)
        prior = {}
    snap_before, _ = h.snapshot("Before reverting %s (Dream run %s %s)" % (skill, rec["date"], run8),
                                {"Dream-Phase": "manual"})
    for path, data in plan:
        h.write_disk(path, data)
    snap_after, _ = h.snapshot("Revert %s to its state before Dream run %s %s%s"
                               % (skill, rec["date"], run8, " (restore)" if args.restore else ""),
                               {"Dream-Run": rec["run"], "Dream-Phase": "revert"})
    exact = all(h.show_bytes(before, p) == h.show_bytes("HEAD", p) for _, p in files)
    updated, ledger_error = 0, None
    if prior:
        try:
            updated = set_ledger(h, {fp: new_status for fp in prior})
        except Exception as e:
            ledger_error = str(e)
    revert_id = snap_after[:10]
    write_json(os.path.join(h.reverts_dir, "%s.json" % revert_id), {
        "id": revert_id, "created_utc": utcnow(), "skill": skill, "run": rec["run"], "run8": run8,
        "date": rec["date"], "before": snap_before, "after": snap_after, "ledger_prior": prior,
        "ledger_status": new_status, "undone": False})
    print("Reverted %s: undid run %s (%s) in %d file(s)%s."
          % (skill, run8, rec["date"], len(plan),
             "; the skill now matches its state before the Dream's edits exactly" if exact
             else "; later edits to other parts of the skill were kept"))
    print("Undo this revert (files and ledger): %s undo-revert --id %s" % (TOOL, revert_id))
    if ledger_error:
        print("WARNING: the ledger was not updated (%s)%s." % (
            ledger_error, "; the claims are NOT vetoed and may come back" if args.veto else ""))
        return 4 if args.veto else 0
    if prior:
        print("Ledger: %d claim(s) from that run marked %s%s." % (
            updated, new_status, " (never re-added)" if args.veto else ""))
    elif args.veto:
        print("WARNING: no ledger claims match run %s's receipt for %s, so nothing was vetoed." % (run8, skill))
        return 4
    elif skill == h.short_term:
        print("Note: the next run may compact %s again unless thresholds.active_work_budget_chars is raised." % skill)
    else:
        print("Ledger: no ledger claims match run %s's receipt for %s; no ledger change." % (run8, skill))
    return 0


def restore_skill(h, skill, target_rev):
    """Make the skill folder match target_rev. Returns the list of paths written or removed."""
    wanted = set(h.files_at(target_rev, skill))
    present = set(h.disk_files(skill))
    touched = []
    for path in sorted(wanted):
        data = h.show_bytes(target_rev, path)
        if h.read_disk(path) != data:
            h.write_disk(path, data)
            touched.append(path)
    for path in sorted(present - wanted):
        h.write_disk(path, None)
        touched.append(path)
    return touched


def cmd_undo_revert(h, args):
    records = sorted((read_json(p) or {} for p in glob.glob(os.path.join(h.reverts_dir, "*.json"))),
                     key=lambda r: r.get("created_utc") or "", reverse=True)
    records = [r for r in records if r.get("id")]
    if args.id:
        records = [r for r in records if r["id"].startswith(args.id)]
    else:
        records = [r for r in records if not r.get("undone")][:1]
    if not records:
        raise HistoryError("no matching revert found (reverts are recorded in %s)" % h.reverts_dir)
    rev = records[0]
    if rev.get("undone"):
        print("Revert %s was already undone." % rev["id"])
        return 0
    files = h.changed_paths(rev["before"], rev["after"], rev["skill"])
    plan, conflicts = h.plan_revert(rev["before"], rev["after"], files)
    if conflicts:
        print("CONFLICT: later edits to %s overlap the lines revert %s changed:\n%s\nNothing changed. To go back to "
              "the state before the revert regardless: %s restore --skill %s --to %s"
              % (rev["skill"], rev["id"], "\n".join("  - %s: %s" % c for c in conflicts), TOOL, rev["skill"],
                 rev["before"][:10]))
        return 2
    h.refuse_during_run("undo a revert")
    h.snapshot("Before undoing revert %s of %s" % (rev["id"], rev["skill"]), {"Dream-Phase": "manual"})
    for path, data in plan:
        h.write_disk(path, data)
    h.snapshot("Undo revert %s of %s" % (rev["id"], rev["skill"]), {"Dream-Phase": "revert"})
    restored = set_ledger(h, rev.get("ledger_prior") or {}) if rev.get("ledger_prior") else 0
    rev["undone"] = True
    rev["undone_utc"] = utcnow()
    write_json(os.path.join(h.reverts_dir, "%s.json" % rev["id"]), rev)
    print("Undid revert %s: %s has the reverted change back (%d file(s)); %d ledger status(es) restored."
          % (rev["id"], rev["skill"], len(plan), restored))
    return 0


def cmd_restore(h, args):
    skill = check_skill(args.skill)
    h.ensure()
    target_rev = h.resolve_rev(args.to)
    if args.check:
        wanted = set(h.files_at(target_rev, skill))
        present = set(h.disk_files(skill))
        changes = ["M %s" % p for p in sorted(wanted & present) if h.read_disk(p) != h.show_bytes(target_rev, p)]
        changes += ["A %s" % p for p in sorted(wanted - present)] + ["D %s" % p for p in sorted(present - wanted)]
        print("\n".join(changes) if changes else "%s already matches %s." % (skill, args.to))
        return 0
    h.refuse_during_run("restore")
    before, _ = h.snapshot("Before restoring %s to %s" % (skill, args.to), {"Dream-Phase": "manual"})
    touched = restore_skill(h, skill, target_rev)
    h.snapshot("Restore %s to %s" % (skill, args.to), {"Dream-Phase": "revert"})
    print("Restored %s to %s (%d file(s) changed)." % (skill, args.to, len(touched)))
    print("Undo: %s restore --skill %s --to %s" % (TOOL, skill, before[:10]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Local history, change reports and revert for Dream-edited skills.")
    ap.add_argument("--config", default="~/.copilot/dream/config.json")
    ap.add_argument("--work-tree", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run-begin"); p.add_argument("--run", required=True); p.add_argument("--date", required=True)
    p.add_argument("--receipts")
    p = sub.add_parser("run-end"); p.add_argument("--run", required=True); p.add_argument("--date", required=True)
    p.add_argument("--receipts"); p.add_argument("--journal"); p.add_argument("--status", default="ok")
    p = sub.add_parser("report"); p.add_argument("--run", default="last"); p.add_argument("--journal")
    p = sub.add_parser("snapshot"); p.add_argument("--message", required=True)
    p = sub.add_parser("runs"); p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("show"); p.add_argument("--run", default="last"); p.add_argument("--skill")
    p.add_argument("--stat", action="store_true"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("log"); p.add_argument("--skill"); p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("revert"); p.add_argument("--run", default="last"); p.add_argument("--skill", required=True)
    p.add_argument("--check", action="store_true"); p.add_argument("--restore", action="store_true")
    p.add_argument("--veto", action="store_true")
    p = sub.add_parser("undo-revert"); p.add_argument("--id")
    p = sub.add_parser("restore"); p.add_argument("--skill", required=True); p.add_argument("--to", required=True)
    p.add_argument("--check", action="store_true")
    args = ap.parse_args()
    h = History(load_config(args.config))
    if args.work_tree:
        h.work = os.path.abspath(expand(args.work_tree))
    if not h.enabled and args.cmd in ("run-begin", "run-end", "snapshot"):
        print("HISTORY disabled (config history.enabled=false)")
        return 0
    handlers = {"run-begin": cmd_run_begin, "run-end": cmd_run_end, "report": cmd_report,
                "snapshot": cmd_snapshot, "runs": cmd_runs, "show": cmd_show, "log": cmd_log,
                "revert": cmd_revert, "undo-revert": cmd_undo_revert, "restore": cmd_restore}
    try:
        return handlers[args.cmd](h, args) or 0
    except HistoryError as e:
        print("HISTORY ERROR: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
