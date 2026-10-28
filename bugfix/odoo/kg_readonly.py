"""Read-only access to the Known-Good Odoo env.

Every KG call goes through here. Writes are refused before any request is
sent, so a model mistake cannot modify KG. The model never sees credentials.
"""
import json
import re
from urllib.parse import urlparse

import requests

READ_METHODS = frozenset({
    "search", "search_read", "read", "search_count", "read_group",
    "fields_get", "name_search", "default_get", "search_fetch", "fetch",
    "web_search_read", "web_read", "web_read_group", "check_access_rights",
    "check_access_rule", "name_get",
})

# Browser POST endpoints that are safe on KG (anything else non-GET is blocked).
BROWSER_POST_PREFIXES = (
    "/web/login",
    "/web/session/authenticate",
    "/web/session/get_session_info",
    "/web/action/load",
    "/web/dataset/search_read",
    "/web/webclient/",
)
CALL_KW_RE = re.compile(r"^/web/dataset/call_kw(?:/[^/]+/(?P<method>[^/?]+))?/?$")


class KGWriteRefused(Exception):
    pass


def method_allowed(method: str) -> bool:
    return method in READ_METHODS


def kg_request_allowed(http_method: str, url: str, post_data: str | None):
    """Decide whether a browser request to KG may proceed -> (ok, reason)."""
    path = urlparse(url).path
    if http_method.upper() in ("GET", "HEAD", "OPTIONS"):
        return True, "read"
    m = CALL_KW_RE.match(path)
    if m:
        method = m.group("method")
        if not method and post_data:
            try:
                method = (json.loads(post_data).get("params") or {}).get("method")
            except (ValueError, AttributeError):
                method = None
        if method and method_allowed(method):
            return True, f"call_kw {method}"
        return False, f"call_kw method not allowed: {method!r}"
    if any(path.startswith(p) for p in BROWSER_POST_PREFIXES):
        return True, "allowlisted endpoint"
    return False, f"non-GET blocked: {http_method} {path}"


class KGReadOnly:
    def __init__(self, url: str, db: str, login: str, secret: str, timeout: int = 60):
        self._url = url.rstrip("/")
        self._db, self._login, self._secret = db, login, secret
        self._timeout = timeout
        self._uid = None

    def _rpc(self, service: str, method: str, *args):
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1,
                   "params": {"service": service, "method": method, "args": list(args)}}
        r = requests.post(f"{self._url}/jsonrpc", json=payload, timeout=self._timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise RuntimeError(body["error"].get("data", {}).get("message") or body["error"])
        return body["result"]

    def version(self):
        return self._rpc("common", "version")

    def _auth(self):
        if self._uid is None:
            self._uid = self._rpc("common", "authenticate", self._db, self._login, self._secret, {})
            if not self._uid:
                raise RuntimeError("KG authentication failed")
        return self._uid

    def execute_kw(self, model: str, method: str, args=None, kwargs=None):
        if not method_allowed(method):
            raise KGWriteRefused(f"KG is read-only; refused {model}.{method}")
        return self._rpc("object", "execute_kw", self._db, self._auth(), self._secret,
                         model, method, args or [], kwargs or {})

    # Convenience read helpers
    def search_read(self, model, domain=None, fields=None, limit=80, **kw):
        return self.execute_kw(model, "search_read", [domain or []],
                               {"fields": fields or [], "limit": limit, **kw})

    def fields_get(self, model, attributes=("string", "type", "relation", "required")):
        return self.execute_kw(model, "fields_get", [], {"attributes": list(attributes)})
