"""Scripted browser sessions against an Odoo env. KG sessions are network-guarded read-only."""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from .kg_readonly import kg_request_allowed

NO_WS_JS = """(() => { try { delete window.SharedWorker; } catch (e) {}
  window.WebSocket = function () { throw new Error('WebSocket disabled by bugfix-agent'); }; })();"""
MAX_TEXT = 4000


class Browser:
    def __init__(self, url, db, login, secret, out_dir, readonly, do_login=True, headless=True, session_file=None):
        self.url, self.db, self.login, self.secret = url.rstrip("/"), db, login, secret
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.readonly, self.do_login, self.headless = readonly, do_login, headless
        self.session_file = Path(session_file) if session_file else None
        self.blocked, self.console_errors, self.page_errors, self.rpc_errors = [], [], [], []

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        kw = {}
        if self.session_file and self.session_file.exists():
            kw["storage_state"] = str(self.session_file)  # reuse the saved login: no new login-log row on the server
        self.ctx = self._browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block", **kw)
        self.ctx.set_default_timeout(45000)
        if self.readonly:
            self.ctx.add_init_script(NO_WS_JS)
            self.ctx.route("**/*", self._guard)
        self.page = self.ctx.new_page()
        self.page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text[:300]))
        self.page.on("pageerror", lambda e: self.page_errors.append(str(e)[:300]))
        self.page.on("response", self._on_response)
        if self.do_login:
            try:
                self._ensure_login()
            except Exception:
                self.__exit__()
                raise
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    # -- guards / capture --------------------------------------------------
    def _guard(self, route):
        req = route.request
        ok, why = kg_request_allowed(req.method, req.url, req.post_data)
        if ok:
            route.continue_()
        else:
            self.blocked.append(f"{req.method} {req.url[:160]} ({why})")
            route.abort("blockedbyclient")

    def _on_response(self, resp):
        if "/web/dataset/call_kw" not in resp.url or len(self.rpc_errors) >= 8:
            return
        try:
            body = resp.json()
        except Exception:
            return
        if isinstance(body, dict) and body.get("error"):
            d = body["error"].get("data") or {}
            self.rpc_errors.append({"url": resp.url.split("/web/dataset/")[-1][:120],
                                    "name": d.get("name"), "message": str(d.get("message") or body["error"].get("message"))[:500]})

    def _ensure_login(self):
        self.page.goto(self.url + "/web", wait_until="domcontentloaded", timeout=120000)
        self._settle()
        if "/web/login" in self.page.url:
            self._login()
        if self.session_file:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.ctx.storage_state(path=str(self.session_file))
            self.session_file.chmod(0o600)

    def _login(self):
        self.page.goto(self.url + "/web/login", wait_until="domcontentloaded", timeout=120000)
        self.page.fill("input[name=login]", self.login)
        self.page.fill("input[name=password]", self.secret)
        self.page.click("form.oe_login_form button[type=submit]")
        self.page.wait_for_function("!location.pathname.startsWith('/web/login')", timeout=90000)
        self._settle()

    def _settle(self, extra_ms=500):
        """Odoo keeps polling, so 'networkidle' may never come; wait briefly for it, then move on."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self.page.wait_for_timeout(extra_ms)

    def dialogs(self):
        try:
            return [t.strip()[:600] for t in self.page.locator(".o_dialog, .o_notification").all_inner_texts() if t.strip()][:4]
        except Exception:
            return []

    # -- step runner -------------------------------------------------------
    def run_steps(self, steps):
        results = []
        for i, st in enumerate(steps):
            act = st.get("action")
            r = {"i": i, "action": act, "ok": True}
            try:
                r.update(self._do(act, st) or {})
            except Exception as e:  # keep going: failures are evidence, not crashes
                r.update(ok=False, error=str(e).splitlines()[0][:300])
            d = self.dialogs()
            if d:
                r["dialogs"] = d
            results.append(r)
            if not r["ok"] and st.get("stop_on_error", True):
                break
        return {"env_url": self.url, "readonly": self.readonly, "steps": results, "final_url": self.page.url,
                "console_errors": self.console_errors[:10], "page_errors": self.page_errors[:10],
                "rpc_errors": self.rpc_errors, "blocked_requests": self.blocked[:20]}

    def _do(self, act, st):
        p = self.page
        if act == "goto":
            p.goto(st["path"] if st["path"].startswith("http") else self.url + st["path"], wait_until="domcontentloaded")
            self._settle()
        elif act == "click":
            p.click(st["selector"]); self._settle(st.get("settle_ms", 800))
        elif act == "fill":
            p.fill(st["selector"], str(st["value"]))
        elif act == "select":
            p.select_option(st["selector"], st["value"])
        elif act == "press":
            p.keyboard.press(st["key"]); p.wait_for_timeout(500)
        elif act == "wait":
            p.wait_for_selector(st["selector"]) if st.get("selector") else p.wait_for_timeout(int(st.get("ms", 1000)))
        elif act == "screenshot":
            f = self.out / f"{int(time.time())}_{st.get('name', 'shot')}.png"
            p.screenshot(path=str(f)); return {"file": str(f)}
        elif act == "text":
            t = p.inner_text(st.get("selector", "body"))
            return {"text": t[:MAX_TEXT]}
        elif act == "exists":
            return {"exists": p.locator(st["selector"]).count()}
        else:
            raise ValueError(f"unknown action {act!r}")
        return {}
