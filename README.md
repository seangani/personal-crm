# CRM

A small CRM that runs on this Mac: a web page at http://localhost:8787 and an MCP server at
`/mcp` so Claude can read and update it. One SQLite file (`data/crm.db`, not in git) holds every
workspace. A workspace is one company you do outreach for, with its own stages and custom fields.

- `bash install.sh` keeps the server running at login and connects it to Claude Code as `crm`.
- In Safari: File > Add to Dock to get it as a desktop app.
- `crm/db.py` storage and rules, `crm/app.py` web API + MCP tools, `crm/static/index.html` the page.
- `import_terms.py` was the one-time move from `Terms GTM.xlsx`; `seed_strategies.py` was the
  one-time move from a single next step to strategies and steps (10/29).
- `import_locations.py` fills empty contact locations from the Deepline lists in `Terms/Deepline` (safe to re-run).

Each contact has steps (email, call, text, LinkedIn, meeting, to do, close): done, current and planned. A strategy is
a template of steps, so a new person gets a whole sequence. `next_step` / `next_due` / `next_draft` mirror the current
step, so the CLI and older tools read it like before.

Rule for Claude: whenever you propose a next step for a contact, save it with `set_next_step` (or `add_step` /
`edit_step` for a typed step with an email subject and message).

The outreach chat tags people it has handled with `mark_ai` (MCP) or `crm.cli mark-ai`: `seen` means it has a plan
for them, `batch` means they are in the next outreach batch. The Overview shows a small mark next to their name.
Calls logged from the Overview's call session can be undone (`POST /api/steps/{id}/undo`).
