"""Storage and rules for the CRM. Everything else (web page, MCP tools, importers) goes through here.

One SQLite file holds every workspace. A workspace is one company you do outreach for; it has its
own pipeline (stages) and its own custom fields, so the CRM fits whatever that company sells.

Each contact has a list of steps (email, call, text, ...): done ones, the current one and planned ones.
A strategy is a named template of steps, so new people get a whole sequence at once. The contact's
next_step / next_due / next_draft columns always mirror the current step, so older callers keep working.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("CRM_DB", Path(__file__).resolve().parent.parent / "data" / "crm.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    stages TEXT NOT NULL,          -- JSON list of {name, kind, hot, meeting, paid, aliases}; kind: open, active, won, closed
    fields TEXT NOT NULL DEFAULT '[]',  -- JSON list of {name, type, help}
    after_call TEXT DEFAULT ''          -- JSON {subject, body}: the email a lukewarm call gets, {first_name} filled in
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    company TEXT NOT NULL,
    person TEXT DEFAULT '',
    role TEXT DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    mobile TEXT DEFAULT '',
    website TEXT DEFAULT '',
    source TEXT DEFAULT '',
    stage TEXT NOT NULL,
    stage_changed TEXT,
    next_step TEXT DEFAULT '',     -- mirror of the current step (see _sync_next)
    next_due TEXT,                 -- YYYY-MM-DD
    next_draft TEXT DEFAULT '',    -- message drafted for the next step, ready to send
    custom TEXT NOT NULL DEFAULT '{}',
    notes TEXT DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    strategy_id INTEGER REFERENCES strategies(id),
    location TEXT DEFAULT '',      -- city and state
    ai_state TEXT DEFAULT '',      -- what the outreach chat has done: seen (has a plan) or batch (in the next batch)
    ai_note TEXT DEFAULT '',       -- e.g. "Batch 9, Sat Oct 31"
    ai_at TEXT,
    hold_until TEXT DEFAULT ''     -- YYYY-MM-DD: no "stuck" warning before this date (e.g. "connect mid-November")
);
CREATE INDEX IF NOT EXISTS contacts_ws ON contacts(workspace_id, stage);
CREATE INDEX IF NOT EXISTS contacts_email ON contacts(email);
CREATE TABLE IF NOT EXISTS touches (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    at TEXT NOT NULL,              -- YYYY-MM-DD or YYYY-MM-DDTHH:MM
    kind TEXT NOT NULL,            -- email, bump, call, voicemail, reply, text, linkedin, meeting, note, stage
    summary TEXT DEFAULT '',
    source TEXT DEFAULT ''         -- who logged it: web, claude, import, sender, inbox
);
CREATE INDEX IF NOT EXISTS touches_contact ON touches(contact_id, at);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]'   -- JSON list of {type, day, title, subject, body, weekend}; day 0 is the start
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    type TEXT NOT NULL,            -- see STEP_TYPES
    title TEXT DEFAULT '',
    due TEXT,                      -- YYYY-MM-DD
    subject TEXT DEFAULT '',       -- email subject
    body TEXT DEFAULT '',          -- the message, talking points, or close reason
    status TEXT NOT NULL DEFAULT 'planned',  -- planned, done, skipped, paused
    outcome TEXT DEFAULT '',
    done_at TEXT,
    origin TEXT DEFAULT 'manual',  -- strategy (made from a template) or manual
    weekend INTEGER DEFAULT 0,     -- 1: belongs on a Saturday or Sunday (Sean calls on weekends)
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS steps_contact ON steps(contact_id, status, due);
CREATE TABLE IF NOT EXISTS undo (
    step_id INTEGER PRIMARY KEY,   -- a step done from the web page, and how things stood just before
    data TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

KINDS = {"open", "active", "won", "closed"}
STEP_TYPES = ("email", "call", "text", "linkedin", "meeting", "task", "close")
STEP_LABELS = {"email": "Email", "call": "Call", "text": "Text", "linkedin": "LinkedIn message",
               "meeting": "Meeting", "task": "To do", "close": "Close if no reply"}
# outcome: (touch kind, summary, stage it moves to when the workspace has that stage)
CALL_OUTCOMES = {
    "no_answer": ("call", "Called, no answer", "Called, no answer"),
    "voicemail": ("voicemail", "Left a voicemail", "Voicemail left"),
    "gatekeeper": ("call", "Reached the gatekeeper", "Reached gatekeeper"),
    "call_back": ("call", "Talked, asked for a call back", "Call back"),
    "interested": ("call", "Talked, interested", "Interested"),
    "rejected": ("call", "Talked, not interested", "@closed"),  # @closed = the workspace's first closed stage
    "talked": ("call", "Talked", None),
    "maybe": ("call", "Talked, lukewarm, wants an email", None),  # also adds the after-call email, due today
}
AFTER_CALL_DEFAULT = {"subject": "Following up on our call", "body": "Hi {first_name},\n\nGreat talking with you today.\n\nBest,\nSean"}


def now():
    return datetime.now().isoformat(timespec="minutes")


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    if "after_call" not in {r["name"] for r in con.execute("PRAGMA table_info(workspaces)")}:
        con.execute("ALTER TABLE workspaces ADD COLUMN after_call TEXT DEFAULT ''")
    if "weekend" not in {r["name"] for r in con.execute("PRAGMA table_info(steps)")}:
        con.execute("ALTER TABLE steps ADD COLUMN weekend INTEGER DEFAULT 0")
    cols = {r["name"] for r in con.execute("PRAGMA table_info(contacts)")}
    for col, ddl in (("strategy_id", "INTEGER REFERENCES strategies(id)"), ("location", "TEXT DEFAULT ''"),
                     ("ai_state", "TEXT DEFAULT ''"), ("ai_note", "TEXT DEFAULT ''"), ("ai_at", "TEXT"),
                     ("hold_until", "TEXT DEFAULT ''")):
        if col not in cols:
            con.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")
            con.commit()
    return con


def _ws(con, workspace):
    row = con.execute("SELECT * FROM workspaces WHERE slug = ? OR name = ?", (workspace, workspace)).fetchone()
    if not row:
        raise ValueError(f"No workspace '{workspace}'. Existing: {', '.join(w['slug'] for w in list_workspaces(con))}")
    return row


def _ws_dict(row):
    d = dict(row)
    d["stages"] = json.loads(d["stages"])
    d["fields"] = json.loads(d["fields"])
    d["after_call"] = json.loads(d.get("after_call") or "null") or AFTER_CALL_DEFAULT
    return d


def set_after_call(con, workspace, subject, body):
    """Save the workspace's after-call email template ({first_name} is filled in per person)."""
    ws = _ws(con, workspace)
    con.execute("UPDATE workspaces SET after_call = ? WHERE id = ?",
                (json.dumps({"subject": subject or "", "body": body or ""}), ws["id"]))
    con.commit()
    return get_workspace(con, workspace)


def _after_call_email(con, contact_id):
    """A lukewarm call wants an email with more details: add it, due today, filled from the template."""
    c = get_contact(con, contact_id, with_touches=False)
    t = get_workspace(con, c["workspace"])["after_call"]
    first = (c["person"] or "").split(" ")[0] or "there"
    fill = lambda s: (s or "").replace("{first_name}", first)
    return _insert_step(con, contact_id, "email", "After-call email", date.today().isoformat(), fill(t["subject"]), fill(t["body"]))


def _contact_dict(row):
    d = dict(row)
    d["custom"] = json.loads(d["custom"] or "{}")
    return d


STALE_DAYS = 4  # Sean's rule: nobody sits in one stage longer than this


def stale_days(c, kinds, today=None):
    """Days someone has sat in their stage when that's over STALE_DAYS, else 0. Finished stages (won, closed)
    never go stale, and neither does anyone on hold (hold_until in the future)."""
    today = today or date.today().isoformat()
    if kinds.get(c["stage"]) in ("won", "closed") or (c.get("hold_until") or "") > today:
        return 0
    since = c.get("stage_changed") or (c.get("created") or "")[:10]
    if not since:
        return 0
    n = (date.fromisoformat(today) - date.fromisoformat(since[:10])).days
    return n if n > STALE_DAYS else 0


def _kinds(con, slug):
    row = con.execute("SELECT stages FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    return {s["name"]: s["kind"] for s in json.loads(row["stages"])} if row else {}


def _workday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _weekend(d):
    """The next Saturday, or the day itself when it's already a Saturday or Sunday."""
    return d if d.weekday() >= 5 else d + timedelta(days=5 - d.weekday())


def _snap(d, weekend, after=None):
    """Move a step date onto the right kind of day: weekend steps to the weekend, the rest to a weekday. With
    `after` (the step before it) it lands later than that step, a weekend step on a later weekend."""
    d = _weekend(d) if weekend else _workday(d)
    while after and d <= after:
        d = d + timedelta(days=7) if weekend else _workday(after + timedelta(days=1))
    return d


# ---- workspaces ---------------------------------------------------------------------------------

def list_workspaces(con):
    return [_ws_dict(r) for r in con.execute("SELECT * FROM workspaces ORDER BY id")]


def get_workspace(con, workspace):
    return _ws_dict(_ws(con, workspace))


def save_workspace(con, slug, name, stages, description="", fields=None):
    """Create a workspace or replace its pipeline. Contacts on a removed stage keep their stage name.
    A stage may carry "hot": true to mark warm leads (a move there pauses the planned strategy steps),
    "meeting": true to count it as a booked meeting, "paid": true for stages that earn money, and
    "aliases": [old names] so a renamed stage still answers to its old name."""
    for s in stages:
        if not s.get("name") or s.get("kind", "active") not in KINDS:
            raise ValueError(f"Bad stage {s}: needs a name and kind in {sorted(KINDS)}")
        s.setdefault("kind", "active")
    existing = con.execute("SELECT id, fields FROM workspaces WHERE slug = ?", (slug,)).fetchone()
    if existing:
        con.execute("UPDATE workspaces SET name = ?, description = ?, stages = ?, fields = ? WHERE id = ?",
                    (name, description, json.dumps(stages),
                     json.dumps(fields) if fields is not None else existing["fields"], existing["id"]))
    else:
        con.execute("INSERT INTO workspaces (slug, name, description, stages, fields) VALUES (?, ?, ?, ?, ?)",
                    (slug, name, description, json.dumps(stages), json.dumps(fields or [])))
    con.commit()
    return get_workspace(con, slug)


def add_field(con, workspace, name, type="text", help=""):
    ws = _ws(con, workspace)
    fields = [f for f in json.loads(ws["fields"]) if f["name"] != name]
    fields.append({"name": name, "type": type, "help": help})
    con.execute("UPDATE workspaces SET fields = ? WHERE id = ?", (json.dumps(fields), ws["id"]))
    con.commit()
    return fields


# ---- strategies ---------------------------------------------------------------------------------

def _strategy_dict(row, used=None):
    d = dict(row)
    d["steps"] = json.loads(d["steps"])
    if used is not None:
        d["used"] = used
    return d


def list_strategies(con, workspace):
    ws = _ws(con, workspace)
    used = dict(con.execute("SELECT strategy_id, count(*) FROM contacts WHERE workspace_id = ? GROUP BY strategy_id",
                            (ws["id"],)).fetchall())
    return [_strategy_dict(r, used.get(r["id"], 0))
            for r in con.execute("SELECT * FROM strategies WHERE workspace_id = ? ORDER BY id", (ws["id"],))]


def get_strategy(con, strategy, workspace_id=None):
    """A strategy by id, or by name within a workspace."""
    if isinstance(strategy, int) or str(strategy).isdigit():
        row = con.execute("SELECT * FROM strategies WHERE id = ?", (int(strategy),)).fetchone()
    else:
        row = con.execute("SELECT * FROM strategies WHERE workspace_id = ? AND name = ?",
                          (workspace_id, strategy)).fetchone()
    if not row:
        raise ValueError(f"No strategy '{strategy}'")
    return _strategy_dict(row)


def save_strategy(con, workspace, name, steps, description="", strategy_id=None):
    """Create a strategy, or change one (only future people and re-applied ones get the new steps)."""
    ws = _ws(con, workspace)
    if not name.strip():
        raise ValueError("A strategy needs a name")
    clean = []
    for s in steps:
        if s.get("type") not in STEP_TYPES:
            raise ValueError(f"Bad step type '{s.get('type')}'. Types: {', '.join(STEP_TYPES)}")
        clean.append({"type": s["type"], "day": max(0, int(s.get("day") or 0)), "title": s.get("title", "") or "",
                      "subject": s.get("subject", "") or "", "body": s.get("body", "") or "",
                      "weekend": s.get("weekend") in (True, 1, "1", "true", "on")})
    clean.sort(key=lambda s: s["day"])
    if strategy_id:
        get_strategy(con, strategy_id)
        con.execute("UPDATE strategies SET name = ?, description = ?, steps = ? WHERE id = ?",
                    (name.strip(), description, json.dumps(clean), strategy_id))
    else:
        strategy_id = con.execute("INSERT INTO strategies (workspace_id, name, description, steps) VALUES (?, ?, ?, ?)",
                                  (ws["id"], name.strip(), description, json.dumps(clean))).lastrowid
    con.commit()
    return get_strategy(con, strategy_id)


def delete_strategy(con, strategy_id):
    """Remove a strategy template. Refused while anyone still follows it."""
    get_strategy(con, strategy_id)
    n = con.execute("SELECT count(*) FROM contacts WHERE strategy_id = ?", (strategy_id,)).fetchone()[0]
    if n:
        raise ValueError(f"{n} people follow this strategy. Move them to another one first.")
    con.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
    con.commit()
    return {"deleted": strategy_id}


def apply_strategy(con, contact_id, strategy, start=None, source="claude"):
    """Put a contact on a strategy: their planned strategy steps are replaced by the template's,
    dated from `start` (default today): weekend steps on the next Saturday (or Sunday), the rest on weekdays,
    each later than the one before. Done steps and manual steps stay."""
    c = get_contact(con, contact_id, with_touches=False)
    if strategy in (None, "", 0):
        con.execute("DELETE FROM steps WHERE contact_id = ? AND origin = 'strategy' AND status IN ('planned', 'paused')",
                    (contact_id,))
        con.execute("UPDATE contacts SET strategy_id = NULL, updated = ? WHERE id = ?", (now(), contact_id))
        _sync_next(con, contact_id)
        con.commit()
        return get_contact(con, contact_id)
    st = get_strategy(con, strategy, _ws(con, c["workspace"])["id"])
    start = date.fromisoformat(start) if start else date.today()
    con.execute("DELETE FROM steps WHERE contact_id = ? AND origin = 'strategy' AND status IN ('planned', 'paused')",
                (contact_id,))
    # steps they already did count toward the new strategy: someone already emailed doesn't get "Email 1" again
    done = dict(con.execute("SELECT type, count(*) FROM steps WHERE contact_id = ? AND status = 'done' GROUP BY type",
                            (contact_id,)).fetchall())
    todo = []
    for s in st["steps"]:
        if done.get(s["type"]):
            done[s["type"]] -= 1
        else:
            todo.append(s)
    first = todo[0]["day"] if todo else 0
    prev = None
    for s in todo:
        prev = _snap(start + timedelta(days=s["day"] - first), s.get("weekend"), prev)
        _insert_step(con, contact_id, s["type"], s["title"], prev.isoformat(), s["subject"], s["body"],
                     origin="strategy", weekend=s.get("weekend"))
    con.execute("UPDATE contacts SET strategy_id = ?, updated = ? WHERE id = ?", (st["id"], now(), contact_id))
    if c["strategy_id"] != st["id"]:
        con.execute("INSERT INTO touches (contact_id, at, kind, summary, source) VALUES (?, ?, 'note', ?, ?)",
                    (contact_id, now(), f"Strategy: {st['name']}", source))
    _sync_next(con, contact_id)
    con.commit()
    return get_contact(con, contact_id)


def strategy_detail(con, strategy_id):
    """One strategy's results: how many people, what share got interested, closed (reached a paid stage)
    or went dead, and for each of its steps how many people did it, are on it now, or still have it ahead."""
    st = get_strategy(con, strategy_id)
    slug = con.execute("SELECT slug FROM workspaces WHERE id = ?", (st["workspace_id"],)).fetchone()["slug"]
    stages = get_workspace(con, slug)["stages"]
    names = [x["name"] for x in stages]
    first = names.index("Interested") if "Interested" in names else len(names)
    interested = {x["name"] for i, x in enumerate(stages) if i >= first and x["kind"] != "closed"}
    paid = {x["name"] for x in stages if x.get("paid")}
    dead = {x["name"] for x in stages if x["kind"] == "closed"}
    people = con.execute(
        "SELECT c.id, c.stage, EXISTS (SELECT 1 FROM touches t WHERE t.contact_id = c.id AND t.kind IN "
        "('email', 'bump', 'call', 'voicemail', 'text', 'linkedin')) AS contacted FROM contacts c WHERE c.strategy_id = ?",
        (strategy_id,)).fetchall()
    ids = [p["id"] for p in people]
    contacted = sum(p["contacted"] for p in people)
    def pct(n):  # one decimal under 10%, so 1 in 239 doesn't read as zero
        v = 100 * n / contacted if contacted else 0
        return round(v, 1) if v < 10 else round(v)
    count = lambda group: sum(p["stage"] in group for p in people)
    steps = [{**t, "done": 0, "now": 0, "ahead": 0, "paused": 0} for t in st["steps"]]
    if ids:
        marks = ",".join("?" * len(ids))
        cur = {}
        for r in con.execute(f"SELECT contact_id, id FROM steps WHERE contact_id IN ({marks}) AND status = 'planned' "
                             f"ORDER BY due IS NULL, due, id", ids):
            cur.setdefault(r["contact_id"], r["id"])
        for r in con.execute(f"SELECT id, contact_id, type, title, status FROM steps WHERE contact_id IN ({marks}) "
                             f"AND origin = 'strategy'", ids):
            t = next((x for x in steps if x["type"] == r["type"] and x["title"] == r["title"]), None)
            if not t:
                continue
            if r["status"] == "done":
                t["done"] += 1
            elif r["status"] == "paused":
                t["paused"] += 1
            elif r["status"] == "planned":
                t["now" if cur.get(r["contact_id"]) == r["id"] else "ahead"] += 1
    return {**st, "volume": len(people), "contacted": contacted,
            "interested": count(interested), "closed": count(paid), "dead": count(dead),
            "interested_pct": pct(count(interested)), "closed_pct": pct(count(paid)), "dead_pct": pct(count(dead)),
            "steps": steps}


def strategy_stats(con, workspace):
    """Per strategy (plus everyone together): how many people, how many were contacted, and how many are
    warm now, booked a meeting, said no or bounced. Based on current stages."""
    ws = get_workspace(con, workspace)
    kind = {s["name"]: s["kind"] for s in ws["stages"]}
    hot = {s["name"] for s in ws["stages"] if s.get("hot")}
    meeting = {s["name"] for s in ws["stages"] if s.get("meeting")}
    paid = {s["name"] for s in ws["stages"] if s.get("paid")}
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM strategies WHERE workspace_id = ?", (ws["id"],))}
    rows = con.execute(
        "SELECT c.strategy_id, c.stage, EXISTS (SELECT 1 FROM touches t WHERE t.contact_id = c.id "
        "AND t.kind IN ('email', 'bump', 'call', 'voicemail', 'text', 'linkedin')) AS contacted "
        "FROM contacts c WHERE c.workspace_id = ?", (ws["id"],)).fetchall()
    out = {}
    for key in ["all", *names, None]:
        rs = [r for r in rows if key == "all" or r["strategy_id"] == key]
        if not rs and key != "all":
            continue
        contacted = sum(r["contacted"] for r in rs)
        warm = sum(r["stage"] in hot or kind.get(r["stage"]) == "won" for r in rs)
        out[str(key)] = {
            "id": None if key == "all" else key,
            "name": "All outreach" if key == "all" else names.get(key, "No strategy"),
            "people": len(rs), "contacted": contacted, "warm": warm,
            "meetings": sum(r["stage"] in meeting or kind.get(r["stage"]) == "won" for r in rs),
            "no": sum(kind.get(r["stage"]) == "closed" and r["stage"] != "Bounced" for r in rs),
            "bounced": sum(r["stage"] == "Bounced" for r in rs),
            "paid": sum(r["stage"] in paid for r in rs),
            "warm_rate": round(100 * warm / contacted) if contacted else 0,
            "by_stage": {s: sum(r["stage"] == s for r in rs) for s in kind},
        }
    return list(out.values())


# ---- contacts -----------------------------------------------------------------------------------

CONTACT_SQL = ("SELECT c.*, w.slug AS workspace, s.name AS strategy FROM contacts c "
               "JOIN workspaces w ON w.id = c.workspace_id LEFT JOIN strategies s ON s.id = c.strategy_id ")


def find_contacts(con, workspace=None, query="", stage=None, limit=50):
    sql = CONTACT_SQL + "WHERE 1=1"
    args = []
    if workspace:
        sql += " AND w.id = ?"
        args.append(_ws(con, workspace)["id"])
    if query:
        sql += " AND (c.company LIKE ? OR c.person LIKE ? OR c.email LIKE ? OR c.phone LIKE ?)"
        args += [f"%{query}%"] * 4
    if stage:
        sql += " AND c.stage = ?"
        args.append(stage)
    sql += " ORDER BY c.updated DESC LIMIT ?"
    args.append(limit)
    return [_contact_dict(r) for r in con.execute(sql, args)]


def get_contact(con, contact_id, with_touches=True):
    row = con.execute(CONTACT_SQL + "WHERE c.id = ?", (contact_id,)).fetchone()
    if not row:
        raise ValueError(f"No contact {contact_id}")
    d = _contact_dict(row)
    d["stale_days"] = stale_days(d, _kinds(con, d["workspace"]))
    if with_touches:
        d["touches"] = [dict(t) for t in con.execute(
            "SELECT * FROM touches WHERE contact_id = ? ORDER BY at DESC, id DESC", (contact_id,))]
        d["steps"] = list_steps(con, contact_id)
    return d


def add_contact(con, workspace, company, person="", stage=None, source="", strategy=None, **fields):
    ws = _ws_dict(_ws(con, workspace))
    email = (fields.get("email") or "").strip().lower()
    if email:
        dup = con.execute("SELECT id FROM contacts WHERE workspace_id = ? AND lower(email) = ?",
                          (ws["id"], email)).fetchone()
        if dup:
            raise ValueError(f"{email} is already contact {dup['id']}")
    stage = _check_stage(ws, stage or ws["stages"][0]["name"])
    t = now()
    custom = fields.pop("custom", {}) or {}
    cols = {k: fields.get(k, "") or "" for k in ("role", "email", "phone", "mobile", "website", "notes", "location")}
    cur = con.execute(
        "INSERT INTO contacts (workspace_id, company, person, stage, stage_changed, source, custom, created, updated, "
        "role, email, phone, mobile, website, notes, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ws["id"], company, person or "", stage, t[:10], source, json.dumps(custom), t, t, *cols.values()))
    con.commit()
    if strategy:
        return apply_strategy(con, cur.lastrowid, strategy, source=source or "claude")
    return get_contact(con, cur.lastrowid)


def update_contact(con, contact_id, **fields):
    """Change plain fields (person, role, email, phone, mobile, website, notes, company, location, hold_until) and
    custom fields. hold_until is a YYYY-MM-DD date that hides the "stuck" warning until then; "" clears it."""
    c = get_contact(con, contact_id, with_touches=False)
    allowed = {"company", "person", "role", "email", "phone", "mobile", "website", "notes", "source", "location", "hold_until"}
    if fields.get("hold_until"):
        try:
            fields["hold_until"] = date.fromisoformat(fields["hold_until"]).isoformat()
        except ValueError:
            raise ValueError(f"hold_until must be a date like 2026-11-16, not '{fields['hold_until']}'")
    email = (fields.get("email") or "").strip().lower()
    if email and email != (c["email"] or "").lower():
        dup = con.execute("SELECT c.id FROM contacts c JOIN workspaces w ON w.id = c.workspace_id "
                          "WHERE w.slug = ? AND lower(c.email) = ? AND c.id != ?",
                          (c["workspace"], email, contact_id)).fetchone()
        if dup:
            raise ValueError(f"{email} is already contact {dup['id']}")
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            args.append(v or "")
    if "custom" in fields and fields["custom"]:
        merged = {**c["custom"], **fields["custom"]}
        sets.append("custom = ?")
        args.append(json.dumps(merged))
    if sets:
        con.execute(f"UPDATE contacts SET {', '.join(sets)}, updated = ? WHERE id = ?", (*args, now(), contact_id))
        con.commit()
    return get_contact(con, contact_id)


AI_STATES = ("seen", "batch", "")


def mark_ai(con, contact_id, state, note=""):
    """Record what the outreach chat has done with someone: seen (it has a plan for them), batch (they are in
    the next outreach batch) or "" to clear. The page shows it as a small mark next to their name."""
    if state not in AI_STATES:
        raise ValueError(f"Bad AI state '{state}'. Use seen, batch, or an empty string to clear.")
    get_contact(con, contact_id, with_touches=False)
    con.execute("UPDATE contacts SET ai_state = ?, ai_note = ?, ai_at = ? WHERE id = ?",
                (state, note if state else "", now() if state else None, contact_id))
    con.commit()
    return get_contact(con, contact_id, with_touches=False)


def _check_stage(ws, stage):
    """The stage's real name. Old names listed in a stage's "aliases" still work, so scripts and tasks
    written before a rename keep working."""
    for s in ws["stages"]:
        if stage == s["name"] or stage in s.get("aliases", []):
            return s["name"]
    raise ValueError(f"Unknown stage '{stage}'. Stages: {', '.join(s['name'] for s in ws['stages'])}")


def move_stage(con, contact_id, stage, reason="", at=None, source="claude"):
    c = get_contact(con, contact_id, with_touches=False)
    ws = get_workspace(con, c["workspace"])
    stage = _check_stage(ws, stage)
    if stage == c["stage"]:
        return c
    at = at or now()
    con.execute("UPDATE contacts SET stage = ?, stage_changed = ?, updated = ? WHERE id = ?",
                (stage, at[:10], now(), contact_id))
    summary = f"{c['stage']} → {stage}" + (f": {reason}" if reason else "")
    con.execute("INSERT INTO touches (contact_id, at, kind, summary, source) VALUES (?, ?, 'stage', ?, ?)",
                (contact_id, at, summary, source))
    new = next(s for s in ws["stages"] if s["name"] == stage)
    old = next((s for s in ws["stages"] if s["name"] == c["stage"]), {})
    if new["kind"] in ("won", "closed"):  # nothing left to do for a finished contact
        con.execute("UPDATE steps SET status = 'skipped', outcome = ?, done_at = ? "
                    "WHERE contact_id = ? AND status IN ('planned', 'paused')", (stage, at, contact_id))
    elif new.get("hot") and not old.get("hot"):  # they're talking to us now; stop the automatic sequence
        con.execute("UPDATE steps SET status = 'paused' WHERE contact_id = ? AND origin = 'strategy' "
                    "AND status = 'planned'", (contact_id,))
    _sync_next(con, contact_id)
    con.commit()
    return get_contact(con, contact_id)


def log_touch(con, contact_id, kind, summary="", at=None, source="claude"):
    get_contact(con, contact_id, with_touches=False)
    con.execute("INSERT INTO touches (contact_id, at, kind, summary, source) VALUES (?, ?, ?, ?, ?)",
                (contact_id, at or now(), kind, summary, source))
    con.execute("UPDATE contacts SET updated = ? WHERE id = ?", (now(), contact_id))
    con.commit()
    return get_contact(con, contact_id)


def _call_stage(con, contact_id, outcome, source):
    """No answer / voicemail move the contact to the matching stage, only forward and never out of a
    won or closed stage."""
    target = CALL_OUTCOMES[outcome][2]
    c = get_contact(con, contact_id, with_touches=False)
    stages = get_workspace(con, c["workspace"])["stages"]
    names = [s["name"] for s in stages]
    if target == "@closed":
        target = next((s["name"] for s in stages if s["kind"] == "closed"), None)
    if target in names and c["stage"] in names:
        cur = names.index(c["stage"])
        if stages[cur]["kind"] in ("open", "active") and names.index(target) > cur:
            move_stage(con, contact_id, target, "", None, source)


def log_call(con, contact_id, outcome, note="", source="web"):
    """Log a quick call outcome. Completes the next planned call step if there is one."""
    if outcome not in CALL_OUTCOMES:
        raise ValueError(f"Unknown call outcome '{outcome}'. Outcomes: {', '.join(CALL_OUTCOMES)}")
    step = con.execute("SELECT id FROM steps WHERE contact_id = ? AND type = 'call' AND status = 'planned' "
                       "ORDER BY due IS NULL, due, id LIMIT 1", (contact_id,)).fetchone()
    if step:
        return complete_step(con, step["id"], outcome, note, source)
    kind, summary, _ = CALL_OUTCOMES[outcome]
    log_touch(con, contact_id, kind, f"{summary}: {note}" if note else summary, source=source)
    _call_stage(con, contact_id, outcome, source)
    email = _after_call_email(con, contact_id) if outcome == "maybe" else None
    _sync_next(con, contact_id)
    con.commit()
    c = get_contact(con, contact_id)
    if email:
        c["after_call_step"] = email
    return c


# ---- steps --------------------------------------------------------------------------------------

STEP_ORDER = ("ORDER BY CASE status WHEN 'planned' THEN 1 WHEN 'paused' THEN 2 ELSE 0 END, "
              "CASE WHEN status IN ('done', 'skipped') THEN coalesce(done_at, due) END, due IS NULL, due, id")


def list_steps(con, contact_id):
    """Done and skipped steps first (oldest first), then planned ones by due date, then paused ones."""
    return [dict(r) for r in con.execute(f"SELECT * FROM steps WHERE contact_id = ? {STEP_ORDER}", (contact_id,))]


def current_step(con, contact_id):
    r = con.execute("SELECT * FROM steps WHERE contact_id = ? AND status = 'planned' ORDER BY due IS NULL, due, id "
                    "LIMIT 1", (contact_id,)).fetchone()
    return dict(r) if r else None


def step_label(s):
    if s.get("title"):
        return s["title"]
    if s["type"] == "email" and s.get("subject"):
        return f"Email: {s['subject']}"
    return STEP_LABELS.get(s["type"], s["type"])


def _get_step(con, step_id):
    r = con.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    if not r:
        raise ValueError(f"No step {step_id}")
    return dict(r)


def _sync_next(con, contact_id):
    """Copy the current step into next_step / next_due / next_draft, which the CLI and older tools read."""
    s = current_step(con, contact_id)
    con.execute("UPDATE contacts SET next_step = ?, next_due = ?, next_draft = ? WHERE id = ?",
                (step_label(s) if s else "", s["due"] if s else None, s["body"] if s else "", contact_id))


def _insert_step(con, contact_id, type, title="", due=None, subject="", body="", origin="manual", weekend=False):
    if type not in STEP_TYPES:
        raise ValueError(f"Bad step type '{type}'. Types: {', '.join(STEP_TYPES)}")
    if due:
        date.fromisoformat(due)
    return con.execute("INSERT INTO steps (contact_id, type, title, due, subject, body, origin, weekend, created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (contact_id, type, title or "", due or None, subject or "", body or "", origin, int(bool(weekend)), now())).lastrowid


def add_step(con, contact_id, type, title="", due=None, subject="", body=""):
    get_contact(con, contact_id, with_touches=False)
    sid = _insert_step(con, contact_id, type, title, due, subject, body)
    con.execute("UPDATE contacts SET updated = ? WHERE id = ?", (now(), contact_id))
    _sync_next(con, contact_id)
    con.commit()
    return _get_step(con, sid)


def edit_step(con, step_id, **fields):
    """Change a step's type, title, due date, subject or body. status 'planned' resumes a paused step."""
    s = _get_step(con, step_id)
    sets, args = [], []
    for k in ("type", "title", "due", "subject", "body"):
        if k in fields:
            v = fields[k]
            if k == "type" and v not in STEP_TYPES:
                raise ValueError(f"Bad step type '{v}'. Types: {', '.join(STEP_TYPES)}")
            if k == "due" and v:
                date.fromisoformat(v)
            sets.append(f"{k} = ?")
            args.append(v or (None if k == "due" else ""))
    if fields.get("status") == "planned" and s["status"] == "paused":
        sets.append("status = 'planned'")
    if sets:
        con.execute(f"UPDATE steps SET {', '.join(sets)} WHERE id = ?", (*args, step_id))
        con.execute("UPDATE contacts SET updated = ? WHERE id = ?", (now(), s["contact_id"]))
        _sync_next(con, s["contact_id"])
        con.commit()
    return _get_step(con, step_id)


def delete_step(con, step_id):
    """Remove a step that hasn't happened yet. Done steps are history and stay."""
    s = _get_step(con, step_id)
    if s["status"] not in ("planned", "paused"):
        raise ValueError("Only planned steps can be removed. Done steps are part of the history.")
    con.execute("DELETE FROM steps WHERE id = ?", (step_id,))
    _sync_next(con, s["contact_id"])
    con.commit()
    return {"deleted": step_id}


def skip_step(con, step_id, source="web"):
    s = _get_step(con, step_id)
    con.execute("UPDATE steps SET status = 'skipped', done_at = ? WHERE id = ?", (now(), step_id))
    _sync_next(con, s["contact_id"])
    con.commit()
    return get_contact(con, s["contact_id"])


def complete_step(con, step_id, outcome="", note="", source="web", at=None):
    """Do a step: mark it done and log what happened on the timeline.
    Calls take an outcome (no_answer, voicemail, talked). A close step moves the contact to the first
    closed stage (or the stage named in `outcome`) with the step's reason."""
    s = _get_step(con, step_id)
    cid, at = s["contact_id"], at or now()
    if s["status"] not in ("planned", "paused"):
        raise ValueError("That step is already done")
    if source == "web":
        _save_undo(con, s)
    label = step_label(s)
    extra = f": {note}" if note else ""
    if s["type"] == "call":
        outcome = outcome or "talked"
        if outcome not in CALL_OUTCOMES:
            raise ValueError(f"Unknown call outcome '{outcome}'. Outcomes: {', '.join(CALL_OUTCOMES)}")
        kind, summary, _ = CALL_OUTCOMES[outcome]
        _finish(con, step_id, summary, at)
        log_touch(con, cid, kind, summary + extra, at, source)
        _call_stage(con, cid, outcome, source)
        email = _after_call_email(con, cid) if outcome == "maybe" else None
    elif s["type"] == "close":
        ws = get_workspace(con, get_contact(con, cid, False)["workspace"])
        closed = [x["name"] for x in ws["stages"] if x["kind"] == "closed"]
        stage = outcome if outcome in closed else (closed[0] if closed else None)
        _finish(con, step_id, stage or "closed", at)
        if stage:
            move_stage(con, cid, stage, note or s["body"] or "no reply", at, source)
    else:
        kind = {"email": "email", "text": "text", "linkedin": "linkedin", "meeting": "meeting"}.get(s["type"], "note")
        what = f"Sent: {s['subject'] or label}" if s["type"] == "email" else f"Done: {label}"
        _finish(con, step_id, outcome or ("sent" if s["type"] in ("email", "text", "linkedin") else "done"), at)
        log_touch(con, cid, kind, what + extra, at, source)
    _sync_next(con, cid)
    con.commit()
    c = get_contact(con, cid)
    if s["type"] == "call" and email:
        c["after_call_step"] = email
    return c


def _save_undo(con, s):
    """Remember how the contact stood before a step was done on the page, so the call session can undo it."""
    c = get_contact(con, s["contact_id"], with_touches=False)
    last = con.execute("SELECT coalesce(max(id), 0) FROM touches WHERE contact_id = ?", (c["id"],)).fetchone()[0]
    steps = [[r["id"], r["status"], r["outcome"], r["done_at"]] for r in
             con.execute("SELECT id, status, outcome, done_at FROM steps WHERE contact_id = ?", (c["id"],))]
    data = {"contact": c["id"], "stage": c["stage"], "stage_changed": c["stage_changed"], "touch": last, "steps": steps}
    con.execute("INSERT OR REPLACE INTO undo (step_id, data, at) VALUES (?, ?, ?)", (s["id"], json.dumps(data), now()))


def undo_step(con, step_id):
    """Put back a step done on the page: its status, the contact's stage and steps, and drop the timeline
    entries it added. Only the most recent thing done for that contact can be undone."""
    row = con.execute("SELECT data FROM undo WHERE step_id = ?", (step_id,)).fetchone()
    if not row:
        raise ValueError("That step can't be undone from here. Change it in their panel instead.")
    d = json.loads(row["data"])
    newer = con.execute("SELECT 1 FROM undo u WHERE u.step_id != ? AND json_extract(u.data, '$.contact') = ? "
                        "AND u.at > (SELECT at FROM undo WHERE step_id = ?)", (step_id, d["contact"], step_id)).fetchone()
    if newer:
        raise ValueError("Something newer was logged for them. Undo that first, or fix it in their panel.")
    con.execute("DELETE FROM touches WHERE contact_id = ? AND id > ?", (d["contact"], d["touch"]))
    known = [x[0] for x in d["steps"]]  # steps the logged call added (the after-call email) go too, if still planned
    con.execute(f"DELETE FROM steps WHERE contact_id = ? AND status = 'planned' AND id NOT IN ({','.join('?' * len(known)) or 'NULL'}) "
                "AND created >= (SELECT at FROM undo WHERE step_id = ?)", (d["contact"], *known, step_id))
    for sid, status, outcome, done_at in d["steps"]:
        con.execute("UPDATE steps SET status = ?, outcome = ?, done_at = ? WHERE id = ?", (status, outcome, done_at, sid))
    con.execute("UPDATE contacts SET stage = ?, stage_changed = ?, updated = ? WHERE id = ?",
                (d["stage"], d["stage_changed"], now(), d["contact"]))
    con.execute("DELETE FROM undo WHERE step_id = ?", (step_id,))
    _sync_next(con, d["contact"])
    con.commit()
    return get_contact(con, d["contact"])


def _finish(con, step_id, outcome, at):
    con.execute("UPDATE steps SET status = 'done', outcome = ?, done_at = ? WHERE id = ?", (outcome, at, step_id))


def finish_step_of_type(con, contact_id, type, outcome, at=None):
    """Mark the next planned step of a type done without logging a touch (the caller already did),
    e.g. the sender logs an email and this checks off the matching email step."""
    s = con.execute("SELECT id FROM steps WHERE contact_id = ? AND type = ? AND status = 'planned' "
                    "ORDER BY due IS NULL, due, id LIMIT 1", (contact_id, type)).fetchone()
    if s:
        _finish(con, s["id"], outcome, at or now())
        _sync_next(con, contact_id)
        con.commit()
    return bool(s)


GUESS = (("linkedin", "linkedin"), ("text", "text"), ("call", "call"), ("meeting", "meeting"), ("demo", "meeting"),
         ("email", "email"), ("reply", "email"), ("send", "email"), ("bump", "email"), ("nudge", "email"))


def _guess_type(text):
    """Step type from a free-text step: the first word (the verb) wins, then any word in it."""
    words = text.lower().replace(":", " ").split()
    for word, kind in GUESS:
        if words and words[0].startswith(word):
            return kind
    for word, kind in GUESS:
        if word in words:
            return kind
    return "task"


def _make_room(con, contact_id, due):
    """A hand-set step due on `due` comes first: "call back Tuesday" means the strategy waits until after
    Tuesday, so its planned steps all move back by the same amount, keeping their spacing."""
    if not due:
        return
    first = con.execute("SELECT min(due) FROM steps WHERE contact_id = ? AND origin = 'strategy' "
                        "AND status = 'planned'", (contact_id,)).fetchone()[0]
    if not first or first > due:
        return
    shift = date.fromisoformat(due) - date.fromisoformat(first) + timedelta(days=1)
    prev = date.fromisoformat(due)
    for s in con.execute("SELECT id, due, weekend FROM steps WHERE contact_id = ? AND origin = 'strategy' "
                         "AND status = 'planned' AND due IS NOT NULL ORDER BY due, id", (contact_id,)).fetchall():
        prev = _snap(date.fromisoformat(s["due"]) + shift, s["weekend"], prev)
        con.execute("UPDATE steps SET due = ? WHERE id = ?", (prev.isoformat(), s["id"]))


def set_next_step(con, contact_id, step, due=None, draft=None, source="claude"):
    """Save what happens next. An empty step marks the current step done.
    A step set here (by Claude, a script or the old next-step box) replaces the current hand-made step,
    or goes in front of the strategy's steps when the current one came from a strategy."""
    get_contact(con, contact_id, with_touches=False)
    if due:
        date.fromisoformat(due)
    cur = current_step(con, contact_id)
    if not step:
        if cur:
            _finish(con, cur["id"], "done", now())
            con.execute("INSERT INTO touches (contact_id, at, kind, summary, source) VALUES (?, ?, 'note', ?, ?)",
                        (contact_id, now(), f"Done: {step_label(cur)}", source))
    elif cur and cur["origin"] == "manual":
        con.execute("UPDATE steps SET title = ?, due = ?, body = ? WHERE id = ?",
                    (step, due or None, draft if draft is not None else cur["body"], cur["id"]))
        _make_room(con, contact_id, due)
    else:
        if not due and cur and cur["due"]:
            due = min(cur["due"], date.today().isoformat())  # stay in front of the strategy's next step
        _make_room(con, contact_id, due)
        _insert_step(con, contact_id, _guess_type(step), step, due, "", draft or "")
    con.execute("UPDATE contacts SET updated = ? WHERE id = ?", (now(), contact_id))
    _sync_next(con, contact_id)
    con.commit()
    return get_contact(con, contact_id)


def due(con, workspace=None, through=None):
    """Next steps due on or before `through` (default today), oldest first, then open ones with no date."""
    through = through or date.today().isoformat()
    sql = CONTACT_SQL + "WHERE c.next_step != ''"
    args = []
    if workspace:
        sql += " AND w.id = ?"
        args.append(_ws(con, workspace)["id"])
    rows = [_contact_dict(r) for r in con.execute(sql + " ORDER BY c.next_due IS NULL, c.next_due", args)]
    stuck = []  # everyone over STALE_DAYS in one stage, longest first, whether or not they have a next step
    for r in con.execute(CONTACT_SQL + ("WHERE w.id = ?" if workspace else ""), args):
        c = _contact_dict(r)
        c["stale_days"] = stale_days(c, _kinds(con, c["workspace"]))
        if c["stale_days"]:
            stuck.append(c)
    return {"due": [r for r in rows if r["next_due"] and r["next_due"] <= through],
            "upcoming": [r for r in rows if r["next_due"] and r["next_due"] > through],
            "undated": [r for r in rows if not r["next_due"]],
            "stuck": sorted(stuck, key=lambda c: -c["stale_days"]),
            "weekend_calls": weekend_calls(con, workspace)}


def weekend_calls(con, workspace=None):
    """The coming weekend's calls (this weekend's when it's Saturday or Sunday), with the date and each person's
    name, company and phone, plus how many weekend calls are already overdue."""
    today = date.today()
    sat = _weekend(today) - timedelta(days=1 if today.weekday() == 6 else 0)
    sun = sat + timedelta(days=1)
    sql = ("SELECT s.id AS step_id, s.title, s.due, c.id, c.person, c.company, c.phone, c.mobile, c.stage FROM steps s "
           "JOIN contacts c ON c.id = s.contact_id JOIN workspaces w ON w.id = c.workspace_id "
           "WHERE s.status = 'planned' AND s.type = 'call' AND s.weekend = 1")
    args = []
    if workspace:
        sql += " AND w.id = ?"
        args.append(_ws(con, workspace)["id"])
    rows = [dict(r) for r in con.execute(sql + " ORDER BY s.due, c.company", args)]
    return {"saturday": sat.isoformat(), "sunday": sun.isoformat(),
            "calls": [r for r in rows if sat.isoformat() <= r["due"] <= sun.isoformat()],
            "overdue": sum(r["due"] < sat.isoformat() for r in rows)}


def pipeline(con, workspace):
    ws = get_workspace(con, workspace)
    counts = dict(con.execute("SELECT stage, count(*) FROM contacts WHERE workspace_id = ? GROUP BY stage",
                              (ws["id"],)).fetchall())
    return [{**s, "count": counts.get(s["name"], 0)} for s in ws["stages"]]


def overview(con, workspace, query=""):
    """Everything the home screen needs: contacts with their current step and progress, plus stats."""
    contacts = find_contacts(con, workspace, query, limit=5000)
    ids = [c["id"] for c in contacts]
    prog, cur, last, said, upd = {}, {}, {}, {}, {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in con.execute(f"SELECT contact_id, sum(status = 'done') AS done, sum(status = 'planned') AS planned, "
                             f"sum(status = 'paused') AS paused, sum(status = 'skipped') AS skipped FROM steps WHERE contact_id IN ({marks}) "
                             f"GROUP BY contact_id", ids):
            prog[r["contact_id"]] = dict(r)
        path = {}  # each person's steps in timeline order (behind them, then planned, then paused), for the step dots
        for r in con.execute(f"SELECT contact_id, type, status FROM steps WHERE contact_id IN ({marks}) ORDER BY contact_id, "
                             f"CASE status WHEN 'planned' THEN 1 WHEN 'paused' THEN 2 ELSE 0 END, "
                             f"coalesce(done_at, due), due IS NULL, due, id", ids):
            path.setdefault(r["contact_id"], []).append([r["type"], r["status"]])
        for r in con.execute(f"SELECT * FROM steps WHERE contact_id IN ({marks}) AND status = 'planned' "
                             f"ORDER BY due IS NULL, due, id", ids):
            cur.setdefault(r["contact_id"], dict(r))
        said = {}  # the latest real contact per person: when, and what was said
        for r in con.execute(f"SELECT contact_id, at, summary FROM touches WHERE contact_id IN ({marks}) AND kind IN "
                             f"('email', 'bump', 'call', 'voicemail', 'reply', 'text', 'linkedin', 'meeting') "
                             f"ORDER BY at, id", ids):
            said[r["contact_id"]] = r
        last = {k: v["at"] for k, v in said.items()}
        # the latest update of any kind (a call, a note, a stage move), for the Needs updates list
        upd = dict(con.execute(f"SELECT contact_id, max(at) FROM touches WHERE contact_id IN ({marks}) GROUP BY contact_id", ids).fetchall())
    kinds = _kinds(con, contacts[0]["workspace"]) if contacts else {}
    for c in contacts:
        c["stale_days"] = stale_days(c, kinds)
        c["progress"] = prog.get(c["id"], {"done": 0, "planned": 0, "paused": 0, "skipped": 0}) | {"steps": path.get(c["id"], [])}
        c["last_touch"] = last.get(c["id"])
        c["last_update"] = upd.get(c["id"])
        c["last_said"] = said[c["id"]]["summary"] if c["id"] in said else ""
        s = cur.get(c["id"])
        c["current"] = {k: s[k] for k in ("id", "type", "due", "subject", "weekend")} | {"label": step_label(s)} if s else None
        if s and s["type"] == "call":
            c["current"]["body"] = s["body"]  # talking points for the call session
    return {"stages": pipeline(con, workspace), "contacts": contacts, "stats": strategy_stats(con, workspace),
            "strategies": list_strategies(con, workspace)}
