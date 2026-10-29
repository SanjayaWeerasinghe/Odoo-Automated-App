#!/usr/bin/env bash
# Build the runnable, source-free copy of the agent: bytecode only (no .py), plus bin/ and config/.
# Runtime data (.env, state/, workspaces/, .venv) is linked in from the source tree's data dir, never copied.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HOME/bugfix-agent-build}"
DATA="${BUGFIX_DATA:-$HOME/bugfix-data}"
rm -rf "$OUT.new" && mkdir -p "$OUT.new" "$DATA"
cp -r "$SRC/bugfix" "$SRC/bin" "$SRC/config" "$OUT.new/"
find "$OUT.new" -name __pycache__ -prune -exec rm -rf {} +
"$DATA/.venv/bin/python" -m compileall -q -b "$OUT.new/bugfix"
find "$OUT.new/bugfix" -name '*.py' -delete
for d in state workspaces reports; do mkdir -p "$DATA/$d"; ln -sfn "$DATA/$d" "$OUT.new/$d"; done
ln -sfn "$DATA/.env" "$OUT.new/.env"; ln -sfn "$DATA/.venv" "$OUT.new/.venv"
rm -rf "$OUT" && mv "$OUT.new" "$OUT"
echo "built $OUT (git $(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo uncommitted))"
