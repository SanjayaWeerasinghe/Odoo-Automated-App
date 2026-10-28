"""Path allowlist: only our custom module directories may be changed in JinStage.

Everything else in the branch is third-party and must never be touched.
"""
import json
import subprocess
from pathlib import Path, PurePosixPath


class PathViolation(Exception):
    pass


def load_allowed(path: str | Path) -> list[str]:
    dirs = json.loads(Path(path).read_text())["allowed_dirs"]
    if not dirs:
        raise ValueError("allowed_paths.json has no allowed_dirs; refusing to run")
    return [d.strip("/") for d in dirs]


def _inside(p: str, allowed: list[str]) -> bool:
    pp = PurePosixPath(p)
    if ".." in pp.parts or pp.is_absolute():
        return False
    return any(pp == PurePosixPath(a) or PurePosixPath(a) in pp.parents for a in allowed)


def violations(changed_paths: list[str], allowed: list[str]) -> list[str]:
    return [p for p in changed_paths if not _inside(p, allowed)]


def assert_diff_allowed(repo: str | Path, base: str, allowed: list[str]) -> None:
    """Raise if the working tree/commits since `base` touch anything outside allowed."""
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-status", "-M", base],
        check=True, capture_output=True, text=True).stdout
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
        check=True, capture_output=True, text=True).stdout.split()
    paths = list(untracked)
    for line in out.splitlines():
        paths.extend(line.split("\t")[1:])  # includes both sides of renames
    bad = violations(paths, allowed)
    if bad:
        raise PathViolation(f"changes outside allowed dirs: {bad[:10]}")
