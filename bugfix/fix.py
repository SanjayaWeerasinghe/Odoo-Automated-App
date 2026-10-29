"""Fix stage: edit only allowed module dirs, statically check, push to Upgrade_Testing, verify on staging, auto-revert on failure."""
import ast
import json
import re
import time
from pathlib import Path

from lxml import etree

from bugfix import git_ops
from bugfix.agents.claude_runner import run_with_limit_retry
from bugfix.git_guard import assert_diff_allowed, load_allowed
from bugfix.odoo.rpc import StagingRPC
from bugfix.triage import secret_deny_rules

ROOT = git_ops.ROOT
MAX_FILES, MAX_LINES = 15, 500

FIX_PROMPT = """You are fixing ONE bug in custom Odoo 17 Enterprise modules. The triage below was already done; trust it but verify in the code.

## Bug ({key})
{report}

## Triage findings
{triage}

## Rules (enforced by tooling; violations abort the fix)
- You may edit ONLY inside these module directories (cwd is the repo root): {allowed}. Everything else is third-party: never touch it.
- Make the smallest correct change. No refactors, no formatting churn, no new dependencies, do not delete files.
- Bump the last number of `version` in the manifest of each module you change (e.g. 17.0.1.0.377 -> 17.0.1.0.378).
- If the fix needs data/schema changes, add them in the module's own XML/data or migrations the way the module already does.
- You cannot run Odoo locally (Enterprise is not available). Reason carefully, keep Python syntax and XML valid;
  syntax is checked after you finish. The fix is then deployed to staging and re-tested automatically.
- If you conclude this is NOT a code bug (data/config/permissions) or needs a business decision, change nothing and say so.
- A change that only adds an error message or guard, without making the reported behaviour actually work, is NOT a fix:
  change nothing. Past attempts of that kind failed verification and were reverted.

Finish with ONE fenced ```json block: {{"changed": true|false, "modules": ["dir"], "commit_title": "short imperative summary", "explanation": "what and why", "risk": "low|medium|high"}}
"""


def _pyflakes(code, name):
    import subprocess, sys, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
    out = subprocess.run([sys.executable, "-m", "pyflakes", f.name], capture_output=True, text=True).stdout
    Path(f.name).unlink()
    # keep real defects only; drop line numbers so base/new messages can be compared
    return {re.sub(r"^.*?:\d+:(\d+:)?\s*", "", l) for l in out.splitlines()
            if "undefined name" in l or "syntax" in l.lower() or "redefinition of unused" in l and False}


def lint_new_defects(wt, rel):
    new = _pyflakes((Path(wt) / rel).read_text(), rel)
    try:
        base = _pyflakes(git_ops.git(wt, "show", f"HEAD:{rel}"), rel)
    except RuntimeError:
        base = set()  # new file
    return sorted(new - base)


def static_checks(wt, changed):
    for rel in changed:
        if rel.endswith(".py") and (Path(wt) / rel).exists():
            bad = lint_new_defects(wt, rel)
            if bad:
                raise ValueError(f"new defects in {rel}: {bad[:3]}")
        p = Path(wt) / rel
        if not p.exists():
            continue
        if rel.endswith(".py"):
            ast.parse(p.read_text(), filename=rel)
        elif rel.endswith(".xml"):
            etree.parse(str(p))
        elif rel.endswith("__manifest__.py"):
            ast.literal_eval(p.read_text())


def changed_files(wt):
    """Modified/deleted/renamed tracked files plus untracked ones, as repo-relative paths."""
    tracked = git_ops.git(wt, "diff", "--name-only", "HEAD").splitlines()
    untracked = git_ops.git(wt, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({p for p in tracked + untracked if p})


def numstat(wt):
    n = 0
    for l in git_ops.git(wt, "diff", "--numstat", "HEAD").splitlines():
        a, d, _ = l.split("\t", 2)
        n += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return n


def manifest_versions(wt, dirs):
    out = {}
    for d in dirs:
        m = re.search(r"""['"]version['"]\s*:\s*['"]([^'"]+)""", (Path(wt) / d / "__manifest__.py").read_text())
        out[d] = m.group(1) if m else None
    return out


def wait_for_deploy(cfg, module_name, version, timeout=1800, poll=45):
    """Poll staging until the module reports the new version installed (Odoo.sh rebuild finished)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = StagingRPC(cfg["STG_URL"], cfg["STG_DB"], cfg["STG_LOGIN"], cfg["STG_SECRET"], cfg["KG_URL"])
            r = s.search_read("ir.module.module", [["name", "=", module_name]], ["installed_version", "state"], limit=1)
            if r and r[0]["installed_version"] == version and r[0]["state"] == "installed":
                return True
        except Exception:
            pass  # build restarting
        time.sleep(poll)
    return False


def finish_fix(bug, st, cfg, verify_fn):
    """After a push: wait for the staging rebuild, re-test, and revert our own commit if the re-test fails.
    Needs only the persisted state, so it can resume after a crash or restart."""
    out = {"stage": "verify", **st}
    out["deployed"] = wait_for_deploy(cfg, st["module"], st["version"])
    if not out["deployed"]:
        out["outcome"] = "pushed_deploy_unconfirmed"
        return out
    v = verify_fn(bug, f"A fix ({st['sha'][:8]}) was just deployed to staging: {st['title']}. "
                       "Verify ONLY whether the reported behaviour is now correct.")
    out["verify"] = v
    if v.get("verdict") == "verified_fixed":
        out["outcome"] = "fixed_verified"
        return out
    wt = git_ops.fresh_worktree("revert-" + st["sha"][:8])
    try:
        out.update(outcome="reverted_after_failed_verify", revert_sha=git_ops.revert_and_push(wt, st["sha"]))
    finally:
        git_ops.remove_worktree(wt)
    return out


def attempt_fix(bug, triage_payload, build_prompt_report, cfg, verify_fn, on_push=None):
    """Returns a dict describing the outcome. verify_fn(bug, extra_note) -> triage verdict dict."""
    from bugfix.triage import FIX_CANDIDATES  # noqa: F401  (kept for clarity of the flow)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", bug.key).strip("-") + f"-{int(time.time())}"
    allowed = load_allowed(ROOT / "config" / "allowed_paths.json")
    wt = git_ops.fresh_worktree(slug)
    out = {"bug": bug.key, "stage": "fix"}
    try:
        base = git_ops.git(wt, "rev-parse", "HEAD")
        data = run_with_limit_retry(
            FIX_PROMPT.format(key=bug.key, report=build_prompt_report, allowed=", ".join(allowed),
                              triage=json.dumps(triage_payload, indent=1)[:6000]),
            cwd=str(wt), max_turns=60, timeout=2400,
            permission_mode="acceptEdits",  # edits auto-approved inside the worktree only; assert_diff_allowed is the hard guard
            allowed_tools=["Read", "Grep", "Glob", "Edit", "Write", "Bash(git diff:*)", "Bash(git status:*)"],
            disallowed_tools=["NotebookEdit", "WebFetch", "WebSearch", "Bash(git push:*)", "Bash(git commit:*)",
                              *secret_deny_rules()])
        m = re.findall(r"```json\s*(\{.*?\})\s*```", data.get("result", ""), re.S)
        summary = json.loads(m[-1]) if m else {}
        files = changed_files(wt)
        if not files or not summary.get("changed"):
            return {**out, "outcome": "no_change", "explanation": summary.get("explanation", "agent made no change")}
        assert_diff_allowed(wt, base, allowed)                       # hard path guard
        if len(files) > MAX_FILES or numstat(wt) > MAX_LINES:
            return {**out, "outcome": "too_large", "files": files[:20]}
        static_checks(wt, files)
        dirs = sorted({f.split("/")[0] for f in files})
        before = manifest_versions(git_ops.BASE, dirs)
        after = manifest_versions(wt, dirs)
        if any(before[d] == after[d] for d in dirs):
            return {**out, "outcome": "missing_version_bump", "files": files}
        mod_dir = dirs[0]
        title = summary.get("commit_title", "autofix")[:100]
        msg = f"Upgrade_Testing: {mod_dir} {after[mod_dir]} (autofix {bug.key}) - {title}"
        sha = git_ops.commit_all(wt, msg)
        pushed = git_ops.push_ff(wt)
        state = dict(sha=pushed, module=mod_dir, version=after[mod_dir], title=title, commit=msg, files=files,
                     risk=summary.get("risk"), explanation=summary.get("explanation"))
        if on_push:
            on_push(state)          # persisted before the slow deploy wait so a restart can resume
        return {**out, **finish_fix(bug, state, cfg, verify_fn)}
    finally:
        git_ops.remove_worktree(wt)
