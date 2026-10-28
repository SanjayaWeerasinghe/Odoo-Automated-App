"""Read/write Odoo RPC for the STAGING env only. Refuses to run against the KG URL."""
import requests

from .kg_readonly import KGReadOnly


class StagingRPC(KGReadOnly):
    def __init__(self, url, db, login, secret, kg_url, timeout=60):
        if url.rstrip("/") == kg_url.rstrip("/"):
            raise RuntimeError("StagingRPC pointed at the KG URL; refusing")
        super().__init__(url, db, login, secret, timeout)

    def execute_kw(self, model, method, args=None, kwargs=None):  # no allowlist: staging may be written
        return self._rpc("object", "execute_kw", self._db, self._auth(), self._secret,
                         model, method, args or [], kwargs or {})

    def create(self, model, vals, **ctx):
        return self.execute_kw(model, "create", [vals], {"context": ctx} if ctx else {})

    def unlink(self, model, ids):
        return self.execute_kw(model, "unlink", [ids])
