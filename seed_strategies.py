"""One-time move from a single next step to strategies and steps (10/29).

Creates the two Terms strategies, marks the warm and meeting stages, and gives every contact steps:
done ones matched from their timeline, their hand-written next step kept as the current step, and the
rest of their strategy planned from there. Does nothing if Terms already has strategies.

    .venv/bin/python seed_strategies.py
"""

from datetime import date, timedelta

from crm import db

STRATEGIES = {
    "Email first": [("email", 0, "Email 1"), ("email", 3, "Bump"), ("call", 5, "Call"),
                    ("call", 9, "Second call"), ("close", 14, "Close if no reply")],
    "Call first": [("call", 0, "Call"), ("email", 1, "Email 1"), ("email", 4, "Bump"),
                   ("call", 7, "Second call"), ("close", 14, "Close if no reply")],
}
DESCRIPTIONS = {"Email first": "Email, bump, two calls, then close after two weeks of quiet",
                "Call first": "Call, email, bump, a second call, then close after two weeks of quiet"}
HOT = {"Call back", "Replied", "Interested", "Meeting booked", "Trying Terms"}
MEETING = {"Meeting booked", "Trying Terms"}
MATCH = {"email": ("email", "bump"), "call": ("call", "voicemail")}


def main():
    con = db.connect()
    if db.list_strategies(con, "terms"):
        print("Terms already has strategies, nothing to do")
        return
    ws = db.get_workspace(con, "terms")
    for s in ws["stages"]:
        s["hot"], s["meeting"] = s["name"] in HOT, s["name"] in MEETING
    db.save_workspace(con, "terms", ws["name"], ws["stages"], ws["description"], ws["fields"])
    kind = {s["name"]: s["kind"] for s in ws["stages"]}
    ids = {name: db.save_strategy(con, "terms", name, [{"type": t, "day": d, "title": ti} for t, d, ti in steps],
                                  DESCRIPTIONS[name])["id"]
           for name, steps in STRATEGIES.items()}
    today = date.today()
    counts = {"done": 0, "planned": 0, "paused": 0, "manual": 0}

    for c in db.find_contacts(con, "terms", limit=5000):
        group = c["custom"].get("Group", "")
        name = "Email first" if group == "Email first" else "Call first" if group.startswith("Call first") else None
        if not name:
            continue
        con.execute("UPDATE contacts SET strategy_id = ? WHERE id = ?", (ids[name], c["id"]))
        touches = con.execute("SELECT at, kind, summary FROM touches WHERE contact_id = ? ORDER BY at, id",
                              (c["id"],)).fetchall()
        queues = {t: [x for x in touches if x["kind"] in kinds] for t, kinds in MATCH.items()}

        remaining, anchor = [], None  # anchor = (date, day) of the latest matched step
        for type, day, title in STRATEGIES[name]:
            q = queues.get(type)
            if q:
                t = q.pop(0)
                sid = db._insert_step(con, c["id"], type, title, None, origin="strategy")
                db._finish(con, sid, t["summary"][:120], t["at"])
                counts["done"] += 1
                at = date.fromisoformat(t["at"][:10])
                if not anchor or at >= anchor[0]:
                    anchor = (at, day)
            else:
                remaining.append((type, day, title))

        if kind.get(c["stage"]) in ("won", "closed"):
            continue
        hand_made = bool(c["next_step"])
        if hand_made:
            db._insert_step(con, c["id"], db._guess_type(c["next_step"]), c["next_step"], c["next_due"], "",
                            c["next_draft"])
            counts["manual"] += 1
        status = "paused" if hand_made or c["stage"] in HOT or c["stage"] == "Not now" else "planned"
        # date the rest from the last thing they did; if that's already past, the whole rest moves up to
        # start today and keeps its spacing
        raw = [anchor[0] + timedelta(days=day - anchor[1]) if anchor else today + timedelta(days=day)
               for _, day, _ in remaining]
        shift = max(timedelta(0), today - raw[0]) if raw else timedelta(0)
        last = today
        for (type, day, title), r in zip(remaining, raw):
            due = db._workday(max(r + shift, last))
            last = due
            sid = db._insert_step(con, c["id"], type, title, due.isoformat(), origin="strategy")
            if status == "paused":
                con.execute("UPDATE steps SET status = 'paused' WHERE id = ?", (sid,))
            counts[status] += 1
        db._sync_next(con, c["id"])
    con.commit()
    print(counts)


if __name__ == "__main__":
    main()
