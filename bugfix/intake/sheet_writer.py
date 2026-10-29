"""Write bot results into the shared sheet as an anonymous editor (the file is shared 'anyone with the link can edit').

Cell-level edits through the Sheets web UI, so concurrent human edits are never overwritten. Rules:
  - only the 'Retest Status' column; never Status, never Comments (those belong to the testers)
  - only if the cell is empty or already starts with 'BOT' (our own earlier note)
  - one-line text starting with 'BOT' (never a formula)
"""
import io
import re
import time

import openpyxl
import requests
from playwright.sync_api import sync_playwright

TARGET_HEADER = "retest status"
PREFIX = "BOT"


def note_for(verdict, summary="", fix=None, fingerprint=None):
    """One-line note for the Retest Status cell. The [fp:...] tag is the bot's memory: the row is skipped while it matches."""
    note = _note_for(verdict, summary, fix)
    return f"{note} [fp:{fingerprint}]" if fingerprint else note


def _note_for(verdict, summary="", fix=None):
    d = time.strftime("%Y-%m-%d")
    summ = re.sub(r"\s+", " ", summary or "").strip()[:170]
    if fix and fix.get("outcome") == "proposed":
        return f"{PREFIX} {d}: fix PROPOSED, awaiting owner approval: {fix.get('pr_url', '')} {summ}"
    if fix and fix.get("outcome") == "fixed_verified":
        return f"{PREFIX} {d}: FIXED and verified on staging ({(fix.get('sha') or '')[:8]}). {fix.get('commit', '')[:110]}"
    if fix and fix.get("outcome") == "reverted_after_failed_verify":
        return f"{PREFIX} {d}: fix attempted but failed verification and was reverted. {summ}"
    if fix and fix.get("outcome") == "pushed_deploy_unconfirmed":
        return f"{PREFIX} {d}: fix pushed ({(fix.get('sha') or '')[:8]}), staging rebuild not confirmed yet. {summ}"
    labels = {"verified_fixed": "verified on staging, behaves as expected", "not_reproducible": "could not reproduce on staging",
              "reproduces_on_both": "reproduces on staging and known-good", "staging_regression": "BROKEN on staging only",
              "staging_bug_no_reference": "BROKEN on staging", "needs_human": "needs a human", "insufficient_info": "report too vague to test"}
    return f"{PREFIX} {d}: {labels.get(verdict, verdict)}. {summ}"


def can_write(current_value):
    return current_value in (None, "") or str(current_value).startswith(PREFIX)


def _export(sheet_id):
    return openpyxl.load_workbook(io.BytesIO(requests.get(
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx", timeout=120).content))


def target_column(ws):
    for i, c in enumerate(ws[1], start=1):
        if " ".join(str(c.value or "").split()).lower().startswith(TARGET_HEADER):
            return openpyxl.utils.get_column_letter(i)
    return None


def write_notes(sheet_id, notes, headless=True):
    """notes: list of (tab, row, text). Returns {(tab,row): 'written'|'skipped: why'|'failed: why'}."""
    result = {}
    wb = _export(sheet_id)
    todo = []
    for tab, row, text in notes:
        if tab not in wb.sheetnames:
            result[(tab, row)] = "skipped: tab not found"; continue
        col = target_column(wb[tab])
        if not col:
            result[(tab, row)] = "skipped: no Retest Status column"; continue
        cur = wb[tab][f"{col}{row}"].value
        if not can_write(cur):
            result[(tab, row)] = "skipped: cell has human content"; continue
        todo.append((tab, row, col, text.replace("\n", " ")))
    if not todo:
        return result
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless)
        pg = b.new_context(viewport={"width": 1500, "height": 900}).new_page()
        try:
            pg.goto(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit", wait_until="domcontentloaded", timeout=90000)
            pg.wait_for_timeout(8000)
            cur_tab = None
            for tab, row, col, text in todo:
                try:
                    if tab != cur_tab:
                        pg.locator(".docs-sheet-tab", has_text=tab).first.click(); pg.wait_for_timeout(2000); cur_tab = tab
                    nb = pg.locator("input.waffle-name-box, #t-name-box").first
                    nb.click(); pg.keyboard.press("Control+a"); pg.keyboard.type(f"{col}{row}"); pg.keyboard.press("Enter"); pg.wait_for_timeout(500)
                    pg.keyboard.type(text); pg.keyboard.press("Enter"); pg.wait_for_timeout(1500)
                    result[(tab, row)] = "written"
                except Exception as e:
                    result[(tab, row)] = f"failed: {str(e)[:100]}"
            pg.wait_for_timeout(4000)  # let autosave flush
        finally:
            b.close()
    return result
