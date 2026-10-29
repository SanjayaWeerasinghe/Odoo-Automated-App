# Odoo-Automated-App: autonomous Odoo bug-fixing bot

A daemon that runs unattended, driven by Claude (headless Claude Code CLI on a Team-plan seat, no API billing).

```
Google Sheet (bug rows)
   -> triage: reproduce on STAGING, compare with KNOWN-GOOD (read-only)      [bugfix/triage.py]
   -> verdict: verified_fixed | not_reproducible | reproduces_on_both | staging_regression | ...
   -> fix (only genuine staging bugs): edit custom modules, push to JinStage  [bugfix/fix.py]
   -> wait for Odoo.sh redeploy, re-verify on staging, revert the commit if it fails
   -> write a one-line "BOT ..." note back to the sheet + state/REPORT.md
```

## Hard rules enforced in code (not just prompts)
| Rule | Where |
|---|---|
| **Known-Good Odoo is never written.** RPC allowlist of read methods; browser requests that are not reads are aborted; bus/websocket/presence blocked; login happens once and the session is reused (no repeated login-log rows). | `bugfix/odoo/kg_readonly.py`, `playwright_runner.py` |
| **Only the 20 custom module folders may change.** The post-run `git diff` is checked against `config/allowed_paths.json`; third-party folders are untouchable. | `bugfix/git_guard.py` |
| **Staging config is protected.** Writes to `ir.*`, `res.users`, `res.company` and similar are refused; test records must be cleaned up. | `bugfix/tools/rpc.py` |
| **Team-plan billing only.** Refuses to start if `ANTHROPIC_API_KEY` (or Bedrock/Vertex vars) is set; sleeps through usage-limit windows. | `bugfix/agents/claude_runner.py` |
| **Fixes are small and safe.** Max 15 files / 500 lines, manifest version bump required, Python AST / XML / pyflakes checks, fast-forward-only push (never force), failed verification triggers an automatic revert commit. | `bugfix/fix.py`, `bugfix/git_ops.py` |
| **Circuit breaker + kill switch.** 3 bad outcomes in a row, or a `state/STOP` file, halts the daemon. | `bugfix/daemon.py` |
| **Secrets stay out of Claude's reach.** Credentials live in `.env`; Claude runs have deny rules for `.env` and saved sessions; every tool call is logged to `state/audit.jsonl`. | `bugfix/triage.py` |

## Layout
```
bugfix/
  daemon.py          main loop: poll sheet, consensus triage, fix, sheet note, resume pending verifications
  triage.py          triage prompt, verdict parsing, sqlite results (2-of-3 consensus lives in daemon)
  fix.py             fix prompt, static checks, deploy wait, verify/revert
  git_ops.py         worktrees, commit, fast-forward push, revert (token passed per command, never stored)
  git_guard.py       path allowlist check
  changes_report.py  builds reports/changes_report.pdf (every failure and why)
  intake/            sheet_reader.py (public xlsx export), sheet_writer.py (anonymous cell editor, "Retest Status" column only)
  odoo/              kg_readonly.py, rpc.py (staging), playwright_runner.py
  tools/             CLIs Claude calls: bin/odoo-rpc, bin/odoo-browse (+ audit log)
  agents/            claude_runner.py (headless Claude Code)
config/allowed_paths.json   the 20 editable module dirs and their manifests
scripts/build.sh            builds the source-free runtime copy
tests/                      guard tests (KG read-only, path guard, parsing, lint)
```

## Sheet statuses
blank = new, `Need Correction` = retry, `Correction Done` / `Completed - New` = re-check, `Completed` = skipped.
The bot only writes to the "Retest Status" column, and only into empty cells or cells that already start with `BOT`.

## Setup
1. Python 3.12: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium`
2. `claude` CLI logged in on the Team seat (`claude /login`). Do **not** export `ANTHROPIC_API_KEY`.
3. `cp .env.example .env && chmod 600 .env`, then fill in: sheet id, staging and KG URL/DB/login (and `KG_UID`/`STG_UID` to avoid login-log writes), `GIT_TOKEN` (needs write on the JinStage repo), `GIT_REMOTE`, `GIT_BRANCH=Upgrade_Testing`.
4. Clone the JinStage branch into `workspaces/JinStage` (the triage agent reads it).
5. `.venv/bin/python -m pytest` (17 tests, all must pass).

## Running without source on the server
```
scripts/build.sh            # bytecode-only copy in ~/bugfix-agent-build; data dir ~/bugfix-data (.env, state, workspaces, .venv)
cd ~/bugfix-agent-build && pm2 start .venv/bin/python --name bugfix-daemon --cwd $PWD -- -m bugfix.daemon && pm2 save
```
Keep the source in git only; to change the bot, clone, edit, run `scripts/build.sh`, `pm2 restart bugfix-daemon`.
Bytecode is not protection against decompiling; it only avoids leaving plain source on disk.

## Operating it
- Progress: `pm2 logs bugfix-daemon`, `state/daemon.log`, `state/REPORT.md`, `state/results.jsonl`, sqlite `state/bugfix.db`.
- Stop: `touch state/STOP` (remove to resume). Reboot persistence needs `pm2 startup` (sudo) or an `@reboot pm2 resurrect` cron entry.
- Restart safe: a fix pushed but not yet verified is stored as `pending_verify` and finished on startup.
- Changes report (changes, failures and why): `.venv/bin/python -m bugfix.changes_report` writes `reports/changes_report.pdf`.

## Known limits
- No local Odoo Enterprise, so fixes are checked by lint and by post-deploy re-test on staging, not by unit tests.
- A fix can alter accounting data logic (e.g. posted journal lines); review bot commits (`autofix` in the message) regularly.
- Bugs that need a save to reproduce can only be reproduced on staging; KG is compared by reading equivalent records and views.
- Throughput is bounded by the Team plan's rolling usage windows.
- Rotate any token or password that has ever been pasted into chat.
