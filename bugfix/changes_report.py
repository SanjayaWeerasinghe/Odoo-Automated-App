"""Generates reports/changes_report.pdf: every failure in the bug-bot project, why it happened, and its status.
Historical entries are curated below; the live section is read from state/bugfix.db."""
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent

# (id, category, stage, what failed, why, impact, status)
FAILURES = [
 ("F01", "Planning", "Design plan, 1st review",
  "The plan was rejected by the owner.",
  "I left out two hard requirements: run on the Claude Team plan's included usage (not bought API credits) and keep the Known-Good (KG) env strictly read-only. I also did not have the real repos, branch and environments.",
  "One wasted planning round.", "Fixed: constraints A (Team-plan billing) and B (KG read-only) added to the plan."),
 ("F02", "Planning", "Design plan, 2nd review",
  "The revised plan was rejected again.",
  "It still lacked the concrete inputs (sheet, 20 repos, JinStage/Upgrade_Testing push target, staging and KG URLs).",
  "Second wasted round.", "Fixed: inputs added, plan approved."),
 ("F03", "Process", "First commit",
  "Commit message missed the required Co-Authored-By trailer.",
  "I overlooked the attribution instruction when committing.",
  "Wrong commit metadata (local repo only, not pushed).", "Fixed: commit amended."),
 ("F04", "Test setup", "First unit-test run",
  "pytest failed at collection: ModuleNotFoundError: No module named 'bugfix'.",
  "No pytest.ini with pythonpath; the package was not importable from tests.",
  "Tests did not run until fixed.", "Fixed: pytest.ini added."),
 ("F05", "Access", "Environment probing",
  "Third URL (rohanabalagalla-jinstage-clear-db-37966019) could not be reached: SSL certificate problem, self-signed certificate.",
  "The Odoo.sh build is most likely stopped or expired and is serving a default certificate. I did not disable TLS checks to get around it.",
  "That environment was never inspected.", "UNRESOLVED: ignored. Not part of the bot's current inputs."),
 ("F06", "Access", "Environment probing",
  "Odoo database list call returned AccessDenied on both environments, so DB names were unknown.",
  "Odoo.sh disables listing databases.",
  "Could not log in until the names were known.", "Fixed: DB name equals the subdomain; both logins verified."),
 ("F07", "Access", "Sheet write-back",
  "The agent cannot write verdicts or comments into the owner's Google Sheet.",
  "The sheet is an .xlsx file in Drive, so the Sheets API cannot edit cells; the Drive API needs Google credentials; the owner declined to share a personal account, and 'anyone with the link can edit' does not give API write access.",
  "Results are visible only locally (state/REPORT.md, state/bugfix.db). Testers see nothing in the sheet.", "NOT DONE. Needs a dedicated bot Google account or a decision to accept local-only reporting."),
 ("F08", "Tool bug (mine)", "Browser tool, 1st smoke test",
  "odoo-browse crashed on both environments; output was a Playwright traceback, not JSON.",
  "Login selector button[type=submit] matched a hidden website search button before the real login button. A failed login also left the browser open.",
  "No browser testing until fixed.", "Fixed: selector scoped to form.oe_login_form; browser now closed on failure."),
 ("F09", "Tool bug (mine)", "Browser tool, 2nd smoke test",
  "Staging page load timed out after 45 s; a KG text step also timed out.",
  "Odoo keeps polling so the page never reaches 'network idle'; and I used .o_main_navbar, which is not the Odoo 17 selector.",
  "Browser steps failed on staging.", "Fixed: settle step waits at most 8 s then continues; correct selector (.o_navbar)."),
 ("F10", "Process", "Manual git check",
  "'fatal: Authentication failed' when I fetched JinStage by hand.",
  "My command had no token header; the bot's own git calls send it. It was not a bad token.",
  "None (the remote showed no change).", "No action needed; noted to avoid a false alarm."),
 ("F11", "Fix stage", "End-to-end run 1 (HelpDesk!3)",
  "Fix attempt ended 'no_change': the edit was denied ('write permission not granted').",
  "My permission rules used absolute paths, which did not match in headless mode, so Claude was not allowed to edit.",
  "No fix produced; nothing pushed.", "Fixed: switched to acceptEdits mode (edits auto-approved only inside the worktree) plus the post-run path guard."),
 ("F12", "Fix stage", "End-to-end run 2 (HelpDesk!3)",
  "Fix attempt ended 'fix_error': No such file ... workspaces/JinStage/ix-repair/__manifest__.py.",
  "I stripped the first line of git status output, so the first changed path lost its first character ('Fix-repair' became 'ix-repair').",
  "Fix not pushed. The daemon had the same bug for about 2 minutes but never reached a fix in that window.", "Fixed: file list now taken from git diff and ls-files; regression test added; daemon restarted; the row was reset to be redone."),
 ("F13", "Quality", "Triage, HelpDesk!3",
  "Verdicts are not stable: the same bug was classed 'reproduces_on_both' once and 'staging_bug_no_reference' twice.",
  "The triage is a free-form AI investigation, so results vary between runs; KG also runs older module versions, so comparisons are ambiguous.",
  "Borderline bugs may or may not be sent to the fix stage.", "OPEN: partly mitigated by the fix-candidate rule. No consensus or re-run logic yet."),
 ("F14", "Limitation", "KG comparison",
  "The agent's KG RPC call for get_view was most likely refused (two attempts in the audit log).",
  "get_view is read-only but was missing from the RPC allowlist (only the browser allowlist had it). The agent had to infer KG's form from Studio view records.",
  "KG comparisons for UI bugs can be incomplete.", "OPEN: add get_view to the RPC allowlist (safe)."),
 ("F15", "Limitation", "Fix verification",
  "No local test can be run before pushing (no fail-before / pass-after test).",
  "Odoo Enterprise (helpdesk, mrp, etc.) is not available as a container image, so Odoo's own test suite cannot run here.",
  "A fix is checked only by syntax checks, then re-tested live on staging after deploy.", "NOT DONE: by design. Relies on post-deploy verification and auto-revert."),
 ("F16", "Constraint", "KG read-only rule",
  "KG is not literally untouched: every KG RPC or browser call logs in first.",
  "Odoo records a login (res.users.log / login_date) on each authentication, and my tools authenticate on every command, so each KG call writes one log row. All other writes are blocked in code (13 tests pass).",
  "Violates 'KG never changed' in the strict sense (login metadata only, no business data).", "OPEN: needs session reuse or API-key auth, or testing against a restored KG copy."),
 ("F17", "Security", "Credential handling",
  "Secrets are exposed in this session's history.",
  "The owner pasted a GitHub token and an Odoo password into chat, and my own commands wrote them in plain text when saving to .env. The same personal admin login is used for staging and KG. The triage agent runs as the same OS user as .env, protected only by a deny rule.",
  "Anyone with access to the transcript or logs could use the credentials.", "OPEN: rotate both secrets; use a fine-grained token for JinStage only and a dedicated read-only KG user."),
 ("F18", "Side effect", "HelpDesk!2 dry run",
  "A staging repair sequence skipped a number.",
  "The test ticket I created and deleted consumed REPAIR/2026/01408, and Odoo does not return a number when a record is deleted.",
  "One gap in the JAM repair numbering on staging (temporary database).", "Accepted: test records were cleaned up."),
 ("F19", "Operations", "Daemon deployment",
  "The daemon will not restart after a server reboot.",
  "pm2 save was done but pm2 startup needs sudo, which I did not run.",
  "Bot stops silently after a reboot.", "NOT DONE: run pm2 startup with sudo."),
 ("F20", "Not yet proven", "Whole pipeline",
  "No fix has been pushed, deployed and re-verified yet.",
  "Both end-to-end attempts failed before the push (F11, F12). The third run is now in the daemon's queue.",
  "The push, Odoo.sh rebuild wait and auto-revert paths are untested.", "OPEN: watch the first real fix closely."),
]


# Status updates after the "fix the open items" round: id -> (status, optional extra text appended to 'why')
UPDATES = {
 "F05": ("Fixed (closed): diagnosed. The certificate is Traefik's default, so Odoo.sh has no route for that hostname and the build no longer exists. Not a bot input.", None),
 "F07": ("Fixed: the bot now writes one-line notes into the Retest Status column as an anonymous editor (cell-level, so no one's edits are overwritten). Only empty cells or cells starting with BOT; never Status or Comments. Tested on the Sample tab and reverted; 7 real rows backfilled; 1 skipped because a tester had written there.", None),
 "F11": (None, " Root cause found later: in Claude Code permission rules a single leading slash means project-relative, not absolute; absolute paths need two slashes."),
 "F13": ("Fixed (mitigated): a fix is attempted only when 2 of up to 3 triage runs agree the bug is a defect. Variance for non-candidates remains.", None),
 "F14": ("Fixed: get_view, get_views and load_views added to the KG RPC allowlist (read-only).", None),
 "F15": ("OPEN (partly mitigated): a lint check now blocks new undefined names and syntax errors in changed Python files. XML views and Odoo behaviour are still verified only after deploy.", None),
 "F16": ("Fixed (measured): RPC calls reuse the known user id (login-log rows 131 before and after 3 calls). The browser reuses a saved session (1 login the first time, 0 after). Residual: one login when the session expires.", None),
 "F17": ("OPEN (partly mitigated): deny rules corrected (two-slash form) and tested; Claude was refused when told to read .env and the saved session file. The owner must still rotate the GitHub token and Odoo password; I cannot do that.", " Also found: my earlier single-slash deny rules never matched, so they did not protect .env until now."),
 "F19": ("NOT DONE: the system blocked my cron @reboot entry as unauthorized persistence and I did not try to bypass it. Needs owner approval: add the crontab line or run pm2 startup with sudo.", None),
 "F20": ("Fixed (proven): the push, Odoo.sh rebuild wait (about 2.5 min), post-deploy verification and automatic revert all ran for real: 5 fixes verified, 2 reverted, 1 no change.", None),
}
FAILURES = [tuple((u[0] or f[6]) if i == 6 else (f[i] + (u[1] or "") if i == 4 else f[i]) for i in range(7))
            if (u := UPDATES.get(f[0])) else f for f in FAILURES]


def live_data():
    c = sqlite3.connect(ROOT / "state" / "bugfix.db")
    tri = c.execute("SELECT key, verdict, payload, ts FROM triage ORDER BY ts").fetchall()
    try:
        fx = c.execute("SELECT key, outcome, payload, ts FROM fixes ORDER BY ts").fetchall()
    except sqlite3.OperationalError:
        fx = []
    return tri, fx


def build(path):
    ss = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=ss["BodyText"], fontSize=7.6, leading=9.4)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=15, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11.5, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9, leading=12)
    P = lambda t, s=cell: Paragraph(str(t).replace("&", "&amp;").replace("<", "&lt;"), s)

    def status_color(s):
        if s.startswith("Fixed"): return colors.HexColor("#d9f2e0")
        if s.startswith(("OPEN", "UNRESOLVED", "NOT DONE")): return colors.HexColor("#fbe3e0")
        return colors.HexColor("#f2f2f2")

    tri, fx = live_data()
    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter), leftMargin=0.45 * inch, rightMargin=0.45 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch, title="Bug Bot - Changes Report", author="bug bot project")
    s = []
    s += [Paragraph("Bug Bot: Changes Report", h1),
          Paragraph(f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Covers every failure, limitation and shortfall in this project from planning to the running daemon: what failed, why, and whether it is fixed.", body),
          Spacer(1, 6)]

    status = Counter("Fixed" if f[6].startswith("Fixed") else "Open / not done" if f[6].startswith(("OPEN", "UNRESOLVED", "NOT DONE")) else "Accepted / no action" for f in FAILURES)
    s.append(Paragraph("Summary", h2))
    s.append(Paragraph(f"{len(FAILURES)} items. Fixed: {status['Fixed']}. Open or not done: {status['Open / not done']}. Accepted / no action: {status['Accepted / no action']}.", body))
    s.append(Paragraph("<b>Still open:</b> (1) F17 the owner must rotate the exposed GitHub token and Odoo password; (2) F19 reboot persistence needs the owner's approval (crontab line or pm2 startup with sudo); "
                       "(3) F25 a person should review the bot's accounting-related commit be0ff596 and the other autofix commits; (4) F21/F22 the data and configuration gaps behind the two reverts must be fixed by a human (load the structure lines on staging, set the JAM default receivable account); (5) F15 no local Odoo tests are possible, only lint plus post-deploy checks.", body))

    s.append(Paragraph("Change and failure log", h2))
    rows = [[P(x, head) for x in ("ID", "Area", "Stage", "What failed", "Why it failed", "Impact", "Status")]]
    for f in FAILURES:
        rows.append([P(f[0]), P(f[1]), P(f[2]), P(f[3]), P(f[4]), P(f[5]), P(f[6])])
    t = Table(rows, colWidths=[0.38 * inch, 0.75 * inch, 0.95 * inch, 1.95 * inch, 2.55 * inch, 1.5 * inch, 1.5 * inch], repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334e68")), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9aa5b1")),
          ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for i, f in enumerate(FAILURES, start=1):
        st.append(("BACKGROUND", (6, i), (6, i), status_color(f[6])))
    t.setStyle(TableStyle(st))
    s.append(t)

    s.append(PageBreak())
    s.append(Paragraph("Live daemon status (read from state/bugfix.db at generation time)", h2))
    vc = Counter(r[1] for r in tri)
    s.append(Paragraph(f"Rows triaged so far: {len(tri)}. Verdicts: " + (", ".join(f"{k}: {v}" for k, v in vc.items()) or "none") +
                       f". Fix attempts recorded: {len(fx)}. Triage errors: {sum(1 for r in tri if json.loads(r[2]).get('error'))}.", body))
    if fx:
        rows = [[P(x, head) for x in ("Bug", "Outcome", "Detail", "Time")]]
        for k, o, p, ts in fx:
            d = json.loads(p)
            rows.append([P(k), P(o), P(str(d.get("error") or d.get("explanation") or d.get("commit") or "")[:300]), P(ts)])
        ft = Table(rows, colWidths=[1.0 * inch, 2.0 * inch, 5.6 * inch, 1.4 * inch], repeatRows=1)
        ft.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334e68")), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        s.append(ft)
    else:
        s.append(Paragraph("No fix attempts recorded since the last reset. The earlier two failed attempts (F11, F12) were cleared from the database after their causes were fixed.", body))

    s.append(Paragraph("Notes on interpretation", h2))
    for line in ("A verdict of verified_fixed means the agent could not reproduce the reported problem on staging; the bot writes a one-line note into the Retest Status column of that row (F07).",
                 "Failures F21-F22 were wrong fixes (reverted), F23-F24 were my own mistakes. Failures F01-F06 and F10 happened before the bot existed (planning and environment setup). F08, F09, F11 and F12 are bugs in code I wrote and have been fixed.",
                 "Limitations F14-F16 are not crashes; they are places where the bot does less than the original brief asked, so they are listed as failures to be honest about scope."):
        s.append(Paragraph("- " + line, body))
    doc.build(s)


if __name__ == "__main__":
    out = ROOT / "reports" / "changes_report.pdf"
    out.parent.mkdir(exist_ok=True)
    build(out)
    print(out)
