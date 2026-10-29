# Odoo-Automated-App: scheduled Odoo bug-fixing bot (Claude cloud routine)

A scheduled Claude Code routine (runs in Claude's cloud sandbox, no server of ours) that reads bug reports from a Google Sheet,
reproduces them on an Odoo **staging** env, compares with a **known-good (KG)** env (read-only), and, for genuine code bugs,
asks the owner what to do before changing anything.

```
sheet row -> triage (staging vs KG)  ->  code bug?  -> PLAN issue on GitHub  --owner replies "approve"-->  edit in worktree
          -> botctl check -> PULL REQUEST  --owner merges (approval)-->  Odoo.sh deploys  -> next run verifies / opens revert PR
```

## Two human gates, nothing deploys on its own
1. **Plan**: the bot opens a `bot-plan` issue (evidence, files, proposed change, risk). It touches no code. The owner comments
   `approve` (optionally with instructions), `decline`, or other instructions. Only comments by `GIT_APPROVERS` count.
2. **Pull request**: after approval the bot edits the code and opens a PR into `Upgrade_Testing`. It never merges or pushes to that
   branch (`push_branch` refuses it). Merging the PR is the owner's approval and starts the deploy.
Recommended GitHub setting: protect `Upgrade_Testing` (require a pull request and the owner's review) so even the token cannot bypass this.

## Other rules enforced in code
| Rule | Where |
|---|---|
| KG is never written: RPC read allowlist, browser writes aborted, bus/websocket blocked, session reused | `bugfix/odoo/kg_readonly.py`, `playwright_runner.py` |
| Only the 20 custom module folders may change (diff checked against `config/allowed_paths.json`) | `bugfix/git_guard.py`, `bugfix/checks.py` |
| Staging config is protected (`ir.*`, `res.users`, `res.company`...) | `bugfix/tools/rpc.py` |
| Limits: 15 files / 500 lines, manifest version bump, AST/XML/pyflakes checks | `bugfix/checks.py` |
| Data/config gaps are never patched in code (`root_cause` must be `code`) | `routine/PROMPT.md` |
| Memory is the sheet: a `BOT ... [fp:<hash>]` note per row; the row is skipped while the hash matches the report text | `bin/botctl queue` |

## Layout
```
routine/PROMPT.md       the instructions the scheduled agent follows (versioned here)
bin/botctl              queue, note, clone, worktree, check, plan, plans, propose, prs, wait-deploy, comment-pr, propose-revert
bin/odoo-rpc, odoo-browse   Odoo tools (staging read/write guarded, KG read-only)
bugfix/                 odoo/ (RPC, Playwright), intake/ (sheet read/write), checks.py, git_ops.py, git_guard.py, tools/
config/allowed_paths.json   the editable module folders
tests/                  guard tests
```

## Setting up the routine
1. Create dedicated least-privilege accounts: a bot user on staging, a **read-only** user on KG (not a personal admin login), and a
   fine-grained GitHub token for `rohanabalagalla/JinStage` only (contents, pull requests, issues: read/write; expiring).
2. Put the variables from `.env.example` into the routine's **environment settings** on claude.ai (never in a prompt, never in git).
3. Create the routine with this repo as its source and the prompt: "Read routine/PROMPT.md and follow it exactly."
   Schedule: hourly at the fastest (cloud routines have a one-hour minimum interval).
4. Revoke any credential that was ever pasted into a chat.

## Known limits
- The cloud agent can read the environment it runs in; least-privilege credentials are the real protection.
- No local Odoo Enterprise, so fixes are checked by static checks and by the post-merge re-test on staging.
- Each run is time-bounded (3 rows, 2 implementations) and starts empty; the sheet and GitHub hold all state.
- Bugs that need a save to reproduce can only be reproduced on staging.
- Proposal branches (`autofix/*`) may trigger Odoo.sh dev builds.
- `bugfix/changes_report.py` keeps the history of past failures (F01-F25); its live section only works if a local `state/bugfix.db` exists.
