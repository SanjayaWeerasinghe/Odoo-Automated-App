"""CLI for the triage agent: run scripted browser steps on stg or kg. Holds the credentials itself.
usage: odoo-browse --env stg|kg --steps steps.json   (or '-' for stdin)"""
import argparse, json, sys, time

from dotenv import dotenv_values

from bugfix.odoo.playwright_runner import Browser
from bugfix.tools import audit

ROOT = audit.ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["stg", "kg"])
    ap.add_argument("--steps", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    steps = json.load(sys.stdin if a.steps == "-" else open(a.steps))
    e = dotenv_values(ROOT / ".env"); t = a.env.upper().replace("STG", "STG")
    out = a.out or str(ROOT / "state" / "runs" / f"{a.env}_{int(time.time())}")
    audit.log(tool="browse", env=a.env, n_steps=len(steps), actions=[s.get("action") for s in steps])
    with Browser(e[f"{t}_URL"], e[f"{t}_DB"], e[f"{t}_LOGIN"], e[f"{t}_SECRET"], out, readonly=(a.env == "kg"), session_file=ROOT / "state" / f"{a.env}_session.json") as b:
        print(json.dumps(b.run_steps(steps), indent=1))


if __name__ == "__main__":
    main()
