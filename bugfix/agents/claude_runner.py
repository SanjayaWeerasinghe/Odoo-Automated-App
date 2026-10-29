"""Run Claude via the headless Claude Code CLI on the Team-plan seat (no API billing)."""
import json
import os
import re
import subprocess
import time

BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX")
LIMIT_RE = re.compile(r"usage limit|limit reached|rate limit|resets? (at|in)", re.I)


class UsageLimit(Exception):
    def __init__(self, wait_s: int):
        super().__init__(f"usage limit hit; wait {wait_s}s")
        self.wait_s = wait_s


def assert_subscription_billing(env=os.environ):
    bad = [v for v in BILLING_VARS if env.get(v)]
    if bad:
        raise RuntimeError(f"{bad} set: Claude Code would bill the API instead of the Team plan. Unset them.")


def _wait_from(text: str) -> int:
    m = re.search(r"in (\d+)\s*(hour|hr|minute|min)", text, re.I)
    if m:
        n = int(m.group(1))
        return n * (3600 if m.group(2).lower().startswith("h") else 60) + 60
    return 30 * 60  # unknown reset time: retry in 30 min


def run_claude(prompt: str, cwd: str, *, model: str | None = None, max_turns: int = 30,
               allowed_tools: list[str] | None = None, disallowed_tools: list[str] | None = None,
               timeout: int = 1800, extra_env: dict | None = None, permission_mode: str | None = None) -> dict:
    assert_subscription_billing()
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    if disallowed_tools:
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    env = {k: v for k, v in os.environ.items()
           if k not in BILLING_VARS and not (k.startswith("CLAUDE_CODE_") and k != "CLAUDE_CODE_OAUTH_TOKEN")}
    env.update(extra_env or {})
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = {"result": p.stdout, "is_error": p.returncode != 0}
    if data.get("is_error") and LIMIT_RE.search(out):
        raise UsageLimit(_wait_from(out))
    return data


def run_with_limit_retry(*a, max_waits: int = 6, sleep=time.sleep, **kw) -> dict:
    for _ in range(max_waits):
        try:
            return run_claude(*a, **kw)
        except UsageLimit as e:
            sleep(e.wait_s)
    raise RuntimeError("gave up waiting for Team-plan usage window to reset")
