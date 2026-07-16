#!/usr/bin/env python3
"""
Dream ledger — the item registry that makes consolidation stateful across nights.

This is what prevents pollution and enables promotion/decay:
  - Every candidate learning is stored once (keyed by a stable fingerprint).
  - Re-seeing an item bumps hit_count / distinct_days -> recurring items become promotable.
  - Short-term items that go stale are surfaced for decay (archival out of active context).

The Dream agent calls these subcommands (deterministic, no fragile inline SQL):

  python ledger.py init
  python ledger.py stats
  python ledger.py upsert --json items.json      # batch upsert list of item dicts
  python ledger.py promotions                     # SHORT items meeting promotion thresholds
  python ledger.py decays                          # active SHORT items past the decay window
  python ledger.py set-status --fingerprint F --status applied|archived|dropped|proposed|active
  python ledger.py record-run --json run.json
  python ledger.py dump [--status active] [--horizon short]

Item dict shape (upsert):
  { "claim": str, "domain": "<domain>|dev-workflow|off-domain",
    "horizon": "long|short|drop", "importance": 1..10,
    "confidence": "high|medium|low", "target": "<skill-name>|dream-active-work|review-queue",
    "status": "active|applied|proposed|archived|dropped",
    "source": "sessions|git|inbox|mixed", "evidence": str, "notes": str,
    "fingerprint": optional (auto-derived from normalized claim if omitted) }
"""
import argparse, hashlib, json, os, re, sqlite3, sys
from datetime import datetime, timezone, timedelta

def expand(p): return os.path.expanduser(p)
def utcnow(): return datetime.now(timezone.utc)
def iso(dt=None): return (dt or utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def today(): return utcnow().strftime("%Y-%m-%d")

def load_config(path):
    with open(expand(path), encoding="utf-8") as f: return json.load(f)

def fingerprint(claim):
    norm = re.sub(r"\s+", " ", (claim or "").strip().lower())
    norm = re.sub(r"[^a-z0-9 ]", "", norm)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]

def connect(cfg):
    db = expand(cfg["paths"]["ledger_db"])
    os.makedirs(os.path.dirname(db), exist_ok=True)
    return sqlite3.connect(db)

DDL_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
  fingerprint TEXT PRIMARY KEY,
  claim TEXT NOT NULL,
  domain TEXT,
  horizon TEXT,
  importance INTEGER,
  confidence TEXT,
  target TEXT,
  status TEXT,
  hit_count INTEGER DEFAULT 1,
  distinct_days INTEGER DEFAULT 1,
  first_seen TEXT,
  last_seen TEXT,
  last_day TEXT,
  source TEXT,
  evidence TEXT,
  notes TEXT,
  updated_at TEXT
);
"""
DDL_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started TEXT, finished TEXT, model TEXT, window_hours REAL,
  harvested INTEGER, dropped INTEGER, applied INTEGER, proposed INTEGER,
  promoted INTEGER, decayed INTEGER, journal_path TEXT, status TEXT, notes TEXT
);
"""

def cmd_init(cfg, args):
    c = connect(cfg); c.execute(DDL_ITEMS); c.execute(DDL_RUNS); c.commit(); c.close()
    print("LEDGER INIT OK -> %s" % expand(cfg["paths"]["ledger_db"]))

def cmd_upsert(cfg, args):
    items = json.load(open(args.json, encoding="utf-8"))
    if isinstance(items, dict): items = [items]
    c = connect(cfg); c.execute(DDL_ITEMS)
    now, day = iso(), today()
    ins = upd = 0
    for it in items:
        claim = it.get("claim", "").strip()
        if not claim: continue
        fp = it.get("fingerprint") or fingerprint(claim)
        row = c.execute("SELECT hit_count,distinct_days,first_seen,last_day FROM items WHERE fingerprint=?", (fp,)).fetchone()
        if row:
            hc, dd, first_seen, last_day = row
            hc += 1
            if last_day != day: dd += 1
            c.execute("""UPDATE items SET claim=?,domain=?,horizon=?,importance=?,confidence=?,
                         target=?,status=COALESCE(?,status),hit_count=?,distinct_days=?,last_seen=?,
                         last_day=?,source=?,evidence=?,notes=?,updated_at=? WHERE fingerprint=?""",
                      (claim, it.get("domain"), it.get("horizon"), it.get("importance"), it.get("confidence"),
                       it.get("target"), it.get("status"), hc, dd, now, day, it.get("source"),
                       it.get("evidence"), it.get("notes"), now, fp))
            upd += 1
        else:
            c.execute("""INSERT INTO items(fingerprint,claim,domain,horizon,importance,confidence,target,
                         status,hit_count,distinct_days,first_seen,last_seen,last_day,source,evidence,notes,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,1,1,?,?,?,?,?,?,?)""",
                      (fp, claim, it.get("domain"), it.get("horizon"), it.get("importance"), it.get("confidence"),
                       it.get("target"), it.get("status", "active"), now, now, day, it.get("source"),
                       it.get("evidence"), it.get("notes"), now))
            ins += 1
    c.commit(); c.close()
    print("UPSERT OK inserted=%d updated=%d" % (ins, upd))

def cmd_promotions(cfg, args):
    th = cfg["thresholds"]
    c = connect(cfg); c.execute(DDL_ITEMS)
    rows = c.execute("""SELECT fingerprint,claim,importance,hit_count,distinct_days,target,domain
                        FROM items WHERE horizon='short' AND status IN('active','applied')
                        AND hit_count>=? AND distinct_days>=? ORDER BY hit_count DESC""",
                     (th["promote_hit_count"], th["promote_distinct_days"])).fetchall()
    c.close()
    out = [dict(zip(["fingerprint","claim","importance","hit_count","distinct_days","target","domain"], r)) for r in rows]
    print(json.dumps(out, indent=2, ensure_ascii=False))

def cmd_decays(cfg, args):
    days = cfg["thresholds"]["decay_days"]
    cutoff = iso(utcnow() - timedelta(days=days))
    c = connect(cfg); c.execute(DDL_ITEMS)
    rows = c.execute("""SELECT fingerprint,claim,last_seen,target FROM items
                        WHERE horizon='short' AND status='active' AND last_seen < ?
                        ORDER BY last_seen ASC""", (cutoff,)).fetchall()
    c.close()
    out = [dict(zip(["fingerprint","claim","last_seen","target"], r)) for r in rows]
    print(json.dumps(out, indent=2, ensure_ascii=False))

def cmd_set_status(cfg, args):
    c = connect(cfg); c.execute(DDL_ITEMS)
    c.execute("UPDATE items SET status=?,updated_at=? WHERE fingerprint=?", (args.status, iso(), args.fingerprint))
    c.commit(); n = c.total_changes; c.close()
    print("SET-STATUS %s -> %s (%d)" % (args.fingerprint, args.status, n))

def cmd_stats(cfg, args):
    c = connect(cfg); c.execute(DDL_ITEMS); c.execute(DDL_RUNS)
    total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    by_h = dict(c.execute("SELECT horizon,COUNT(*) FROM items GROUP BY horizon").fetchall())
    by_s = dict(c.execute("SELECT status,COUNT(*) FROM items GROUP BY status").fetchall())
    runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    last = c.execute("SELECT run_id,finished,model,status FROM runs ORDER BY finished DESC LIMIT 1").fetchone()
    c.close()
    print(json.dumps({"items": total, "by_horizon": by_h, "by_status": by_s,
                      "runs": runs, "last_run": last}, indent=2, ensure_ascii=False))

def cmd_dump(cfg, args):
    c = connect(cfg); c.execute(DDL_ITEMS)
    q = "SELECT fingerprint,claim,domain,horizon,importance,confidence,target,status,hit_count,distinct_days,last_seen FROM items"
    conds, params = [], []
    if args.status: conds.append("status=?"); params.append(args.status)
    if args.horizon: conds.append("horizon=?"); params.append(args.horizon)
    if conds: q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY importance DESC, hit_count DESC"
    rows = c.execute(q, params).fetchall()
    c.close()
    cols = ["fingerprint","claim","domain","horizon","importance","confidence","target","status","hit_count","distinct_days","last_seen"]
    print(json.dumps([dict(zip(cols, r)) for r in rows], indent=2, ensure_ascii=False))

def cmd_record_run(cfg, args):
    rec = json.load(open(args.json, encoding="utf-8"))
    c = connect(cfg); c.execute(DDL_RUNS)
    c.execute("""INSERT OR REPLACE INTO runs(run_id,started,finished,model,window_hours,harvested,
                 dropped,applied,proposed,promoted,decayed,journal_path,status,notes)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (rec.get("run_id"), rec.get("started"), rec.get("finished"), rec.get("model"),
               rec.get("window_hours"), rec.get("harvested"), rec.get("dropped"), rec.get("applied"),
               rec.get("proposed"), rec.get("promoted"), rec.get("decayed"), rec.get("journal_path"),
               rec.get("status"), rec.get("notes")))
    c.commit(); c.close()
    print("RECORD-RUN OK %s" % rec.get("run_id"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.copilot/dream/config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("stats")
    sub.add_parser("promotions")
    sub.add_parser("decays")
    up = sub.add_parser("upsert"); up.add_argument("--json", required=True)
    ss = sub.add_parser("set-status"); ss.add_argument("--fingerprint", required=True); ss.add_argument("--status", required=True)
    du = sub.add_parser("dump"); du.add_argument("--status"); du.add_argument("--horizon")
    rr = sub.add_parser("record-run"); rr.add_argument("--json", required=True)
    args = ap.parse_args()
    cfg = load_config(args.config)
    {"init": cmd_init, "stats": cmd_stats, "upsert": cmd_upsert, "promotions": cmd_promotions,
     "decays": cmd_decays, "set-status": cmd_set_status, "dump": cmd_dump,
     "record-run": cmd_record_run}[args.cmd](cfg, args)

if __name__ == "__main__":
    main()
