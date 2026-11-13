"""The CRM server: the web page, its JSON API, and the MCP tools Claude uses, all on one port.

    .venv/bin/python -m crm.app            # http://localhost:8787  (MCP at /mcp)
"""

import json
import os
import time
import subprocess
from urllib.parse import quote
from datetime import date
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from . import db

PORT = int(os.environ.get("CRM_PORT", 8787))
STATIC = Path(__file__).resolve().parent / "static"
# Extra host names allowed to reach the server, e.g. a Tailscale name: CRM_HOSTS="sean-mac.tail1234.ts.net"
EXTRA_HOSTS = [h for h in os.environ.get("CRM_HOSTS", "").split(",") if h]

mcp = FastMCP(
    "crm",
    instructions=(
        "Sean's CRM. Each workspace is one company he does outreach for, with its own pipeline stages. "
        "Each contact has steps (email, call, text, linkedin, meeting, task, close): done ones, a current one and "
        "planned ones, usually made from a strategy (a named template like 'Email first'). "
        "Rules: look a person up (find_contacts) before adding them. Log every call, email, reply or text "
        "with log_touch, or with complete_step when it was a planned step. Whenever you propose a next step for "
        "a contact, save it right away (set_next_step, or add_step / edit_step for a typed step with an email "
        "subject and message) so it is never lost. Move stages only on real events, and say why."),
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", *EXTRA_HOSTS, *[f"{h}:*" for h in EXTRA_HOSTS]],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*", *[f"https://{h}" for h in EXTRA_HOSTS]],
    ),
)


db.ACTOR.set("claude")


def _with(fn, *a, **kw):
    con = db.connect()
    try:
        return fn(con, *a, **kw)
    finally:
        con.close()


def _slim(c):
    keep = ("id", "workspace", "company", "person", "role", "email", "phone", "stage", "stage_changed",
            "strategy", "next_step", "next_due", "next_draft", "custom", "location", "ai_state", "ai_note",
            "hold_until", "stale_days")
    return {k: c[k] for k in keep if c.get(k) not in (None, "", {})}


# ---- MCP tools ----------------------------------------------------------------------------------

@mcp.tool()
def list_workspaces() -> list[dict]:
    """List workspaces (one per company Sean works for) with their stages and custom fields."""
    return _with(db.list_workspaces)


@mcp.tool()
def pipeline(workspace: str) -> list[dict]:
    """Stages of a workspace in order, with how many contacts sit in each."""
    return _with(db.pipeline, workspace)


@mcp.tool()
def find_contacts(query: str = "", workspace: str = "", stage: str = "", limit: int = 25) -> list[dict]:
    """Search contacts by company, person, email or phone. Optionally filter by workspace and stage."""
    return [_slim(c) for c in _with(db.find_contacts, workspace or None, query, stage or None, limit)]


@mcp.tool()
def get_contact(contact_id: int) -> dict:
    """One contact with their full timeline (every call, email, reply and stage change)."""
    return _with(db.get_contact, contact_id)


@mcp.tool()
def add_contact(workspace: str, company: str, person: str = "", role: str = "", email: str = "", phone: str = "",
                website: str = "", stage: str = "", source: str = "", notes: str = "",
                custom: dict | None = None, location: str = "") -> dict:
    """Add a person. Stage defaults to the first stage of the workspace. Refuses duplicate emails.
    location is the city and state, e.g. "Austin, TX"."""
    return _slim(_with(db.add_contact, workspace, company, person, stage or None, source, role=role, email=email,
                       phone=phone, website=website, notes=notes, custom=custom or {}, location=location))


@mcp.tool()
def update_contact(contact_id: int, company: str = "", person: str = "", role: str = "", email: str = "",
                   phone: str = "", mobile: str = "", website: str = "", notes: str = "",
                   custom: dict | None = None, location: str = "", hold_until: str = "") -> dict:
    """Change a contact's details. Only the fields you pass are changed. `custom` merges into custom fields.
    Anyone in one stage over 4 days gets a "stuck" warning. hold_until (YYYY-MM-DD) hides it until that date, for
    people who asked to be contacted later (e.g. "connect mid-November"); pass "none" to clear the hold."""
    fields = {k: v for k, v in dict(company=company, person=person, role=role, email=email, phone=phone,
                                    mobile=mobile, website=website, notes=notes, location=location).items() if v}
    if hold_until:
        fields["hold_until"] = "" if hold_until.lower() == "none" else hold_until
    return _slim(_with(db.update_contact, contact_id, custom=custom or {}, **fields))


@mcp.tool()
def log_touch(contact_id: int, kind: str, summary: str, at: str = "", reply_text: str = "", subject: str = "",
              body: str = "") -> dict:
    """Record a call, email, bump, voicemail, reply, text, meeting or note. `at` is YYYY-MM-DD[THH:MM], default now.
    For an email or bump Sean sent, pass the text he sent as body and its subject: the Conversations tab shows it.
    For a reply, pass everything they wrote as reply_text (and the email's subject): the person panel shows it at
    the top and the lists show a preview."""
    return _slim(_with(db.log_touch, contact_id, kind, summary, at or None, "claude", body or reply_text, subject))


@mcp.tool()
def find_touches(contact_id: int, kind: str = "", on: str = "") -> list[dict]:
    """A person's timeline entries with ids, oldest first (text_len is how much text each holds). Filter by kind
    (email, bump, reply, call...) and/or day `on` (YYYY-MM-DD). Use with set_touch_detail."""
    return _with(db.find_touches, contact_id, kind or None, on or None)


@mcp.tool()
def set_touch_detail(touch_id: int, subject: str = "", body: str = "") -> dict:
    """Fill in an existing timeline entry's subject and text, e.g. the email Sean actually sent, instead of
    logging a duplicate. Empty leaves that part unchanged."""
    return _with(db.set_touch_detail, touch_id, subject or None, body or None)


@mcp.tool()
def outreach_analytics(workspace: str) -> dict:
    """How a workspace's outreach is doing, live from the CRM: people contacted, emails sent (first emails, bumps,
    Sean's own replies), response rate, call connect rate, 1st and 2nd conversations, warm, meetings, clients,
    the funnel, weekly activity, results by email subject, reply timing and email-only vs called."""
    return _with(db.analytics, workspace)


@mcp.tool()
def recent_changes(workspace: str = "", actor: str = "", since: str = "", contact_id: int = 0, limit: int = 100) -> list[dict]:
    """The change history, newest first: who changed which person and what changed. actor is web (Sean on the
    page), claude, task (scripts), inbox or sender; since is YYYY-MM-DD."""
    return _with(db.list_changes, workspace or None, actor or None, since or None, None, contact_id or None, limit)


@mcp.tool()
def undo_changes(change_id: int = 0, actor: str = "", since: str = "", until: str = "", workspace: str = "",
                 preview: bool = True) -> dict:
    """Undo one change (change_id), putting that person back as they were just before it (later changes to them
    go too), or everything one actor did from since to until (YYYY-MM-DD). People someone else changed afterwards
    are skipped. With an actor, preview=True (the default) only lists what would happen; ask Sean before preview=False."""
    if change_id:
        return _with(db.undo_change, change_id, "claude")
    if not actor or not since:
        raise ValueError("Pass a change_id, or an actor and a since date")
    return _with(db.undo_actor, actor, since, until or None, workspace or None, preview, "claude")


@mcp.tool()
def kill(contact_id: int, stage: str = "", undo: bool = False) -> dict:
    """Mark a lead dead: move them to a closed stage (default Not interested; or Do not contact), skipping every
    planned step so they leave the call lists and warm tabs. Nothing is deleted. undo=True puts back how they
    stood before the kill."""
    if undo:
        return _slim(_with(db.undo_step, -contact_id))
    return _slim(_with(db.kill, contact_id, stage or None, "claude"))


@mcp.tool()
def mark_client(contact_id: int, undo: bool = False) -> dict:
    """Mark someone an active client (Terms: the Active client stage). Their planned steps stay. undo=True
    puts back how they stood before."""
    if undo:
        return _slim(_with(db.undo_step, -contact_id))
    return _slim(_with(db.mark_client, contact_id, "claude"))


@mcp.tool()
def move_stage(contact_id: int, stage: str, reason: str = "", at: str = "") -> dict:
    """Move a contact to another pipeline stage, with the reason. Closing stages clear the next step."""
    return _slim(_with(db.move_stage, contact_id, stage, reason, at or None, "claude"))


@mcp.tool()
def set_next_step(contact_id: int, step: str, due: str = "", draft: str = "") -> dict:
    """Save the next step for a contact (due YYYY-MM-DD, optional ready-to-send draft). Empty step = done."""
    return _slim(_with(db.set_next_step, contact_id, step, due or None, draft or None, "claude"))


@mcp.tool()
def whats_due(workspace: str = "", through: str = "") -> dict:
    """Next steps due today or earlier (or through a YYYY-MM-DD date), plus upcoming and undated ones, plus
    "stuck": everyone over 4 days in one stage (not won, closed or on hold), longest first, plus
    "weekend_calls": the coming weekend's calls (Saturday and Sunday dates, who, phone) and how many are overdue."""
    r = _with(db.due, workspace or None, through or None)
    return {k: v if k == "weekend_calls" else [_slim(c) for c in v] for k, v in r.items()}


@mcp.tool()
def list_strategies(workspace: str) -> list[dict]:
    """Strategies (step templates) of a workspace, with their steps and how many people follow each."""
    return _with(db.list_strategies, workspace)


@mcp.tool()
def strategy_results(strategy_id: int) -> dict:
    """How a strategy is doing: volume, contacted, % interested, % closed (reached a paid stage), % dead, and for
    each of its steps how many people did it, are on it now, or still have it ahead."""
    return _with(db.strategy_detail, strategy_id)


@mcp.tool()
def save_strategy(workspace: str, name: str, steps: list[dict], description: str = "", strategy_id: int = 0) -> dict:
    """Create a strategy, or change one by passing strategy_id. steps = [{type, day, title, subject, body, weekend}];
    type is email, call, text, linkedin, meeting, task or close; day counts from the start (0 = first day).
    weekend: true puts that step on the next Saturday (Saturday or Sunday stay as is), for Sean's weekend calls;
    other steps move off weekends to Monday. Saving changes only people put on it afterwards."""
    return _with(db.save_strategy, workspace, name, steps, description, strategy_id or None)


@mcp.tool()
def apply_strategy(contact_id: int, strategy: str, start: str = "") -> dict:
    """Put a contact on a strategy (name or id). Their planned strategy steps are replaced, dated from start
    (YYYY-MM-DD, default today). Done steps and hand-made steps stay. Empty strategy takes them off it."""
    return _slim(_with(db.apply_strategy, contact_id, strategy, start or None, "claude"))


@mcp.tool()
def add_step(contact_id: int, type: str, title: str = "", due: str = "", subject: str = "", body: str = "") -> dict:
    """Plan a step for a contact. type: email, call, text, linkedin, meeting, task or close. For an email,
    subject + body are the ready-to-send message; for a call, body is the talking points."""
    return _with(db.add_step, contact_id, type, title, due or None, subject, body)


@mcp.tool()
def edit_step(step_id: int, type: str = "", title: str = "", due: str = "", subject: str = "", body: str = "",
              resume: bool = False) -> dict:
    """Change a planned step. Only the fields you pass change. resume=True restarts a paused step."""
    fields = {k: v for k, v in dict(type=type, title=title, due=due, subject=subject, body=body).items() if v}
    if resume:
        fields["status"] = "planned"
    return _with(db.edit_step, step_id, **fields)


@mcp.tool()
def complete_step(step_id: int, outcome: str = "", note: str = "") -> dict:
    """Mark a step done and log it. Calls take outcome no_answer, voicemail, gatekeeper, call_back, loom (adds a Loom to do), interested,
    rejected, talked or maybe; each moves the stage to match (only forward), rejected closes them as not interested,
    maybe (lukewarm, wants an email) also adds the after-call email step, due today.
    A close step moves them to a closed stage with its reason."""
    return _slim(_with(db.complete_step, step_id, outcome, note, "claude"))


@mcp.tool()
def mark_ai(contact_ids: list[int], state: str, note: str = "") -> dict:
    """Tag people with what you (the outreach chat) have done, so Sean sees it next to their name and nobody
    gets contacted twice. state: "seen" = you looked at them and saved a plan or next step; "batch" = they are in
    the next outreach batch (note says which, e.g. "Batch 9, Sat Oct 31"); "" clears it. Logging the batch send
    with the CLI clears "batch" on its own."""
    return {"marked": [_slim(_with(db.mark_ai, cid, state, note)) for cid in contact_ids]}


@mcp.tool()
def setup_workspace(slug: str, name: str, stages: list[dict], description: str = "") -> dict:
    """Create a workspace for a new company, or replace its pipeline. stages = [{name, kind}] in order;
    kind is open (not yet contacted), active, won or closed (a final no). Add "hot": true to stages that
    mean a warm lead, and "meeting": true to stages that count as a booked meeting."""
    return _with(db.save_workspace, slug, name, stages, description)


@mcp.tool()
def add_custom_field(workspace: str, name: str, type: str = "text", help: str = "") -> list[dict]:
    """Add a custom field to a workspace (e.g. 'Team size', 'Uses DocuSign'). Values go in contact.custom."""
    return _with(db.add_field, workspace, name, type, help)


# ---- web page + JSON API ------------------------------------------------------------------------

def _json(data, status=200):
    return JSONResponse(data, status_code=status)


async def _body(request):
    try:
        return await request.json()
    except json.JSONDecodeError:
        return {}


def _safe(fn):
    async def handler(request: Request):
        db.ACTOR.set("web")  # everything done from the page is Sean's; MCP tools keep the "claude" default
        try:
            return _json(await fn(request))
        except ValueError as e:
            return _json({"error": str(e)}, 400)
        except KeyError as e:
            return _json({"error": f"Missing {e}"}, 400)
    return handler


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request):
    # always check for a newer page, so an update shows on the next refresh instead of a cached copy
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@mcp.custom_route("/manifest.webmanifest", methods=["GET"])
async def manifest(request: Request):
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@mcp.custom_route("/icon.svg", methods=["GET"])
async def icon(request: Request):
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@mcp.custom_route("/api/workspaces", methods=["GET"])
@_safe
async def api_workspaces(request):
    return _with(db.list_workspaces)


@mcp.custom_route("/api/compose", methods=["POST"])
@_safe
async def api_compose(request):
    """Open a new email in the Outlook app on this Mac. From another device (the phone) it says so, and the page
    falls back to a mailto link there."""
    if request.client.host not in ("127.0.0.1", "::1", "localhost"):
        return {"opened": False}
    b = await _body(request)
    url = f"mailto:{quote(b.get('to', ''), safe='@,')}?subject={quote(b.get('subject', ''))}&body={quote(b.get('body', ''))}"
    done = subprocess.run(["open", "-b", "com.microsoft.Outlook", url], capture_output=True, timeout=10)
    return {"opened": done.returncode == 0}


@mcp.custom_route("/api/workspaces/{slug}/after-call", methods=["PUT"])
@_safe
async def api_after_call(request):
    b = await _body(request)
    return _with(db.set_after_call, request.path_params["slug"], b.get("subject", ""), b.get("body", ""))


@mcp.custom_route("/api/board", methods=["GET"])
@_safe
async def api_board(request):
    r = _with(db.overview, request.query_params["workspace"], request.query_params.get("q", ""))
    r["contacts"] = [_slim(c) | {k: c[k] for k in ("updated", "progress", "current", "last_touch", "last_update", "last_reply", "last_said", "last_kind", "mobile", "notes", "website")}
                     for c in r["contacts"]]
    return r


@mcp.custom_route("/api/analytics", methods=["GET"])
@_safe
async def api_analytics(request):
    return _with(db.analytics, request.query_params["workspace"])


# Today's automatic Terms email send. The outreach chat's send job writes this file (every 20 s while sending,
# once more at the end, swapped in whole), so the CRM only reads it and needs no extra permissions.
OUTREACH_STATUS = Path(__file__).resolve().parents[3] / "Terms" / "Deepline" / "daily" / "status.json"


@mcp.custom_route("/api/outreach-today", methods=["GET"])
@_safe
async def api_outreach_today(request):
    today = date.today()
    try:
        out = json.loads(OUTREACH_STATUS.read_text())
        age = time.time() - OUTREACH_STATUS.stat().st_mtime
    except (OSError, json.JSONDecodeError):
        return {"state": "unavailable", "error": "The daily send hasn't written its status file yet."}
    if out.get("date") != today.isoformat():  # today's send hasn't started; the file still holds the last run
        return {"date": today.isoformat(), "state": "weekend" if today.weekday() >= 5 else "waiting", "total": 0, "sent": 0,
                "window": out.get("window", ""), "per_day": out.get("per_day"), "lists": [], "upcoming": []}
    if out.get("state") == "sending" and age > 120:
        out["state"] = "stalled"
    # match each person to their CRM contact, so the page can open their profile and show their full name
    con = db.connect()
    try:
        for lst in out.get("lists", []):
            for p in lst.get("people", []):
                row = con.execute("SELECT c.id, c.person, c.company FROM contacts c JOIN workspaces w ON w.id = c.workspace_id "
                                  "WHERE w.slug = 'terms' AND lower(c.email) = ?", ((p.get("email") or "").lower(),)).fetchone()
                if row:
                    p.update(id=row["id"], person=row["person"], company=p.get("company") or row["company"])
    finally:
        con.close()
    return out


@mcp.custom_route("/api/changes", methods=["GET"])
@_safe
async def api_changes(request):
    q = request.query_params
    return _with(db.list_changes, q.get("workspace") or None, q.get("actor") or None, q.get("since") or None,
                 q.get("until") or None, int(q.get("contact") or 0) or None, int(q.get("limit") or 300))


@mcp.custom_route("/api/changes/{hid:int}/undo", methods=["POST"])
@_safe
async def api_undo_change(request):
    return _with(db.undo_change, request.path_params["hid"], "web")


@mcp.custom_route("/api/changes/undo-actor", methods=["POST"])
@_safe
async def api_undo_actor(request):
    b = await _body(request)
    return _with(db.undo_actor, b["actor"], b["since"], b.get("until") or None, b.get("workspace") or None,
                 bool(b.get("preview", True)), "web")


@mcp.custom_route("/api/due", methods=["GET"])
@_safe
async def api_due(request):
    r = _with(db.due, request.query_params.get("workspace") or None)
    return {k: v if k == "weekend_calls" else [_slim(c) for c in v] for k, v in r.items()}


@mcp.custom_route("/api/contacts/{cid:int}", methods=["GET"])
@_safe
async def api_contact(request):
    return _with(db.get_contact, request.path_params["cid"])


@mcp.custom_route("/api/contacts", methods=["POST"])
@_safe
async def api_add(request):
    b = await _body(request)
    return _with(db.add_contact, b.pop("workspace"), b.pop("company"), b.pop("person", ""), b.pop("stage", None),
                 "web", b.pop("strategy", None) or None, **b)


@mcp.custom_route("/api/contacts/{cid:int}", methods=["PATCH"])
@_safe
async def api_update(request):
    return _with(db.update_contact, request.path_params["cid"], **await _body(request))


@mcp.custom_route("/api/contacts/{cid:int}/stage", methods=["POST"])
@_safe
async def api_stage(request):
    b = await _body(request)
    return _with(db.move_stage, request.path_params["cid"], b["stage"], b.get("reason", ""), None, "web")


@mcp.custom_route("/api/contacts/{cid:int}/touch", methods=["POST"])
@_safe
async def api_touch(request):
    b = await _body(request)
    return _with(db.log_touch, request.path_params["cid"], b["kind"], b.get("summary", ""), b.get("at") or None, "web")


@mcp.custom_route("/api/contacts/{cid:int}/kill", methods=["POST"])
@_safe
async def api_kill(request):
    b = await _body(request)
    cid = request.path_params["cid"]
    return _with(db.undo_step, -cid) if b.get("undo") else _with(db.kill, cid, b.get("stage") or None, "web")


@mcp.custom_route("/api/contacts/{cid:int}/client", methods=["POST"])
@_safe
async def api_client(request):
    b = await _body(request)
    cid = request.path_params["cid"]
    return _with(db.undo_step, -cid) if b.get("undo") else _with(db.mark_client, cid, "web")


@mcp.custom_route("/api/contacts/{cid:int}/call", methods=["POST"])
@_safe
async def api_call(request):
    b = await _body(request)
    return _with(db.log_call, request.path_params["cid"], b.get("outcome", ""), b.get("note", ""), "web")


@mcp.custom_route("/api/contacts/{cid:int}/next", methods=["POST"])
@_safe
async def api_next(request):
    b = await _body(request)
    return _with(db.set_next_step, request.path_params["cid"], b.get("step", ""), b.get("due") or None,
                 b.get("draft"), "web")


@mcp.custom_route("/api/strategies", methods=["POST"])
@_safe
async def api_save_strategy(request):
    b = await _body(request)
    return _with(db.save_strategy, b["workspace"], b.get("name", ""), b.get("steps", []), b.get("description", ""),
                 b.get("id") or None)


@mcp.custom_route("/api/strategies/{sid:int}", methods=["GET"])
@_safe
async def api_strategy(request):
    return _with(db.strategy_detail, request.path_params["sid"])


@mcp.custom_route("/api/strategies/{sid:int}", methods=["DELETE"])
@_safe
async def api_delete_strategy(request):
    return _with(db.delete_strategy, request.path_params["sid"])


@mcp.custom_route("/api/contacts/{cid:int}/strategy", methods=["POST"])
@_safe
async def api_apply_strategy(request):
    b = await _body(request)
    return _with(db.apply_strategy, request.path_params["cid"], b.get("strategy"), b.get("start") or None, "web")


@mcp.custom_route("/api/contacts/{cid:int}/steps", methods=["POST"])
@_safe
async def api_add_step(request):
    b = await _body(request)
    return _with(db.add_step, request.path_params["cid"], b["type"], b.get("title", ""), b.get("due") or None,
                 b.get("subject", ""), b.get("body", ""))


@mcp.custom_route("/api/steps/{sid:int}", methods=["PATCH"])
@_safe
async def api_edit_step(request):
    return _with(db.edit_step, request.path_params["sid"], **await _body(request))


@mcp.custom_route("/api/steps/{sid:int}", methods=["DELETE"])
@_safe
async def api_delete_step(request):
    return _with(db.delete_step, request.path_params["sid"])


@mcp.custom_route("/api/steps/{sid:int}/done", methods=["POST"])
@_safe
async def api_complete_step(request):
    b = await _body(request)
    return _with(db.complete_step, request.path_params["sid"], b.get("outcome", ""), b.get("note", ""), "web")


@mcp.custom_route("/api/steps/{sid:int}/undo", methods=["POST"])
@_safe
async def api_undo_step(request):
    return _with(db.undo_step, request.path_params["sid"])


@mcp.custom_route("/api/steps/{sid:int}/skip", methods=["POST"])
@_safe
async def api_skip_step(request):
    return _with(db.skip_step, request.path_params["sid"], "web")


app = mcp.streamable_http_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("CRM_BIND", "127.0.0.1"), port=PORT, log_level="warning")
