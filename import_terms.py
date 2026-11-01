"""One-time move of the Terms outreach CRM (Terms GTM.xlsx) into the CRM database.

Creates the "terms" workspace, turns each spreadsheet row into a contact on the right stage,
turns the dated parts of each Notes cell into timeline entries, adds the 10/23 call outcomes
that never made it into the spreadsheet, and saves the open next steps (with drafted replies).
Refuses to run twice; delete data/crm.db first to redo it.

    .venv/bin/python import_terms.py
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

from crm import db

XLSX = Path.home() / "Documents" / "Sean's Vault" / "Flexible CRM" / "Old Excel CRM" / "Terms GTM.xlsx"
DEEPLINE = Path.home() / "Documents" / "Sean's Vault" / "Terms" / "Deepline"
YEAR = 2026

STAGES = [
    {"name": "Not contacted", "kind": "open"},
    {"name": "Emailed", "kind": "active"},
    {"name": "Bumped", "kind": "active"},
    {"name": "Called, no answer", "kind": "active"},
    {"name": "Voicemail left", "kind": "active"},
    {"name": "Reached gatekeeper", "kind": "active"},
    {"name": "Call back", "kind": "active"},
    {"name": "Replied", "kind": "active"},
    {"name": "Not now", "kind": "active"},
    {"name": "Interested", "kind": "active"},
    {"name": "Meeting booked", "kind": "active"},
    {"name": "Trying Terms", "kind": "active"},
    {"name": "Won", "kind": "won"},
    {"name": "Not interested", "kind": "closed"},
    {"name": "Do not contact", "kind": "closed"},
    {"name": "Bounced", "kind": "closed"},
]
FIELDS = [
    {"name": "Group", "type": "text", "help": "Email first or Call first (the two test groups)"},
    {"name": "Batch", "type": "text", "help": "Which email batch they were in"},
]


def stage_for(steps, notes):
    s = set(steps)
    n = notes.lower()
    if "Booked Meeting" in s:
        return "Meeting booked"
    if s & {"Do Not Email", "Do Not Contact"}:
        return "Do not contact"
    if "Not Interested" in s:
        return "Not interested"
    if "Interested" in s:
        return "Interested"
    if "Call Back" in s:
        return "Call back"
    if "Replied" in s:
        return "Replied"
    if "bounced" in n:
        return "Bounced"
    if "bump 1 sent" in n:
        return "Bumped"
    if "Sent" in s:
        return "Emailed"
    if "Voicemail" in s:
        return "Voicemail left"
    if "Ghosted" in s:
        return "Called, no answer"
    return "Not contacted"


def touch_kind(text):
    t = text.lower()
    for word, kind in (("bump", "bump"), ("bounce", "bounce"), ("out of office", "reply"), ("leave", "reply"),
                       ("replied", "reply"), ("email sent", "email"), ("voicemail", "voicemail"), ("call", "call")):
        if word in t:
            return kind
    return "note"


def note_touches(notes):
    """'Batch 2 email sent 10/19 | Bump 1 sent 10/28' -> dated timeline entries; undated parts stay as notes."""
    dated, loose = [], []
    for part in [p.strip() for p in notes.split(" | ") if p.strip()]:
        m = re.search(r"\b(1[0-2]|0?[1-9])/([0-3]?\d)\b", part)
        if m:
            dated.append((f"{YEAR}-{int(m[1]):02d}-{int(m[2]):02d}", touch_kind(part), part))
        else:
            loose.append(part)
    return dated, " | ".join(loose)


def iso(v):
    return v.strftime("%Y-%m-%d") if isinstance(v, datetime) else None


def main():
    con = db.connect()
    if con.execute("SELECT 1 FROM workspaces WHERE slug = 'terms'").fetchone():
        sys.exit("The terms workspace already exists. Delete data/crm.db to import again.")
    db.save_workspace(con, "terms", "Terms.so",
                      STAGES, "Cold outreach to 2-15 person design studios and marketing agencies for Terms.so "
                              "(contract drafting, redlining, e-sign) with Family Office.", FIELDS)

    wb = openpyxl.load_workbook(XLSX, data_only=True)
    by_company = {}
    counts = {}
    for tab, group, step_cols in (("Call then Email", "Call first", ("Step 1: Phone Call", "Step 2: Email")),
                                  ("Email First then Call", "Email first", ("Step 1: Email", "Step 2: Phone Call"))):
        ws = wb[tab]
        h = {c.value: c.column for c in ws[1] if c.value}
        for r in range(2, ws.max_row + 1):
            v = lambda col: ws.cell(r, h[col]).value if col in h else None
            company = str(v("Company") or "").strip()
            if not company:
                continue
            notes = str(v("Notes") or "").strip()
            steps = [str(v(c) or "") for c in step_cols]
            stage = stage_for(steps, notes)
            batch = re.search(r"Batch \d+", notes)
            c = db.add_contact(con, "terms", company.split(" | ")[0].strip(), str(v("Person") or "").strip(), stage,
                               f"Excel: {tab}", role=str(v("Role") or ""), email=str(v("Email") or "").strip(),
                               phone=str(v("Company Phone") or ""), mobile=str(v("Direct / Mobile Phone") or ""),
                               custom={"Group": group, **({"Batch": batch[0]} if batch else {})})
            dated, loose = note_touches(notes)
            touches = [(at, kind, text, "import") for at, kind, text in dated]
            call_step = steps[0] if group == "Call first" else steps[1]
            if call_step not in ("", "Not Started"):
                first = min((t[0] for t in touches), default=None)
                when = iso(v("Last Touch Date") or v("Call Date"))
                if not when or (first and when > first):
                    when = "2026-10-15"
                touches.append((when, "call", f"Called: {call_step}" + (" (date approximate)" if when == "2026-10-15" else ""), "import"))
            con.executemany("INSERT INTO touches (contact_id, at, kind, summary, source) VALUES (?, ?, ?, ?, ?)",
                            [(c["id"], *t) for t in touches])
            con.execute("UPDATE contacts SET notes = ?, stage_changed = ? WHERE id = ?",
                        (loose, max((t[0] for t in touches), default=None), c["id"]))
            by_company[company.lower()] = c["id"]
            counts[stage] = counts.get(stage, 0) + 1
    con.commit()

    # 10/23 call outcomes from Sean's call sheet (never made it into the spreadsheet).
    outcomes = json.loads((DEEPLINE / "recovered-outcomes-2026-10-23.json").read_text())
    for company, outcome in outcomes.items():
        cid = by_company.get(company.lower())
        if not cid:
            print("No contact for call outcome:", company)
            continue
        o = outcome.lower()
        kind = "voicemail" if ("voice" in o or o.startswith("vn")) else "call"
        db.log_touch(con, cid, kind, f"Call 10/23: {outcome}", "2026-10-23", "import")
        stage = db.get_contact(con, cid, False)["stage"]
        if "rejected" in o or "not a good fit" in o:
            db.move_stage(con, cid, "Not interested", "said no on the 10/23 call", "2026-10-23", "import")
        elif "assistant" in o or "front desk" in o:
            if stage in ("Emailed", "Bumped"):
                db.move_stage(con, cid, "Reached gatekeeper", outcome, "2026-10-23", "import")
        elif kind == "voicemail" and stage == "Emailed":
            db.move_stage(con, cid, "Voicemail left", "", "2026-10-23", "import")
        elif "jan" in o:
            db.move_stage(con, cid, "Call back", "agreed to catch up in January", "2026-10-23", "import")

    print("Imported:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    con.close()


if __name__ == "__main__":
    main()
