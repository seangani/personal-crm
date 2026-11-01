"""One-time update of the Terms pipeline after Interested (10/29): Conference call, Testing Terms,
Feedback call and Client story. Feedback call and Client story are the paid stages.

Old names stay as aliases (Meeting booked, Trying Terms, Won), so the 8 AM task and older notes keep
working, and anyone sitting on an old stage moves to its new name. Does nothing if already done.

    .venv/bin/python seed_paid_stages.py
"""

from crm import db

AFTER_INTERESTED = [
    {"name": "Conference call", "kind": "active", "hot": True, "meeting": True, "aliases": ["Meeting booked"]},
    {"name": "Testing Terms", "kind": "active", "hot": True, "meeting": True, "aliases": ["Trying Terms"]},
    {"name": "Feedback call", "kind": "active", "hot": True, "meeting": True, "paid": True},
    {"name": "Client story", "kind": "won", "paid": True, "aliases": ["Won"]},
]


def main():
    con = db.connect()
    ws = db.get_workspace(con, "terms")
    names = [s["name"] for s in ws["stages"]]
    if "Client story" in names:
        print("Terms already has the paid stages, nothing to do")
        return
    i = names.index("Interested") + 1
    end = names.index("Won") + 1
    stages = ws["stages"][:i] + AFTER_INTERESTED + ws["stages"][end:]
    db.save_workspace(con, "terms", ws["name"], stages, ws["description"], ws["fields"])
    moved = 0
    for new in AFTER_INTERESTED:
        for old in new.get("aliases", []):
            moved += con.execute("UPDATE contacts SET stage = ? WHERE workspace_id = ? AND stage = ?",
                                 (new["name"], ws["id"], old)).rowcount
    con.commit()
    print({"stages": [s["name"] for s in stages], "contacts_moved": moved})


if __name__ == "__main__":
    main()
