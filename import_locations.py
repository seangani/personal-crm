"""Fill in contacts' city and state from the Deepline lists in the vault (Terms/Deepline/*.csv), matched by
company name. Only empty locations are filled, so a location typed in the app is never overwritten. Reads the
lists, never changes them. Safe to run again after new lists arrive.

    .venv/bin/python import_locations.py
"""

import csv
import glob
import re
from pathlib import Path

from crm import db

LISTS = Path(__file__).resolve().parents[2] / "Terms" / "Deepline"


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def read_locations():
    found = {}
    for path in sorted(glob.glob(str(LISTS / "*.csv"))):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                place = next((v for k, v in r.items() if ("city" in k or k == "location") and v), "") or r.get("state", "")
                company = r.get("company") or r.get("company name") or r.get("organization") or ""
                if place and company:
                    found.setdefault(norm(company), place)
    return found


def main():
    places = read_locations()
    con = db.connect()
    filled = 0
    for c in con.execute("SELECT id, company FROM contacts WHERE coalesce(location, '') = ''").fetchall():
        place = places.get(norm(c["company"]))
        if place:
            con.execute("UPDATE contacts SET location = ? WHERE id = ?", (place, c["id"]))
            filled += 1
    con.commit()
    left = con.execute("SELECT count(*) FROM contacts WHERE coalesce(location, '') = ''").fetchone()[0]
    print({"filled": filled, "still_missing": left})


if __name__ == "__main__":
    main()
