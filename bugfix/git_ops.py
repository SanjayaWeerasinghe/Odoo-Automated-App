"""Git plumbing for JinStage/Upgrade_Testing. The token is passed per command, never stored in git config."""
import base64
import os
import subprocess
from pathlib import Path

from bugfix import envcfg

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(os.environ.get("JINSTAGE_DIR") or ROOT / "workspaces" / "JinStage")
REMOTE = "https://github.com/rohanabalagalla/JinStage.git"
BRANCH = "Upgrade_Testing"
AUTHOR = ["-c", "user.name=bugfix-agent", "-c", "user.email=bugfix-agent@users.noreply.github.com"]
TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


def _hdr():
    tok = envcfg.load()["GIT_TOKEN"]
    return ["-c", "http.extraHeader=Authorization: Basic " + base64.b64encode(f"x-access-token:{tok}".encode()).decode()], tok


def git(repo, *args, auth=False, check=True):
    pre, tok = _hdr() if auth else ([], None)
    p = subprocess.run(["git", *pre, *AUTHOR, "-C", str(repo), *args], capture_output=True, text=True)
    out = ((p.stdout or "") + (p.stderr or "")).replace(tok, "***") if tok else (p.stdout or "") + (p.stderr or "")
    if check and p.returncode:
        raise RuntimeError(f"git {args[0]} failed: {out.strip()[-500:]}")
    return out.strip()


def fresh_worktree(slug):
    """New branch off the latest remote Upgrade_Testing, in its own directory."""
    git(BASE, "fetch", "origin", BRANCH, auth=True)
    wt = BASE.parent / f"fix_{slug}"
    if wt.exists():
        git(BASE, "worktree", "remove", "--force", str(wt), check=False)
    git(BASE, "branch", "-D", f"autofix/{slug}", check=False)
    git(BASE, "worktree", "add", "-b", f"autofix/{slug}", str(wt), f"origin/{BRANCH}")
    return wt


def commit_all(wt, message):
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", f"{message}\n\n{TRAILER}")
    return git(wt, "rev-parse", "HEAD")


def push_branch(wt, branch):
    """Push a proposal branch. The deploy branch is never pushed to: a human merges the pull request (approval)."""
    if not branch.startswith("autofix/") or branch == BRANCH:
        raise RuntimeError(f"refusing to push to {branch!r}; only autofix/* proposal branches")
    git(wt, "push", "origin", f"HEAD:refs/heads/{branch}", auth=True)
    return git(wt, "rev-parse", "HEAD")


def prepare_revert(wt, sha):
    """Branch with a revert of one of our own merged commits, ready for a revert pull request."""
    git(wt, "fetch", "origin", BRANCH, auth=True)
    branch = f"autofix/revert-{sha[:8]}"
    git(wt, "checkout", "-q", "-B", branch, f"origin/{BRANCH}")
    git(wt, "revert", "--no-edit", sha)
    git(wt, "commit", "--amend", "-q", "-m", f"Upgrade_Testing: revert autofix {sha[:8]} (failed verification)\n\n{TRAILER}")
    return branch


def _api(method, path, body=None):
    import requests
    tok = envcfg.load()["GIT_TOKEN"]
    r = requests.request(method, f"https://api.github.com/repos/rohanabalagalla/JinStage{path}", json=body, timeout=60,
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub {method} {path}: {r.status_code} {r.text[:300]}".replace(tok, "***"))
    return r.json()


def open_pr(branch, title, body):
    pr = _api("POST", "/pulls", {"title": title, "head": branch, "base": BRANCH, "body": body, "maintainer_can_modify": False})
    return {"number": pr["number"], "url": pr["html_url"]}


def list_autofix_prs():
    out = []
    for pr in _api("GET", "/pulls?state=all&per_page=50&sort=updated&direction=desc"):
        if pr["head"]["ref"].startswith("autofix/"):
            comments = [c["body"] for c in _api("GET", f"/issues/{pr['number']}/comments?per_page=100")]
            out.append({"number": pr["number"], "url": pr["html_url"], "title": pr["title"], "branch": pr["head"]["ref"],
                        "state": pr["state"], "merged": bool(pr.get("merged_at")), "merge_sha": pr.get("merge_commit_sha"),
                        "verified": any(c.startswith("BOT VERIFIED") or c.startswith("BOT VERIFY FAILED") for c in comments)})
    return out


def approvers():
    """GitHub logins whose comments count as the owner's decision (anyone else's comments are ignored)."""
    return {x.strip().lower() for x in (envcfg.load().get("GIT_APPROVERS") or "").split(",") if x.strip()}


def plan_status(comments, allowed):
    """comments: [{'user','body'}] oldest first. Decision = the latest comment by an approver.
    'approve...' -> approved (the rest of the comment is the owner's instruction); 'decline|reject|no' -> declined;
    any other approver comment -> 'instructions' (bot re-plans, does not edit). A BOT IMPLEMENTED marker -> implemented."""
    if any(c["body"].startswith("BOT IMPLEMENTED") for c in comments):
        return "implemented", None
    for c in reversed(comments):
        if c["user"].lower() not in allowed:
            continue
        body = c["body"].strip()
        low = body.lower()
        if low.startswith("approve"):
            return "approved", body[len("approve"):].strip(" :,-\n")
        if low.split(None, 1)[0].strip(".,!:") in ("decline", "reject", "no", "declined", "rejected"):
            return "declined", None
        return "instructions", body
    return "awaiting", None


def open_plan(title, body):
    i = _api("POST", "/issues", {"title": title, "body": body, "labels": ["bot-plan"]})
    return {"number": i["number"], "url": i["html_url"]}


def list_plans():
    ok = approvers()
    out = []
    for i in _api("GET", "/issues?labels=bot-plan&state=open&per_page=50"):
        cs = [{"user": c["user"]["login"], "body": c["body"]} for c in _api("GET", f"/issues/{i['number']}/comments?per_page=100")]
        status, instruction = plan_status(cs, ok)
        out.append({"number": i["number"], "url": i["html_url"], "title": i["title"], "plan": i["body"],
                    "status": status, "owner_instruction": instruction})
    return out


def close_issue(number):
    _api("PATCH", f"/issues/{number}", {"state": "closed"})


def comment_pr(number, text):
    _api("POST", f"/issues/{number}/comments", {"body": text})


def remove_worktree(wt):
    git(BASE, "worktree", "remove", "--force", str(wt), check=False)
