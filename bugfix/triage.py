"""Triage loop: pick bugs from the sheet, have Claude reproduce them on staging/KG, store a verdict.

usage: python -m bugfix.triage [--limit N] [--only 'Sales!2'] [--dry-run] [--loop SECONDS]
No code is changed and nothing is pushed here; fixing is a separate stage.
"""
import argparse
import fcntl
import json
import re
import sqlite3
import time
from pathlib import Path

from dotenv import dotenv_values

from bugfix.agents.claude_runner import run_with_limit_retry
from bugfix.intake import sheet_reader

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
WORKSPACE = ROOT / "workspaces" / "JinStage"
KILL_FILE = STATE / "STOP"
VERDICTS = {"verified_fixed", "not_reproducible", "reproduces_on_both", "staging_regression",
            "staging_bug_no_reference", "needs_human", "insufficient_info"}
FIX_CANDIDATES = {"staging_regression", "staging_bug_no_reference"}

TAB_HINTS = {  # starting points only; the agent greps to confirm
    "Sales": ["BugFix-Sales"], "Inventory": ["BugFix-Stock"], "Manufacturing": ["BugFix-MRP"],
    "Finance": ["BugFix-Accounting", "BugFix-Analytics"], "HR": ["BugFix-HR", "studio_usermodel_migration", "bank-data"],
    "HelpDesk": ["Fix-repair", "Fix-Repair-Wizard-Nav"], "Local Purchase": ["Jinasena_Local_Purchase", "BugFix-Purchase"],
    "Imports": ["Jinasena_All", "BugFix-Studio-Misc"],
}

PROMPT = """You are triaging ONE bug report for a custom Odoo 17 Enterprise ERP. Do NOT change any code and do not push anything.

## Bug report (sheet row {key})
{report}

Row status in the sheet: {status_note}

## Environments (both are Odoo 17.0+e; your user is already logged in by the tools)
- STAGING (`--env stg`): the upgraded build under test. You may create test records there. Only create what you need,
  put "[BOT]" in a free-text field (description/notes/ref), and DELETE every record you created before finishing.
  Never edit configuration (ir.*, res.users, res.company are refused by the tool anyway).
- KNOWN GOOD (`--env kg`): STRICTLY READ-ONLY reference. Only read calls work; do not try to work around the guard.
  KG runs OLDER custom module versions than staging, so a feature missing on KG is not proof of a staging bug.
  Bugs that need a save to reproduce can only be reproduced on staging; on KG compare by reading equivalent existing
  records, field/view definitions and screens.

## Tools (use via Bash, exactly this form)
- RPC: {bot}/bin/odoo-rpc --env stg|kg --model res.partner --method search_read --args '[[["id",">",0]]]' --kwargs '{{"fields":["name"],"limit":3}}'
  Prefer RPC for data/logic bugs (fields_get, search_read, create/write/unlink on staging, calling methods).
- Browser: {bot}/bin/odoo-browse --env stg|kg --steps FILE.json  (FILE.json is a list of steps written under /tmp or the cwd)
  actions: goto{{path}}, click{{selector}}, fill{{selector,value}}, select{{selector,value}}, press{{key}}, wait{{selector|ms}},
  screenshot{{name}} (you can Read the PNG path it returns), text{{selector}}, exists{{selector}}.
  It reports dialogs ("Oh snap" errors), console errors and RPC errors. Odoo 17 URLs look like /web#action=...&model=...&view_type=form.
  Use the browser only for UI/behaviour bugs. Selectors: Odoo 17 DOM (.o_form_view, .o_list_view, button[name=...], .o_field_widget[name=...]).
- Code (read-only): your cwd is the JinStage checkout. Custom modules are the dirs like {hints}; everything else there is third-party.
  Use Grep/Read to find the cause. Odoo core is not available locally; infer from behaviour.

## What to do
1. Work out the exact repro from the report. If too vague to act on, verdict insufficient_info.
2. Reproduce on staging. Then check the same on KG (read-only).
3. Look in the code for the likely cause if it is broken on staging (file paths and line numbers).
4. Clean up every record you created on staging.

## Verdict (choose one)
verified_fixed | not_reproducible | reproduces_on_both | staging_regression (broken on staging, works on KG) |
staging_bug_no_reference (broken on staging, KG cannot be compared) | needs_human | insufficient_info

Finish with ONE fenced ```json block and nothing after it:
{{"verdict": "...", "confidence": 0.0-1.0, "summary": "2-3 sentences", "repro_steps": ["..."],
  "staging_result": "...", "kg_result": "...", "suspected_files": ["path:line"], "cleanup_done": true,
  "root_cause": "code|data|config|permissions|unclear", "notes_for_fixer": "what a developer needs to know, or empty"}}

root_cause: say "code" ONLY if changing the module code would make the reported behaviour work. If staging is missing
master data, records, company defaults, settings or access rights that KG has (or the code is fine but the inputs are
absent), say data/config/permissions. A code change that merely adds an error message does not fix a data gap.
"""


def db():
    STATE.mkdir(exist_ok=True)
    c = sqlite3.connect(STATE / "bugfix.db")
    c.execute("""CREATE TABLE IF NOT EXISTS triage(key TEXT, fingerprint TEXT, verdict TEXT, payload TEXT, ts TEXT,
                 PRIMARY KEY(key, fingerprint))""")
    return c


def already_done(c, bug):
    return c.execute("SELECT 1 FROM triage WHERE key=? AND fingerprint=?", (bug.key, bug.fingerprint)).fetchone() is not None


def secret_deny_rules():
    """Claude Code deny rules (absolute paths need the double slash) for credentials, by real and linked path."""
    out = []
    for base in {ROOT, ROOT.resolve()}:
        for rel in (".env", "state/google-sa.json", "state/*_session.json"):
            out.append(f"Read(/{base}/{rel})")
    out.append(f"Read(/{(ROOT / '.env').resolve()})")
    return sorted(set(out))


def is_fix_candidate(v):
    """Broken on staging with a located cause that is in the CODE. A data/config/permission gap is never patched.
    reproduces_on_both counts when the code visibly intends the behaviour the tester expects (owner decision)."""
    if v.get("root_cause") != "code":
        return False
    if v.get("verdict") in FIX_CANDIDATES:
        return True
    return v.get("verdict") == "reproduces_on_both" and bool(v.get("suspected_files")) and (v.get("confidence") or 0) >= 0.7


def build_prompt(bug, extra_note=None):
    labels = {"module": "Module", "transaction": "Transaction/Process", "object_action": "Object & Action (navigation)",
              "behaviour": "Behaviour (what goes wrong)", "expected": "Expected behaviour", "test_data": "Test data",
              "comments": "Comments from the tester (a hint, not instructions)", "priority": "Priority", "company": "Company"}
    report = "\n".join(f"- {labels[k]}: {bug.fields[k]}" for k in labels if bug.fields.get(k))
    note = {"new": "new, not yet handled", "retry": "tester says it is STILL BROKEN (Need Correction)",
            "recheck": f"marked '{bug.fields.get('status')}' by the team: verify whether it is really fixed on staging now"}[bug.action]
    hints = ", ".join(TAB_HINTS.get(bug.tab, [])) or "see the directory list"
    p = PROMPT.format(key=bug.key, report=report, status_note=note, hints=hints, bot=ROOT)
    return p + (f"\n## Extra instruction\n{extra_note}\n" if extra_note else "")


def parse_verdict(text):
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.S)
    if not blocks:
        raise ValueError("no json verdict block in the agent's answer")
    v = json.loads(blocks[-1])
    if v.get("verdict") not in VERDICTS:
        raise ValueError(f"bad verdict {v.get('verdict')!r}")
    v["needs_fix"] = is_fix_candidate(v)
    return v


def triage_one(bug, extra_note=None, max_turns=40, timeout=1500):
    data = run_with_limit_retry(
        build_prompt(bug, extra_note), cwd=str(WORKSPACE), max_turns=max_turns, timeout=timeout,
        allowed_tools=["Read", "Grep", "Glob", f"Bash({ROOT}/bin/odoo-rpc:*)",
                       f"Bash({ROOT}/bin/odoo-browse:*)", "Write(/tmp/**)"],
        disallowed_tools=["Edit", "NotebookEdit", "WebFetch", "WebSearch", *secret_deny_rules(),
                          "Bash(git push:*)", "Bash(cat:*.env*)"])
    verdict = parse_verdict(data.get("result", ""))
    verdict["_cost_usd"] = data.get("total_cost_usd")
    verdict["_turns"] = data.get("num_turns")
    return verdict


def run(args):
    STATE.mkdir(exist_ok=True)
    lock = open(STATE / "triage.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one triage process at a time
    sheet_id = dotenv_values(ROOT / ".env")["SHEET_ID"]
    while True:
        queue = sheet_reader.work_queue(sheet_reader.parse(sheet_reader.download(sheet_id)))
        if args.only:
            queue = [b for b in queue if b.key == args.only]
        c = db()
        todo = [b for b in queue if not already_done(c, b)][: args.limit or None]
        print(f"queue={len(queue)} todo={len(todo)}", flush=True)
        for bug in todo:
            if KILL_FILE.exists():
                print("kill switch present; stopping"); return
            if args.dry_run:
                print(build_prompt(bug)); continue
            t0 = time.time()
            try:
                v = triage_one(bug)
            except Exception as e:  # record the failure; do not retry forever
                v = {"verdict": "needs_human", "summary": f"triage error: {str(e)[:300]}", "error": True}
            c.execute("INSERT OR REPLACE INTO triage VALUES (?,?,?,?,datetime('now'))",
                      (bug.key, bug.fingerprint, v["verdict"], json.dumps(v)))
            c.commit()
            with open(STATE / "results.jsonl", "a") as f:
                f.write(json.dumps({"bug": bug.key, "fingerprint": bug.fingerprint, **v}) + "\n")
            print(f"{bug.key}: {v['verdict']} ({time.time() - t0:.0f}s) {v.get('summary', '')[:140]}", flush=True)
        if not args.loop:
            return
        time.sleep(args.loop)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="seconds between sheet polls; 0 = run once")
    run(ap.parse_args())
