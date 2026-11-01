# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
One user: Sean, running cold outreach for Terms.so (contract tooling for small creative agencies and studios). He works it at his laptop at night and in the morning, and on his phone sometimes, mostly while making calls. Other uses (personal networking, later lists) would be more workspaces he runs himself, not other people.

## Product Purpose
A personal outreach CRM that tells him who to call and email today, logs what happened in a couple of clicks, and keeps warm leads from going cold. Success is getting people to the paid stages, Feedback call and Client story.

## Positioning
Built around his own routine rather than a sales team's: every person follows a strategy (a named sequence of steps such as Email first or Call first), the current step is always visible and editable, and Claude works the same data through the MCP server and the outreach scripts through the CLI.

## Operating Context
- Daily: open the Overview, make the calls due today and log each outcome from a dropdown (no answer, voicemail, gatekeeper, call back, interested, not interested), then send due emails through Outlook.
- Nightly or in the morning, Claude drafts and sends batches through the CLI and logs them here.
- Pipeline for Terms: Not started, Emailed, Bumped, Called, Called no answer, Replied, Call back, Interested, Conference call, Testing Terms, Feedback call (paid), Client story (paid), plus closed stages (Not interested, Bounced, Do not contact).

## Capabilities and Constraints
- Local Python app on port 8787, SQLite database, one HTML page served from `crm/static/index.html` (no build step; edits are live on save).
- The CLI and MCP contract must stay backward compatible; stage names Emailed, Bumped, Replied, Bounced must not change.
- Views: Overview, Pipeline, Everyone, Strategies, and a person panel with steps and a timeline.
- Possible later: hosting (Railway or Render plus Supabase), undecided.

## Evidence on Hand
Real pipeline data only (about 340 contacts). No testimonials, logos or metrics exist for the app itself; never invent them.

## Product Principles
1. Today's work first: what to do now is the first thing on every visit.
2. Nothing warm goes cold: interested people and anyone further along stay visible until there is a next step.
3. Logging takes seconds: an outcome is one choice, a note is optional.
4. Plain language everywhere: labels say what happens, no CRM jargon.
5. Colour means something: warm, paid and overdue only.
