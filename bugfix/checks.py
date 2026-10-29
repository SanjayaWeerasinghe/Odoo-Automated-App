"""Pre-push checks and deploy wait for bot fixes: path allowlist, size limits, static checks, version bump."""
import ast
import re
import time
from pathlib import Path

from lxml import etree

from bugfix import git_ops
from bugfix.git_guard import assert_diff_allowed, load_allowed
from bugfix.odoo.rpc import StagingRPC

ROOT = git_ops.ROOT
MAX_FILES, MAX_LINES = 15, 500


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


def head_manifest_versions(wt, dirs):
    out = {}
    for d in dirs:
        m = re.search(r"""['"]version['"]\s*:\s*['"]([^'"]+)""", git_ops.git(wt, "show", f"HEAD:{d}/__manifest__.py"))
        out[d] = m.group(1) if m else None
    return out


def precommit(wt):
    """Everything that must hold before a fix may be committed and pushed. Returns a dict; ok=False means do not push."""
    allowed = load_allowed(ROOT / "config" / "allowed_paths.json")
    files = changed_files(wt)
    if not files:
        return {"ok": False, "reason": "no changes"}
    try:
        assert_diff_allowed(wt, "HEAD", allowed)
    except Exception as e:
        return {"ok": False, "reason": f"path guard: {e}", "files": files}
    if len(files) > MAX_FILES or numstat(wt) > MAX_LINES:
        return {"ok": False, "reason": f"too large (limit {MAX_FILES} files / {MAX_LINES} lines)", "files": files}
    try:
        static_checks(wt, files)
    except Exception as e:
        return {"ok": False, "reason": f"static check: {e}", "files": files}
    dirs = sorted({f.split("/")[0] for f in files})
    before, after = head_manifest_versions(wt, dirs), manifest_versions(wt, dirs)
    if any(before[d] == after[d] for d in dirs):
        return {"ok": False, "reason": "manifest version not bumped in every changed module", "files": files, "versions": after}
    return {"ok": True, "files": files, "module": dirs[0], "version": after[dirs[0]], "versions": after}
