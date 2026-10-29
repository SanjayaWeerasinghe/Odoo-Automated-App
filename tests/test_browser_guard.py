import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from bugfix.odoo.playwright_runner import Browser

SEEN = []
PAGE = b"""<html><body><script>
const post = (p, b) => fetch(p, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b)}).catch(()=>{});
fetch('/read-ok').catch(()=>{});
post('/web/dataset/call_kw/sale.order/search_read', {params:{method:'search_read'}});
post('/web/dataset/call_kw/sale.order/write', {params:{method:'write'}});
post('/web/dataset/call_kw/sale.order/unlink', {params:{method:'unlink'}});
post('/web/dataset/call_kw/sale.order/action_confirm', {params:{method:'action_confirm'}});
post('/web/dataset/call_button', {params:{method:'x'}});
post('/mail/data', {});
fetch('/bus/poll').catch(()=>{});
fetch('/anything', {method:'DELETE'}).catch(()=>{});
fetch('/anything', {method:'PUT', body:'x'}).catch(()=>{});
try { new WebSocket('ws://localhost:1/websocket'); } catch (e) { document.title = 'ws-blocked'; }
</script>ok</body></html>"""


class H(BaseHTTPRequestHandler):
    def _h(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        SEEN.append((self.command, self.path))
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(PAGE if self.path == "/" else b"{}")
    do_GET = do_POST = do_PUT = do_DELETE = _h
    def log_message(self, *a): pass


def test_kg_session_never_sends_writes(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    SEEN.clear()
    with Browser(url, "db", "u", "p", tmp_path, readonly=True, do_login=False) as b:
        b.page.goto(url + "/")
        b.page.wait_for_timeout(1500)
        title = b.page.title()
        blocked = list(b.blocked)
    srv.shutdown()
    methods = {m for m, _ in SEEN}
    assert ("POST", "/web/dataset/call_kw/sale.order/search_read") in SEEN      # read allowed
    assert ("GET", "/read-ok") in SEEN
    assert methods <= {"GET", "POST"}, SEEN                                     # no PUT/DELETE
    forbidden = [s for s in SEEN if s[0] == "POST" and s[1] != "/web/dataset/call_kw/sale.order/search_read"]
    assert forbidden == [], forbidden                                           # no write POSTs reached the server
    assert not any("/bus/" in p for _, p in SEEN)
    assert title == "ws-blocked"
    assert len(blocked) >= 7, blocked
