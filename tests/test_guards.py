import json
import pytest

from bugfix.git_guard import violations
from bugfix.odoo.kg_readonly import KGReadOnly, KGWriteRefused, kg_request_allowed


@pytest.mark.parametrize("method", ["create", "write", "unlink", "action_confirm",
                                    "button_validate", "onchange", "copy", "_cr", "execute"])
def test_kg_rpc_refuses_writes(method):
    kg = KGReadOnly("http://invalid.local", "db", "u", "s")
    with pytest.raises(KGWriteRefused):  # refused before any network call
        kg.execute_kw("sale.order", method, [[1]])


def test_browser_blocks_writes():
    body = json.dumps({"params": {"method": "write"}})
    assert not kg_request_allowed("POST", "http://x/web/dataset/call_kw/sale.order/write", body)[0]
    assert not kg_request_allowed("POST", "http://x/web/dataset/call_kw", body)[0]
    assert not kg_request_allowed("POST", "http://x/web/dataset/call_button", body)[0]
    assert not kg_request_allowed("POST", "http://x/longpolling/poll", None)[0]
    assert not kg_request_allowed("DELETE", "http://x/anything", None)[0]


def test_browser_allows_reads():
    assert kg_request_allowed("GET", "http://x/odoo/sales", None)[0]
    assert kg_request_allowed("POST", "http://x/web/dataset/call_kw/sale.order/web_search_read", "{}")[0]
    assert kg_request_allowed("POST", "http://x/web/session/authenticate", "{}")[0]


def test_path_guard():
    allowed = ["BugFix-Sales/sale_custom", "Jinasena_All"]
    assert violations(["BugFix-Sales/sale_custom/models/a.py", "Jinasena_All/x/y.xml"], allowed) == []
    assert violations(["oca_module/a.py"], allowed) == ["oca_module/a.py"]
    assert violations(["BugFix-Sales/other/a.py"], allowed) != []
    assert violations(["BugFix-Sales/sale_custom/../../evil.py"], allowed) != []
    assert violations(["/etc/passwd"], allowed) != []
