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


def test_triage_parse_and_prompt():
    from bugfix.triage import parse_verdict, build_prompt
    from bugfix.intake.sheet_reader import Bug
    ok = 'blah\n```json\n{"verdict": "staging_regression", "confidence": 0.8}\n```'
    assert parse_verdict(ok)["needs_fix"] is True
    assert parse_verdict('```json\n{"verdict": "verified_fixed"}\n```')["needs_fix"] is False
    for bad in ("no block", '```json\n{"verdict": "banana"}\n```'):
        with pytest.raises(ValueError):
            parse_verdict(bad)
    b = Bug("Sales", 2, {"transaction": "creating the sales order", "behaviour": "x", "status": "Correction Done", "priority": "Normal"})
    p = build_prompt(b)
    assert "Sales!2" in p and "STRICTLY READ-ONLY" in p and "BugFix-Sales" in p


def test_changed_files_paths_intact(tmp_path):
    import subprocess
    from bugfix.fix import changed_files
    r = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    r("init", "-q"); (tmp_path / "Fix-repair").mkdir(); (tmp_path / "Fix-repair/a.py").write_text("x=1\n")
    r("add", "-A"); r("commit", "-qm", "init")
    (tmp_path / "Fix-repair/a.py").write_text("x=2\n"); (tmp_path / "Fix-repair/new.xml").write_text("<a/>")
    assert changed_files(tmp_path) == ["Fix-repair/a.py", "Fix-repair/new.xml"]


def test_lint_flags_only_new_undefined_names(tmp_path):
    import subprocess
    from bugfix.fix import lint_new_defects
    r = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    r("init", "-q"); f = tmp_path / "m.py"; f.write_text("def a():\n    return old_missing\n")
    r("add", "-A"); r("commit", "-qm", "init")
    f.write_text("def a():\n    return old_missing\n\ndef b():\n    return brand_new_typo\n")
    bad = lint_new_defects(tmp_path, "m.py")
    assert len(bad) == 1 and "brand_new_typo" in bad[0]          # pre-existing problem ignored, new one caught
    f.write_text("def a():\n    return old_missing\n")
    assert lint_new_defects(tmp_path, "m.py") == []


def test_sheet_writer_rules():
    from bugfix.intake.sheet_writer import can_write, note_for
    assert can_write(None) and can_write("") and can_write("BOT 2026-10-28: old note")
    assert not can_write("Retested OK by Malsha") and not can_write("Failed")
    n = note_for("verified_fixed", "On staging the field exists.\nSecond line")
    assert n.startswith("BOT ") and "\n" not in n and not n.startswith(("=", "+", "-", "@"))
    assert "FIXED" in note_for("x", "", {"outcome": "fixed_verified", "sha": "abcdef123456", "commit": "msg"})
