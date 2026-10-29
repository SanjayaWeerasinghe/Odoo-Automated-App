"""One CLI for everything the scheduled cloud run needs besides Odoo RPC/browser. Every subcommand prints JSON.
  queue [--limit N]                     rows still to handle (the sheet's BOT note + [fp:] tag is the only memory)
  note TAB ROW VERDICT SUMMARY --fp F [--outcome O --pr-url U]   write the one-line BOT note into the Retest Status cell
  clone                                 clone/refresh JinStage (Upgrade_Testing) into $JINSTAGE_DIR
  worktree SLUG                         fresh branch off origin/Upgrade_Testing -> path
  check DIR                             path allowlist, size limits, static checks, version bump (must be ok before push)
  plan TITLE BODY                       STEP 1, no code touched: open a "bot-plan" GitHub issue describing the proposed
                                        change and ask the owner what to do
  plans                                 plan issues with status awaiting|approved|declined|instructions|implemented
                                        (only comments by GIT_APPROVERS count) and the owner's instruction
  propose DIR TITLE BODY                commit, push an autofix/* branch and open a pull request. NEVER deploys:
                                        the owner approves by merging the PR
  prs                                   our proposal PRs: open / merged / already verified
  wait-deploy MODULE VERSION            after the owner merged a PR: wait for the Odoo.sh rebuild
  close-plan N                          close a declined or finished plan issue
  comment-pr N TEXT                     record the post-merge verification result on the PR
  propose-revert DIR SHA                open a revert PR for a merged fix that failed verification (also needs approval)
"""
import argparse
import json
import re
import subprocess
import sys

from bugfix import checks, envcfg, git_ops
from bugfix.intake import sheet_reader, sheet_writer
from bugfix.tools import audit

FP_RE = re.compile(r"\[fp:([0-9a-f]+)\]")


def queue(a):
    cfg = envcfg.load()
    bugs = sheet_reader.work_queue(sheet_reader.parse(sheet_reader.download(cfg["SHEET_ID"])))
    out = []
    for b in bugs:
        m = FP_RE.search(b.fields.get("retest") or "")
        if m and m.group(1) == b.fingerprint:
            continue  # already handled for this exact report text
        out.append({"key": b.key, "tab": b.tab, "row": b.row, "action": b.action, "fingerprint": b.fingerprint,
                    "human_retest_text": None if sheet_writer.can_write(b.fields.get("retest")) else b.fields.get("retest"),
                    "fields": b.fields})
    return out[: a.limit or None]


def note(a):
    fix = {"outcome": a.outcome, "pr_url": a.pr_url, "commit": a.summary} if a.outcome else None
    text = sheet_writer.note_for(a.verdict, a.summary, fix, a.fp)
    res = sheet_writer.write_notes(envcfg.load()["SHEET_ID"], [(a.tab, int(a.row), text)])
    return {"result": list(res.values())[0], "text": text}


def clone(a):
    git_ops.BASE.parent.mkdir(parents=True, exist_ok=True)
    if (git_ops.BASE / ".git").exists():
        git_ops.git(git_ops.BASE, "fetch", "origin", git_ops.BRANCH, auth=True)
        git_ops.git(git_ops.BASE, "checkout", "-q", "--detach", f"origin/{git_ops.BRANCH}")
    else:
        pre, tok = git_ops._hdr()
        p = subprocess.run(["git", *pre, "clone", "-q", "--branch", git_ops.BRANCH, "--single-branch", git_ops.REMOTE, str(git_ops.BASE)],
                           capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError((p.stderr or "").replace(tok, "***")[-400:])
    return {"path": str(git_ops.BASE), "head": git_ops.git(git_ops.BASE, "rev-parse", "HEAD")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("queue"); q.add_argument("--limit", type=int, default=0)
    n = sp.add_parser("note")
    for x in ("tab", "row", "verdict", "summary"):
        n.add_argument(x)
    n.add_argument("--fp")
    n.add_argument("--outcome", choices=["proposed", "fixed_verified", "reverted_after_failed_verify"])
    n.add_argument("--pr-url")
    sp.add_parser("clone")
    sp.add_parser("worktree").add_argument("slug")
    sp.add_parser("check").add_argument("dir")
    p = sp.add_parser("propose"); p.add_argument("dir"); p.add_argument("title"); p.add_argument("body")
    pl = sp.add_parser("plan"); pl.add_argument("title"); pl.add_argument("body")
    sp.add_parser("plans")
    ci = sp.add_parser("close-plan"); ci.add_argument("number", type=int)
    sp.add_parser("prs")
    w = sp.add_parser("wait-deploy"); w.add_argument("module"); w.add_argument("version")
    c = sp.add_parser("comment-pr"); c.add_argument("number", type=int); c.add_argument("text")
    r = sp.add_parser("propose-revert"); r.add_argument("dir"); r.add_argument("sha")
    a = ap.parse_args()
    audit.log(tool="botctl", cmd=a.cmd, args={k: v for k, v in vars(a).items() if k not in ("cmd",)})
    try:
        if a.cmd == "queue":
            res = queue(a)
        elif a.cmd == "note":
            res = note(a)
        elif a.cmd == "clone":
            res = clone(a)
        elif a.cmd == "worktree":
            res = {"path": str(git_ops.fresh_worktree(a.slug))}
        elif a.cmd == "check":
            res = checks.precommit(a.dir)
        elif a.cmd == "propose":
            res = checks.precommit(a.dir)
            if res["ok"]:
                msg = f"Upgrade_Testing: {res['module']} {res['version']} (autofix) - {a.title}"
                res["commit"] = git_ops.commit_all(a.dir, msg)
                branch = f"autofix/{re.sub(r'[^A-Za-z0-9]+', '-', a.title).strip('-')[:40].lower()}-{res['commit'][:7]}"
                git_ops.push_branch(a.dir, branch)
                res.update(branch=branch, pr=git_ops.open_pr(branch, msg, a.body + "\n\nProposed by the bug bot. Review the diff; merging this PR approves the change and deploys it to staging."))
        elif a.cmd == "plan":
            res = git_ops.open_plan(a.title, a.body + "\n\n---\nNo code has been changed. Reply on this issue: `approve` (optionally followed by instructions), `decline`, or other instructions to change the plan. Only comments by the owner are read.")
        elif a.cmd == "plans":
            res = git_ops.list_plans()
        elif a.cmd == "close-plan":
            git_ops.close_issue(a.number); res = {"ok": True}
        elif a.cmd == "prs":
            res = git_ops.list_autofix_prs()
        elif a.cmd == "wait-deploy":
            res = {"deployed": checks.wait_for_deploy(envcfg.load(), a.module, a.version)}
        elif a.cmd == "comment-pr":
            git_ops.comment_pr(a.number, a.text); res = {"ok": True}
        elif a.cmd == "propose-revert":
            branch = git_ops.prepare_revert(a.dir, a.sha)
            git_ops.push_branch(a.dir, branch)
            res = {"branch": branch, "pr": git_ops.open_pr(branch, f"Revert autofix {a.sha[:8]} (failed verification)", "The merged fix failed the post-deploy check. Merge to revert.")}
        print(json.dumps(res, default=str, indent=1))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)[:800]}))
        sys.exit(1)


if __name__ == "__main__":
    main()
