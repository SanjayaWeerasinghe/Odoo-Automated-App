"""Autonomous loop: sheet -> triage -> fix -> push -> verify. No human in the loop.

Run: python -m bugfix.daemon     Stop: touch state/STOP (also written automatically by the circuit breaker).
"""
import fcntl
import json
import time
import traceback

from dotenv import dotenv_values

from bugfix import fix, triage
from bugfix.intake import sheet_reader, sheet_writer

ROOT = triage.ROOT
STATE = triage.STATE
POLL_IDLE = 900          # seconds between sheet polls when nothing is queued
BREAKER = 3              # consecutive bad fix outcomes before the daemon stops itself
BAD = {"reverted_after_failed_verify", "pushed_deploy_unconfirmed", "too_large", "missing_version_bump"}


def conn():
    c = triage.db()
    c.execute("CREATE TABLE IF NOT EXISTS fixes(key TEXT, fingerprint TEXT, outcome TEXT, payload TEXT, ts TEXT)")
    return c


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def write_report(c):
    rows = c.execute("SELECT key, verdict, payload, ts FROM triage ORDER BY ts DESC").fetchall()
    fx = {r[0]: r for r in c.execute("SELECT key, outcome, payload, ts FROM fixes ORDER BY ts").fetchall()}
    lines = ["# Bug bot report", f"_updated {time.strftime('%Y-%m-%d %H:%M:%S')}_", "",
             "| Bug | Verdict | Fix outcome | Summary |", "|---|---|---|---|"]
    for key, verdict, payload, ts in rows:
        p = json.loads(payload)
        f = fx.get(key)
        lines.append(f"| {key} | {verdict} | {f[1] if f else ''} | {(p.get('summary') or '')[:160].replace('|', '/')} |")
    (STATE / "REPORT.md").write_text("\n".join(lines) + "\n")


def _interesting(v):
    return bool(v.get("needs_fix")) or (v.get("verdict") == "reproduces_on_both" and bool(v.get("suspected_files")))


def triage_consensus(bug):
    """Triage is a free-form investigation and varies between runs. Before any code change, require a majority
    (2 of up to 3 runs) that the bug is a fixable defect; non-candidates are accepted after one run."""
    runs = [triage.triage_one(bug)]
    if _interesting(runs[0]):
        for _ in range(2):
            try:
                runs.append(triage.triage_one(bug))
            except Exception:
                break
            fixes = sum(1 for r in runs if r.get("needs_fix"))
            if fixes >= 2 or len(runs) - fixes >= 2:
                break
    votes = [bool(r.get("needs_fix")) for r in runs]
    want = sum(votes) * 2 > len(votes)
    pick = next((r for r in runs if bool(r.get("needs_fix")) == want), runs[0])
    pick = dict(pick, needs_fix=want, consensus=f"{sum(votes)}/{len(votes)} runs say fix",
                run_verdicts=[r.get("verdict") for r in runs])
    return pick


def _note(bug, verdict, summary, outcome, cfg):
    try:
        res = sheet_writer.write_notes(cfg["SHEET_ID"], [(bug.tab, bug.row, sheet_writer.note_for(verdict, summary, outcome))])
        log(f"{bug.key}: sheet note -> {list(res.values())[0]}")
    except Exception as e:  # the sheet is a convenience; never let it stop the loop
        log(f"{bug.key}: sheet write failed: {str(e)[:120]}")


def resume_pending(c, cfg):
    """A fix was pushed but the process died before verification finished: finish it now."""
    rows = c.execute("SELECT rowid, key, fingerprint, payload FROM fixes WHERE outcome='pending_verify'").fetchall()
    if not rows:
        return
    bugs = {b.key: b for b in sheet_reader.parse(sheet_reader.download(cfg["SHEET_ID"]))}
    for rid, key, fp, payload in rows:
        st = json.loads(payload)
        log(f"{key}: resuming verification of pushed fix {st['sha'][:8]}")
        try:
            out = fix.finish_fix(bugs[key], st, cfg, lambda b, note: triage.triage_one(b, extra_note=note))
        except Exception as e:
            out = {**st, "outcome": "fix_error", "error": str(e)[:300]}
        c.execute("UPDATE fixes SET outcome=?, payload=? WHERE rowid=?", (out["outcome"], json.dumps(out, default=str), rid))
        c.commit()
        log(f"{key}: FIX -> {out['outcome']}")
        write_report(c)
        _note(bugs[key], (out.get("verify") or {}).get("verdict", "needs_human"), (out.get("verify") or {}).get("summary", ""), out, cfg)


def process(bug, c, cfg):
    t0 = time.time()
    try:
        v = triage_consensus(bug)
    except Exception as e:
        v = {"verdict": "needs_human", "summary": f"triage error: {str(e)[:300]}", "error": True}
    c.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?,?,datetime('now'))", (bug.key, bug.fingerprint, v["verdict"], json.dumps(v)))
    c.commit()
    log(f"{bug.key}: {v['verdict']} ({time.time() - t0:.0f}s) {v.get('summary', '')[:120]}")
    outcome = None
    if v.get("needs_fix") and not v.get("error"):
        tries = c.execute("SELECT COUNT(*) FROM fixes WHERE key=? AND fingerprint=?", (bug.key, bug.fingerprint)).fetchone()[0]
        if tries < 2:
            pending = {}

            def on_push(state):
                cur = c.execute("INSERT INTO fixes VALUES (?,?,?,?,datetime('now'))",
                                (bug.key, bug.fingerprint, "pending_verify", json.dumps(state, default=str)))
                c.commit()
                pending["rowid"] = cur.lastrowid
            try:
                outcome = fix.attempt_fix(bug, v, triage.build_prompt(bug).split("## Environments")[0], cfg,
                                          lambda b, note: triage.triage_one(b, extra_note=note), on_push=on_push)
            except Exception as e:
                outcome = {"bug": bug.key, "outcome": "fix_error", "error": f"{e}"[:400], "trace": traceback.format_exc()[-600:]}
            if pending:
                c.execute("UPDATE fixes SET outcome=?, payload=? WHERE rowid=?", (outcome["outcome"], json.dumps(outcome, default=str), pending["rowid"]))
            else:
                c.execute("INSERT INTO fixes VALUES (?,?,?,?,datetime('now'))", (bug.key, bug.fingerprint, outcome["outcome"], json.dumps(outcome, default=str)))
            c.commit()
            log(f"{bug.key}: FIX -> {outcome['outcome']} {outcome.get('commit', '')}")
    with open(STATE / "results.jsonl", "a") as f:
        f.write(json.dumps({"bug": bug.key, "triage": v, "fix": outcome}, default=str) + "\n")
    write_report(c)
    _note(bug, v["verdict"], v.get("summary", ""), outcome, cfg)
    return outcome


def main():
    STATE.mkdir(exist_ok=True)
    lock = open(STATE / "daemon.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cfg = dict(dotenv_values(ROOT / ".env"))
    bad_streak = 0
    log("daemon started")
    try:
        resume_pending(conn(), cfg)
    except Exception as e:
        log(f"resume_pending failed: {str(e)[:200]}")
    while True:
        if triage.KILL_FILE.exists():
            log("STOP file present; sleeping"); time.sleep(60); continue
        try:
            queue = sheet_reader.work_queue(sheet_reader.parse(sheet_reader.download(cfg["SHEET_ID"])))
        except Exception as e:
            log(f"sheet read failed: {e}"); time.sleep(120); continue
        c = conn()
        todo = [b for b in queue if not triage.already_done(c, b)]
        log(f"queue={len(queue)} todo={len(todo)}")
        for bug in todo:
            if triage.KILL_FILE.exists():
                break
            outcome = process(bug, c, cfg)
            if outcome:
                bad_streak = bad_streak + 1 if outcome["outcome"] in BAD or outcome["outcome"] == "fix_error" else 0
                if bad_streak >= BREAKER:
                    triage.KILL_FILE.write_text(f"circuit breaker: {BREAKER} bad fix outcomes in a row at {bug.key}\n")
                    log("circuit breaker tripped; STOP file written"); break
            time.sleep(5)
        else:
            time.sleep(POLL_IDLE)


if __name__ == "__main__":
    main()
