import json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def log(**kw):
    (ROOT / "state").mkdir(exist_ok=True)
    with open(ROOT / "state" / "audit.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw}, default=str)[:3000] + "\n")
