"""Command line for scripts and scheduled tasks that update the CRM without going through Claude's MCP.

    python -m crm.cli due [--workspace terms] [--through YYYY-MM-DD]
    python -m crm.cli calls                       # emailed but never called, oldest first
    python -m crm.cli find "<text>"
    python -m crm.cli touch <id> <kind> "<summary>" [--at YYYY-MM-DD] [--subject "..."] [--body "..."]
    python -m crm.cli stage <id> "<stage>" [--reason "..."]
    python -m crm.cli next <id> "<step>" [--due YYYY-MM-DD] [--draft "..."]
    python -m crm.cli log-send --sent-log sent_log.csv --contacts batch.csv --label "Batch 7" [--bump] [--strategy "Email first"] [--template template.txt]
    python -m crm.cli log-mail --email x@y.com --kind reply|auto|bounce --summary "..." [--at YYYY-MM-DD] [--subject "..."] [--body "..."]
    python -m crm.cli kill <id> [--stage "Do not contact"]   # mark dead: closed stage, planned steps skipped
    python -m crm.cli mark-ai --ids 12,40 --state seen|batch|clear [--note "Batch 9, Sat Oct 31"]
    python -m crm.cli client <id> [--undo]       # mark an active client (planned steps stay)
    python -m crm.cli hold <id> YYYY-MM-DD|none   # no "stuck" warning before that date
    python -m crm.cli touches (<id> | --email x@y.com) [--kind email] [--on YYYY-MM-DD]   # timeline entries with ids
    python -m crm.cli set-touch-detail (<touch_id> | --email x@y.com --kind email --on YYYY-MM-DD) [--subject "..."] [--body "..."|--body-file f]

Prints JSON.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from . import db

# Stages an automatic event may move someone out of. Anything further along was set by a person; leave it.
EARLY = {"Not contacted", "Emailed", "Bumped", "Called, no answer", "Voicemail left", "Reached gatekeeper"}


def by_email(con, workspace, email):
    rows = con.execute("SELECT c.id FROM contacts c JOIN workspaces w ON w.id = c.workspace_id "
                       "WHERE w.slug = ? AND lower(c.email) = ?", (workspace, email.strip().lower())).fetchall()
    return rows[0]["id"] if rows else None


def column_key(header):
    # same as send_outreach.py: "First Name" -> first_name, "Email (Apollo)" -> email_apollo
    return re.sub(r"[^a-z0-9]+", "_", (header or "").lower()).strip("_")


def load_template(path):
    """A send_outreach.py template: first line 'Subject: ...', then the body. Returns (subject, body)."""
    first, _, body = Path(path).read_text(encoding="utf-8").partition("\n")
    if not first.lower().startswith("subject:"):
        raise ValueError(f"{path}: first line must be 'Subject: ...'")
    return first.split(":", 1)[1].strip(), body.lstrip("\n")


def render(template, row):
    """Fill {placeholders} from a contacts CSV row exactly as send_outreach.py does."""
    keyed = {column_key(k): (v or "").strip() for k, v in row.items() if k}
    email_col = next((k for k in keyed if k == "email" or k.startswith("email_")), None)
    if email_col:
        keyed["email"] = keyed[email_col]
    subject_t, body_t = template
    try:
        return subject_t.format(**keyed), body_t.format(**keyed)
    except (KeyError, IndexError, ValueError):
        return None


def log_send(con, workspace, sent_log, contacts, label, bump, strategy=None, template=None):
    """Record sends from a sender log. People not in the CRM yet are added from the contacts CSV (and put on
    `strategy` if given). Each send checks off that person's next planned email step. With `template` (the
    send_outreach.py template file), each person's email is filled in from their CSV row and kept on the entry."""
    info = {}
    tpl = load_template(template) if template else None
    if contacts:
        with open(contacts, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                keyed = {column_key(k): (v or "") for k, v in r.items() if k}
                email = next((keyed[k] for k in keyed if k == "email" or k.startswith("email_")), "")
                info[email.strip().lower()] = r
    added = logged = 0
    with open(sent_log, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            email = r["email"].strip().lower()
            if info and email not in info:
                continue  # only this batch's people
            cid = by_email(con, workspace, email)
            if cid is None:
                c = info.get(email)
                if not c:
                    continue
                person = " ".join(x for x in (c.get("first_name", ""), c.get("last_name", "")) if x).strip()
                cid = db.add_contact(con, workspace, c.get("company", ""), person, "Not contacted", "sender",
                                     role=c.get("title", ""), email=email, phone=c.get("phone", ""),
                                     website=c.get("website", ""), custom={"Batch": label})["id"]
                if strategy:
                    db.apply_strategy(con, cid, strategy, r["sent_at"][:10], "sender")
                added += 1
            summary = f"{label} {'bump' if bump else 'email'} sent: {r.get('subject', '')}".strip()
            exists = con.execute("SELECT 1 FROM touches WHERE contact_id = ? AND summary = ?", (cid, summary)).fetchone()
            if exists:
                continue
            at = r["sent_at"][:16]
            mail = render(tpl, info[email]) if tpl and email in info else None
            subject, body = mail or (r.get("subject", ""), "")
            db.log_touch(con, cid, "bump" if bump else "email", summary, at, "sender", body, subject)
            db.finish_step_of_type(con, cid, "email", summary, at)
            if db.get_contact(con, cid, False).get("ai_state") == "batch":  # the batch went out, so the mark is done
                db.mark_ai(con, cid, "")
            if db.get_contact(con, cid, False)["stage"] in EARLY:
                db.move_stage(con, cid, "Bumped" if bump else "Emailed", label, at, "sender")
            logged += 1
    return {"logged": logged, "added": added, "with_text": bool(tpl)}


def set_touch_detail(con, workspace, touch_id, email, kind, on, subject, body):
    """Attach text to an existing entry, found by id or by person + kind + day. Several matches is an error."""
    if not touch_id:
        cid = by_email(con, workspace, email or "")
        if cid is None:
            return {"found": False, "email": email}
        hits = db.find_touches(con, cid, kind, on)
        if len(hits) != 1:
            return {"found": False, "matches": hits, "error": "need exactly one match; pass the touch id"}
        touch_id = hits[0]["id"]
    return db.set_touch_detail(con, touch_id, subject, body)


def log_mail(con, workspace, email, kind, summary, at, body="", subject=""):
    """An email came in. body is everything they wrote (quoted original cut off) and subject its subject line;
    the person panel shows a reply's full text at the top."""
    cid = by_email(con, workspace, email)
    if cid is None:
        return {"found": False, "email": email}
    db.log_touch(con, cid, "reply", summary if kind != "bounce" else f"Bounced: {summary}", at, "inbox",
                 detail=body if kind == "reply" else "", subject=subject)
    stage = db.get_contact(con, cid, False)["stage"]
    if kind == "bounce":
        db.move_stage(con, cid, "Bounced", summary, at, "inbox")
    elif kind == "reply" and stage in EARLY:
        db.move_stage(con, cid, "Replied", "replied to email", at, "inbox")
    return {"found": True, "id": cid}


def call_queue(con, workspace):
    """Emailed (or bumped) people never called yet, oldest email first: the cold-call list."""
    rows = con.execute(
        "SELECT c.id, c.company, c.person, c.phone, c.stage, min(t.at) AS emailed FROM contacts c "
        "JOIN workspaces w ON w.id = c.workspace_id JOIN touches t ON t.contact_id = c.id AND t.kind = 'email' "
        "WHERE w.slug = ? AND c.stage IN ('Emailed', 'Bumped') AND c.phone != '' AND NOT EXISTS ("
        "  SELECT 1 FROM touches x WHERE x.contact_id = c.id AND x.kind IN ('call', 'voicemail')) "
        "GROUP BY c.id ORDER BY emailed", (workspace,)).fetchall()
    return [dict(r) for r in rows]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", default="terms")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("due"); s.add_argument("--through")
    sub.add_parser("calls")
    s = sub.add_parser("find"); s.add_argument("query")
    s = sub.add_parser("touch"); s.add_argument("id", type=int); s.add_argument("kind"); s.add_argument("summary"); s.add_argument("--at")
    s.add_argument("--subject", default=""); s.add_argument("--body", default="")
    s = sub.add_parser("touches"); s.add_argument("id", type=int, nargs="?"); s.add_argument("--email")
    s.add_argument("--kind"); s.add_argument("--on")
    s = sub.add_parser("set-touch-detail"); s.add_argument("touch_id", type=int, nargs="?"); s.add_argument("--email")
    s.add_argument("--kind"); s.add_argument("--on"); s.add_argument("--subject"); s.add_argument("--body")
    s.add_argument("--body-file", help="read the body from this file (keeps line breaks)")
    s = sub.add_parser("stage"); s.add_argument("id", type=int); s.add_argument("stage"); s.add_argument("--reason", default="")
    s = sub.add_parser("next"); s.add_argument("id", type=int); s.add_argument("step"); s.add_argument("--due"); s.add_argument("--draft")
    s = sub.add_parser("log-send"); s.add_argument("--sent-log", required=True); s.add_argument("--contacts")
    s.add_argument("--label", required=True); s.add_argument("--bump", action="store_true"); s.add_argument("--strategy")
    s.add_argument("--template", help="the send_outreach.py template: each person's email is kept on their entry")
    s = sub.add_parser("log-mail"); s.add_argument("--email", required=True); s.add_argument("--kind", required=True)
    s.add_argument("--summary", default=""); s.add_argument("--at"); s.add_argument("--subject", default=""); s.add_argument("--body", default="")
    s = sub.add_parser("kill"); s.add_argument("id", type=int); s.add_argument("--stage")
    s = sub.add_parser("client"); s.add_argument("id", type=int); s.add_argument("--undo", action="store_true")
    s = sub.add_parser("mark-ai"); s.add_argument("--ids", required=True, help="contact ids, comma separated")
    s.add_argument("--state", required=True, choices=["seen", "batch", "clear"]); s.add_argument("--note", default="")
    s = sub.add_parser("hold"); s.add_argument("id", type=int); s.add_argument("until", help="YYYY-MM-DD, or none to clear")
    a = p.parse_args()
    db.ACTOR.set("task")  # changes made here show as Scripts in the history

    con = db.connect()
    try:
        if a.cmd == "due":
            out = db.due(con, a.workspace, a.through)
        elif a.cmd == "calls":
            out = call_queue(con, a.workspace)
        elif a.cmd == "find":
            out = db.find_contacts(con, a.workspace, a.query)
        elif a.cmd == "touch":
            out = db.log_touch(con, a.id, a.kind, a.summary, a.at, "task", a.body, a.subject)
        elif a.cmd == "touches":
            cid = a.id or by_email(con, a.workspace, a.email or "")
            out = db.find_touches(con, cid, a.kind, a.on) if cid else {"found": False, "email": a.email}
        elif a.cmd == "set-touch-detail":
            body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else a.body
            out = set_touch_detail(con, a.workspace, a.touch_id, a.email, a.kind, a.on, a.subject, body)
        elif a.cmd == "stage":
            out = db.move_stage(con, a.id, a.stage, a.reason, None, "task")
        elif a.cmd == "next":
            out = db.set_next_step(con, a.id, a.step, a.due, a.draft, "task")
        elif a.cmd == "mark-ai":
            state = "" if a.state == "clear" else a.state
            out = [db.mark_ai(con, int(i), state, a.note)["id"] for i in a.ids.split(",") if i.strip()]
        elif a.cmd == "client":
            out = db.undo_step(con, -a.id) if a.undo else db.mark_client(con, a.id, "task")
        elif a.cmd == "kill":
            out = db.kill(con, a.id, a.stage, "task")
        elif a.cmd == "hold":
            out = db.update_contact(con, a.id, hold_until="" if a.until.lower() == "none" else a.until)
        elif a.cmd == "log-send":
            out = log_send(con, a.workspace, a.sent_log, a.contacts, a.label, a.bump, a.strategy, a.template)
        else:
            out = log_mail(con, a.workspace, a.email, a.kind, a.summary, a.at, a.body, a.subject)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        con.close()
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
