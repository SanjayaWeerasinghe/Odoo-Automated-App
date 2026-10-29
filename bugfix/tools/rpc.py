"""CLI for the triage agent: Odoo RPC on stg (read/write, guarded) or kg (read-only).
usage: odoo-rpc --env stg|kg --model M --method search_read --args '[[]]' --kwargs '{"limit":5}'"""
import argparse, json

from bugfix.odoo.kg_readonly import KGReadOnly, KGWriteRefused
from bugfix.odoo.rpc import StagingRPC
from bugfix import envcfg
from bugfix.tools import audit

ROOT = audit.ROOT
WRITE_METHODS = {"create", "write", "unlink", "copy", "toggle_active", "action_archive"}
PROTECTED_PREFIXES = ("ir.", "res.users", "res.company", "res.config", "base.", "mail.template")  # no config edits via the agent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["stg", "kg"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--method", required=True)
    ap.add_argument("--args", default="[]")
    ap.add_argument("--kwargs", default="{}")
    a = ap.parse_args()
    e = envcfg.load()
    args, kwargs = json.loads(a.args), json.loads(a.kwargs)
    audit.log(tool="rpc", env=a.env, model=a.model, method=a.method, args=args, kwargs=kwargs)
    try:
        if a.env == "kg":
            cli = KGReadOnly(e["KG_URL"], e["KG_DB"], e["KG_LOGIN"], e["KG_SECRET"], uid=int(e["KG_UID"]) if e.get("KG_UID") else None)
        else:
            if a.method in WRITE_METHODS and a.model.startswith(PROTECTED_PREFIXES):
                raise KGWriteRefused(f"agent may not {a.method} protected model {a.model} on staging")
            cli = StagingRPC(e["STG_URL"], e["STG_DB"], e["STG_LOGIN"], e["STG_SECRET"], e["KG_URL"])
        res = cli.execute_kw(a.model, a.method, args, kwargs)
        print(json.dumps({"ok": True, "result": res}, default=str)[:20000])
    except Exception as ex:
        print(json.dumps({"ok": False, "error": str(ex)[:1500]}))


if __name__ == "__main__":
    main()
