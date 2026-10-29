"""Git plumbing for JinStage/Upgrade_Testing. The token is passed per command, never stored in git config."""
import base64
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "workspaces" / "JinStage"
BRANCH = "Upgrade_Testing"
AUTHOR = ["-c", "user.name=bugfix-agent", "-c", "user.email=bugfix-agent@users.noreply.github.com"]
TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


def _hdr():
    tok = dotenv_values(ROOT / ".env")["GIT_TOKEN"]
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
    wt = ROOT / "workspaces" / f"fix_{slug}"
    if wt.exists():
        git(BASE, "worktree", "remove", "--force", str(wt), check=False)
    git(BASE, "branch", "-D", f"autofix/{slug}", check=False)
    git(BASE, "worktree", "add", "-b", f"autofix/{slug}", str(wt), f"origin/{BRANCH}")
    return wt


def commit_all(wt, message):
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", f"{message}\n\n{TRAILER}")
    return git(wt, "rev-parse", "HEAD")


def push_ff(wt, retries=3):
    """Fast-forward push to Upgrade_Testing; rebase on rejection (the sync job also pushes here). Never forces."""
    for _ in range(retries):
        try:
            git(wt, "push", "origin", f"HEAD:refs/heads/{BRANCH}", auth=True)
            return git(wt, "rev-parse", "HEAD")
        except RuntimeError as e:
            if "rejected" not in str(e) and "fetch first" not in str(e):
                raise
            git(wt, "fetch", "origin", BRANCH, auth=True)
            git(wt, "rebase", f"origin/{BRANCH}")
    raise RuntimeError("push kept being rejected")


def revert_and_push(wt, sha):
    git(wt, "fetch", "origin", BRANCH, auth=True)
    git(wt, "checkout", "-q", "-B", f"autofix/revert-{sha[:8]}", f"origin/{BRANCH}")
    git(wt, "revert", "--no-edit", sha)
    git(wt, "commit", "--amend", "-q", "-m", f"Upgrade_Testing: revert autofix {sha[:8]} (failed verification)\n\n{TRAILER}")
    return push_ff(wt)


def remove_worktree(wt):
    git(BASE, "worktree", "remove", "--force", str(wt), check=False)
